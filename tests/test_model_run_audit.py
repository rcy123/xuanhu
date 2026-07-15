"""Unit contracts for fail-closed model-run terminal replay validation."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

import pytest

from app.models.model_run_audit import ModelRunAudit
from app.services import model_run_audit as audit_module
from app.services.model_run_audit import (
    ModelRunAuditAlreadyFinalizedError,
    ModelRunAuditProvenanceConflictError,
    ModelRunAuditTerminalConflictError,
    PostgresModelRunRecorder,
)

_Status = Literal["started", "succeeded", "failed", "cancelled"]


def test_postgres_recorder_is_required_for_production_durability() -> None:
    assert PostgresModelRunRecorder.required is True


def _payload(status: _Status = "succeeded") -> audit_module._AuditPayload:
    values: dict[str, Any] = {
        "run_id": uuid4(),
        "session_id": uuid4(),
        "agent_name": "formula",
        "stage": "formula",
        "agent_spec_version": "formula-spec-v1",
        "prompt_version": "formula-prompt-v2",
        "policy_version": "formula-policy-v3",
        "input_digest": "a" * 64,
        "output_schema_id": "tests.Output:deadbeef",
        "model_requested": "requested/model-alias",
        "model_actual": "provider/served-revision-1",
        "attempts": 1,
        "latency_ms": 17,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "output_digest": "b" * 64,
        "trace_id": "trace:model-run-replay",
        "error_code": None,
    }
    if status == "failed":
        values.update(
            model_actual=None,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            output_digest=None,
            error_code="MODEL_GATEWAY_UNAVAILABLE",
        )
    elif status in {"started", "cancelled"}:
        values.update(
            model_actual=None,
            attempts=0,
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            output_digest=None,
            error_code=None,
        )
    return audit_module._AuditPayload.model_validate(values)


def _existing(
    status: _Status,
    payload: audit_module._AuditPayload,
) -> ModelRunAudit:
    return ModelRunAudit(**payload.model_dump(mode="python"), status=status)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_identical_terminal_replay_is_idempotent(status: _Status) -> None:
    payload = _payload(status)

    assert not audit_module._terminal_replay_conflicts(
        _existing(status, payload),
        payload,
        status,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("model_actual", "provider/served-revision-2"),
        ("attempts", 2),
        ("latency_ms", 23),
        ("prompt_tokens", 4),
        ("completion_tokens", 3),
        ("total_tokens", 7),
        ("output_digest", "c" * 64),
    ],
)
def test_succeeded_replay_rejects_any_different_terminal_field(
    field: str,
    replacement: object,
) -> None:
    durable = _payload("succeeded")
    replay = durable.model_copy(update={field: replacement})

    assert audit_module._terminal_replay_conflicts(
        _existing("succeeded", durable),
        replay,
        "succeeded",
    )


def test_failed_replay_rejects_different_error_code() -> None:
    durable = _payload("failed")
    replay = durable.model_copy(update={"error_code": "MODEL_GATEWAY_TIMEOUT"})

    assert audit_module._terminal_replay_conflicts(
        _existing("failed", durable),
        replay,
        "failed",
    )


def test_succeeded_and_failed_outcomes_conflict() -> None:
    durable = _payload("succeeded")

    assert audit_module._terminal_replay_conflicts(
        _existing("succeeded", durable),
        _payload("failed"),
        "failed",
    )


def test_started_replay_cannot_be_misclassified_as_terminal_conflict() -> None:
    terminal = _payload("succeeded")
    started = _payload("started").model_copy(
        update={
            "run_id": terminal.run_id,
            "session_id": terminal.session_id,
            "trace_id": terminal.trace_id,
        }
    )

    assert not audit_module._terminal_replay_conflicts(
        _existing("succeeded", terminal),
        started,
        "started",
    )


def test_integrity_exception_messages_are_fixed_and_sanitized() -> None:
    provenance = ModelRunAuditProvenanceConflictError()
    terminal = ModelRunAuditTerminalConflictError()
    already_finalized = ModelRunAuditAlreadyFinalizedError()

    assert str(provenance) == "model-run audit provenance conflict"
    assert str(terminal) == "model-run audit terminal conflict"
    assert str(already_finalized) == "model-run audit already finalized"
    with pytest.raises(TypeError):
        ModelRunAuditTerminalConflictError("private patient or provider detail")  # type: ignore[call-arg]
