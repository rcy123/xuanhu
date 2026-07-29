"""Durable rollout-phase history contracts for the L9 stable window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.runtime_rollout_phase_audit import (
    RuntimeRolloutPhaseAuditConflict,
    RuntimeRolloutPhaseAuditService,
    RuntimeRolloutPhaseRecord,
)
from scripts import audit_runtime_rollout_phase


class _MemoryPhaseRepository:
    def __init__(self, records: list[RuntimeRolloutPhaseRecord] | None = None) -> None:
        self.records = list(records or [])

    async def lock_chain(self) -> None:
        return None

    async def list_chain(self) -> tuple[RuntimeRolloutPhaseRecord, ...]:
        return tuple(self.records)

    async def by_deployment_id(
        self,
        deployment_id: str,
    ) -> RuntimeRolloutPhaseRecord | None:
        return next(
            (item for item in reversed(self.records) if item.deployment_id == deployment_id),
            None,
        )

    async def append(self, record: RuntimeRolloutPhaseRecord) -> bool:
        self.records.append(record)
        return True


_BASE_TIME = datetime(2026, 7, 1, tzinfo=UTC)


def _phase(
    *,
    source: str,
    target: str,
    runtime: str = "langgraph",
    runtime_deployment: str | None = "runtime-deploy-a",
    phase_deployment: str,
    at: datetime,
) -> RuntimeRolloutPhaseRecord:
    return RuntimeRolloutPhaseRecord(
        from_phase=source,
        to_phase=target,
        runtime=runtime,
        runtime_switch_deployment_id=runtime_deployment,
        operator="release-bot",
        reason="approved staged rollout transition",
        deployment_id=phase_deployment,
        timestamp=at,
    )


@pytest.mark.asyncio
async def test_long_canary_does_not_count_toward_a_just_entered_full_window() -> None:
    full_at = _BASE_TIME + timedelta(days=30)
    repository = _MemoryPhaseRepository(
        [
            _phase(
                source="legacy",
                target="canary",
                phase_deployment="phase-canary",
                at=_BASE_TIME,
            ),
            _phase(
                source="canary",
                target="full",
                phase_deployment="phase-full",
                at=full_at,
            ),
        ]
    )

    status = await RuntimeRolloutPhaseAuditService(repository).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
        expected_phase_deployment_id="phase-full",
    )

    assert status.status == "ok"
    assert status.full_entered_at == full_at
    assert status.full_entered_at != _BASE_TIME


@pytest.mark.asyncio
async def test_continuous_full_window_exposes_durable_entry_timestamp() -> None:
    full_at = _BASE_TIME - timedelta(hours=2)
    repository = _MemoryPhaseRepository(
        [
            _phase(
                source="legacy",
                target="full",
                phase_deployment="phase-full",
                at=full_at,
            )
        ]
    )

    status = await RuntimeRolloutPhaseAuditService(repository).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
        expected_phase_deployment_id="phase-full",
    )

    assert status.status == "ok"
    assert status.full_entered_at == full_at


@pytest.mark.asyncio
async def test_full_rollback_full_resets_the_continuous_window() -> None:
    second_full_at = _BASE_TIME + timedelta(hours=5)
    repository = _MemoryPhaseRepository(
        [
            _phase(
                source="legacy",
                target="full",
                phase_deployment="phase-full-a",
                at=_BASE_TIME,
            ),
            _phase(
                source="full",
                target="rollback",
                runtime="legacy",
                runtime_deployment="runtime-deploy-b",
                phase_deployment="phase-rollback",
                at=_BASE_TIME + timedelta(hours=4),
            ),
            _phase(
                source="rollback",
                target="full",
                runtime_deployment="runtime-deploy-c",
                phase_deployment="phase-full-b",
                at=second_full_at,
            ),
        ]
    )

    status = await RuntimeRolloutPhaseAuditService(repository).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-c",
        expected_phase_deployment_id="phase-full-b",
    )

    assert status.status == "ok"
    assert status.full_entered_at == second_full_at


@pytest.mark.asyncio
async def test_missing_phase_history_fails_closed_for_full() -> None:
    status = await RuntimeRolloutPhaseAuditService(_MemoryPhaseRepository()).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
        expected_phase_deployment_id="phase-full",
    )

    assert status.status == "missing"
    assert status.full_entered_at is None


@pytest.mark.asyncio
async def test_out_of_order_phase_history_fails_closed() -> None:
    repository = _MemoryPhaseRepository(
        [
            _phase(
                source="legacy",
                target="canary",
                phase_deployment="phase-canary",
                at=_BASE_TIME + timedelta(hours=2),
            ),
            _phase(
                source="canary",
                target="full",
                phase_deployment="phase-full",
                at=_BASE_TIME + timedelta(hours=1),
            ),
        ]
    )

    status = await RuntimeRolloutPhaseAuditService(repository).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
        expected_phase_deployment_id="phase-full",
    )

    assert status.status == "invalid_chain"
    assert status.full_entered_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_deployment", "phase_deployment"),
    [
        ("wrong-runtime-deployment", "phase-full"),
        ("runtime-deploy-a", "wrong-phase-deployment"),
    ],
)
async def test_runtime_or_phase_deployment_mismatch_fails_closed(
    runtime_deployment: str,
    phase_deployment: str,
) -> None:
    repository = _MemoryPhaseRepository(
        [
            _phase(
                source="legacy",
                target="full",
                phase_deployment="phase-full",
                at=_BASE_TIME,
            )
        ]
    )

    status = await RuntimeRolloutPhaseAuditService(repository).status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id=runtime_deployment,
        expected_phase_deployment_id=phase_deployment,
    )

    assert status.status == "mismatch"
    assert status.full_entered_at is None


@pytest.mark.asyncio
async def test_phase_transition_is_linear_and_idempotent_by_deployment() -> None:
    repository = _MemoryPhaseRepository()
    service = RuntimeRolloutPhaseAuditService(repository)
    record = _phase(
        source="legacy",
        target="canary",
        phase_deployment="phase-canary",
        at=_BASE_TIME,
    )

    stored, replayed = await service.record_transition(
        record,
        configured_phase="canary",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
    )
    retry = record.model_copy(update={"timestamp": record.timestamp + timedelta(minutes=5)})
    replay, was_replayed = await service.record_transition(
        retry,
        configured_phase="canary",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="runtime-deploy-a",
    )

    assert stored == replay == record
    assert replayed is False
    assert was_replayed is True
    assert repository.records == [record]

    with pytest.raises(RuntimeRolloutPhaseAuditConflict, match="source"):
        await service.record_transition(
            _phase(
                source="legacy",
                target="full",
                phase_deployment="phase-full",
                at=_BASE_TIME + timedelta(minutes=1),
            ),
            configured_phase="full",
            configured_runtime="langgraph",
            runtime_switch_deployment_id="runtime-deploy-a",
        )


def test_phase_audit_cli_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://operator:password@private-host/release"

    async def fail(_args: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(audit_runtime_rollout_phase, "_record", fail)
    exit_code = audit_runtime_rollout_phase.main(
        [
            "--from-phase",
            "canary",
            "--to-phase",
            "full",
            "--operator",
            "release-bot",
            "--reason",
            "approved full rollout",
            "--deployment-id",
            "phase-full",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "ROLLOUT_PHASE_AUDIT_FAILED" in captured.err
    assert secret not in captured.err
    assert "approved full rollout" not in captured.err
