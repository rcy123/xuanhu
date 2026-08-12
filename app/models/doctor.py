"""医师账户表 doctors — 阶段 1 认证授权（T1.1）。

存储用于 ``POST /api/v1/auth/login`` 的医师登录凭据。密码一律以
argon2id 哈希存储，禁止明文；``enabled`` 为账号启停开关。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Doctor(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """医师账户表 — doctors。"""

    __tablename__ = "doctors"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "enabled IN (true, false)",
            name="chk_doctors_enabled",
        ),
    )
