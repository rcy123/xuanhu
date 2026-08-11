"""SQLAlchemy DeclarativeBase — 所有 ORM 模型的共同基类。

提供统一 metadata 和通用工具 mixin。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import DateTime, FetchedValue, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# 命名约定（用于 Alembic 自动生成迁移时的约束/索引命名）
# ---------------------------------------------------------------------------

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """所有 ORM 模型的 declarative 基类。"""

    metadata = metadata_obj

    type_annotation_map: dict[Any, Any] = {}


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class UUIDPrimaryKeyMixin:
    """为模型提供 UUID 主键。

    server_default 使用 PostgreSQL gen_random_uuid()，
    同时也允许应用侧传入 UUID。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """为模型提供 created_at / updated_at 时间戳。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        server_onupdate=cast(FetchedValue, func.now()),
    )
