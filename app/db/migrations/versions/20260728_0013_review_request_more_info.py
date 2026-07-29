"""Allow the additive request_more_info review action.

Revision ID: 20260728_0013
Revises: 20260715_0012
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "20260728_0013"
down_revision = "20260715_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``0001`` created this exact name with raw SQL.  ``op.f`` marks it as an
    # already-finalized identifier so Alembic's naming convention does not
    # rewrite it to ``ck_doctor_reviews_chk_doctor_reviews_action``.
    op.drop_constraint(op.f("chk_doctor_reviews_action"), "doctor_reviews", type_="check")
    op.create_check_constraint(
        op.f("chk_doctor_reviews_action"),
        "doctor_reviews",
        "action IN ('confirm','modify','reject','request_more_info')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("chk_doctor_reviews_action"), "doctor_reviews", type_="check")
    # Older application versions cannot represent request_more_info.  Map it
    # to their conservative non-advancing outcome before restoring the old
    # constraint; the append-only Domain/audit records remain the authority for
    # the original decision if the deployment is upgraded again.
    op.execute("UPDATE doctor_reviews SET action = 'reject' WHERE action = 'request_more_info'")
    op.create_check_constraint(
        op.f("chk_doctor_reviews_action"),
        "doctor_reviews",
        "action IN ('confirm','modify','reject')",
    )
