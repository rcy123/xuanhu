"""阶段 4 运行态安全测试：接口暴露面收敛 + 兜底异常（T4.3 / T4.4 / H9）。

- 生产环境交互式文档（/docs）关闭，local/staging 保留。
- 未捕获异常 → 500 INTERNAL_ERROR 标准 envelope + trace_id，
  响应体不含 stacktrace / 异常字符串（M6 脱敏）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.redis import reset_redis
from app.main import app, resolve_docs_url

pytestmark = [pytest.mark.integration]


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


def test_docs_disabled_in_production() -> None:
    assert resolve_docs_url("production") is None
    assert resolve_docs_url("staging") == "/docs"
    assert resolve_docs_url("local") == "/docs"


@pytest.mark.asyncio(loop_scope="module")
async def test_docs_available_in_test_env(client: AsyncClient) -> None:
    """当前装配环境（local/staging）保留 /docs。"""
    response = await client.get("/docs")
    assert response.status_code in (200, 405)


@pytest.mark.asyncio(loop_scope="module")
async def test_catch_all_unhandled_exception_500_envelope() -> None:
    """未注册异常 → 500 INTERNAL_ERROR + trace_id，不含内部细节。

    Starlette 1.x 的 ServerErrorMiddleware 发送响应后恒会重抛异常
    （供服务器日志/测试客户端感知），因此此处用
    ``raise_app_exceptions=False`` 捕获 500 响应本身。
    """

    def _boom() -> None:
        raise RuntimeError("internal-stack-detail-should-never-leak")

    added = "/_test/route_added_" + "x" * 16
    app.add_api_route(added, _boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(added)
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "INTERNAL_ERROR"
        assert body["message"] == "服务器内部错误"
        assert body["detail"] is None
        assert body["retryable"] is True
        assert body["trace_id"]
        assert "internal-stack-detail-should-never-leak" not in response.text
        assert "RuntimeError" not in response.text
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != added]
