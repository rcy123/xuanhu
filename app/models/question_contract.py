"""Immutable R9 question-contract and coverage-ledger persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QuestionContractRecord(Base):
    """One immutable root question or residual follow-up contract."""

    __tablename__ = "question_contracts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    root_contract_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_contract_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    safety_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_followups: Mapped[int] = mapped_column(Integer, nullable=False)
    question_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    aspects: Mapped[list[dict[str, Any]]] = mapped_column(cast(Any, JSONB)(none_as_null=True), nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "session_id", name="uq_question_contracts_id_session"),
        UniqueConstraint("session_id", "question_message_id", name="uq_question_contracts_question_message"),
        UniqueConstraint("root_contract_id", "revision", name="uq_question_contracts_root_revision"),
        ForeignKeyConstraint(
            ["root_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_contracts_root_session",
        ),
        ForeignKeyConstraint(
            ["parent_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_contracts_parent_session",
        ),
        CheckConstraint(
            "schema_version = 'question-contract.v1'",
            name="chk_question_contracts_schema_version",
        ),
        CheckConstraint(
            "dimension ~ '^[a-z][a-z0-9_.-]{0,63}$'",
            name="chk_question_contracts_dimension",
        ),
        CheckConstraint(
            "selection_kind IN ('required','conflict')",
            name="chk_question_contracts_selection_kind",
        ),
        CheckConstraint(
            "max_followups BETWEEN 1 AND 4",
            name="chk_question_contracts_max_followups",
        ),
        CheckConstraint(
            "revision BETWEEN 1 AND max_followups + 1",
            name="chk_question_contracts_revision_cap",
        ),
        CheckConstraint(
            "((revision = 1 AND root_contract_id = id AND parent_contract_id IS NULL) OR "
            "(revision > 1 AND root_contract_id <> id AND parent_contract_id IS NOT NULL))",
            name="chk_question_contracts_root_relation",
        ),
        CheckConstraint(
            "question_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_contracts_question_digest",
        ),
        CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_contracts_contract_digest",
        ),
        CheckConstraint(
            "jsonb_typeof(aspects) = 'array' AND jsonb_array_length(aspects) BETWEEN 1 AND 4",
            name="chk_question_contracts_aspects",
        ),
        Index("idx_question_contracts_session_created", "session_id", "created_at"),
        Index("idx_question_contracts_root_revision", "root_contract_id", "revision"),
    )

    def __repr__(self) -> str:
        return (
            f"<QuestionContractRecord id={self.id} session_id={self.session_id} "
            f"root_contract_id={self.root_contract_id} revision={self.revision}>"
        )


class QuestionCoverageEventRecord(Base):
    """One append-only, quote-free answer coverage event per contract."""

    __tablename__ = "question_coverage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    root_contract_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    answer_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consult_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    items: Mapped[list[dict[str, Any]]] = mapped_column(cast(Any, JSONB)(none_as_null=True), nullable=False)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_coverage_events_contract_session",
        ),
        ForeignKeyConstraint(
            ["root_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_coverage_events_root_session",
        ),
        UniqueConstraint("contract_id", name="uq_question_coverage_events_contract"),
        UniqueConstraint("answer_message_id", name="uq_question_coverage_events_answer_message"),
        CheckConstraint(
            "schema_version = 'question-coverage-event.v1'",
            name="chk_question_coverage_events_schema_version",
        ),
        CheckConstraint(
            "event_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_coverage_events_event_digest",
        ),
        CheckConstraint(
            "jsonb_typeof(items) = 'array' AND jsonb_array_length(items) BETWEEN 1 AND 4",
            name="chk_question_coverage_events_items",
        ),
        Index("idx_question_coverage_events_session_created", "session_id", "created_at"),
        Index("idx_question_coverage_events_root_created", "root_contract_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<QuestionCoverageEventRecord id={self.id} session_id={self.session_id} "
            f"contract_id={self.contract_id}>"
        )


__all__ = ["QuestionContractRecord", "QuestionCoverageEventRecord"]
