"""R7 rollout admission for the durable async-command 202 path.

R7 flips the default: the three POST endpoints (messages / advance / review)
prefer the durable async 202 path and fall back to synchronous only when the
async feature is unavailable. ``try_rollout_async_admission`` is the single
centralized rollout decision used by all three routes — no ad-hoc conditionals
are duplicated. An explicit ``Prefer: respond-async`` continues to be honoured
(it is a subset of readiness). There is deliberately **no** synchronous override
header: RFC 7240 defines no standards-compatible sync preference, and the sync
path is kept strictly as the fail-closed fallback when async is unavailable.

Admission does only bounded work in the request task: read readiness, build the
canonical private payload, validate the session, and enqueue a durable command
row together with its Outbox ``queued`` row. No model invocation, graph resume,
review, or safety execution happens in the request.

The canonical private ``request_payload`` carries only worker-required body and
bounded actor/version metadata. The raw client idempotency key and trace id are
never stored in it; the worker derives a deterministic downstream idempotency
key from the ``command_id`` so a lease takeover replays the exact same business
claim instead of duplicating messages / transitions / reviews / safety / outbox.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.agent_runtime.async_command import (
    ASYNC_COMMAND_OPERATIONS,
    AsyncCommandRef,
    PostgresAsyncCommandRepository,
)
from app.db.session import get_session_factory
from app.schemas.common import success_response

# Header parsed per RFC 7240. Lowercased and split on commas so any spelling is
# accepted; only the exact ``respond-async`` token opts in.
PREFER_RESPOND_ASYNC = "respond-async"

# Bounded Retry-After on the 202 acceptance (the worker poll is sub-second, so
# a small advisory delay is honest and discourages hot polling).
ACCEPTED_RETRY_AFTER_SECONDS = 1

# The set of operations that must have a registered handler before admission is
# allowed to honour ``Prefer: respond-async``.
ALLOWED_ADMISSION_OPERATIONS = frozenset(ASYNC_COMMAND_OPERATIONS)


@dataclass(frozen=True, slots=True)
class AsyncCommandAdmissionState:
    """Privacy-safe readiness summary stored on ``FastAPI.state``."""

    enabled: bool
    ready: bool
    handler_operations: frozenset[str]

    @classmethod
    def disabled(cls) -> AsyncCommandAdmissionState:
        return cls(enabled=False, ready=False, handler_operations=frozenset())

    @classmethod
    def ready_state(cls, handler_operations: frozenset[str]) -> AsyncCommandAdmissionState:
        return cls(enabled=True, ready=True, handler_operations=frozenset(handler_operations))


def prefers_respond_async(request: Request) -> bool:
    """Whether the request opts into async execution via the Prefer header."""
    prefer = request.headers.get("prefer")
    if not prefer:
        return False
    return any(
        token.strip().lower() == PREFER_RESPOND_ASYNC
        for token in prefer.split(",")
    )


def derive_downstream_key(command_id: uuid.UUID, operation: str) -> str:
    """Deterministic downstream idempotency key derived from the command id.

    The worker passes this to the shared business execution as its internal
    idempotency key. Because it is a pure function of the stable ``command_id``
    and the fixed ``operation``, a lease takeover replays the exact same
    downstream claim and cannot duplicate messages / transitions / reviews /
    safety / outbox rows.
    """
    digest = hashlib.sha256(f"{operation}\0{command_id}".encode()).hexdigest()
    return f"async-command:{digest}"


def derive_command_trace_id(command_id: uuid.UUID) -> str:
    """Stable, privacy-safe trace id for one command's worker execution.

    Never derived from a client trace; only from the opaque command id.
    """
    return f"async-command:{command_id}"


def async_admission_ready(state: AsyncCommandAdmissionState | None) -> bool:
    """Whether admission may honour ``Prefer: respond-async``.

    Requires the feature to be enabled, the worker to be marked ready, and every
    allowlisted operation to have a registered handler. A worker that failed to
    start, or lacks a handler, must never accept async work (fail closed).
    """
    return (
        state is not None
        and state.enabled
        and state.ready
        and state.handler_operations >= ALLOWED_ADMISSION_OPERATIONS
    )


async def enqueue_command(
    app_state: Any,
    *,
    session_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> AsyncCommandRef | None:
    """Enqueue one durable command under the public idempotency contract.

    Returns ``None`` when the feature is not ready so the caller can fall back
    to the synchronous path. Raises the repository's deterministic errors
    (session not found / busy / idempotency conflict) unchanged so the existing
    exception handlers map them.
    """
    if operation not in ALLOWED_ADMISSION_OPERATIONS:
        raise ValueError(f"operation must be in {sorted(ALLOWED_ADMISSION_OPERATIONS)}")
    state = getattr(app_state, "async_command_state", None)
    if not async_admission_ready(state):
        return None
    repository = PostgresAsyncCommandRepository(get_session_factory())
    return await repository.enqueue(
        session_id=session_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )


def build_accepted_response(
    request: Request,
    session_id: str,
    ref: AsyncCommandRef,
) -> JSONResponse:
    """Build the HTTP 202 acceptance with the typed command body and links.

    ``Location`` and ``Retry-After`` are always present. ``Preference-Applied:
    respond-async`` is emitted only when the incoming request actually carried
    the ``respond-async`` preference (RFC 7240 never claims a preference the
    client did not request): the R7 default admission returns 202 without a
    ``Prefer`` header and must not advertise a preference that was never sent.
    """
    command_id = str(ref.command_id)
    self_link = f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"
    body: dict[str, Any] = {
        "command_id": command_id,
        "operation": ref.operation,
        "status": ref.status,
        "replayed": ref.replayed,
        "attempt_count": ref.attempt_count,
        "links": {
            "self": self_link,
            "session": f"/api/v1/consult/sessions/{session_id}",
            "stream": f"/api/v1/consult/sessions/{session_id}/stream",
        },
    }
    trace_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or str(uuid.uuid4())
    )
    headers: dict[str, str] = {
        "Location": self_link,
        "Retry-After": str(ACCEPTED_RETRY_AFTER_SECONDS),
    }
    if prefers_respond_async(request):
        headers["Preference-Applied"] = PREFER_RESPOND_ASYNC
    return JSONResponse(
        status_code=202,
        content=success_response(data=body, trace_id=trace_id),
        headers=headers,
    )


async def try_rollout_async_admission(
    request: Request,
    app_state: Any,
    *,
    session_id: str,
    operation: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> JSONResponse | None:
    """R7 centralized rollout decision: prefer the durable async 202 path.

    Returns the HTTP 202 acceptance when the R6 async-command substrate is
    enabled, ready, and fully registered. Otherwise returns ``None`` so the
    caller runs the existing synchronous path with byte/field/error semantics
    unchanged. This is the **only** decision the three POST routes make — they
    must not duplicate ad-hoc conditionals.

    An explicit ``Prefer: respond-async`` is honoured (it is a subset of
    readiness); no sync-override header exists. Admission commits the durable
    command + queued Outbox row and preserves the public idempotency / replay /
    conflict / session-busy / PHI rules exactly as in R6-B. It does only bounded
    work in the request task — never inline business execution.
    """
    if not async_admission_ready(getattr(app_state, "async_command_state", None)):
        return None
    try:
        parsed_session_id = uuid.UUID(session_id)
    except ValueError:
        # An invalid session id is left to the synchronous path, which raises the
        # canonical SESSION_NOT_FOUND — error semantics are unchanged.
        return None
    # 阶段4 背压：队列过深时拒绝新命令（503），不 fallback 到同步路径——
    # 同步路径同样会堆积，拒绝才是保护 worker/DB 的正确行为。
    overloaded = await _queue_overloaded_response(request)
    if overloaded is not None:
        return overloaded
    ref = await enqueue_command(
        app_state,
        session_id=parsed_session_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    if ref is None:
        return None
    return build_accepted_response(request, session_id, ref)


async def _queue_overloaded_response(request: Request) -> JSONResponse | None:
    """队列过深时返回 503；否则 None（best-effort，查询失败按不超载处理）。

    背压阈值 ``ASYNC_COMMAND_MAX_QUEUE_DEPTH``=0 时禁用。count_active 查询失败
    不阻断 admission（宁可放行也不因监控查询失败拒绝临床请求）。
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.async_command_max_queue_depth <= 0:
        return None
    try:
        repository = PostgresAsyncCommandRepository(get_session_factory())
        active = await repository.count_active()
    except Exception:
        return None
    if active < settings.async_command_max_queue_depth:
        return None
    trace_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or str(uuid.uuid4())
    )
    return JSONResponse(
        status_code=503,
        content={
            "code": "QUEUE_OVERLOADED",
            "message": "系统繁忙，请稍后重试",
            "detail": None,
            "retryable": True,
            "stage": None,
            "trace_id": trace_id,
        },
        headers={"Retry-After": str(ACCEPTED_RETRY_AFTER_SECONDS)},
    )


__all__ = [
    "PREFER_RESPOND_ASYNC",
    "ACCEPTED_RETRY_AFTER_SECONDS",
    "ALLOWED_ADMISSION_OPERATIONS",
    "AsyncCommandAdmissionState",
    "prefers_respond_async",
    "derive_downstream_key",
    "derive_command_trace_id",
    "async_admission_ready",
    "enqueue_command",
    "build_accepted_response",
    "try_rollout_async_admission",
]
