"""Alembic 迁移环境。

从 app.core.config.get_settings() 读取 database_url，
加载 app.models 中所有 ORM 模型的 metadata，
支持 upgrade / downgrade / 自动生成迁移。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# -- 导入 app.models 注册所有 ORM 模型到 Base.metadata --
# 必须在 settings 实例化之前完成，但实际只要在 context.configure 之前即可
import app.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base  # noqa: E402

# -- Alembic Config --
config = context.config

# Normal CLI use keeps the sentinel from ``alembic.ini`` and resolves the URL
# through application settings.  Guarded migration tests may instead inject an
# explicit worker-database URL into their ``Config`` object; preserving that
# value makes the migration target independent of process-global Settings cache
# state during module teardown.
configured_url = config.get_main_option("sqlalchemy.url")
if not configured_url or configured_url == "override_in_env_py":
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

# 日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 文件而不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接 PostgreSQL 执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
