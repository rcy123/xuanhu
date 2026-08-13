"""Add account roles and token revocation versions for administration.

Revision ID: 20260813_0020
Revises: 20260813_0019
Create Date: 2026-08-13

Existing doctor accounts remain clinical accounts after the upgrade.  Their
``auth_version`` starts at 1; newly issued JWTs must match this value and are
therefore revocable by incrementing it on the account row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260813_0020"
down_revision = "20260813_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable first so deployments with populated ``doctors`` tables can
    # be upgraded safely, then backfill and make the invariants explicit.
    op.add_column("doctors", sa.Column("role", sa.String(length=16), nullable=True))
    op.add_column("doctors", sa.Column("auth_version", sa.Integer(), nullable=True))
    op.execute("UPDATE doctors SET role = 'doctor' WHERE role IS NULL")
    op.execute("UPDATE doctors SET auth_version = 1 WHERE auth_version IS NULL")
    op.alter_column(
        "doctors",
        "role",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default=sa.text("'doctor'"),
    )
    op.alter_column(
        "doctors",
        "auth_version",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
    )
    op.create_check_constraint(
        "chk_doctors_role",
        "doctors",
        "role IN ('doctor','admin')",
    )
    op.create_check_constraint(
        "chk_doctors_auth_version_positive",
        "doctors",
        "auth_version >= 1",
    )

    # ``audit_events`` predates Alembic naming conventions, so ``op.f`` keeps
    # the raw historical constraint name intact while it is replaced.
    op.drop_constraint(op.f("chk_audit_events_actor_type"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("chk_audit_events_actor_type"),
        "audit_events",
        "actor_type IN ('doctor','admin','agent','system')",
    )


def downgrade() -> None:
    # Preserve audit rows for an older binary by retaining their actor ID and
    # event/payload, while mapping the newly introduced actor type back to an
    # old-schema-compatible value.
    op.execute("UPDATE audit_events SET actor_type = 'system' WHERE actor_type = 'admin'")
    op.drop_constraint(op.f("chk_audit_events_actor_type"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("chk_audit_events_actor_type"),
        "audit_events",
        "actor_type IN ('doctor','agent','system')",
    )

    op.drop_constraint(op.f("ck_doctors_chk_doctors_auth_version_positive"), "doctors", type_="check")
    op.drop_constraint(op.f("ck_doctors_chk_doctors_role"), "doctors", type_="check")
    op.drop_column("doctors", "auth_version")
    op.drop_column("doctors", "role")
