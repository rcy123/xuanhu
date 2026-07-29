"""Durable audit contract for changing the default Agent runtime.

The deployment configuration is intentionally not mutable through the public
API.  Operators record a switch before starting a deployment with the new
``AGENT_RUNTIME_VERSION``.  The application can then fail closed when the
configured default and the durable audit trail disagree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent

RUNTIME_SWITCH_EVENT = "runtime.switched"
RuntimeName = Literal["legacy", "langgraph"]


class RuntimeSwitchAuditError(RuntimeError):
    """A sanitized runtime-switch audit failure."""


class RuntimeSwitchAuditConflict(RuntimeSwitchAuditError):
    """The requested switch conflicts with the durable runtime chain."""


class RuntimeSwitchAuditMismatch(RuntimeSwitchAuditError):
    """The configured default runtime has not been durably authorized."""


class RuntimeSwitchRecord(BaseModel):
    """Allowlisted payload stored in ``audit_events``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_runtime: RuntimeName
    to_runtime: RuntimeName
    operator: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=5, max_length=500)
    deployment_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime switch timestamp must be timezone-aware")
        return value


class RuntimeSwitchAuditStatus(BaseModel):
    """Privacy-safe status consumed by readiness checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "mismatch"]
    configured_runtime: RuntimeName
    audited_runtime: RuntimeName
    audit_present: bool
    last_switch_at: datetime | None


class RuntimeSwitchAuditRepository(Protocol):
    """Minimal storage seam used by the service and unit tests."""

    async def lock_chain(self) -> None: ...

    async def latest(self) -> RuntimeSwitchRecord | None: ...

    async def by_deployment_id(self, deployment_id: str) -> RuntimeSwitchRecord | None: ...

    async def append(self, record: RuntimeSwitchRecord) -> bool: ...


class PostgresRuntimeSwitchAuditRepository:
    """Store global runtime switch events in the existing audit ledger."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def lock_chain(self) -> None:
        # Serialize the global transition chain until the surrounding
        # transaction commits.  Two different deployment IDs must not both
        # observe the same source runtime and append divergent transitions.
        await self._db.execute(text("SELECT pg_advisory_xact_lock(148120306, 1)"))

    @staticmethod
    def _record(row: AuditEvent | None) -> RuntimeSwitchRecord | None:
        if row is None:
            return None
        record = RuntimeSwitchRecord.model_validate(row.payload)
        if row.session_id is not None or row.actor_type != "system":
            raise RuntimeSwitchAuditMismatch("runtime switch audit row has invalid authority")
        if row.actor_id != record.operator or row.trace_id != record.deployment_id:
            raise RuntimeSwitchAuditMismatch("runtime switch audit row has inconsistent provenance")
        return record

    async def latest(self) -> RuntimeSwitchRecord | None:
        row = await self._db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        return self._record(row)

    async def by_deployment_id(self, deployment_id: str) -> RuntimeSwitchRecord | None:
        row = await self._db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.event_type == RUNTIME_SWITCH_EVENT,
                AuditEvent.trace_id == deployment_id,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        return self._record(row)

    async def append(self, record: RuntimeSwitchRecord) -> bool:
        statement = (
            pg_insert(AuditEvent)
            .values(
                session_id=None,
                event_type=RUNTIME_SWITCH_EVENT,
                actor_type="system",
                actor_id=record.operator,
                payload=record.model_dump(mode="json"),
                trace_id=record.deployment_id,
            )
            .on_conflict_do_nothing(
                index_elements=[AuditEvent.event_type, AuditEvent.trace_id],
                # Keep the partial-index inference predicate literal. A bound
                # event_type may stop matching the index under PostgreSQL's
                # generic prepared plan after repeated executions.
                index_where=text("event_type = 'runtime.switched' AND trace_id IS NOT NULL"),
            )
            .returning(AuditEvent.id)
        )
        return (await self._db.scalar(statement)) is not None


class RuntimeSwitchAuditService:
    """Validate and append a linear, idempotent runtime switch chain."""

    def __init__(self, repository: RuntimeSwitchAuditRepository) -> None:
        self._repository = repository

    async def status(self, configured_runtime: RuntimeName) -> RuntimeSwitchAuditStatus:
        latest = await self._repository.latest()
        # ``legacy`` is the immutable initial default.  The first transition
        # away from it must be explicit; ordinary first startup needs no fake
        # switch event.
        audited_runtime: RuntimeName = latest.to_runtime if latest is not None else "legacy"
        return RuntimeSwitchAuditStatus(
            status="ok" if audited_runtime == configured_runtime else "mismatch",
            configured_runtime=configured_runtime,
            audited_runtime=audited_runtime,
            audit_present=latest is not None,
            last_switch_at=latest.timestamp if latest is not None else None,
        )

    async def ensure_configured_runtime(self, configured_runtime: RuntimeName) -> None:
        status = await self.status(configured_runtime)
        if status.status != "ok":
            raise RuntimeSwitchAuditMismatch("configured default runtime does not match the durable switch audit")

    async def record_switch(
        self,
        record: RuntimeSwitchRecord,
        *,
        configured_runtime: RuntimeName,
    ) -> tuple[RuntimeSwitchRecord, bool]:
        """Append one switch or replay the same deployment idempotently."""

        await self._repository.lock_chain()

        if record.from_runtime == record.to_runtime:
            raise RuntimeSwitchAuditConflict("runtime switch must change the runtime")
        if record.to_runtime != configured_runtime:
            raise RuntimeSwitchAuditConflict("switch target does not match AGENT_RUNTIME_VERSION for this deployment")

        existing = await self._repository.by_deployment_id(record.deployment_id)
        if existing is not None:
            same_command = (
                existing.from_runtime == record.from_runtime
                and existing.to_runtime == record.to_runtime
                and existing.operator == record.operator
                and existing.reason == record.reason
            )
            if not same_command:
                raise RuntimeSwitchAuditConflict("deployment id was already used by a different runtime switch")
            return existing, True

        latest = await self._repository.latest()
        current_runtime: RuntimeName = latest.to_runtime if latest is not None else "legacy"
        if record.from_runtime != current_runtime:
            raise RuntimeSwitchAuditConflict("runtime switch source does not match the durable current runtime")

        if await self._repository.append(record):
            return record, False

        # The unique deployment index is defense in depth for callers that
        # bypass the advisory-lock implementation.  Resolve a concurrent
        # identical command as an idempotent replay and reject any collision.
        concurrent = await self._repository.by_deployment_id(record.deployment_id)
        if concurrent is not None and (
            concurrent.from_runtime == record.from_runtime
            and concurrent.to_runtime == record.to_runtime
            and concurrent.operator == record.operator
            and concurrent.reason == record.reason
        ):
            return concurrent, True
        raise RuntimeSwitchAuditConflict("deployment id was concurrently used by a different runtime switch")
