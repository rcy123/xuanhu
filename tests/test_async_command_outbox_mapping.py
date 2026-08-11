"""R6-A Outbox -> client event mapping unit tests.

Verifies the exact versioned async-command mappings fail closed on malformed or
unknown rows, never project the private request/result payload, and that agent
events still require a graph_run_id while command events require none.
"""

from __future__ import annotations

import uuid

import pytest

from app.agent_runtime.repository import OutboxMessage
from app.services.outbox_publisher import OutboxMappingError, map_outbox_event


def _message(
    event_type: str,
    payload: dict[str, object],
    *,
    graph_run_id: uuid.UUID | None = None,
    state_version: int = 1,
    event_id: uuid.UUID | None = None,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id or uuid.uuid4(),
        event_type=event_type,
        session_id=uuid.uuid4(),
        graph_run_id=graph_run_id,
        state_version=state_version,
        trace_id=f"async-command:{uuid.uuid4()}",
        payload=payload,
        status="pending",
        attempt_count=0,
        leased_by=None,
    )


def _command_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "command_id": str(uuid.uuid4()),
        "operation": "intake.message",
        "status": "queued",
        "attempt": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# exact versioned mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected_client_type"),
    [
        ("async_command.queued.v1", "command.queued"),
        ("async_command.running.v1", "command.running"),
        ("async_command.succeeded.v1", "command.succeeded"),
        ("async_command.failed.v1", "command.failed"),
    ],
)
def test_lifecycle_versions_map_to_client_event_types(version: str, expected_client_type: str) -> None:
    payload = _command_payload(status=version.split(".")[1])
    if version == "async_command.failed.v1":
        payload["error_code"] = "HANDLER_REJECTED"
    events = map_outbox_event(_message(version, payload))
    assert len(events) == 1
    event = events[0]
    assert event.event_type == expected_client_type
    assert event.payload["command_id"] == payload["command_id"]
    assert event.payload["operation"] == "intake.message"
    assert event.payload["attempt"] == 0


def test_succeeded_and_failed_never_carry_each_others_fields() -> None:
    succeeded = map_outbox_event(
        _message("async_command.succeeded.v1", _command_payload(status="succeeded"))
    )[0]
    assert "error_code" not in succeeded.payload

    failed = map_outbox_event(
        _message(
            "async_command.failed.v1",
            _command_payload(status="failed", error_code="HANDLER_REJECTED"),
        )
    )[0]
    assert failed.payload["error_code"] == "HANDLER_REJECTED"


# ---------------------------------------------------------------------------
# privacy: the private/result payload never leaks into client events
# ---------------------------------------------------------------------------


def test_command_events_project_only_allowlisted_fields() -> None:
    payload = _command_payload(
        status="succeeded",
        # A malicious producer embeds PHI and other identifiers; none may
        # survive into the projected client event.
        request_payload={"patient": "PHI"},
        result_payload={"prescription": "secret"},
        result_http_status=200,
        idempotency_key_digest="d" * 64,
        request_digest="e" * 64,
        exception="full traceback",
    )
    event = map_outbox_event(_message("async_command.succeeded.v1", payload))[0]
    allowed = {"command_id", "operation", "status", "attempt", "source_event_id", "state_version"}
    assert set(event.payload) <= allowed
    assert "PHI" not in str(event.payload)


def test_failed_event_only_carries_sanitized_error_code() -> None:
    payload = _command_payload(status="failed", error_code="HANDLER_REJECTED")
    event = map_outbox_event(_message("async_command.failed.v1", payload))[0]
    assert event.payload["error_code"] == "HANDLER_REJECTED"


# ---------------------------------------------------------------------------
# fail closed on malformed / unknown
# ---------------------------------------------------------------------------


def test_unknown_command_version_fails_closed() -> None:
    payload = _command_payload(status="queued")
    with pytest.raises(OutboxMappingError):
        map_outbox_event(_message("async_command.pending.v1", payload))


def test_status_mismatch_fails_closed() -> None:
    # The queued version demands status == queued; a running status must not map.
    with pytest.raises(OutboxMappingError):
        map_outbox_event(
            _message("async_command.queued.v1", _command_payload(status="running"))
        )


def test_missing_required_references_fail_closed() -> None:
    with pytest.raises(OutboxMappingError):
        map_outbox_event(_message("async_command.queued.v1", {"status": "queued"}))
    with pytest.raises(OutboxMappingError):
        map_outbox_event(
            _message("async_command.queued.v1", _command_payload(command_id=""))
        )


def test_attempt_must_be_a_nonnegative_int() -> None:
    for bad in ("1", -1, True, None, 1.5):
        with pytest.raises(OutboxMappingError):
            map_outbox_event(
                _message("async_command.queued.v1", _command_payload(attempt=bad))
            )


def test_failed_version_requires_error_code() -> None:
    with pytest.raises(OutboxMappingError):
        map_outbox_event(
            _message("async_command.failed.v1", _command_payload(status="failed"))
        )


def test_operation_outside_allowlist_fails_closed() -> None:
    payload = _command_payload(status="queued", operation="doctor.prescribe")
    with pytest.raises(OutboxMappingError):
        map_outbox_event(_message("async_command.queued.v1", payload))


def test_failed_error_code_outside_allowlist_fails_closed() -> None:
    payload = _command_payload(status="failed", error_code="PATIENT_BLOCKED")
    with pytest.raises(OutboxMappingError):
        map_outbox_event(_message("async_command.failed.v1", payload))


# ---------------------------------------------------------------------------
# graph_run_id boundary
# ---------------------------------------------------------------------------


def test_command_events_do_not_need_a_graph_run_id() -> None:
    events = map_outbox_event(
        _message("async_command.queued.v1", _command_payload(status="queued"), graph_run_id=None)
    )
    assert events[0].event_type == "command.queued"


def test_agent_event_still_requires_a_graph_run_id() -> None:
    with pytest.raises(OutboxMappingError):
        map_outbox_event(
            _message("intake.message_created.v1", {"message_id": str(uuid.uuid4()), "role": "doctor", "stage": "inquiry"})
        )
    mapped = map_outbox_event(
        _message(
            "intake.message_created.v1",
            {"message_id": str(uuid.uuid4()), "role": "doctor", "stage": "inquiry"},
            graph_run_id=uuid.uuid4(),
        )
    )
    assert any(e.event_type == "message.created" for e in mapped)
