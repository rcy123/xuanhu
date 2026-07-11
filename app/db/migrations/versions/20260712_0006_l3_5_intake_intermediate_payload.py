"""Add L3-5 intake intermediate payload.

Revision ID: 20260712_0006
Revises: 20260711_0005
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260712_0006"
down_revision = "20260711_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "intake_command_claims",
        sa.Column("intermediate_payload", postgresql.JSONB, nullable=True),
    )
    op.create_check_constraint(
        "chk_intake_command_claims_intermediate_object",
        "intake_command_claims",
        "intermediate_payload IS NULL OR jsonb_typeof(intermediate_payload) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_intake_command_claims_intermediate_object",
        "intake_command_claims",
        type_="check",
    )
    op.drop_column("intake_command_claims", "intermediate_payload")
