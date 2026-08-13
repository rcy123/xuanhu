"""会话表 consult_sessions 和消息表 consult_messages。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent import AgentRun
    from app.models.audit import AuditEvent
    from app.models.review import DoctorReview, MedicalRecord
    from app.models.safety import SafetyRuleRun


class ConsultSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """问诊会话表 — consult_sessions。"""

    __tablename__ = "consult_sessions"

    patient_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    patient_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chief_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="inquiry")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # 0c 单一后端收敛（docs/02_agent逻辑优化/0c_单一后端收敛边界.md）：本字段自大改起
    # 降级为【仅历史 session 兼容读】——新 session 一律创建为 "langgraph"，统一后端路径。
    # 字段本身保留（老 session 数据仍可读），但不参与新 session 路由；legacy 路径代码
    # 冻结不演进，阶段 3d 收口后删除（supervisor.py 等）。不得再新增任何按本字段分叉的
    # 业务路由；分叉点清单见 0c 文档"分叉点现状"节。
    agent_runtime: Mapped[str] = mapped_column(String(16), nullable=False, default="legacy")
    pending_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rollback_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_checkpoint_at: Mapped[datetime | None] = mapped_column(nullable=True)
    recovery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    blocked_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 阶段 2 加固：会话负责医师（JWT claim 写入，非客户端可改）。
    # NULL 仅可能出现在历史存量数据中；on 模式下一律 fail-closed 不可见。
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doctors.id", ondelete="RESTRICT"), nullable=True
    )

    # -- relationships --
    messages: Mapped[list[ConsultMessage]] = relationship(
        "ConsultMessage", back_populates="session", lazy="raise"
    )
    agent_runs: Mapped[list[AgentRun]] = relationship(
        "AgentRun", back_populates="session", lazy="raise"
    )
    safety_rule_runs: Mapped[list[SafetyRuleRun]] = relationship(
        "SafetyRuleRun", back_populates="session", lazy="raise"
    )
    doctor_reviews: Mapped[list[DoctorReview]] = relationship(
        "DoctorReview", back_populates="session", lazy="raise"
    )
    medical_records: Mapped[list[MedicalRecord]] = relationship(
        "MedicalRecord", back_populates="session", lazy="raise"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent", back_populates="session", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(patient_info) = 'object'",
            name="chk_consult_sessions_patient_info_object",
        ),
        CheckConstraint(
            "state_version >= 1",
            name="chk_consult_sessions_state_version_positive",
        ),
        CheckConstraint(
            "current_stage IN ('inquiry','sufficiency','syndrome','prescription',"
            "'modification','safety','review','record','done','blocked')",
            name="chk_consult_sessions_current_stage",
        ),
        CheckConstraint(
            "status IN ('active','pending_review','done','blocked','terminated')",
            name="chk_consult_sessions_status",
        ),
        CheckConstraint(
            "agent_runtime IN ('legacy','langgraph')",
            name="chk_consult_sessions_agent_runtime",
        ),
        CheckConstraint(
            "recovery_status IN ('normal','recovering','manual_required')",
            name="chk_consult_sessions_recovery_status",
        ),
        Index("idx_consult_sessions_status_updated_at", "status", sa.text("updated_at DESC")),
        Index("idx_consult_sessions_agent_runtime", "agent_runtime"),
        Index("idx_consult_sessions_patient_ref", "patient_ref"),
        Index("idx_consult_sessions_current_stage", "current_stage"),
        Index("idx_consult_sessions_recovery_status", "recovery_status"),
        Index("idx_consult_sessions_blocked", "blocked_reason", "blocked_at"),
    )


class ConsultMessage(Base, UUIDPrimaryKeyMixin):
    """问诊消息表 — consult_messages。"""

    __tablename__ = "consult_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_delta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    session: Mapped[ConsultSession] = relationship("ConsultSession", back_populates="messages")
    agent_run: Mapped[AgentRun | None] = relationship("AgentRun", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "role IN ('doctor','patient_proxy','agent','system')",
            name="chk_consult_messages_role",
        ),
        Index("idx_consult_messages_session_created", "session_id", "created_at"),
        Index("idx_consult_messages_agent_run", "agent_run_id"),
        Index("idx_consult_messages_trace_id", "trace_id"),
    )
