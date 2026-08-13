"""阶段 1 认证测试：未授权 / 伪造 / 过期 token 全路由扫描（T1.7）。"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.core.auth import JWT_ALGORITHM
from app.core.redis import reset_redis
from app.main import app
from app.models.doctor import Doctor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _auth_on() -> AsyncIterator[None]:
    import os

    from app.core.config import get_settings

    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["JWT_SIGNING_KEY"] = "protected-routes-test-key-0123456789"
    get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ["XUANHU_AUTH_ENABLED"] = "off"
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
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
    record = Doctor(username="protected-route-doctor", name="受保护路由测试医师", password_hash=hash_password("p"), enabled=True)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    yield record
    await db.execute(delete(Doctor).where(Doctor.id == record.id))
    await db.commit()


def _token(doctor_id: str, *, key: str = "protected-routes-test-key-0123456789", expired: bool = False) -> str:
    now = datetime.now(UTC)
    exp = now - timedelta(minutes=10) if expired else now + timedelta(hours=8)
    return pyjwt.encode(
        {
            "sub": doctor_id,
            "name": "测试",
            "role": "doctor",
            "roles": ["doctor"],
            "auth_version": 1,
            "iat": int((now - timedelta(minutes=5)).timestamp()),
            "exp": int(exp.timestamp()),
            "jti": uuid.uuid4().hex,
        },
        key,
        algorithm=JWT_ALGORITHM,
    )


# 覆盖业务路由：写接口 + 读接口 + 会话列表 + 病历
_PROTECTED_PATHS = [
    ("GET", "/api/v1/consult/sessions"),
    ("POST", "/api/v1/consult/sessions"),
    ("GET", "/api/v1/consult/sessions/{session_id}"),
    ("POST", "/api/v1/consult/sessions/{session_id}/terminate"),
    ("GET", "/api/v1/consult/sessions/{session_id}/messages"),
    ("POST", "/api/v1/consult/sessions/{session_id}/messages"),
    ("POST", "/api/v1/consult/sessions/{session_id}/advance"),
    ("POST", "/api/v1/consult/sessions/{session_id}/recover"),
    ("POST", "/api/v1/consult/sessions/{session_id}/review"),
    ("GET", "/api/v1/consult/sessions/{session_id}/record"),
    ("PUT", "/api/v1/consult/sessions/{session_id}/record"),
    ("GET", "/api/v1/consult/sessions/{session_id}/commands/{command_id}"),
]


@pytest.mark.parametrize(("method", "path"), _PROTECTED_PATHS)
async def test_no_token_returns_401(client: AsyncClient, method: str, path: str) -> None:
    """任意业务路由未携带 token → 401 UNAUTHENTICATED（标准 envelope）。"""
    sid = str(uuid.uuid4())
    url = path.format(session_id=sid, command_id=str(uuid.uuid4()))
    resp = await client.request(method, url, json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "UNAUTHENTICATED"
    assert body["message"]
    assert body["trace_id"]
    # 错误信息不暴露内部细节
    assert body["detail"] is None


async def test_forged_token_returns_invalid(client: AsyncClient) -> None:
    """伪造签名 token → 401 INVALID_TOKEN。"""
    forged = _token(str(uuid.uuid4()), key="attacker-controlled-key")
    resp = await client.get(
        "/api/v1/consult/sessions",
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_TOKEN"


async def test_expired_token_returns_expired(client: AsyncClient) -> None:
    """过期 token → 401 TOKEN_EXPIRED。"""
    expired = _token(str(uuid.uuid4()), expired=True)
    resp = await client.get(
        "/api/v1/consult/sessions",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "TOKEN_EXPIRED"


async def test_valid_token_passes(client: AsyncClient, doctor: Doctor) -> None:
    """有效 token 通过认证（返回业务结果而非 401）。"""
    valid = _token(str(doctor.id))
    resp = await client.get(
        "/api/v1/consult/sessions",
        headers={"Authorization": f"Bearer {valid}"},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == "SUCCESS"


async def test_x_doctor_id_no_longer_authorizes(client: AsyncClient) -> None:
    """客户端自报 X-Doctor-Id 在 on 模式下完全失效 → 401。"""
    resp = await client.get(
        "/api/v1/consult/sessions",
        headers={"X-Doctor-Id": "anyone-can-claim"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


async def test_health_endpoints_exempt(client: AsyncClient) -> None:
    """健康检查与 Prometheus 探针端点不要求认证（K8s/Prom 硬约束）。

    关键断言是不返回 401/403（未受认证拦截）；健康状态本身（200/503/501）
    由各自探针语义决定，与认证无关。
    """
    for path in (
        "/api/v1/health",
        "/api/v1/health/llm",
        "/api/v1/health/ready",
        "/api/v1/health/rag",
        "/api/v1/health/outbox",
        "/metrics",
        "/metrics/outbox",
    ):
        resp = await client.get(path)
        assert resp.status_code not in (401, 403), f"{path} 应豁免认证，实际 {resp.status_code}"
