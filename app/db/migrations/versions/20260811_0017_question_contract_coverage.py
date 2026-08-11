"""Add immutable R9 question contracts and coverage ledger.

Revision ID: 20260811_0017
Revises: 20260729_0016
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260811_0017"
down_revision = "20260729_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_contract_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("dimension", sa.String(length=64), nullable=False),
        sa.Column("selection_kind", sa.String(length=16), nullable=False),
        sa.Column("safety_critical", sa.Boolean(), nullable=False),
        sa.Column("max_followups", sa.Integer(), nullable=False),
        sa.Column("question_digest", sa.String(length=64), nullable=False),
        sa.Column("aspects", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'question-contract.v1'",
            name="chk_question_contracts_schema_version",
        ),
        sa.CheckConstraint(
            "dimension ~ '^[a-z][a-z0-9_.-]{0,63}$'",
            name="chk_question_contracts_dimension",
        ),
        sa.CheckConstraint(
            "selection_kind IN ('required','conflict')",
            name="chk_question_contracts_selection_kind",
        ),
        sa.CheckConstraint(
            "max_followups BETWEEN 1 AND 4",
            name="chk_question_contracts_max_followups",
        ),
        sa.CheckConstraint(
            "revision BETWEEN 1 AND max_followups + 1",
            name="chk_question_contracts_revision_cap",
        ),
        sa.CheckConstraint(
            "((revision = 1 AND root_contract_id = id AND parent_contract_id IS NULL) OR "
            "(revision > 1 AND root_contract_id <> id AND parent_contract_id IS NOT NULL))",
            name="chk_question_contracts_root_relation",
        ),
        sa.CheckConstraint(
            "question_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_contracts_question_digest",
        ),
        sa.CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_contracts_contract_digest",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(aspects) = 'array' AND jsonb_array_length(aspects) BETWEEN 1 AND 4",
            name="chk_question_contracts_aspects",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["consult_sessions.id"],
            ondelete="CASCADE",
            name="fk_question_contracts_session_id",
        ),
        sa.ForeignKeyConstraint(
            ["question_message_id"],
            ["consult_messages.id"],
            ondelete="RESTRICT",
            name="fk_question_contracts_question_message_id",
        ),
        sa.ForeignKeyConstraint(
            ["root_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_contracts_root_session",
        ),
        sa.ForeignKeyConstraint(
            ["parent_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_contracts_parent_session",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_contracts"),
        sa.UniqueConstraint("id", "session_id", name="uq_question_contracts_id_session"),
        sa.UniqueConstraint(
            "session_id",
            "question_message_id",
            name="uq_question_contracts_question_message",
        ),
        sa.UniqueConstraint(
            "root_contract_id",
            "revision",
            name="uq_question_contracts_root_revision",
        ),
    )
    op.create_index(
        "idx_question_contracts_session_created",
        "question_contracts",
        ["session_id", "created_at"],
    )
    op.create_index(
        "idx_question_contracts_root_revision",
        "question_contracts",
        ["root_contract_id", "revision"],
    )

    op.create_table(
        "question_coverage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("answer_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("items", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'question-coverage-event.v1'",
            name="chk_question_coverage_events_schema_version",
        ),
        sa.CheckConstraint(
            "event_digest ~ '^[0-9a-f]{64}$'",
            name="chk_question_coverage_events_event_digest",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array' AND jsonb_array_length(items) BETWEEN 1 AND 4",
            name="chk_question_coverage_events_items",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["consult_sessions.id"],
            ondelete="CASCADE",
            name="fk_question_coverage_events_session_id",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_coverage_events_contract_session",
        ),
        sa.ForeignKeyConstraint(
            ["root_contract_id", "session_id"],
            ["question_contracts.id", "question_contracts.session_id"],
            ondelete="CASCADE",
            name="fk_question_coverage_events_root_session",
        ),
        sa.ForeignKeyConstraint(
            ["answer_message_id"],
            ["consult_messages.id"],
            ondelete="RESTRICT",
            name="fk_question_coverage_events_answer_message_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_coverage_events"),
        sa.UniqueConstraint("contract_id", name="uq_question_coverage_events_contract"),
        sa.UniqueConstraint(
            "answer_message_id",
            name="uq_question_coverage_events_answer_message",
        ),
    )
    op.create_index(
        "idx_question_coverage_events_session_created",
        "question_coverage_events",
        ["session_id", "created_at"],
    )
    op.create_index(
        "idx_question_coverage_events_root_created",
        "question_coverage_events",
        ["root_contract_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_question_coverage_events_root_created", table_name="question_coverage_events")
    op.drop_index("idx_question_coverage_events_session_created", table_name="question_coverage_events")
    op.drop_table("question_coverage_events")
    op.drop_index("idx_question_contracts_root_revision", table_name="question_contracts")
    op.drop_index("idx_question_contracts_session_created", table_name="question_contracts")
    op.drop_table("question_contracts")
