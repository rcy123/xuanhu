"""阶段 4 运行态安全测试：CORS 白名单（T4.5 / M2）。

- 白名单来源：预检 OPTIONS 放行，携带 Access-Control-Allow-*。
- 非白名单来源：不返回 CORS 头（浏览器拒绝跨域读取）。
- 简单请求：回显允许的来源。
- 配置层：``*`` 通配来源被 Settings 校验直接拒绝（fail-fast）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.redis import reset_redis
from app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_ALLOWED_ORIGIN = "http://localhost:5173"
_EVIL_ORIGIN = "http://evil.example"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup() -> AsyncIterator[None]:
    yield
    with contextlib.suppress(Exception):
        await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_preflight_allowed_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/consult/sessions",
        headers={
            "Origin": _ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN
    assert "POST" in (response.headers.get("access-control-allow-methods") or "")
    assert "authorization" in (response.headers.get("access-control-allow-headers") or "").lower()


async def test_preflight_disallowed_origin_no_cors_headers(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/consult/sessions",
        headers={
            "Origin": _EVIL_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    # 非白名单来源：响应正常（业务侧处理），但不返回任何 CORS 头
    assert response.headers.get("access-control-allow-origin") is None


async def test_simple_request_echoes_allowed_origin(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": _ALLOWED_ORIGIN})
    assert response.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN


async def test_wildcard_origin_rejected_at_config() -> None:
    """通配来源在 Settings 校验层即被拒绝，无法装配出非法 CORS。"""
    import pydantic

    from app.core.config import Settings

    base = {
        "app_env": "local",
        "database_url": "postgresql://u:p@localhost:5432/xuanhu",
        "redis_url": "redis://:p@localhost:6379/0",
        "model_gateway_base_url": "http://gw.internal/v1",
        "model_gateway_api_key": "sk-test",
        "chat_model": "m",
        "embedding_model": "e",
        "embedding_dim": 768,
        "jwt_signing_key": "k" * 40,
    }
    with pytest.raises(pydantic.ValidationError, match="wildcard"):
        Settings(**base, **{"CORS_ALLOWED_ORIGINS": "*"})


async def test_wildcard_origin_rejected_even_with_credentials_explicit() -> None:
    """即使显式带 allow_credentials 的组合意图，通配来源同样被拒。"""
    import pydantic

    from app.core.config import Settings

    base = {
        "app_env": "local",
        "database_url": "postgresql://u:p@localhost:5432/xuanhu",
        "redis_url": "redis://:p@localhost:6379/0",
        "model_gateway_base_url": "http://gw.internal/v1",
        "model_gateway_api_key": "sk-test",
        "chat_model": "m",
        "embedding_model": "e",
        "embedding_dim": 768,
        "jwt_signing_key": "k" * 40,
    }
    with pytest.raises(pydantic.ValidationError):
        Settings(**base, **{"CORS_ALLOWED_ORIGINS": "http://localhost:5173,*"})
