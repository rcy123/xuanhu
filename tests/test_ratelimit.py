"""阶段 4 运行态安全测试：Redis 滑动窗口限流（T4.1 / T4.2 / H6）。

- ``RateLimiter.allow`` 滑动窗口：窗口内计数、超限拒绝、窗口过期恢复。
- 路由注入：写接口超限 → 429 RATE_LIMITED + ``Retry-After``。
- 总开关关闭时（默认测试态）不触发限流（既有套件回归保护）。
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.ratelimit import RateLimiter, require_write_rate_limit
from app.core.redis import get_redis, reset_redis
from app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def _token(doctor_id: str) -> str:
    from app.core.auth import create_access_token

    token, _ = create_access_token(doctor_id)
    return token


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _ratelimit_on() -> AsyncIterator[None]:
    """开启鉴权 + 限流（写接口 1 次/分钟），结束还原为默认关闭态。"""
    from app.core.config import get_settings

    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["XUANHU_RATELIMIT_ENABLED"] = "true"
    os.environ["WRITE_RATE_LIMIT_PER_MINUTE"] = "1"
    get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ["XUANHU_AUTH_ENABLED"] = "off"
        os.environ["XUANHU_RATELIMIT_ENABLED"] = "false"
        os.environ.pop("WRITE_RATE_LIMIT_PER_MINUTE", None)
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_redis() -> AsyncIterator[None]:
    await reset_redis()
    redis = await get_redis()
    try:
        await redis.flushdb()
    finally:
        yield
    with contextlib.suppress(Exception):
        await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _redis() -> object:
    return await get_redis()


# ---------------------------------------------------------------------------
# RateLimiter 滑动窗口
# ---------------------------------------------------------------------------


async def test_sliding_window_allows_until_limit() -> None:
    redis = await _redis()
    await redis.flushdb()
    limiter = RateLimiter(redis, key_prefix="ut", max_calls=2, window_seconds=60)

    allowed, remaining = await limiter.allow("doctor-a")
    assert allowed is True
    assert remaining == 1

    allowed, remaining = await limiter.allow("doctor-a")
    assert allowed is True
    assert remaining == 0

    allowed, _ = await limiter.allow("doctor-a")
    assert allowed is False

    # 不同身份互不影响
    allowed, _ = await limiter.allow("doctor-b")
    assert allowed is True


async def test_sliding_window_expires() -> None:
    redis = await _redis()
    await redis.flushdb()
    limiter = RateLimiter(redis, key_prefix="ut-exp", max_calls=1, window_seconds=1)

    assert (await limiter.allow("doctor-c"))[0] is True
    assert (await limiter.allow("doctor-c"))[0] is False
    time.sleep(1.1)
    # 窗口过期后旧计数被剔除，重新放行
    assert (await limiter.allow("doctor-c"))[0] is True


async def test_over_limit_removes_its_own_count() -> None:
    redis = await _redis()
    await redis.flushdb()
    limiter = RateLimiter(redis, key_prefix="ut-self", max_calls=1, window_seconds=60)

    assert (await limiter.allow("doctor-d"))[0] is True
    assert (await limiter.allow("doctor-d"))[0] is False
    count = await redis.zcard("ratelimit:ut-self:doctor-d")
    # 超限请求的计数已从窗口移除，不占用配额
    assert count == 1


# ---------------------------------------------------------------------------
# 路由注入（HTTP 层）
# ---------------------------------------------------------------------------


async def test_write_route_rate_limited_429(client: AsyncClient) -> None:
    headers = {"Authorization": f"Bearer {_token('rl-doctor-1')}"}
    session_id = str(uuid.uuid4())
    payload = {"reason": "测试限流"}

    # 第一次：通过限流（1 次/分钟），会话不存在 → 404（限流先于所有权检查）
    first = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 404

    # 第二次：限流命中 → 429 + Retry-After
    second = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json=payload,
        headers=headers,
    )
    assert second.status_code == 429
    body = second.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["trace_id"]
    assert second.headers["Retry-After"] == "60"


async def test_rate_limit_disabled_does_not_block(client: AsyncClient) -> None:
    """总开关关闭时（默认测试态）重复请求不触发 429（回归保护）。"""
    from app.core.config import get_settings

    os.environ["XUANHU_RATELIMIT_ENABLED"] = "false"
    get_settings.cache_clear()
    try:
        headers = {"Authorization": f"Bearer {_token('rl-doctor-2')}"}
        session_id = str(uuid.uuid4())
        payload = {"reason": "测试限流"}
        for _ in range(3):
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/terminate",
                json=payload,
                headers=headers,
            )
            assert response.status_code == 404  # 全部放行到所有权检查
    finally:
        os.environ["XUANHU_RATELIMIT_ENABLED"] = "true"
        get_settings.cache_clear()


async def test_require_write_rate_limit_noop_without_identity() -> None:
    """无可信身份（off 回退态）时依赖不生效，不触碰 Redis。"""
    from types import SimpleNamespace

    await require_write_rate_limit(request=SimpleNamespace(), doctor=SimpleNamespace(doctor_id=None))


async def test_stream_concurrency_limit_blocks_second_connection() -> None:
    """SSE 并发上限：同一医师第二路连接被拒（429），释放后可重连。"""
    from types import SimpleNamespace

    from app.core.config import get_settings
    from app.core.ratelimit import stream_concurrency_limit

    os.environ["STREAM_CONCURRENT_LIMIT"] = "1"
    get_settings.cache_clear()
    try:
        redis = await _redis()
        await redis.flushdb()
        doctor = SimpleNamespace(doctor_id="rl-stream-doctor")
        request = SimpleNamespace()

        first = stream_concurrency_limit(request, doctor=doctor)
        await first.__anext__()
        try:
            second = stream_concurrency_limit(request, doctor=doctor)
            with pytest.raises(Exception) as exc_info:
                await second.__anext__()
            from app.core.exceptions import RateLimitedError

            assert isinstance(exc_info.value, RateLimitedError)
            assert exc_info.value.code == "RATE_LIMITED"
            await second.aclose()
        finally:
            await first.aclose()
            # 连接释放后计数归零，可再次建连
            third = stream_concurrency_limit(request, doctor=doctor)
            await third.__anext__()
            await third.aclose()
    finally:
        os.environ.pop("STREAM_CONCURRENT_LIMIT", None)
        get_settings.cache_clear()
