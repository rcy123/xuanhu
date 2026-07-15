"""Make runtime-switch deployment IDs unique within the global audit chain.

Revision ID: 20260715_0012
Revises: 20260715_0011
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0012"
down_revision = "20260715_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_audit_events_runtime_switch_deployment",
        "audit_events",
        ["event_type", "trace_id"],
        unique=True,
        postgresql_where=sa.text(
            "event_type = 'runtime.switched' AND trace_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_audit_events_runtime_switch_deployment",
        table_name="audit_events",
    )
