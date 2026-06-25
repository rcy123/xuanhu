"""数据库会话工厂。

提供异步 SQLAlchemy 引擎和会话，复用 P1-2 Settings 中的 database_url。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def _build_async_pg_url(database_url: str) -> str:
    """将同步风格的 PostgreSQL URL 转为 asyncpg 可用的异步 URL。

    例：
        postgresql://user:pass@host/db -> postgresql+asyncpg://user:pass@host/db
        postgresql+asyncpg://...       -> 原样返回
    """
    prefix = "postgresql+asyncpg://"
    if database_url.startswith(prefix):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", prefix, 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", prefix, 1)
    # 未知 scheme — 不做转换，交由 SQLAlchemy 报错
    return database_url


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取（延迟创建的）异步 SQLAlchemy 引擎。

    使用数据库设计文档建议的连接池参数：
    - pool_size=10
    - max_overflow=20
    - pool_timeout=30s
    - pool_recycle=3600s
    - pool_pre_ping=True
    """
    global _engine  # noqa: PLW0603
    if _engine is None:
        settings = get_settings()
        async_url = _build_async_pg_url(settings.database_url)
        _engine = create_async_engine(
            async_url,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=3600,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取（延迟创建的）异步会话工厂。"""
    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory
