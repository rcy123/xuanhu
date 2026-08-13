"""阶段 1 认证测试：登录、失败锁定、token 颁发（T1.7 / 验收清单）。"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.core.redis import get_redis, reset_redis
from app.main import app
from app.models.doctor import Doctor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _auth_on() -> AsyncIterator[None]:
    """本模块所有用例显式启用认证（XUANHU_AUTH_ENABLED=on），
    并把登录 IP 限流阈值调高，避免用例间请求数互相污染。"""
    import os

    from app.core.config import get_settings

    previous_auth = os.environ.get("XUANHU_AUTH_ENABLED")
    previous_limit = os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE")
    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "600"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous_auth is None:
            os.environ.pop("XUANHU_AUTH_ENABLED", None)
        else:
            os.environ["XUANHU_AUTH_ENABLED"] = previous_auth
        if previous_limit is None:
            os.environ.pop("LOGIN_RATE_LIMIT_PER_MINUTE", None)
        else:
            os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = previous_limit
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def doctor(db: AsyncSession) -> Doctor:
    """创建一个启用密码的测试医师。"""
    record = Doctor(
        username="auth-login-doctor",
        name="认证测试医师",
        password_hash=hash_password("correct-password"),
        enabled=True,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    yield record
    await db.execute(delete(Doctor).where(Doctor.id == record.id))
    await db.commit()


async def test_login_success_issues_token(client: AsyncClient, doctor: Doctor) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": doctor.username, "password": "correct-password"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 28_800
    token: str = data["access_token"]
    assert len(token) > 50

    # 签发 token 立即可用于受保护路由
    from app.core.auth import _decode_token_payload
    from app.core.config import get_settings

    payload = _decode_token_payload(token, get_settings())
    assert payload["sub"] == str(doctor.id)
    assert payload["role"] == "doctor"
    assert payload["roles"] == ["doctor"]
    assert payload["auth_version"] == 1
    assert data["user"] == {
        "id": str(doctor.id),
        "username": doctor.username,
        "name": doctor.name,
        "role": "doctor",
    }


async def test_login_wrong_password_unauthenticated(client: AsyncClient, doctor: Doctor) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": doctor.username, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "UNAUTHENTICATED"
    # 不区分"用户不存在"与"密码错误"
    assert "wrong" not in body["message"]
    assert body["detail"] is None


async def test_login_unknown_doctor_same_error(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "no-such-user", "password": "whatever"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


async def test_login_invalid_uuid_same_error(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "!!invalid!!", "password": "whatever"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


async def test_login_disabled_account_403(client: AsyncClient, db: AsyncSession) -> None:
    disabled = Doctor(username="auth-login-disabled", name="停用医师", password_hash=hash_password("p"), enabled=False)
    db.add(disabled)
    await db.commit()
    try:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": disabled.username, "password": "p"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "ACCOUNT_DISABLED"
    finally:
        await db.execute(delete(Doctor).where(Doctor.id == disabled.id))
        await db.commit()


async def test_five_failures_lock_account(client: AsyncClient, db: AsyncSession, doctor: Doctor) -> None:
    """连续失败 5 次后账号锁定：第 6 次返回 ACCOUNT_LOCKED。"""
    redis = await get_redis()
    fail_key = f"auth:fail:{doctor.username}"
    await redis.delete(fail_key)
    try:
        for _ in range(5):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": doctor.username, "password": "wrong"},
            )
            assert resp.status_code == 401

        locked = await client.post(
            "/api/v1/auth/login",
            json={"username": doctor.username, "password": "correct-password"},
        )
        assert locked.status_code == 401
        assert locked.json()["code"] == "ACCOUNT_LOCKED"
        # 即使密码正确也被锁定
        assert await redis.ttl(fail_key) > 0
    finally:
        await redis.delete(fail_key)


async def test_login_failure_lock_is_sliding_window(client: AsyncClient, doctor: Doctor) -> None:
    """每次失败都刷新锁定窗口（滑动窗口），而非仅首次失败设 TTL。

    文档 01 §2.2「连续失败 5 次锁 15 分钟」要求滑动语义：若第 1 次失败后
    过了 14 分钟又失败，锁定应从这次失败重新起算 15 分钟，而非只剩 1 分钟。
    """
    from app.core.config import get_settings

    settings = get_settings()
    redis = await get_redis()
    fail_key = f"auth:fail:{doctor.username}"
    await redis.delete(fail_key)
    try:
        # 第一次失败：窗口起算
        await client.post(
            "/api/v1/auth/login",
            json={"username": doctor.username, "password": "wrong"},
        )
        first_ttl = await redis.ttl(fail_key)
        assert first_ttl > settings.login_fail_lock_seconds - 5

        # 模拟窗口即将到期：手动把 TTL 压到 3 秒
        await redis.expire(fail_key, 3)

        # 第二次失败：滑动窗口应重新拉满到 ~15 分钟，而非停留在 3 秒
        await client.post(
            "/api/v1/auth/login",
            json={"username": doctor.username, "password": "wrong"},
        )
        second_ttl = await redis.ttl(fail_key)
        assert second_ttl > settings.login_fail_lock_seconds - 5, (
            f"滑动窗口未刷新：预期 ~{settings.login_fail_lock_seconds}s，实际 {second_ttl}s"
        )
    finally:
        await redis.delete(fail_key)


async def test_login_ip_rate_limit(client: AsyncClient, doctor: Doctor) -> None:
    """同 IP 1 分钟内超过 10 次登录 → 429。"""
    import os

    from app.core.config import get_settings

    # 本用例单独把阈值降到 10，验证限流语义后恢复
    previous = os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE")
    os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "10"
    get_settings.cache_clear()
    try:
        # 清空此前用例累积的 IP 限流计数，保证本次从 0 开始
        from app.api.auth import LOGIN_RATE_LIMIT_PREFIX

        redis = await get_redis()
        async for key in redis.scan_iter(f"{LOGIN_RATE_LIMIT_PREFIX}*"):
            await redis.delete(key)

        responses = []
        for _ in range(11):
            responses.append(
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": doctor.username, "password": "wrong"},
                )
            )
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == "RATE_LIMITED"
        assert responses[-2].status_code == 401  # 第 10 次仍正常失败
    finally:
        if previous is None:
            os.environ.pop("LOGIN_RATE_LIMIT_PER_MINUTE", None)
        else:
            os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = previous
        get_settings.cache_clear()
