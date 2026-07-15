"""Durable, allowlist-only recorder for production AgentRuntime calls."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ModelRunAuditIntegrityError
from app.db.session import get_session_factory
from app.models.model_run_audit import ModelRunAudit


class ModelRunAuditProvenanceConflictError(ModelRunAuditIntegrityError):
    """A run id was replayed with different immutable provenance."""

    def __init__(self) -> None:
        super().__init__("model-run audit provenance conflict")


class ModelRunAuditTerminalConflictError(ModelRunAuditIntegrityError):
    """A finalized run id was replayed with a different terminal outcome."""

    def __init__(self) -> None:
        super().__init__("model-run audit terminal conflict")


class ModelRunAuditAlreadyFinalizedError(ModelRunAuditIntegrityError):
    """A finalized run id was submitted as a new model execution."""

    def __init__(self) -> None:
        super().__init__("model-run audit already finalized")


_PROVENANCE_FIELDS = (
    "session_id",
    "agent_name",
    "stage",
    "agent_spec_version",
    "prompt_version",
    "policy_version",
    "input_digest",
    "output_schema_id",
    "model_requested",
    "trace_id",
)

_TERMINAL_FIELDS = (
    "model_actual",
    "attempts",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "output_digest",
    "error_code",
)


class _AuditPayload(BaseModel):
    """The complete and exclusive set of values permitted into audit storage."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    session_id: UUID
    agent_name: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    agent_spec_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    policy_version: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_id: str = Field(min_length=1, max_length=255)
    model_requested: str = Field(min_length=1, max_length=200)
    model_actual: str | None = Field(default=None, min_length=1, max_length=200)
    attempts: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trace_id: str = Field(min_length=1, max_length=200)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


def _terminal_replay_conflicts(
    existing: ModelRunAudit,
    payload: _AuditPayload,
    status: Literal["started", "succeeded", "failed", "cancelled"],
) -> bool:
    """Return whether a second finalization disagrees with the durable outcome."""

    if existing.status == "started" or status == "started":
        return False
    return existing.status != status or any(
        getattr(existing, field) != getattr(payload, field) for field in _TERMINAL_FIELDS
    )


class PostgresModelRunRecorder:
    """Persist runtime lifecycle events in an independent short transaction.

    This production recorder is required and uses a bounded longer timeout;
    generic recorders retain AgentRuntime's 50 ms fail-open behavior.
    """

    timeout_seconds = 2.0
    finalization_timeout_seconds = 2.0
    required = True

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def record(self, event: str, data: dict[str, Any]) -> None:
        status: Literal["started", "succeeded", "failed", "cancelled"]
        if event == "started":
            status = "started"
        elif event == "succeeded":
            status = "succeeded"
        elif event == "failed":
            status = "failed"
        elif event == "cancelled":
            status = "cancelled"
        else:
            raise ValueError("unsupported model-run audit event")

        payload = _AuditPayload.model_validate(data)
        if status == "succeeded":
            if payload.output_digest is None or payload.error_code is not None:
                raise ValueError("invalid succeeded model-run audit payload")
        elif status == "failed":
            if payload.error_code is None or payload.output_digest is not None:
                raise ValueError("invalid failed model-run audit payload")
        elif payload.output_digest is not None or payload.error_code is not None:
            raise ValueError("invalid non-terminal model-run audit payload")

        values = {
            **payload.model_dump(mode="python"),
            "status": status,
        }
        insert = pg_insert(ModelRunAudit).values(**values)
        # First terminal event wins.  An in-flight worker may replay ``started``
        # while the row is still started, and exact terminal events are
        # idempotent; finalized runs cannot be restarted or overwritten.
        update_values = {
            column: getattr(insert.excluded, column)
            for column in (
                "agent_name",
                "stage",
                "agent_spec_version",
                "prompt_version",
                "policy_version",
                "input_digest",
                "output_schema_id",
                "model_requested",
                "model_actual",
                "status",
                "attempts",
                "latency_ms",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "output_digest",
                "error_code",
                "trace_id",
            )
        }
        update_values["updated_at"] = func.now()
        statement = insert.on_conflict_do_update(
            index_elements=[ModelRunAudit.run_id],
            set_=update_values,
            where=and_(
                ModelRunAudit.status == "started",
                ModelRunAudit.session_id == insert.excluded.session_id,
                ModelRunAudit.agent_name == insert.excluded.agent_name,
                ModelRunAudit.stage == insert.excluded.stage,
                ModelRunAudit.agent_spec_version == insert.excluded.agent_spec_version,
                ModelRunAudit.prompt_version == insert.excluded.prompt_version,
                ModelRunAudit.policy_version == insert.excluded.policy_version,
                ModelRunAudit.input_digest == insert.excluded.input_digest,
                ModelRunAudit.output_schema_id == insert.excluded.output_schema_id,
                ModelRunAudit.model_requested == insert.excluded.model_requested,
                ModelRunAudit.trace_id == insert.excluded.trace_id,
            ),
        ).returning(ModelRunAudit.run_id)
        factory = self._factory()
        async with factory() as session, session.begin():
            result = await session.execute(statement)
            if result.scalar_one_or_none() is not None:
                return

            # ``ON CONFLICT ... WHERE`` intentionally returns no row for an
            # immutable terminal replay and for a provenance mismatch.  Treat
            # an exact terminal replay as an idempotent no-op, but never let a
            # caller mistake a conflicting run id for a successful write.
            existing = await session.get(ModelRunAudit, payload.run_id)
            if existing is None:
                raise RuntimeError("model-run audit conflict resolution failed")
            if any(getattr(existing, field) != getattr(payload, field) for field in _PROVENANCE_FIELDS):
                raise ModelRunAuditProvenanceConflictError
            if existing.status != "started" and status == "started":
                raise ModelRunAuditAlreadyFinalizedError
            if _terminal_replay_conflicts(existing, payload, status):
                raise ModelRunAuditTerminalConflictError
