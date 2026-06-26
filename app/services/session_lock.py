"""会话级分布式锁。

优先级：Redis SET NX（主）→ PostgreSQL advisory lock（降级兜底）。

规则：
- 获取锁失败（两种实现均失败）→ 抛出 SessionBusyError
- 释放锁必须校验 value 等于当前 trace_id（Redis）
- Redis 不可用时自动降级为 PG advisory lock
- 锁释放必须在异常路径中执行（上下文管理器保证）
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import SessionBusyError
from app.core.redis import get_redis

logger = logging.getLogger("xuanhu.session_lock")

# Redis lock key 前缀
_LOCK_KEY_PREFIX = "xuanhu:session_lock:"

# PG advisory lock ID 上限（int64）
_ADVISORY_LOCK_MAX = 2**63 - 1


def _redis_key(session_id: str) -> str:
    """构造 Redis 锁 key。"""
    return f"{_LOCK_KEY_PREFIX}{session_id}"


def _advisory_lock_id(session_id: str) -> int:
    """从 session_id 生成 PG advisory lock ID（bigint）。"""
    digest = hashlib.sha256(session_id.encode()).digest()
    # 取前 8 字节转为有符号 int64
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    # 确保为正数（pg_advisory_lock 需要 bigint 范围）
    return lock_id & _ADVISORY_LOCK_MAX


class SessionLock:
    """会话写操作分布式锁。

    用法（上下文管理器）：

        lock = SessionLock(db, session_id, trace_id)
        async with lock:
            # 写操作

    获取失败时抛出 SessionBusyError (409 SESSION_BUSY)。
    """

    def __init__(self, db: AsyncSession, session_id: str, trace_id: str) -> None:
        self._db = db
        self._session_id = session_id
        self._trace_id = trace_id
        self._lock_type: str | None = None  # "redis" | "pg_advisory"
        self._redis: Redis | None = None

    async def __aenter__(self) -> SessionLock:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    async def acquire(self) -> None:
        """获取会话锁，优先 Redis，降级 PG advisory lock。

        Redis 可用但锁冲突时直接返回 SESSION_BUSY，不降级 PG。
        仅当 Redis 完全不可用时才降级为 PG advisory lock。
        """
        settings = get_settings()
        ttl = settings.session_lock_ttl_seconds

        # 1) 尝试 Redis 锁
        try:
            redis = await get_redis()
        except Exception:
            redis = None

        if redis is not None:
            self._redis = redis
            key = _redis_key(self._session_id)
            acquired = await redis.set(key, self._trace_id, nx=True, ex=ttl)
            if acquired:
                self._lock_type = "redis"
                logger.debug(
                    "Redis 锁获取成功 session=%s trace=%s",
                    self._session_id,
                    self._trace_id,
                )
                return
            logger.debug("Redis 锁冲突 session=%s", self._session_id)
            # Redis 可用但锁已被占用 → 直接报错，不降级 PG
            raise SessionBusyError(
                message="会话正在处理其他请求，请稍后重试",
                detail=f"session_id={self._session_id} Redis 锁冲突",
                retryable=True,
            )

        # 2) Redis 不可用 → 降级为 PG advisory lock
        lock_id = _advisory_lock_id(self._session_id)
        result = await self._db.execute(
            __import__("sqlalchemy").text("SELECT pg_try_advisory_lock(:id)"),
            {"id": lock_id},
        )
        acquired_pg = result.scalar()
        if acquired_pg:
            self._lock_type = "pg_advisory"
            logger.debug(
                "PG advisory lock 获取成功 session=%s lock_id=%s",
                self._session_id,
                lock_id,
            )
            return

        # 两种方式均失败
        raise SessionBusyError(
            message="会话正在处理其他请求，请稍后重试",
            detail=(
                f"session_id={self._session_id} 锁获取失败 "
                f"(redis={'unavailable' if redis is None else 'conflict'}, pg_advisory=conflict)"
            ),
            retryable=True,
        )

    async def release(self) -> None:
        """释放已获取的锁，异常安全。"""
        if self._lock_type == "redis" and self._redis is not None:
            try:
                await self._release_redis()
            except Exception:
                logger.warning(
                    "Redis 锁释放异常 session=%s",
                    self._session_id,
                    exc_info=True,
                )
        elif self._lock_type == "pg_advisory":
            try:
                await self._release_pg()
            except Exception:
                logger.warning(
                    "PG advisory lock 释放异常 session=%s",
                    self._session_id,
                    exc_info=True,
                )

    async def _release_redis(self) -> None:
        """安全释放 Redis 锁（校验 value 等于 trace_id）。"""
        assert self._redis is not None
        key = _redis_key(self._session_id)
        # 使用 Lua 脚本原子性校验 value 并删除
        script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        else
            return 0
        end
        """
        result = await self._redis.eval(script, 1, key, self._trace_id)  # type: ignore[misc]
        if result:
            logger.debug(
                "Redis 锁释放成功 session=%s trace=%s",
                self._session_id,
                self._trace_id,
            )

    async def _release_pg(self) -> None:
        """释放 PG advisory lock。"""
        lock_id = _advisory_lock_id(self._session_id)
        await self._db.execute(
            __import__("sqlalchemy").text("SELECT pg_advisory_unlock(:id)"),
            {"id": lock_id},
        )
        logger.debug(
            "PG advisory lock 释放成功 session=%s lock_id=%s",
            self._session_id,
            lock_id,
        )


@asynccontextmanager
async def session_lock(
    db: AsyncSession,
    session_id: str,
    trace_id: str,
) -> AsyncIterator[None]:
    """会话锁上下文管理器便捷函数。

    用法：

        async with session_lock(db, session_id, trace_id):
            # 写操作
    """
    lock = SessionLock(db, session_id, trace_id)
    await lock.acquire()
    try:
        yield
    finally:
        await lock.release()
