"""Make rollout-phase deployment IDs unique in the global audit chain.

Revision ID: 20260728_0014
Revises: 20260728_0013
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_0014"
down_revision = "20260728_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_audit_events_rollout_phase_deployment",
        "audit_events",
        ["event_type", "trace_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'runtime.rollout_phase_changed' AND trace_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_audit_events_rollout_phase_deployment",
        table_name="audit_events",
    )
