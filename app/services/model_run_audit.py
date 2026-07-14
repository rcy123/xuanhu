"""Durable, allowlist-only recorder for production AgentRuntime calls."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session_factory
from app.models.model_run_audit import ModelRunAudit


class _AuditPayload(BaseModel):
    """The complete and exclusive set of values permitted into audit storage."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    session_id: UUID
    agent_name: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    agent_spec_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
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


class PostgresModelRunRecorder:
    """Persist runtime lifecycle events in an independent short transaction.

    The longer timeout applies only to this known durable recorder.  Generic
    recorders retain AgentRuntime's 50 ms fail-open behavior.
    """

    timeout_seconds = 2.0
    finalization_timeout_seconds = 2.0

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
        # First terminal event wins.  A restarted worker may replay ``started``
        # or the same terminal event, but cannot duplicate or downgrade a row.
        update_values = {
            column: getattr(insert.excluded, column)
            for column in (
                "agent_name",
                "stage",
                "agent_spec_version",
                "prompt_version",
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
            where=ModelRunAudit.status == "started",
        )
        factory = self._factory()
        async with factory() as session, session.begin():
            await session.execute(statement)
