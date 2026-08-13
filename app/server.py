"""Supported API launcher with a psycopg-compatible Uvicorn loop factory."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import uvicorn

from app.core.config import get_settings

logger = logging.getLogger("xuanhu.server")

UVICORN_LOOP_FACTORY = "app.uvicorn_loop:selector_event_loop_factory"


def _pool_capacity_total(settings: Any) -> int:
    """阶段2配套：所有 worker 的连接池总容量。

    每个进程独立持有 SQLAlchemy 连接池（pool_size + max_overflow），多 worker
    部署时总连接数 = api_workers × (pool_size + max_overflow)。
    """
    return settings.api_workers * (settings.db_pool_size + settings.db_pool_max_overflow)


def _pool_capacity_warning(settings: Any, *, max_connections: int) -> str | None:
    """连接池总容量超过 PG max_connections 时返回告警消息，否则 None。

    纯函数，便于单测；实际查询 max_connections 的 I/O 在调用方完成。
    """
    total = _pool_capacity_total(settings)
    if total <= max_connections:
        return None
    return (
        f"连接池容量告警：API_WORKERS={settings.api_workers} × "
        f"(DB_POOL_SIZE={settings.db_pool_size} + DB_POOL_MAX_OVERFLOW="
        f"{settings.db_pool_max_overflow}) = {total} 连接，超过 PostgreSQL "
        f"max_connections={max_connections}。请降低 DB_POOL_SIZE/DB_POOL_MAX_OVERFLOW、"
        "减少 API_WORKERS，或调高 PostgreSQL max_connections。"
    )


def _warn_pool_capacity(settings: Any) -> None:
    """启动前检查连接池容量（best-effort，DB 不可达时静默跳过）。

    多 worker 部署最易踩的坑：每进程 pool 都是满配时总连接数会超过 PG
    max_connections，导致启动或高峰时 `too many connections`。这里只告警
    不阻断——它属于容量提示而非配置错误（PG 会在运行时拒绝超限连接）。
    """
    try:
        import psycopg

        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            max_connections = int(conn.execute("SHOW max_connections").fetchone()[0])  # type: ignore[index]
    except Exception as exc:  # pragma: no cover - 依赖 DB 可达性
        logger.warning("无法查询 PG max_connections，跳过连接池容量检查: %s", type(exc).__name__)
        return
    message = _pool_capacity_warning(settings, max_connections=max_connections)
    if message is not None:
        logger.warning(message)


def main() -> None:
    """Run the API without importing the ASGI app before loop selection."""

    settings = get_settings()
    _warn_pool_capacity(settings)
    kwargs: dict[str, object] = {
        "host": settings.api_host,
        "port": settings.api_port,
        "loop": cast(Literal["none", "auto", "asyncio", "uvloop"], UVICORN_LOOP_FACTORY),
    }
    # 阶段2 横向扩展：API_WORKERS>1 时用 uvicorn 多进程 worker。命令 worker/
    # outbox publisher 靠 FOR UPDATE SKIP LOCKED 安全 claim，会话锁靠 PG
    # advisory lock（多进程唯一权威），因此每个进程可安全并行消费。
    if settings.api_workers > 1:
        kwargs["workers"] = settings.api_workers
    uvicorn.run("app.main:app", **kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    main()
