"""Add doctor_id ownership to consult_sessions (stage-2 PHI access control).

Revision ID: 20260813_0019
Revises: 20260813_0018
Create Date: 2026-08-13

回填策略（文档 02 §2.1）：存量会话优先取 ``state_snapshot.doctor_id``，
其次按 ``created_by`` 文本匹配已存在的 doctors；两者都匹配不到则置 NULL
（灰度过渡期语义：无主会话允许任一登录医师访问，见 app/core/access.py）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260813_0019"
down_revision = "20260813_0018"
branch_labels = None
depends_on = None

_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def upgrade() -> None:
    # 1) 先加可空列（回填后再收紧），避免存量行违反 NOT NULL。
    op.add_column(
        "consult_sessions",
        sa.Column("doctor_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 2) 回填：state_snapshot.doctor_id（合法 UUID）→ created_by 匹配 doctors → NULL
    op.execute(
        sa.text(
            f"""
            UPDATE consult_sessions SET doctor_id = CASE
                WHEN state_snapshot IS NOT NULL
                     AND state_snapshot->>'doctor_id' ~ '{_UUID_RE}'
                THEN (state_snapshot->>'doctor_id')::uuid
                WHEN created_by ~ '{_UUID_RE}'
                     AND EXISTS (SELECT 1 FROM doctors d WHERE d.id::text = consult_sessions.created_by)
                THEN created_by::uuid
                ELSE NULL
            END
            """
        )
    )

    # 3) 外键 + 索引
    op.create_foreign_key(
        "fk_consult_sessions_doctor_id",
        "consult_sessions",
        "doctors",
        ["doctor_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "idx_consult_sessions_doctor_id",
        "consult_sessions",
        ["doctor_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_consult_sessions_doctor_id", table_name="consult_sessions")
    op.drop_constraint("fk_consult_sessions_doctor_id", "consult_sessions", type_="foreignkey")
    op.drop_column("consult_sessions", "doctor_id")
