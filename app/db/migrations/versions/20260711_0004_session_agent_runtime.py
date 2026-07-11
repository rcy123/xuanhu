"""Persist session runtime identity.

Revision ID: 20260711_0004
Revises: 20260711_0003
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260711_0004"
down_revision = "20260711_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consult_sessions",
        sa.Column("agent_runtime", sa.String(length=16), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "chk_consult_sessions_agent_runtime",
        "consult_sessions",
        "agent_runtime IN ('legacy','langgraph')",
    )
    op.create_index("idx_consult_sessions_agent_runtime", "consult_sessions", ["agent_runtime"])
    op.alter_column("consult_sessions", "agent_runtime", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_consult_sessions_agent_runtime", table_name="consult_sessions")
    op.drop_constraint(
        "chk_consult_sessions_agent_runtime",
        "consult_sessions",
        type_="check",
    )
    op.drop_column("consult_sessions", "agent_runtime")
