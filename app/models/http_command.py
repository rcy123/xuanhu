"""Durable idempotency claims for public HTTP write commands."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class HttpCommandClaim(Base, UUIDPrimaryKeyMixin):
    """One durable claim for one logical public write request."""

    __tablename__ = "http_command_claims"

    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    concurrency_scope: Mapped[str | None] = mapped_column(String(160), nullable=True)
    idempotency_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        server_onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "operation",
            "scope_key",
            "idempotency_key_digest",
            name="uq_http_command_claims_logical_command",
        ),
        CheckConstraint(
            "idempotency_mode IN ('public','non_idempotent')",
            name="chk_http_command_claims_idempotency_mode",
        ),
        CheckConstraint(
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_http_command_claims_key_digest",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="chk_http_command_claims_request_digest",
        ),
        CheckConstraint(
            "status IN ('running','completed','failed','ambiguous')",
            name="chk_http_command_claims_status",
        ),
        CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="chk_http_command_claims_http_status",
        ),
        CheckConstraint(
            "response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'",
            name="chk_http_command_claims_response_object",
        ),
        CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="chk_http_command_claims_error_object",
        ),
        CheckConstraint(
            "(status = 'completed' AND response_payload IS NOT NULL AND http_status IS NOT NULL) "
            "OR status <> 'completed'",
            name="chk_http_command_claims_completed_payload",
        ),
        Index(
            "uq_http_command_claims_inflight_scope",
            "concurrency_scope",
            unique=True,
            postgresql_where=text("status = 'running' AND concurrency_scope IS NOT NULL"),
        ),
        Index("idx_http_command_claims_status_lease", "status", "lease_expires_at"),
        Index("idx_http_command_claims_scope_created", "scope_key", "created_at"),
    )
