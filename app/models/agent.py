"""Agent 运行表 agent_runs 和证据表 agent_evidences。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.consult import ConsultMessage, ConsultSession
    from app.models.review import DoctorReview
    from app.models.safety import SafetyRuleRun


class AgentRun(Base, UUIDPrimaryKeyMixin):
    """Agent 运行记录表 — agent_runs。"""

    __tablename__ = "agent_runs"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    session: Mapped[ConsultSession] = relationship("ConsultSession", back_populates="agent_runs")
    messages: Mapped[list[ConsultMessage]] = relationship(
        "ConsultMessage", back_populates="agent_run", lazy="raise"
    )
    evidences: Mapped[list[AgentEvidence]] = relationship(
        "AgentEvidence", back_populates="agent_run", lazy="raise"
    )
    safety_rule_runs: Mapped[list[SafetyRuleRun]] = relationship(
        "SafetyRuleRun", back_populates="agent_run", lazy="raise"
    )
    doctor_reviews: Mapped[list[DoctorReview]] = relationship(
        "DoctorReview", back_populates="agent_run", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','failed','blocked')",
            name="chk_agent_runs_status",
        ),
        Index("idx_agent_runs_session_created", "session_id", created_at.desc()),
        Index("idx_agent_runs_agent_status", "agent_name", "status", created_at.desc()),
        Index("idx_agent_runs_trace_id", "trace_id"),
    )


class AgentEvidence(Base, UUIDPrimaryKeyMixin):
    """Agent 证据引用表 — agent_evidences。"""

    __tablename__ = "agent_evidences"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    agent_run: Mapped[AgentRun] = relationship("AgentRun", back_populates="evidences")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('formula','herb','acupoint','theory','case')",
            name="chk_agent_evidences_source_type",
        ),
        Index("idx_agent_evidences_agent_run", "agent_run_id"),
        Index("idx_agent_evidences_session", "session_id", created_at.desc()),
        Index("idx_agent_evidences_source", "source_type", "source_id"),
        Index("idx_agent_evidences_chunk", "chunk_id"),
    )
