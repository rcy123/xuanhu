"""Unit contracts for explicit default-runtime switch auditing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.services.runtime_switch_audit import (
    PostgresRuntimeSwitchAuditRepository,
    RuntimeSwitchAuditConflict,
    RuntimeSwitchAuditMismatch,
    RuntimeSwitchAuditService,
    RuntimeSwitchRecord,
)
from scripts import audit_runtime_switch


class _MemoryRepository:
    def __init__(self, records: list[RuntimeSwitchRecord] | None = None) -> None:
        self.records = list(records or [])

    async def lock_chain(self) -> None:
        return None

    async def latest(self) -> RuntimeSwitchRecord | None:
        return self.records[-1] if self.records else None

    async def by_deployment_id(self, deployment_id: str) -> RuntimeSwitchRecord | None:
        return next(
            (item for item in reversed(self.records) if item.deployment_id == deployment_id),
            None,
        )

    async def append(self, record: RuntimeSwitchRecord) -> bool:
        self.records.append(record)
        return True


def _record(
    *,
    source: str = "legacy",
    target: str = "langgraph",
    deployment_id: str = "deploy-0001",
    reason: str = "approved canary rollout",
) -> RuntimeSwitchRecord:
    return RuntimeSwitchRecord(
        from_runtime=source,
        to_runtime=target,
        operator="release-bot",
        reason=reason,
        deployment_id=deployment_id,
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_initial_legacy_default_is_valid_without_fabricating_a_switch() -> None:
    service = RuntimeSwitchAuditService(_MemoryRepository())
    status = await service.status("legacy")
    assert status.model_dump() == {
        "status": "ok",
        "configured_runtime": "legacy",
        "audited_runtime": "legacy",
        "audit_present": False,
    }


@pytest.mark.asyncio
async def test_unaudited_langgraph_default_fails_closed() -> None:
    service = RuntimeSwitchAuditService(_MemoryRepository())
    with pytest.raises(RuntimeSwitchAuditMismatch, match="does not match"):
        await service.ensure_configured_runtime("langgraph")


@pytest.mark.asyncio
async def test_switch_is_linear_and_replays_same_deployment_id() -> None:
    repository = _MemoryRepository()
    service = RuntimeSwitchAuditService(repository)
    command = _record()

    stored, replayed = await service.record_switch(command, configured_runtime="langgraph")
    replay, was_replayed = await service.record_switch(command, configured_runtime="langgraph")

    assert stored == command
    assert replay == command
    assert replayed is False
    assert was_replayed is True
    assert repository.records == [command]
    await service.ensure_configured_runtime("langgraph")


@pytest.mark.asyncio
async def test_switch_rejects_wrong_source_target_or_reused_deployment() -> None:
    service = RuntimeSwitchAuditService(_MemoryRepository())
    with pytest.raises(RuntimeSwitchAuditConflict, match="target"):
        await service.record_switch(_record(), configured_runtime="legacy")
    with pytest.raises(RuntimeSwitchAuditConflict, match="source"):
        await service.record_switch(
            _record(source="langgraph", target="legacy"),
            configured_runtime="legacy",
        )

    repository = _MemoryRepository([_record()])
    service = RuntimeSwitchAuditService(repository)
    with pytest.raises(RuntimeSwitchAuditConflict, match="already used"):
        await service.record_switch(
            _record(reason="a different authorized reason"),
            configured_runtime="langgraph",
        )


@pytest.mark.asyncio
async def test_switch_back_to_legacy_requires_matching_audited_source() -> None:
    first = _record()
    repository = _MemoryRepository([first])
    service = RuntimeSwitchAuditService(repository)
    second = _record(
        source="langgraph",
        target="legacy",
        deployment_id="deploy-0002",
        reason="approved rollback",
    )
    await service.record_switch(second, configured_runtime="legacy")
    assert (await service.status("legacy")).status == "ok"
    assert len(repository.records) == 2


def test_record_rejects_naive_timestamp_and_unsafe_deployment_id() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RuntimeSwitchRecord(
            from_runtime="legacy",
            to_runtime="langgraph",
            operator="release-bot",
            reason="approved rollout",
            deployment_id="deploy-0001",
            timestamp=datetime.now(),
        )
    with pytest.raises(ValueError):
        _record(deployment_id="bad id with spaces")


def test_postgres_conflict_predicate_is_literal_for_prepared_plans() -> None:
    """The partial-index inference clause must not contain bind parameters."""

    class _CaptureSession:
        statement: object | None = None

        async def scalar(self, statement: object) -> None:
            self.statement = statement

    session = _CaptureSession()
    repository = PostgresRuntimeSwitchAuditRepository(session)  # type: ignore[arg-type]

    import asyncio

    assert asyncio.run(repository.append(_record())) is False
    compiled = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    conflict_clause = compiled.split("ON CONFLICT", maxsplit=1)[1]
    assert "event_type = 'runtime.switched'" in conflict_clause
    assert "event_type_1" not in conflict_clause


def test_cli_failure_is_fixed_and_does_not_echo_lower_layer_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://user:password@private-host/private-db"

    async def fail(_args: object) -> dict[str, object]:
        raise RuntimeError(f"connection failed: {secret}")

    monkeypatch.setattr(audit_runtime_switch, "_record", fail)
    exit_code = audit_runtime_switch.main(
        [
            "--from-runtime",
            "legacy",
            "--to-runtime",
            "langgraph",
            "--operator",
            "release-bot",
            "--reason",
            "approved canary rollout",
            "--deployment-id",
            "deploy-0001",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "RUNTIME_SWITCH_AUDIT_FAILED" in captured.err
    assert secret not in captured.err
    assert "approved canary rollout" not in captured.err
