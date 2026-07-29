"""Allow provenance-bound deterministic intake reply candidates.

Revision ID: 20260729_0015
Revises: 20260728_0014
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "20260729_0015"
down_revision = "20260728_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "chk_safety_fact_assertions_source_kind",
        "safety_fact_assertions",
        type_="check",
    )
    op.create_check_constraint(
        "chk_safety_fact_assertions_source_kind",
        "safety_fact_assertions",
        (
            "source_kind IN ("
            "'model_extraction','deterministic_precheck',"
            "'deterministic_reply_binding','structured_form'"
            ")"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_safety_fact_assertions_source_kind",
        "safety_fact_assertions",
        type_="check",
    )
    op.create_check_constraint(
        "chk_safety_fact_assertions_source_kind",
        "safety_fact_assertions",
        "source_kind IN ('model_extraction','deterministic_precheck','structured_form')",
    )
