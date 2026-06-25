"""医师确认表 doctor_reviews 和病历表 medical_records。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent import AgentRun
    from app.models.consult import ConsultSession
    from app.models.safety import SafetyRuleRun


class DoctorReview(Base, UUIDPrimaryKeyMixin):
    """医师确认记录表 — doctor_reviews。"""

    __tablename__ = "doctor_reviews"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    safety_rule_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("safety_rule_runs.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    original_formula: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    formula_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    session: Mapped[ConsultSession] = relationship("ConsultSession", back_populates="doctor_reviews")
    agent_run: Mapped[AgentRun | None] = relationship("AgentRun", back_populates="doctor_reviews")
    safety_rule_run: Mapped[SafetyRuleRun | None] = relationship(
        "SafetyRuleRun", back_populates="doctor_reviews"
    )
    medical_records: Mapped[list[MedicalRecord]] = relationship(
        "MedicalRecord", back_populates="doctor_review", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('confirm','modify','reject')",
            name="chk_doctor_reviews_action",
        ),
        Index("idx_doctor_reviews_session_created", "session_id", created_at.desc()),
        Index("idx_doctor_reviews_agent_run", "agent_run_id"),
        Index("idx_doctor_reviews_safety_rule_run", "safety_rule_run_id"),
        Index("idx_doctor_reviews_reviewed_by", "reviewed_by", created_at.desc()),
    )


class MedicalRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """病历表 — medical_records。"""

    __tablename__ = "medical_records"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    record_text: Mapped[str] = mapped_column(Text, nullable=False)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diff_from_previous: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    doctor_review_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doctor_reviews.id", ondelete="SET NULL"), nullable=True
    )
    disclaimer: Mapped[str] = mapped_column(Text, nullable=False)
    edited_by_doctor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- relationships --
    session: Mapped[ConsultSession] = relationship("ConsultSession", back_populates="medical_records")
    doctor_review: Mapped[DoctorReview | None] = relationship(
        "DoctorReview", back_populates="medical_records"
    )

    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="chk_medical_records_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(record_json) = 'object'",
            name="chk_medical_records_record_json_object",
        ),
        Index(
            "uniq_medical_records_session_version",
            "session_id",
            "version",
            unique=True,
        ),
        Index("idx_medical_records_session_version", "session_id", version.desc()),
        Index("idx_medical_records_doctor_review", "doctor_review_id"),
    )
