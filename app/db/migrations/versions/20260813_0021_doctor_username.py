"""Add human-readable login usernames to doctor accounts.

Revision ID: 20260813_0021
Revises: 20260813_0020
Create Date: 2026-08-13

The account ``id`` remains the stable internal UUID for foreign keys, but it
is no longer the login credential.  A unique, human-readable ``username``
(拼音/工号) becomes the login identifier.  Existing rows are backfilled with
``id::text`` so they keep working unchanged until an operator renames them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260813_0021"
down_revision = "20260813_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable first so a populated table upgrades safely, then backfill and
    # tighten to NOT NULL + UNIQUE.
    op.add_column("doctors", sa.Column("username", sa.String(length=64), nullable=True))
    # Existing accounts keep logging in with their current UUID (now stored as
    # their username).  Operators may assign a readable name afterwards via
    # scripts/create_doctor.py --username.
    op.execute("UPDATE doctors SET username = id::text WHERE username IS NULL")
    op.alter_column("doctors", "username", existing_type=sa.String(length=64), nullable=False)
    op.create_unique_constraint("uq_doctors_username", "doctors", ["username"])


def downgrade() -> None:
    op.drop_constraint("uq_doctors_username", "doctors", type_="unique")
    op.drop_column("doctors", "username")
