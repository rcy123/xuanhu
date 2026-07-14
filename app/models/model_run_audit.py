"""Minimal durable audit rows for LangGraph model executions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelRunAudit(Base):
    """Allowlisted model provenance; never stores prompts or model output."""

    __tablename__ = "model_run_audits"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_spec_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    output_schema_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_requested: Mapped[str] = mapped_column(String(200), nullable=False)
    model_actual: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('started','succeeded','failed','cancelled')",
            name="chk_model_run_audits_status",
        ),
        CheckConstraint("attempts >= 0", name="chk_model_run_audits_attempts"),
        CheckConstraint("latency_ms >= 0", name="chk_model_run_audits_latency"),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="chk_model_run_audits_token_usage",
        ),
        CheckConstraint(
            "output_digest IS NULL OR output_digest ~ '^[0-9a-f]{64}$'",
            name="chk_model_run_audits_output_digest",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND output_digest IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND output_digest IS NULL AND error_code IS NOT NULL) OR "
            "(status IN ('started','cancelled') AND output_digest IS NULL AND error_code IS NULL)",
            name="chk_model_run_audits_terminal_payload",
        ),
        Index("idx_model_run_audits_session_created", "session_id", "created_at"),
        Index("idx_model_run_audits_trace", "trace_id"),
        Index("idx_model_run_audits_status_updated", "status", "updated_at"),
    )
