"""/health/llm 连通检查接口测试。

使用 respx mock 覆盖成功与降级路径，不依赖真实外部服务。
验证 API key 不泄露、响应格式正确。
"""

from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.main import app

# ---------------------------------------------------------------------------
# /health/llm 成功测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_llm_returns_ok() -> None:
    """GET /health/llm 全部正常时返回 status=ok。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/llm")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["checks"]["chat"] == "ok"
    assert data["checks"]["embedding"] == "ok"
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_api_v1_health_llm_returns_ok() -> None:
    """GET /api/v1/health/llm 同样返回 200（版本化路径兼容接口设计文档 §4.7.3）。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health/llm")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# /health/llm 降级测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_llm_degraded_chat_unavailable() -> None:
    """GET /health/llm chat 不可用时返回 status=degraded。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(503)
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/llm")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert data["checks"]["chat"] == "unavailable"
    assert data["checks"]["embedding"] == "ok"


@pytest.mark.asyncio
async def test_health_llm_degraded_embedding_unavailable() -> None:
    """GET /health/llm embedding 不可用时返回 status=degraded。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(503)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/llm")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert data["checks"]["chat"] == "ok"
    assert data["checks"]["embedding"] == "unavailable"


# ---------------------------------------------------------------------------
# API key 不泄露测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_llm_no_api_key_in_response() -> None:
    """GET /health/llm 响应不包含 API key。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")
    api_key = settings.model_gateway_api_key

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/llm")

    response_text = response.text
    assert api_key not in response_text
    assert "sk-" not in response_text  # 不包含任何 API key 前缀


@pytest.mark.asyncio
async def test_health_llm_no_prompt_in_response() -> None:
    """GET /health/llm 响应不包含 prompt 原文或完整模型输出。"""
    settings = get_settings()
    base_url = settings.model_gateway_base_url.rstrip("/")

    with respx.mock:
        respx.post(f"{base_url}/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "This is a long model response that should not appear in health check output"}}]},
            )
        )
        respx.post(f"{base_url}/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/llm")

    response_text = response.text
    assert "This is a long model response" not in response_text
    assert "ping" not in response_text  # health check 使用的 prompt 也不应出现


# ---------------------------------------------------------------------------
# 现有 /health 测试继续通过
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_still_works() -> None:
    """GET /health 仍然正常工作（回归测试）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_api_v1_health_still_works() -> None:
    """GET /api/v1/health 仍然正常工作（回归测试）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
