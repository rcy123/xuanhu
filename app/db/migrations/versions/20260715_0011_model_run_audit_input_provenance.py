"""Add model-run policy and input-digest provenance.

Revision ID: 20260715_0011
Revises: 20260715_0010
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0011"
down_revision = "20260715_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows predate this provenance contract.  Backfill an explicit
    # unavailable policy marker and a per-run irreversible sentinel; never try
    # to reconstruct or persist historical clinical input.
    op.add_column("model_run_audits", sa.Column("policy_version", sa.String(100), nullable=True))
    op.add_column("model_run_audits", sa.Column("input_digest", sa.String(64), nullable=True))
    op.execute(
        """
        UPDATE model_run_audits
        SET policy_version = 'pre-input-provenance-unavailable.v1',
            input_digest = encode(
                sha256(convert_to('xuanhu:model-input:unavailable:v1:' || run_id::text, 'UTF8')),
                'hex'
            )
        """
    )
    op.alter_column("model_run_audits", "policy_version", existing_type=sa.String(100), nullable=False)
    op.alter_column("model_run_audits", "input_digest", existing_type=sa.String(64), nullable=False)
    op.create_check_constraint(
        "chk_model_run_audits_input_digest",
        "model_run_audits",
        "input_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("chk_model_run_audits_input_digest", "model_run_audits", type_="check")
    op.drop_column("model_run_audits", "input_digest")
    op.drop_column("model_run_audits", "policy_version")
