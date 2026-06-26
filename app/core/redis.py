"""Redis 客户端工厂。

提供异步 Redis 连接，用于会话锁、checkpoint 和事件流。
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger("xuanhu.redis")

_redis: Redis | None = None


async def get_redis() -> Redis:
    """获取（延迟创建的）异步 Redis 客户端。

    连接失败时抛异常，调用方需自行降级处理。
    缓存连接断开时自动重新创建。
    """
    global _redis  # noqa: PLW0603
    if _redis is not None:
        try:
            await _redis.ping()
            return _redis
        except Exception:
            logger.warning("Redis 缓存连接已断开，重新创建")
            _redis = None

    settings = get_settings()
    _redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_keepalive=True,
        health_check_interval=30,
    )
    await _redis.ping()
    logger.info("Redis 连接成功")
    return _redis


async def close_redis() -> None:
    """关闭 Redis 连接。"""
    global _redis  # noqa: PLW0603
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def reset_redis() -> None:
    """关闭并清空缓存的 Redis 连接（用于测试重建）。"""
    await close_redis()
