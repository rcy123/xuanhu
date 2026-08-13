"""医师账户表 doctors — 阶段 1 认证授权（T1.1）。

存储用于 ``POST /api/v1/auth/login`` 的医师登录凭据。密码一律以
argon2id 哈希存储，禁止明文；``enabled`` 为账号启停开关。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Doctor(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """医师账户表 — doctors。"""

    __tablename__ = "doctors"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 登录名：唯一、好记的 ASCII 标识（如拼音/工号），用于登录与展示；
    # UUID 主键仅作内部引用，不要求人类记忆。
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    # ``role`` is deliberately a single, persisted account role.  The JWT
    # mirrors it for request-time routing, then every authenticated request
    # binds that claim back to this record before it is trusted.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="doctor", server_default="doctor")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Increment whenever an operator invalidates credentials or disables an
    # account.  JWTs carry the value issued at login, so stale tokens are
    # rejected without keeping a server-side token allowlist.
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "enabled IN (true, false)",
            name="chk_doctors_enabled",
        ),
        CheckConstraint(
            "role IN ('doctor','admin')",
            name="chk_doctors_role",
        ),
        CheckConstraint(
            "auth_version >= 1",
            name="chk_doctors_auth_version_positive",
        ),
    )
