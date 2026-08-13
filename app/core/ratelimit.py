"""速率限制 — 阶段 4 运行态安全加固（T4.1 / H6）。

方案见 ``docs/04_生产环境加固/04-运行态安全加固.md`` §2。

基于 Redis Sorted Set 的滑动窗口：每次请求把当前时间戳写入窗口，
剔除窗口外旧元素后统计窗口内计数，超限返回 429（``RateLimitedError``，
携带 ``Retry-After``）。登录接口按 IP、业务写接口按医师限流。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.auth import DoctorPrincipal, get_current_doctor, get_current_doctor_from_query, require_admin
from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.redis import get_redis

logger = logging.getLogger("xuanhu.ratelimit")

# 限流 key 前缀（登录按 IP 的前缀在 app/api/auth.py：ratelimit:login:）
RATELIMIT_PREFIX = "ratelimit:"


class RateLimiter:
    """Redis 滑动窗口限流器。"""

    def __init__(self, redis: Redis, *, key_prefix: str, max_calls: int, window_seconds: int) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self.max_calls = max_calls
        self.window_seconds = window_seconds

    def _key(self, identity: str) -> str:
        return f"{RATELIMIT_PREFIX}{self._key_prefix}:{identity}"

    async def allow(self, identity: str) -> tuple[bool, int]:
        """滑动窗口判定；返回 (是否放行, 剩余配额)。"""
        key = self._key(identity)
        now_ms = int(time.time() * 1000)
        window_ms = self.window_seconds * 1000
        member = f"{now_ms}:{uuid.uuid4().hex}"

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now_ms - window_ms)
        pipe.zadd(key, {member: now_ms})
        pipe.zcard(key)
        pipe.expire(key, self.window_seconds)
        results = await pipe.execute()
        count = int(results[2]) if results else 1

        allowed = count <= self.max_calls
        if not allowed:
            # 从窗口移除本次计数，避免超限请求持续占用配额
            await self._redis.zrem(key, member)
        # 剩余配额 = 上限 - 本次请求后的窗口计数（含本次）
        remaining = max(self.max_calls - count, 0)
        return allowed, remaining


def _ratelimit_enabled() -> bool:
    return get_settings().xuanhu_ratelimit_enabled


async def _require_within_limit(
    *,
    key_prefix: str,
    max_calls: int,
    window_seconds: int,
    identity: str | None,
) -> None:
    """限流依赖核心；``XUANHU_RATELIMIT_ENABLED=false`` 或不具备可信身份时不生效。"""
    if not _ratelimit_enabled():
        return
    if not identity:
        # off/audit 回退态无可信身份——不按身份限流（限流是纵深防御，非身份边界）
        return
    redis = await get_redis()
    limiter = RateLimiter(redis, key_prefix=key_prefix, max_calls=max_calls, window_seconds=window_seconds)
    allowed, _ = await limiter.allow(identity)
    if not allowed:
        raise RateLimitedError(retry_after=window_seconds)


async def require_advance_rate_limit(
    request: Request,
    doctor: DoctorPrincipal = Depends(get_current_doctor),
) -> None:
    """advance 接口按医师限流（默认 20 次/分钟）。"""
    del request
    await _require_within_limit(
        key_prefix="advance",
        max_calls=get_settings().advance_rate_limit_per_minute,
        window_seconds=60,
        identity=doctor.doctor_id,
    )


async def require_write_rate_limit(
    request: Request,
    doctor: DoctorPrincipal = Depends(get_current_doctor),
) -> None:
    """其余写接口按医师限流（默认 60 次/分钟）。"""
    del request
    await _require_within_limit(
        key_prefix="write",
        max_calls=get_settings().write_rate_limit_per_minute,
        window_seconds=60,
        identity=doctor.doctor_id,
    )


async def require_admin_write_rate_limit(
    request: Request,
    admin: DoctorPrincipal = Depends(require_admin),
) -> None:
    """Apply the standard write budget to administration changes.

    This deliberately depends on ``require_admin`` instead of the clinical
    dependency: administrator tokens must never acquire clinical privileges in
    order to be rate-limited.
    """
    del request
    await _require_within_limit(
        key_prefix="write",
        max_calls=get_settings().write_rate_limit_per_minute,
        window_seconds=60,
        identity=admin.doctor_id,
    )


async def stream_concurrency_limit(
    request: Request,
    doctor: DoctorPrincipal = Depends(get_current_doctor_from_query),
) -> AsyncIterator[None]:
    """SSE 按医师并发连接上限（默认 5 路）；连接结束自动释放。

    以 yield 依赖形态挂载：请求处理结束（含连接断开）时 finally 递减计数。
    """
    del request
    if not _ratelimit_enabled() or not doctor.doctor_id:
        yield
        return
    redis = await get_redis()
    key = f"{RATELIMIT_PREFIX}stream:concurrent:{doctor.doctor_id}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 60)
    if current > get_settings().stream_concurrent_limit:
        await redis.decr(key)
        raise RateLimitedError(retry_after=60)
    try:
        yield
    finally:
        await redis.decr(key)
