"""安全规则运行表 safety_rule_runs。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent import AgentRun
    from app.models.consult import ConsultSession
    from app.models.review import DoctorReview


class SafetyRuleRun(Base, UUIDPrimaryKeyMixin):
    """安全规则运行记录表 — safety_rule_runs。"""

    __tablename__ = "safety_rule_runs"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    formula_source: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    formula_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized_formula: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    patient_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    session: Mapped[ConsultSession] = relationship("ConsultSession", back_populates="safety_rule_runs")
    agent_run: Mapped[AgentRun | None] = relationship("AgentRun", back_populates="safety_rule_runs")
    doctor_reviews: Mapped[list[DoctorReview]] = relationship(
        "DoctorReview", back_populates="safety_rule_run", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(
            "formula_source IN ('agent_output','doctor_override')",
            name="chk_safety_rule_runs_formula_source",
        ),
        CheckConstraint(
            "jsonb_typeof(issues) = 'array'",
            name="chk_safety_rule_runs_issues_array",
        ),
        CheckConstraint(
            "jsonb_typeof(formula_snapshot) = 'object'",
            name="chk_safety_rule_runs_formula_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(patient_snapshot) = 'object'",
            name="chk_safety_rule_runs_patient_snapshot_object",
        ),
        Index("idx_safety_rule_runs_session_created", "session_id", created_at.desc()),
        Index("idx_safety_rule_runs_agent_run", "agent_run_id"),
        Index("idx_safety_rule_runs_passed", "passed", created_at.desc()),
        Index("idx_safety_rule_runs_rule_version", "rule_version", created_at.desc()),
    )
