"""Add a durable terminal state for failed outbox publications.

Revision ID: 20260714_0008
Revises: 20260712_0007
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260714_0008"
down_revision = "20260712_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("chk_outbox_events_status", "outbox_events", type_="check")
    op.create_check_constraint(
        "chk_outbox_events_status",
        "outbox_events",
        "status IN ('pending','leased','published','dead_letter')",
    )
    op.create_check_constraint(
        "chk_outbox_events_dead_letter_relation",
        "outbox_events",
        "(status = 'dead_letter' AND dead_lettered_at IS NOT NULL) OR "
        "(status <> 'dead_letter' AND dead_lettered_at IS NULL)",
    )
    op.create_index(
        "idx_outbox_events_dead_lettered",
        "outbox_events",
        ["status", "dead_lettered_at"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE outbox_events SET status = 'pending', dead_lettered_at = NULL, "
        "last_error_code = 'PUBLISH_UNKNOWN' WHERE status = 'dead_letter'"
    )
    op.drop_index("idx_outbox_events_dead_lettered", table_name="outbox_events")
    op.drop_constraint(
        "chk_outbox_events_dead_letter_relation",
        "outbox_events",
        type_="check",
    )
    op.drop_constraint("chk_outbox_events_status", "outbox_events", type_="check")
    op.create_check_constraint(
        "chk_outbox_events_status",
        "outbox_events",
        "status IN ('pending','leased','published')",
    )
    op.drop_column("outbox_events", "dead_lettered_at")
