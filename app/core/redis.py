"""Redis 客户端工厂。

提供异步 Redis 连接，用于会话锁、checkpoint 和事件流。
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger("xuanhu.redis")

_redis: Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


async def get_redis() -> Redis:
    """获取（延迟创建的）异步 Redis 客户端。

    连接失败时抛异常，调用方需自行降级处理。
    缓存连接断开时自动重新创建。
    """
    global _redis, _redis_loop  # noqa: PLW0603
    current_loop = asyncio.get_running_loop()
    if _redis is not None and _redis_loop is not current_loop:
        # redis.asyncio connections belong to the event loop that created
        # them. Test runners and embedded hosts may replace that loop.
        logger.debug("Discarding Redis client owned by a different event loop")
        _redis = None
        _redis_loop = None

    if _redis is not None:
        try:
            await _redis.ping()
            return _redis
        except Exception:
            logger.warning("Redis 缓存连接已断开，重新创建")
            await _redis.aclose()
            _redis = None
            _redis_loop = None

    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_keepalive=True,
        health_check_interval=30,
    )
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        raise
    _redis = client
    _redis_loop = current_loop
    logger.info("Redis 连接成功")
    return _redis


async def close_redis() -> None:
    """关闭 Redis 连接。"""
    global _redis, _redis_loop  # noqa: PLW0603
    client = _redis
    owner_loop = _redis_loop
    _redis = None
    _redis_loop = None
    if client is None:
        return

    current_loop = asyncio.get_running_loop()
    if owner_loop is None or owner_loop is current_loop:
        await client.aclose()
    else:
        # Awaiting aclose() from another loop raises "Event loop is closed".
        # The owning loop is responsible for its transports; dropping this
        # process-local cache lets the new loop create a valid client.
        logger.debug("Discarded Redis client after its owning event loop changed")


async def reset_redis() -> None:
    """关闭并清空缓存的 Redis 连接（用于测试重建）。"""
    await close_redis()
