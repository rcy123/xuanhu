"""审计事件表 audit_events。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.consult import ConsultSession


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    """审计事件表 — audit_events。"""

    __tablename__ = "audit_events"

    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
    )

    # -- relationships --
    session: Mapped[ConsultSession | None] = relationship("ConsultSession", back_populates="audit_events")

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('doctor','agent','system')",
            name="chk_audit_events_actor_type",
        ),
        Index("idx_audit_events_session_created", "session_id", created_at.desc()),
        Index("idx_audit_events_type_created", "event_type", created_at.desc()),
        Index("idx_audit_events_trace_id", "trace_id"),
        Index(
            "uq_audit_events_runtime_switch_deployment",
            "event_type",
            "trace_id",
            unique=True,
            postgresql_where=text(
                "event_type = 'runtime.switched' AND trace_id IS NOT NULL"
            ),
        ),
    )
