"""Supported API launcher with a psycopg-compatible Uvicorn loop factory."""

from __future__ import annotations

from typing import Literal, cast

import uvicorn

from app.core.config import get_settings

UVICORN_LOOP_FACTORY = "app.uvicorn_loop:selector_event_loop_factory"


def main() -> None:
    """Run the API without importing the ASGI app before loop selection."""

    settings = get_settings()
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
