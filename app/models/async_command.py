"""Durable async command substrate for R6-A.

This is a *separate* table from ``http_command_claims``: the synchronous
HTTP write path keeps its exact R1-R5 semantics, while R6-B will opt
selected POST endpoints into an async, worker-dispatched lifecycle backed by
this table.

Security contract for the private payloads:
``request_payload`` and ``result_payload`` are private domain data. They may
contain protected health information (PHI) and are treated as PostgreSQL domain
data — they must never be returned to clients, logged, projected into
Outbox/SSE, copied into error details, emitted in metrics, or surfaced through
any representation-oriented DTO or the status API. The only safe projection of
``request_payload`` is the worker ``ClaimedCommand`` used to dispatch, and the
digest ``request_digest``. ``error_payload`` is a sanitized empty object in
R6-A: the repository persists exactly ``{}`` regardless of caller input, so it
can never carry private content.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

# The single explicit, finite operation allowlist for the planned R6-B
# operations. It is the source of truth enforced by the repository validator,
# the ORM/migration DB check constraint, the worker registry validator and the
# Outbox mapper. Adding an operation here is a deliberate, cross-cutting change.
ASYNC_COMMAND_OPERATIONS = frozenset({"intake.message", "session.advance", "prescription.review"})

_ASYNC_COMMAND_OPERATION_LIST = ", ".join(repr(op) for op in sorted(ASYNC_COMMAND_OPERATIONS))


class AsyncCommand(Base, UUIDPrimaryKeyMixin):
    """One durable, worker-dispatched asynchronous command.

    ``id`` is the public stable command UUID returned to clients.
    """

    __tablename__ = "async_commands"

    # ---- identity / routing ----
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Private worker-dispatch payload (may contain PHI). Protected as domain
    # data — never returned, logged, projected or emitted.
    request_payload: Mapped[dict[str, Any]] = mapped_column(cast(Any, JSONB)(none_as_null=True), nullable=False)

    # ---- lifecycle / lease ----
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- terminal outcome (public-safe) ----
    result_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Private worker-produced domain output (may contain PHI). Protected as
    # domain data — never returned, logged, projected or emitted. R6-A does not
    # yet populate it; the R6-B outcome contracts will do so.
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(cast(Any, JSONB)(none_as_null=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # R6-A: error_payload is a sanitized empty object — exactly {} is persisted
    # by the repository regardless of caller input, so it can never carry PHI.
    error_payload: Mapped[dict[str, Any] | None] = mapped_column(cast(Any, JSONB)(none_as_null=True), nullable=True)

    # ---- timestamps ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        server_onupdate=cast(FetchedValue, func.now()),
        nullable=False,
    )

    __table_args__ = (
        # One logical command per (session, operation, idempotency key). Stored
        # as a digest only — the raw idempotency key is never persisted. The
        # operation is part of the logical identity so the same key on a
        # different operation is a new logical command, never a replay.
        UniqueConstraint(
            "session_id",
            "operation",
            "idempotency_key_digest",
            name="uq_async_commands_logical_command",
        ),
        # At most one active (queued OR running) command per session.
        Index(
            "uq_async_commands_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        CheckConstraint(
            f"operation IN ({_ASYNC_COMMAND_OPERATION_LIST})",
            name="chk_async_commands_operation_allowlist",
        ),
        CheckConstraint(
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_async_commands_key_digest",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="chk_async_commands_request_digest",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed')",
            name="chk_async_commands_status",
        ),
        CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'",
            name="chk_async_commands_request_object",
        ),
        CheckConstraint(
            "result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'",
            name="chk_async_commands_result_object",
        ),
        CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="chk_async_commands_error_object",
        ),
        CheckConstraint("attempt_count >= 0", name="chk_async_commands_attempt_count"),
        CheckConstraint(
            "result_http_status IS NULL OR (result_http_status >= 100 AND result_http_status <= 599)",
            name="chk_async_commands_http_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="chk_async_commands_error_code",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="chk_async_commands_lease_relation",
        ),
        # Terminal payload invariants: succeeded carries a result; failed
        # carries a sanitized error code/payload. Neither carries the other.
        CheckConstraint(
            "(status = 'succeeded' AND result_http_status IS NOT NULL AND result_payload IS NOT NULL "
            "AND error_code IS NULL AND error_payload IS NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL AND error_payload IS NOT NULL "
            "AND result_http_status IS NULL AND result_payload IS NULL) "
            "OR status IN ('queued','running')",
            name="chk_async_commands_terminal_payload",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed') AND completed_at IS NOT NULL) "
            "OR (status IN ('queued','running') AND completed_at IS NULL)",
            name="chk_async_commands_completed_relation",
        ),
        Index("idx_async_commands_claim", "status", "available_at", "lease_expires_at"),
        Index("idx_async_commands_session_created", "session_id", "created_at"),
    )

    def __repr__(self) -> str:
        # Never include request_payload / digests / lease internals in repr.
        return (
            f"<AsyncCommand id={self.id} session_id={self.session_id} "
            f"operation={self.operation!r} status={self.status!r} "
            f"attempt_count={self.attempt_count}>"
        )
