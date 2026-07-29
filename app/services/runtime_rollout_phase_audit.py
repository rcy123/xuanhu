"""Durable, linear audit contract for L9 rollout-phase transitions.

Runtime switches and rollout phases are deliberately separate authorities.  A
runtime switch says which implementation a deployment defaults to; a phase
transition says when that deployment entered canary, full, or rollback.  The
stable-window check must use the latter and therefore cannot accidentally
credit time spent in canary or before a rollback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.services.runtime_rollout import RuntimeName, RuntimeRolloutPhase

ROLLOUT_PHASE_EVENT = "runtime.rollout_phase_changed"
RolloutPhaseAuditState = Literal["ok", "missing", "mismatch", "invalid_chain"]

_DEPLOYMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class RuntimeRolloutPhaseAuditError(RuntimeError):
    """A sanitized rollout-phase audit failure."""


class RuntimeRolloutPhaseAuditConflict(RuntimeRolloutPhaseAuditError):
    """The requested transition conflicts with the durable phase chain."""


class RuntimeRolloutPhaseAuditMismatch(RuntimeRolloutPhaseAuditError):
    """Stored rollout-phase authority is malformed or has invalid provenance."""


class RuntimeRolloutPhaseRecord(BaseModel):
    """Allowlisted deployment phase transition persisted in ``audit_events``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_phase: RuntimeRolloutPhase
    to_phase: RuntimeRolloutPhase
    runtime: RuntimeName
    runtime_switch_deployment_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=_DEPLOYMENT_PATTERN,
    )
    operator: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=5, max_length=500)
    deployment_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=_DEPLOYMENT_PATTERN,
    )
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rollout phase timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def transition_must_change_phase_and_match_terminal_runtime(
        self,
    ) -> RuntimeRolloutPhaseRecord:
        if self.from_phase == self.to_phase:
            raise ValueError("rollout phase transition must change phase")
        if self.to_phase == "full" and self.runtime != "langgraph":
            raise ValueError("phase=full requires runtime=langgraph")
        if self.to_phase == "rollback" and self.runtime != "legacy":
            raise ValueError("phase=rollback requires runtime=legacy")
        if self.runtime == "langgraph" and self.runtime_switch_deployment_id is None:
            raise ValueError("runtime=langgraph requires a runtime switch deployment reference")
        return self


class RuntimeRolloutPhaseAuditStatus(BaseModel):
    """Privacy-safe read-only result consumed by rollout readiness checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RolloutPhaseAuditState
    configured_phase: RuntimeRolloutPhase
    configured_runtime: RuntimeName
    audited_phase: RuntimeRolloutPhase | None
    audit_present: bool
    deployment_id: str | None
    runtime_switch_deployment_id: str | None
    full_entered_at: datetime | None


class RuntimeRolloutPhaseAuditRepository(Protocol):
    """Storage seam for unit tests and PostgreSQL."""

    async def lock_chain(self) -> None: ...

    async def list_chain(self) -> tuple[RuntimeRolloutPhaseRecord, ...]: ...

    async def by_deployment_id(
        self,
        deployment_id: str,
    ) -> RuntimeRolloutPhaseRecord | None: ...

    async def append(self, record: RuntimeRolloutPhaseRecord) -> bool: ...


class PostgresRuntimeRolloutPhaseAuditRepository:
    """Persist the global rollout-phase chain in the existing audit ledger."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def lock_chain(self) -> None:
        # Bind the phase entry to one stable runtime-switch head.  The runtime
        # ledger uses key ``(..., 1)``; always acquire it before the phase key
        # to avoid a cross-ledger race or lock-order inversion.
        await self._db.execute(text("SELECT pg_advisory_xact_lock(148120306, 1)"))
        await self._db.execute(text("SELECT pg_advisory_xact_lock(148120306, 2)"))

    async def lock_for_status(self) -> None:
        """Hold a coherent read view while both deployment ledgers are read."""

        await self._db.execute(text("SELECT pg_advisory_xact_lock_shared(148120306, 1)"))
        await self._db.execute(text("SELECT pg_advisory_xact_lock_shared(148120306, 2)"))

    @staticmethod
    def _record(row: AuditEvent) -> RuntimeRolloutPhaseRecord:
        try:
            record = RuntimeRolloutPhaseRecord.model_validate(row.payload)
        except (TypeError, ValueError):
            raise RuntimeRolloutPhaseAuditMismatch("rollout phase audit row has invalid payload") from None
        if row.session_id is not None or row.actor_type != "system":
            raise RuntimeRolloutPhaseAuditMismatch("rollout phase audit row has invalid authority")
        if row.actor_id != record.operator or row.trace_id != record.deployment_id:
            raise RuntimeRolloutPhaseAuditMismatch("rollout phase audit row has inconsistent provenance")
        if row.created_at != record.timestamp:
            raise RuntimeRolloutPhaseAuditMismatch("rollout phase audit row has inconsistent timestamp")
        return record

    async def list_chain(self) -> tuple[RuntimeRolloutPhaseRecord, ...]:
        rows = (
            await self._db.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == ROLLOUT_PHASE_EVENT)
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            )
        ).all()
        return tuple(self._record(row) for row in rows)

    async def by_deployment_id(
        self,
        deployment_id: str,
    ) -> RuntimeRolloutPhaseRecord | None:
        row = await self._db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.event_type == ROLLOUT_PHASE_EVENT,
                AuditEvent.trace_id == deployment_id,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        return self._record(row) if row is not None else None

    async def append(self, record: RuntimeRolloutPhaseRecord) -> bool:
        statement = (
            pg_insert(AuditEvent)
            .values(
                session_id=None,
                event_type=ROLLOUT_PHASE_EVENT,
                actor_type="system",
                actor_id=record.operator,
                payload=record.model_dump(mode="json"),
                trace_id=record.deployment_id,
                # A unique, service-validated transition timestamp provides a
                # deterministic durable chain order. PostgreSQL ``now()`` is
                # transaction-start time and can reorder lock waiters.
                created_at=record.timestamp,
            )
            .on_conflict_do_nothing(
                index_elements=[AuditEvent.event_type, AuditEvent.trace_id],
                index_where=text("event_type = 'runtime.rollout_phase_changed' AND trace_id IS NOT NULL"),
            )
            .returning(AuditEvent.id)
        )
        return (await self._db.scalar(statement)) is not None


class RuntimeRolloutPhaseAuditService:
    """Validate and append a durable phase chain; expose current full entry."""

    def __init__(self, repository: RuntimeRolloutPhaseAuditRepository) -> None:
        self._repository = repository

    @staticmethod
    def _same_command(
        stored: RuntimeRolloutPhaseRecord,
        requested: RuntimeRolloutPhaseRecord,
    ) -> bool:
        """Treat retry time as transport metadata, not a new phase entry."""

        return stored.model_dump(exclude={"timestamp"}) == requested.model_dump(exclude={"timestamp"})

    async def status(
        self,
        *,
        configured_phase: RuntimeRolloutPhase,
        configured_runtime: RuntimeName,
        runtime_switch_deployment_id: str | None,
        expected_phase_deployment_id: str | None,
    ) -> RuntimeRolloutPhaseAuditStatus:
        records = await self._repository.list_chain()
        if not records:
            initial_ok = configured_phase == "legacy" and configured_runtime == "legacy"
            return RuntimeRolloutPhaseAuditStatus(
                status="ok" if initial_ok else "missing",
                configured_phase=configured_phase,
                configured_runtime=configured_runtime,
                audited_phase="legacy" if initial_ok else None,
                audit_present=False,
                deployment_id=None,
                runtime_switch_deployment_id=None,
                full_entered_at=None,
            )

        current_phase: RuntimeRolloutPhase = "legacy"
        previous_timestamp: datetime | None = None
        for record in records:
            if record.from_phase != current_phase or (
                previous_timestamp is not None and record.timestamp <= previous_timestamp
            ):
                return self._status_from_latest(
                    "invalid_chain",
                    configured_phase,
                    configured_runtime,
                    records[-1],
                )
            current_phase = record.to_phase
            previous_timestamp = record.timestamp

        latest = records[-1]
        matches = (
            current_phase == configured_phase
            and latest.runtime == configured_runtime
            and latest.runtime_switch_deployment_id == runtime_switch_deployment_id
            and (expected_phase_deployment_id is None or latest.deployment_id == expected_phase_deployment_id)
        )
        return self._status_from_latest(
            "ok" if matches else "mismatch",
            configured_phase,
            configured_runtime,
            latest,
        )

    @staticmethod
    def _status_from_latest(
        status: RolloutPhaseAuditState,
        configured_phase: RuntimeRolloutPhase,
        configured_runtime: RuntimeName,
        latest: RuntimeRolloutPhaseRecord,
    ) -> RuntimeRolloutPhaseAuditStatus:
        return RuntimeRolloutPhaseAuditStatus(
            status=status,
            configured_phase=configured_phase,
            configured_runtime=configured_runtime,
            audited_phase=latest.to_phase,
            audit_present=True,
            deployment_id=latest.deployment_id,
            runtime_switch_deployment_id=latest.runtime_switch_deployment_id,
            full_entered_at=(latest.timestamp if status == "ok" and latest.to_phase == "full" else None),
        )

    async def record_transition(
        self,
        record: RuntimeRolloutPhaseRecord,
        *,
        configured_phase: RuntimeRolloutPhase,
        configured_runtime: RuntimeName,
        runtime_switch_deployment_id: str | None,
    ) -> tuple[RuntimeRolloutPhaseRecord, bool]:
        """Append one transition, replaying an identical deployment command."""

        await self._repository.lock_chain()
        if (
            record.to_phase != configured_phase
            or record.runtime != configured_runtime
            or record.runtime_switch_deployment_id != runtime_switch_deployment_id
        ):
            raise RuntimeRolloutPhaseAuditConflict("rollout phase command does not match this deployment")

        existing = await self._repository.by_deployment_id(record.deployment_id)
        if existing is not None:
            if not self._same_command(existing, record):
                raise RuntimeRolloutPhaseAuditConflict("deployment id was already used by another phase transition")
            return existing, True

        records = await self._repository.list_chain()
        current_phase: RuntimeRolloutPhase = "legacy"
        previous_timestamp: datetime | None = None
        for stored in records:
            if stored.from_phase != current_phase or (
                previous_timestamp is not None and stored.timestamp <= previous_timestamp
            ):
                raise RuntimeRolloutPhaseAuditConflict("durable rollout phase chain is invalid")
            current_phase = stored.to_phase
            previous_timestamp = stored.timestamp
        if record.from_phase != current_phase:
            raise RuntimeRolloutPhaseAuditConflict("rollout phase source does not match the durable current phase")
        if previous_timestamp is not None and record.timestamp <= previous_timestamp:
            raise RuntimeRolloutPhaseAuditConflict("rollout phase timestamp precedes the durable phase chain")

        if await self._repository.append(record):
            return record, False
        concurrent = await self._repository.by_deployment_id(record.deployment_id)
        if concurrent is not None and self._same_command(concurrent, record):
            return concurrent, True
        raise RuntimeRolloutPhaseAuditConflict("deployment id was concurrently used by another phase transition")


__all__ = [
    "PostgresRuntimeRolloutPhaseAuditRepository",
    "ROLLOUT_PHASE_EVENT",
    "RuntimeRolloutPhaseAuditConflict",
    "RuntimeRolloutPhaseAuditError",
    "RuntimeRolloutPhaseAuditMismatch",
    "RuntimeRolloutPhaseAuditService",
    "RuntimeRolloutPhaseAuditStatus",
    "RuntimeRolloutPhaseRecord",
]
