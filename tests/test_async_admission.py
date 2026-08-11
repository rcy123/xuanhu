"""R6-B async admission unit tests (no DB required).

Covers: ``Prefer: respond-async`` parsing, downstream-key derivation (stable and
privacy-safe), readiness gating (fail closed), the bounded admission state, the
HTTP 202 acceptance envelope/headers/links, and the deterministic PHI-safe error
mapping used by the worker handlers.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent_runtime.async_command import ASYNC_COMMAND_OPERATIONS
from app.agent_runtime.async_command_admission import (
    ACCEPTED_RETRY_AFTER_SECONDS,
    AsyncCommandAdmissionState,
    async_admission_ready,
    build_accepted_response,
    derive_command_trace_id,
    derive_downstream_key,
    prefers_respond_async,
)
from app.agent_runtime.async_command_worker import CommandFailureError
from app.agent_runtime.async_handlers import _map_business_error, build_async_command_handlers
from app.core.exceptions import (
    AgentTriggerFailedError,
    IdempotencyConflictError,
    InvalidStateVersionError,
    SessionNotFoundError,
    SessionTerminatedError,
)

# ---------------------------------------------------------------------------
# prefers_respond_async
# ---------------------------------------------------------------------------


def _request_with_prefer(value: str | None) -> SimpleNamespace:
    return SimpleNamespace(headers={"prefer": value} if value is not None else {})


def test_prefer_absent_is_false() -> None:
    assert prefers_respond_async(_request_with_prefer(None)) is False


@pytest.mark.parametrize(
    "header",
    [
        "respond-async",
        "respond-async, wait=10",
        "wait=10, respond-async",
        " RESPOND-ASYNC ",
        "Respond-Async",
    ],
)
def test_prefer_respond_async_accepted(header: str) -> None:
    assert prefers_respond_async(_request_with_prefer(header)) is True


@pytest.mark.parametrize("header", ["wait=10", "return=representation", "", "  "])
def test_prefer_other_values_ignored(header: str) -> None:
    assert prefers_respond_async(_request_with_prefer(header)) is False


# ---------------------------------------------------------------------------
# downstream key derivation
# ---------------------------------------------------------------------------


def test_downstream_key_is_stable_and_privacy_safe() -> None:
    command_id = uuid.uuid4()
    k1 = derive_downstream_key(command_id, "intake.message")
    k2 = derive_downstream_key(command_id, "intake.message")
    assert k1 == k2  # deterministic => lease takeover replays the same claim
    assert k1.startswith("async-command:")
    # Never embeds the raw command id or any client-supplied value.
    assert str(command_id) not in k1


def test_downstream_key_distinguishes_operations() -> None:
    command_id = uuid.uuid4()
    assert derive_downstream_key(command_id, "intake.message") != derive_downstream_key(
        command_id, "session.advance"
    )
    assert derive_downstream_key(command_id, "session.advance") != derive_downstream_key(
        command_id, "prescription.review"
    )


def test_command_trace_id_is_stable_and_opaque() -> None:
    command_id = uuid.uuid4()
    assert derive_command_trace_id(command_id) == f"async-command:{command_id}"
    assert str(command_id) in derive_command_trace_id(command_id)  # opaque, not secret


# ---------------------------------------------------------------------------
# readiness gating (fail closed)
# ---------------------------------------------------------------------------


def test_admission_ready_requires_enabled_ready_and_all_handlers() -> None:
    ready = AsyncCommandAdmissionState.ready_state(frozenset(ASYNC_COMMAND_OPERATIONS))
    assert async_admission_ready(ready) is True

    # Not ready (e.g. worker failed / registry empty).
    assert async_admission_ready(AsyncCommandAdmissionState.disabled()) is False
    # Enabled but not ready.
    assert (
        async_admission_ready(AsyncCommandAdmissionState(enabled=True, ready=False, handler_operations=frozenset()))
        is False
    )
    # Ready but missing a handler => never honour async.
    partial = AsyncCommandAdmissionState.ready_state(frozenset({"intake.message"}))
    assert async_admission_ready(partial) is False
    # Absent state (feature off at startup) => fails closed.
    assert async_admission_ready(None) is False


def test_admission_state_is_bounded_and_frozen() -> None:
    state = AsyncCommandAdmissionState.ready_state(frozenset(ASYNC_COMMAND_OPERATIONS))
    assert state.enabled is True
    assert state.ready is True
    assert state.handler_operations >= ASYNC_COMMAND_OPERATIONS
    with pytest.raises(AttributeError):
        state.enabled = False  # frozen dataclass


# ---------------------------------------------------------------------------
# 202 acceptance envelope / headers / links
# ---------------------------------------------------------------------------


def test_accepted_response_headers_body_and_links() -> None:
    # An explicit Prefer header => Preference-Applied is advertised.
    request = SimpleNamespace(headers={"x-request-id": "trace-123", "prefer": "respond-async"})
    session_id = str(uuid.uuid4())
    ref = SimpleNamespace(
        command_id=uuid.uuid4(),
        operation="intake.message",
        status="queued",
        replayed=False,
        attempt_count=0,
    )
    response = build_accepted_response(request, session_id, ref)

    assert response.status_code == 202
    assert response.headers["Location"] == f"/api/v1/consult/sessions/{session_id}/commands/{ref.command_id}"
    assert response.headers["Preference-Applied"] == "respond-async"
    assert response.headers["Retry-After"] == str(ACCEPTED_RETRY_AFTER_SECONDS)

    body = response.body
    import json

    envelope = json.loads(body)
    data = envelope["data"]
    assert data["command_id"] == str(ref.command_id)
    assert data["operation"] == "intake.message"
    assert data["status"] == "queued"
    assert data["replayed"] is False
    assert data["attempt_count"] == 0
    assert data["links"]["self"] == f"/api/v1/consult/sessions/{session_id}/commands/{ref.command_id}"
    assert data["links"]["session"] == f"/api/v1/consult/sessions/{session_id}"
    assert data["links"]["stream"] == f"/api/v1/consult/sessions/{session_id}/stream"
    assert envelope["trace_id"] == "trace-123"


def test_accepted_response_without_prefer_omits_preference_applied() -> None:
    """R7 default: no Prefer header => 202 but never advertise respond-async."""
    request = SimpleNamespace(headers={"x-request-id": "trace-456"})
    session_id = str(uuid.uuid4())
    ref = SimpleNamespace(
        command_id=uuid.uuid4(),
        operation="prescription.review",
        status="queued",
        replayed=False,
        attempt_count=0,
    )
    response = build_accepted_response(request, session_id, ref)

    assert response.status_code == 202
    # Location and Retry-After are always present; Preference-Applied is not
    # claimed when the client never sent the respond-async preference.
    assert response.headers["Location"] == f"/api/v1/consult/sessions/{session_id}/commands/{ref.command_id}"
    assert response.headers["Retry-After"] == str(ACCEPTED_RETRY_AFTER_SECONDS)
    assert "Preference-Applied" not in response.headers


def test_accepted_response_replayed_attempt_roundtrip() -> None:
    request = SimpleNamespace(headers={})
    ref = SimpleNamespace(
        command_id=uuid.uuid4(),
        operation="session.advance",
        status="queued",
        replayed=True,
        attempt_count=2,
    )
    import json

    data = json.loads(build_accepted_response(request, "s", ref).body)["data"]
    assert data["replayed"] is True
    assert data["attempt_count"] == 2


# ---------------------------------------------------------------------------
# handler registry + error mapping
# ---------------------------------------------------------------------------


def test_build_handlers_empty_without_runtime_fails_closed() -> None:
    assert build_async_command_handlers(None) == {}


def test_build_handlers_registers_all_allowlisted_operations() -> None:
    handlers = build_async_command_handlers(object())
    assert set(handlers.keys()) == set(ASYNC_COMMAND_OPERATIONS)


def test_map_retryable_agent_trigger_collapses_to_handler_unavailable() -> None:
    exc = AgentTriggerFailedError(detail="runtime down", retryable=True)
    mapped = _map_business_error(exc)
    assert isinstance(mapped, CommandFailureError)
    assert mapped.error_code == "HANDLER_UNAVAILABLE"
    assert mapped.retryable is True


def test_map_nonretryable_agent_trigger_surfaces_deterministic_code() -> None:
    exc = AgentTriggerFailedError(
        detail="legacy decommissioned",
        retryable=False,
        agent_error_code="LEGACY_RUNTIME_DECOMMISSIONED",
    )
    mapped = _map_business_error(exc)
    assert mapped.error_code == "AGENT_TRIGGER_FAILED"
    assert mapped.retryable is False


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_retryable"),
    [
        (SessionNotFoundError(detail="missing"), "SESSION_NOT_FOUND", False),
        (InvalidStateVersionError(detail="stale"), "INVALID_STATE_VERSION", True),
        (SessionTerminatedError(detail="ended"), "SESSION_TERMINATED", False),
        (IdempotencyConflictError(message="conflict", detail="d"), "IDEMPOTENCY_KEY_REUSED", False),
    ],
)
def test_map_known_business_codes_pass_through(
    exc: object, expected_code: str, expected_retryable: bool
) -> None:
    mapped = _map_business_error(exc)  # type: ignore[arg-type]
    assert mapped.error_code == expected_code
    assert mapped.retryable is expected_retryable


def test_map_unknown_code_collapses_to_rejected_or_unavailable() -> None:
    from app.core.exceptions import XuanhuError

    class MysteryError(XuanhuError):
        code = "MYSTERY_DYNAMIC_CODE"
        retryable = False

    assert _map_business_error(MysteryError()).error_code == "HANDLER_REJECTED"

    class RetryableMystery(XuanhuError):
        code = "MYSTERY_RETRYABLE"
        retryable = True

    mapped = _map_business_error(RetryableMystery())
    assert mapped.error_code == "HANDLER_UNAVAILABLE"
    assert mapped.retryable is True
