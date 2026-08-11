"""R6-B real-handler dispatch integration evidence (real PG/Redis).

The existing ``test_async_command_admission_integration.py`` drives the full
202 -> worker -> outbox -> Redis pipeline with a *generic replacement handler*
(``_fake_success_handler``). This suite instead dispatches the **actual** worker
handler registry produced by ``build_async_command_handlers(shared_runtime)``
for ALL THREE allowlisted operations, while using a controlled fake business
boundary (the spec's "controlled fake model/runtime/business boundaries") so no
external model call occurs.

Because the real handler wrappers call the real business services, each service
method is monkeypatched to assert the handler's contract and return a canned
terminal result. The R6 substrate is entirely real: Postgres command rows, the
``PostgresAsyncCommandRepository``, the ``AsyncCommandWorker``, the Outbox
rows/``OutboxPublisher``, the status API and Redis/SSE.

Proven for every operation: 202 -> worker -> ``succeeded`` -> bounded Outbox ->
Redis ``command.succeeded``; body validation; ``doctor_id`` / ``state_version``
propagation; the deterministic downstream idempotency key derived from the
command id; shared runtime reuse with no request-local / per-job runtime; a
fresh job-local DB session; and that private result/PHI, the raw public
idempotency key and the client trace id never leak into status / outbox / Redis.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.async_command import PostgresAsyncCommandRepository
from app.agent_runtime.async_command_admission import (
    AsyncCommandAdmissionState,
    derive_downstream_key,
)
from app.agent_runtime.async_command_worker import AsyncCommandWorker
from app.agent_runtime.async_handlers import build_async_command_handlers
from app.agent_runtime.repository import PostgresDomainRepository
from app.core.exceptions import SessionNotFoundError
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.main import app
from app.models.async_command import AsyncCommand
from app.models.consult import ConsultSession
from app.models.domain import OutboxEvent
from app.schemas.message import MessageCreateRequest, MessageCreateResponse
from app.schemas.review import ReviewRequest, ReviewResponse
from app.services.events import EventService, session_event_stream_key
from app.services.outbox_publisher import OutboxPublisher

pytestmark = pytest.mark.integration

OPERATIONS = ["intake.message", "session.advance", "prescription.review"]
# Max publisher cycles before a test's outbox must have reached Redis. The real
# OutboxPublisher drains a bounded batch (batch_size=50) per run_once(); under
# the full suite an earlier eligible outbox backlog can consume the batch before
# this test's events, so a single run_once is not guaranteed to publish them. We
# re-drain the SAME real publisher up to this small explicit bound, re-reading
# only this session's Redis stream, and stop as soon as all lifecycle events land.
MAX_PUBLISH_CYCLES = 8
SECRET = "PHI-handler-9c41"
CLIENT_TRACE = "client-trace-abc123"
# A sentinel shared runtime: the handler closes over it and forwards it to the
# business boundary; the patched boundaries assert they receive this exact
# object (shared runtime reuse, never a request-local / per-job runtime).
SHARED_RUNTIME = object()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed_session() -> UUID:
    session_id = uuid4()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage="inquiry",
                status="active",
                agent_runtime="langgraph",
                rollback_counts={},
                state_version=1,
                recovery_status="normal",
            )
        )
    return session_id


async def _cleanup(session_id: UUID) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    redis = await get_redis()
    await redis.delete(session_event_stream_key(str(session_id)))


def _set_admission_ready() -> None:
    app.state.async_command_state = AsyncCommandAdmissionState.ready_state(
        frozenset(OPERATIONS)
    )


def _clear_admission() -> None:
    if hasattr(app.state, "async_command_state"):
        delattr(app.state, "async_command_state")


async def _command_row(command_id: UUID) -> AsyncCommand | None:
    async with get_session_factory()() as db:
        return await db.get(AsyncCommand, command_id)


async def _outbox_events(session_id: UUID) -> list[OutboxEvent]:
    async with get_session_factory()() as db:
        return list(
            (
                await db.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.session_id == session_id)
                    .order_by(OutboxEvent.created_at)
                )
            ).all()
        )


async def _drain_session_to_redis(
    publisher: OutboxPublisher,
    session_id: UUID,
) -> list[tuple[str, dict[str, object]]]:
    """Re-run the real publisher until this session's lifecycle events reach Redis.

    Full-suite backlog: the publisher claims a bounded batch per ``run_once`` and
    earlier eligible outbox rows can win the batch before this test's events, so
    one call is not enough. Drain up to a small explicit maximum, re-reading only
    this session's Redis stream, and stop once queued/running/succeeded are all
    observed. Diagnostics report only event types (no PHI / payloads).
    """
    required = {"command.queued", "command.running", "command.succeeded"}
    redis = await get_redis()
    rows: list[tuple[str, dict[str, object]]] = []
    event_types: list[str] = []
    for _ in range(MAX_PUBLISH_CYCLES):
        await publisher.run_once()
        rows = await redis.xrange(session_event_stream_key(str(session_id)))
        event_types = [r[1]["event_type"] for r in rows]
        if required.issubset(set(event_types)):
            break
    if not required.issubset(set(event_types)):
        missing = sorted(required - set(event_types))
        raise AssertionError(
            f"outbox never reached Redis after {MAX_PUBLISH_CYCLES} publisher cycles; "
            f"missing={missing} observed_event_types={sorted(set(event_types))}"
        )
    return rows


def _post_url(session_id: UUID, operation: str) -> str:
    return {
        "intake.message": f"/api/v1/consult/sessions/{session_id}/messages",
        "session.advance": f"/api/v1/consult/sessions/{session_id}/advance",
        "prescription.review": f"/api/v1/consult/sessions/{session_id}/review",
    }[operation]


def _body_for(operation: str) -> dict[str, Any]:
    if operation == "intake.message":
        return {"content": "医生问诊信息", "role": "doctor"}
    if operation == "session.advance":
        return {"force": False}
    return {"action": "confirm"}


def _build_worker(max_attempts: int = 2) -> AsyncCommandWorker:
    """A real worker with the ACTUAL three-handler registry."""
    return AsyncCommandWorker(
        PostgresAsyncCommandRepository(get_session_factory()),
        handlers=build_async_command_handlers(SHARED_RUNTIME),
        worker_id=f"worker-real-{uuid.uuid4().hex[:6]}",
        max_attempts=max_attempts,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )


# ---------------------------------------------------------------------------
# canned terminal business results (returned by the patched boundaries)
# ---------------------------------------------------------------------------


def _message_response(session_id: str) -> MessageCreateResponse:
    return MessageCreateResponse(
        message_id=str(uuid4()),
        session_id=session_id,
        role="agent",
        stage="inquiry",
        content=f"agent-reply {SECRET}",
        current_stage="inquiry",
        state_version=2,
        created_at=datetime.now(UTC),
        agent_message=None,
    )


def _review_response(session_id: str) -> ReviewResponse:
    return ReviewResponse(
        session_id=session_id,
        action="confirm",
        current_stage="safety",
        status="pending_review",
        pending_review=True,
        review_id=str(uuid4()),
        state_version=2,
        original_formula={"formula": f"gambir {SECRET}"},
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# per-operation full pipeline: 202 -> worker -> succeeded -> outbox -> redis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_real_handler_full_pipeline_per_operation(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    seen: dict[str, Any] = {"command_id": None, "db_session": None}
    _install_boundary(operation, seen, monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {
                "Prefer": "respond-async",
                "X-Idempotency-Key": f"raw-key-{operation}",
                "X-Request-Id": CLIENT_TRACE,
                "X-Doctor-Id": "doc-7",
                "X-State-Version": "5",
            }
            accepted = await client.post(
                _post_url(session_id, operation),
                json=_body_for(operation),
                headers=headers,
            )
            assert accepted.status_code == 202, accepted.text
            command_id = UUID(accepted.json()["data"]["command_id"])
            seen["command_id"] = command_id

            # The real worker dispatches the real handler; the patched business
            # boundary runs and asserts the contract, then returns a canned result.
            await _build_worker().run_once()

            # status -> succeeded, result kept private.
            row = await _command_row(command_id)
            assert row is not None and row.status == "succeeded"
            status = await client.get(
                f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"
            )
            assert status.status_code == 200
            data = status.json()["data"]
            assert data["status"] == "succeeded"
            assert data["result"]["http_status"] == 200
            # Private result/PHI never reaches HTTP status.
            assert SECRET not in status.text

            # Bounded outbox succeeded projection, PHI-free.
            succeeded = [
                e
                for e in await _outbox_events(session_id)
                if e.event_type == "async_command.succeeded.v1"
            ]
            assert len(succeeded) == 1
            assert SECRET not in json.dumps(succeeded[0].payload)

            # Redis/SSE stream carries command.succeeded, PHI-free.
            publisher = OutboxPublisher(
                PostgresDomainRepository(get_session_factory()),
                EventService(dedupe_ttl_seconds=30),
                worker_id=f"pub-real-{uuid.uuid4().hex[:6]}",
            )
            # Under the full suite an earlier eligible outbox backlog can consume
            # the publisher's bounded batch before this test's events, so drain
            # the real publisher repeatedly (bounded) until this session's
            # lifecycle events all land in Redis.
            rows = await _drain_session_to_redis(publisher, session_id)
            event_types = [r[1]["event_type"] for r in rows]
            assert "command.queued" in event_types
            assert "command.running" in event_types
            assert "command.succeeded" in event_types
            assert SECRET not in json.dumps(rows)

            # Raw public idempotency key and client trace id never persisted.
            assert row.request_payload is not None
            payload_json = json.dumps(row.request_payload)
            assert "raw-key-" not in payload_json
            assert CLIENT_TRACE not in payload_json
            assert CLIENT_TRACE not in json.dumps(succeeded[0].payload)
            assert CLIENT_TRACE not in json.dumps(rows)
            assert "raw-key-" not in json.dumps(rows)
            assert "raw-key-" not in json.dumps(succeeded[0].payload)

            # The deterministic downstream idempotency key derived from command_id.
            assert seen["idempotency_key"] == derive_downstream_key(command_id, operation)

            # Shared runtime reuse; no request-local / per-job runtime; fresh DB.
            assert seen["shared_runtime"] is SHARED_RUNTIME
            assert seen["allow_request_local"] is False
            assert isinstance(seen["db_session"], AsyncSession)
            assert seen["db_session"] is not None
    finally:
        _clear_admission()
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# contract assertions the patched boundaries enforce (per operation)
# ---------------------------------------------------------------------------


def _install_boundary(
    operation: str,
    seen: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if operation == "intake.message":
        async def intake_boundary(
            self: Any,
            session_id: str,
            body: MessageCreateRequest,
            *,
            doctor_id: str | None,
            trace_id: str,
            x_state_version: int | None,
            idempotency_key: str,
        ) -> MessageCreateResponse:
            seen["idempotency_key"] = idempotency_key
            seen["shared_runtime"] = self._shared_langgraph_runtime
            seen["allow_request_local"] = self._allow_request_local_langgraph_runtime
            seen["db_session"] = self._db
            # Body already validated by the handler into a MessageCreateRequest.
            assert isinstance(body, MessageCreateRequest)
            assert body.content == "医生问诊信息"
            assert doctor_id == "doc-7"
            assert x_state_version == 5
            assert trace_id == f"async-command:{seen['command_id']}"
            return _message_response(session_id)

        import app.services.message as _msg_mod

        monkeypatch.setattr(_msg_mod.MessageService, "submit_message", intake_boundary)

    elif operation == "session.advance":
        async def advance_boundary(
            db: AsyncSession,
            *,
            session_id: str,
            state_version: int | None,
            trace_id: str,
            force: bool,
            idempotency_key: str,
            alternative_index: int | None,
            shared_runtime: Any | None,
            allow_request_local_runtime: bool,
        ) -> dict[str, Any]:
            seen["idempotency_key"] = idempotency_key
            seen["shared_runtime"] = shared_runtime
            seen["allow_request_local"] = allow_request_local_runtime
            seen["db_session"] = db
            assert state_version == 5
            assert force is False
            assert alternative_index is None
            assert trace_id == f"async-command:{seen['command_id']}"
            assert shared_runtime is SHARED_RUNTIME
            assert allow_request_local_runtime is False
            return {"session_id": session_id, "current_stage": "review", "force": False}

        import app.api.advance as _adv_mod

        monkeypatch.setattr(_adv_mod, "run_langgraph_advance_flow", advance_boundary)

    elif operation == "prescription.review":
        async def review_boundary(
            self: Any,
            session_id: str,
            request: ReviewRequest,
            *,
            doctor_id: str | None,
            trace_id: str,
            x_state_version: int | None,
            idempotency_key: str,
            shared_runtime: Any | None,
            allow_request_local_runtime: bool,
        ) -> ReviewResponse:
            seen["idempotency_key"] = idempotency_key
            seen["shared_runtime"] = shared_runtime
            seen["allow_request_local"] = allow_request_local_runtime
            seen["db_session"] = self._db
            assert isinstance(request, ReviewRequest)
            assert request.action == "confirm"
            assert doctor_id == "doc-7"
            assert x_state_version == 5
            assert trace_id == f"async-command:{seen['command_id']}"
            assert shared_runtime is SHARED_RUNTIME
            assert allow_request_local_runtime is False
            return _review_response(session_id)

        import app.services.langgraph_review as _rev_mod

        monkeypatch.setattr(_rev_mod.LangGraphReviewService, "review", review_boundary)

    else:  # pragma: no cover
        raise AssertionError(operation)


# ---------------------------------------------------------------------------
# finite PHI-safe errors for real handlers
# ---------------------------------------------------------------------------


async def test_real_handler_maps_business_failure_to_finite_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    seen: dict[str, Any] = {"command_id": None}

    async def failing_intake_boundary(
        self: Any,
        session_id: str,
        body: MessageCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
        idempotency_key: str,
    ) -> MessageCreateResponse:
        del self, session_id, body, doctor_id, trace_id, x_state_version, idempotency_key
        raise SessionNotFoundError(detail=f"missing {SECRET}")

    import app.services.message as _msg_mod

    monkeypatch.setattr(_msg_mod.MessageService, "submit_message", failing_intake_boundary)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post(
                _post_url(session_id, "intake.message"),
                json=_body_for("intake.message"),
                headers={
                    "Prefer": "respond-async",
                    "X-Idempotency-Key": "raw-key-fail",
                },
            )
            assert accepted.status_code == 202
            seen["command_id"] = UUID(accepted.json()["data"]["command_id"])

            await _build_worker().run_once()

            row = await _command_row(seen["command_id"])
            assert row is not None and row.status == "failed"
            # Finite PHI-safe error code; no exception text / PHI persisted.
            assert row.error_code == "SESSION_NOT_FOUND"
            assert row.error_payload == {}
            status = await client.get(
                f"/api/v1/consult/sessions/{session_id}/commands/{seen['command_id']}"
            )
            assert SECRET not in status.text
    finally:
        _clear_admission()
        await _cleanup(session_id)


async def test_real_handler_cancellation_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real handler that never returns, then is cancelled, must leave the
    command running (lease expires; a later worker reclaims) — never settle."""
    session_id = await _seed_session()
    _set_admission_ready()
    gate = asyncio.Event()

    async def blocking_review_boundary(
        self: Any,
        session_id: str,
        request: ReviewRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
        idempotency_key: str,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> ReviewResponse:
        del self, session_id, request, doctor_id, trace_id, x_state_version, idempotency_key
        del shared_runtime, allow_request_local_runtime
        await gate.wait()  # never returns until cancelled
        raise AssertionError("unreachable")  # pragma: no cover

    import app.services.langgraph_review as _rev_mod

    monkeypatch.setattr(_rev_mod.LangGraphReviewService, "review", blocking_review_boundary)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post(
                _post_url(session_id, "prescription.review"),
                json=_body_for("prescription.review"),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": "raw-key-cancel"},
            )
            assert accepted.status_code == 202
            command_id = UUID(accepted.json()["data"]["command_id"])

            worker = _build_worker()
            task = asyncio.create_task(worker.run_once())
            await asyncio.sleep(0.2)  # let the worker claim + enter the handler
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Never settled: the command is still claimed (running), awaiting a
            # lease takeover by a later worker.
            row = await _command_row(command_id)
            assert row is not None and row.status == "running"
            assert row.result_http_status is None
            assert row.error_code is None
    finally:
        _clear_admission()
        await _cleanup(session_id)
