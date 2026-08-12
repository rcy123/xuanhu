"""阶段 2 认证测试：session_id UUID 格式校验（T2.8 / H7）。

所有 ``{session_id}`` 路径参数路由对非法格式统一返回 422 VALIDATION_ERROR，
堵住 SSE 等路径的 SSRF / 路径遍历输入。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.redis import reset_redis
from app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _hardening_on() -> AsyncIterator[None]:
    from app.core.config import get_settings

    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["XUANHU_ACCESS_ENABLED"] = "on"
    get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ["XUANHU_AUTH_ENABLED"] = "off"
        os.environ["XUANHU_ACCESS_ENABLED"] = "off"
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


# 注意：httpx 会按 RFC 3986 折叠 "../" 点段，路径遍历形态无法原样到达应用。
# 用等价的非法 UUID 字符串覆盖同一校验路径；真实服务器（uvicorn）不折叠路径，
# 遍历形态会命中同一 validate_session_id 分支（见 test_no_phi_in_logs 的
# 路径遍历断言）。
_MALFORMED = "not-a-uuid"

# (method, path_template)
_ROUTES = [
    ("GET", "/api/v1/consult/sessions/{sid}"),
    ("POST", "/api/v1/consult/sessions/{sid}/terminate"),
    ("POST", "/api/v1/consult/sessions/{sid}/messages"),
    ("GET", "/api/v1/consult/sessions/{sid}/messages"),
    ("POST", "/api/v1/consult/sessions/{sid}/advance"),
    ("POST", "/api/v1/consult/sessions/{sid}/recover"),
    ("POST", "/api/v1/consult/sessions/{sid}/review"),
    ("GET", "/api/v1/consult/sessions/{sid}/record"),
    ("PUT", "/api/v1/consult/sessions/{sid}/record"),
    ("GET", "/api/v1/consult/sessions/{sid}/record/export?format=txt"),
    ("GET", "/api/v1/consult/sessions/{sid}/safety-assertions"),
    ("GET", "/api/v1/consult/sessions/{sid}/commands/{cid}"),
    ("GET", "/api/v1/consult/sessions/{sid}/stream"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
async def test_malformed_session_id_rejected(client: AsyncClient, method: str, path: str) -> None:
    """非法 session_id（含路径遍历形态）→ 422 VALIDATION_ERROR。"""
    url = path.format(sid=_MALFORMED, cid=str(uuid.uuid4()))
    resp = await client.request(method, url, json={})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
