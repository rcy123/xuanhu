"""P3-4 健康检查 API 测试。

覆盖：
- GET /api/v1/health/ready（ready / degraded）
- GET /api/v1/health/rag（ok / degraded）
- 响应不泄露 API key / 连接串
- 集成测试 mock service 层避免真实网络调用超时
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = [pytest.mark.asyncio(loop_scope="module")]

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_READY_ALL_OK = {
    "status": "ready",
    "version": "0.1.0",
    "checks": {
        "database": "ok",
        "redis": "ok",
        "milvus": "ok",
        "llm_gateway": "ok",
        "embedding_gateway": "ok",
    },
    "timestamp": "2026-06-26T00:00:00Z",
}

_READY_DEGRADED = {
    "status": "degraded",
    "version": "0.1.0",
    "checks": {
        "database": "ok",
        "redis": "unavailable",
        "milvus": "ok",
        "llm_gateway": "ok",
        "embedding_gateway": "ok",
    },
    "timestamp": "2026-06-26T00:00:00Z",
}

_RAG_OK = {
    "status": "ok",
    "checks": {
        "pg_fulltext": "ok",
        "milvus_collection": "ok",
        "sample_query": "ok",
    },
    "timestamp": "2026-06-26T00:00:00Z",
}

_RAG_DEGRADED = {
    "status": "degraded",
    "checks": {
        "pg_fulltext": "ok",
        "milvus_collection": "unavailable",
        "sample_query": "ok",
    },
    "timestamp": "2026-06-26T00:00:00Z",
}


def _mock_ready_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock HealthService.ready_check 返回 all-ok 响应。"""
    from app.services.health import HealthService

    async def _mock(_self: object) -> dict:
        return _READY_ALL_OK

    monkeypatch.setattr(HealthService, "ready_check", _mock)


def _mock_ready_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock HealthService.ready_check 返回 degraded 响应。"""
    from app.services.health import HealthService

    async def _mock(_self: object) -> dict:
        return _READY_DEGRADED

    monkeypatch.setattr(HealthService, "ready_check", _mock)


def _mock_rag_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock HealthService.rag_check 返回 ok 响应。"""
    from app.services.health import HealthService

    async def _mock(_self: object) -> dict:
        return _RAG_OK

    monkeypatch.setattr(HealthService, "rag_check", _mock)


def _mock_rag_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock HealthService.rag_check 返回 degraded 响应。"""
    from app.services.health import HealthService

    async def _mock(_self: object) -> dict:
        return _RAG_DEGRADED

    monkeypatch.setattr(HealthService, "rag_check", _mock)


# ---------------------------------------------------------------------------
# GET /api/v1/health/ready — 集成测试（mock service 层）
# ---------------------------------------------------------------------------


async def test_ready_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """ready 检查全部 ok 时返回 status=ready。"""
    _mock_ready_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()

    # 扁平 JSON，无 envelope
    assert "code" not in body
    assert "data" not in body

    assert body["status"] == "ready"
    assert body["version"] == "0.1.0"
    assert "checks" in body
    assert "timestamp" in body

    checks = body["checks"]
    assert checks["database"] == "ok"
    assert checks["redis"] == "ok"
    assert checks["milvus"] == "ok"
    assert checks["llm_gateway"] == "ok"
    assert checks["embedding_gateway"] == "ok"


async def test_ready_response_flat_json_no_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """ready 响应为扁平 JSON，不含标准 envelope 字段。"""
    _mock_ready_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    body = response.json()
    # 标准 envelope 字段不应出现
    assert "code" not in body
    assert "message" not in body
    assert "data" not in body
    assert "detail" not in body
    assert "retryable" not in body

    # 扁平 JSON 应包含这些字段
    assert "status" in body
    assert "checks" in body
    assert "timestamp" in body


async def test_ready_no_api_key_leaked(monkeypatch: pytest.MonkeyPatch) -> None:
    """ready 响应不泄露 API key 或连接串（mock 固定返回值确保不引入）。"""
    _mock_ready_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    body = response.json()
    body_str = str(body)

    # 敏感字段不应出现
    forbidden = [
        "api_key",
        "apiKey",
        "Bearer ",
        "sk-",
        "password",
        "secret",
    ]
    for keyword in forbidden:
        assert keyword.lower() not in body_str.lower(), f"ready 响应泄露敏感信息: 发现 '{keyword}'"

    # 连接串中的密码不应出现
    if "database_url" in body_str or "redis_url" in body_str:
        assert "postgresql://" not in body_str.lower()
        assert "redis://" not in body_str.lower()


# ---------------------------------------------------------------------------
# GET /api/v1/health/rag — 集成测试（mock service 层）
# ---------------------------------------------------------------------------


async def test_rag_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAG 健康检查成功。"""
    _mock_rag_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/rag")

    assert response.status_code == 200
    body = response.json()

    # 扁平 JSON，无 envelope
    assert "code" not in body
    assert "data" not in body

    assert body["status"] == "ok"
    assert "checks" in body
    assert "timestamp" in body

    checks = body["checks"]
    assert checks["pg_fulltext"] == "ok"
    assert checks["milvus_collection"] == "ok"
    assert checks["sample_query"] == "ok"


async def test_rag_response_flat_json_no_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAG health 响应为扁平 JSON，不含标准 envelope 字段。"""
    _mock_rag_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/rag")

    body = response.json()
    # 标准 envelope 字段不应出现
    assert "code" not in body
    assert "message" not in body
    assert "data" not in body
    assert "detail" not in body
    assert "retryable" not in body

    # 扁平 JSON 应包含这些字段
    assert "status" in body
    assert "checks" in body
    assert "timestamp" in body


async def test_rag_response_does_not_leak_sensitive_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAG health 响应不泄露敏感信息。"""
    _mock_rag_ok(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/rag")

    body = response.json()
    body_str = str(body)

    # API key / 连接串 / 完整模型响应不应出现
    forbidden = [
        "api_key",
        "apiKey",
        "Bearer ",
        "sk-",
        "password",
        "secret",
        "postgresql://",
        "redis://",
        "full_model_response",
        "raw_response",
    ]
    for keyword in forbidden:
        assert keyword.lower() not in body_str.lower(), f"RAG health 响应泄露敏感信息: 发现 '{keyword}'"


# ---------------------------------------------------------------------------
# RAG health 降级不抛 500（mock degraded 场景验证端点行为）
# ---------------------------------------------------------------------------


async def test_rag_health_never_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAG 不可用时也不抛出 500，始终返回 200 含 degraded。"""
    _mock_rag_degraded(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/rag")

    # 始终返回 200
    assert response.status_code == 200
    body = response.json()
    # status 必为 ok 或 degraded
    assert body["status"] == "degraded"


async def test_ready_health_returns_503_when_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 降级时返回 503，让编排器停止分发流量。"""
    _mock_ready_degraded(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# Health 服务单元测试：单项不可用时的降级
# ---------------------------------------------------------------------------


class TestHealthServiceDegraded:
    """通过 monkeypatch 模拟组件不可用时的降级行为。"""

    async def test_ready_degraded_when_database_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 database 不可用时 status=degraded。"""
        from app.services.health import HealthService

        async def mock_check_db(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_database", mock_check_db)

        service = HealthService()
        result = await service.ready_check()
        assert result["status"] == "degraded"
        assert result["checks"]["database"] == "unavailable"

    async def test_ready_degraded_when_redis_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 redis 不可用时 status=degraded。"""
        from app.services.health import HealthService

        async def mock_check_redis(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_redis", mock_check_redis)

        service = HealthService()
        result = await service.ready_check()
        assert result["status"] == "degraded"
        assert result["checks"]["redis"] == "unavailable"

    async def test_ready_degraded_when_milvus_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 milvus 不可用时 status=degraded。"""
        from app.services.health import HealthService

        async def mock_check_milvus(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_milvus", mock_check_milvus)

        service = HealthService()
        result = await service.ready_check()
        assert result["status"] == "degraded"
        assert result["checks"]["milvus"] == "unavailable"

    async def test_ready_degraded_when_llm_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 llm_gateway 不可用时 status=degraded。"""
        from app.services.health import HealthService

        async def mock_check_gw(_self: object) -> dict[str, str]:
            return {"chat": "unavailable", "embedding": "ok"}

        monkeypatch.setattr(HealthService, "_check_gateway", mock_check_gw)

        service = HealthService()
        result = await service.ready_check()
        assert result["status"] == "degraded"
        assert result["checks"]["llm_gateway"] == "unavailable"

    async def test_ready_degraded_when_embedding_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 embedding_gateway 不可用时 status=degraded。"""
        from app.services.health import HealthService

        async def mock_check_gw(_self: object) -> dict[str, str]:
            return {"chat": "ok", "embedding": "unavailable"}

        monkeypatch.setattr(HealthService, "_check_gateway", mock_check_gw)

        service = HealthService()
        result = await service.ready_check()
        assert result["status"] == "degraded"
        assert result["checks"]["embedding_gateway"] == "unavailable"

    async def test_rag_degraded_when_pg_fulltext_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 pg_fulltext 不可用时 RAG status=degraded。"""
        from app.services.health import HealthService

        async def mock_pg_ft(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_pg_fulltext", mock_pg_ft)

        service = HealthService()
        result = await service.rag_check()
        assert result["status"] == "degraded"
        assert result["checks"]["pg_fulltext"] == "unavailable"

    async def test_rag_degraded_when_milvus_collection_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 milvus_collection 不可用时 RAG status=degraded。"""
        from app.services.health import HealthService

        async def mock_milvus_coll(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_milvus_collection", mock_milvus_coll)

        service = HealthService()
        result = await service.rag_check()
        assert result["status"] == "degraded"
        assert result["checks"]["milvus_collection"] == "unavailable"

    async def test_rag_degraded_when_sample_query_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 sample_query 不可用时 RAG status=degraded。"""
        from app.services.health import HealthService

        async def mock_sample(_self: object) -> str:
            return "unavailable"

        monkeypatch.setattr(HealthService, "_check_sample_query", mock_sample)

        service = HealthService()
        result = await service.rag_check()
        assert result["status"] == "degraded"
        assert result["checks"]["sample_query"] == "unavailable"


# ---------------------------------------------------------------------------
# health 服务不会泄露 API key / 连接串
# ---------------------------------------------------------------------------


class TestHealthServiceNoSensitiveLeak:
    """验证 ready 和 rag 响应均不泄露敏感信息。"""

    async def test_ready_check_no_sensitive_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ready_check() 返回字典不含敏感字段名。"""
        from app.services.health import HealthService

        _mock_ready_ok(monkeypatch)

        service = HealthService()
        result = await service.ready_check()
        result_str = str(result)

        forbidden = ["api_key", "apiKey", "Bearer ", "password", "secret", "token"]
        for keyword in forbidden:
            assert keyword.lower() not in result_str.lower(), f"ready_check 泄露: '{keyword}'"

    async def test_rag_check_no_sensitive_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """rag_check() 返回字典不含敏感字段名。"""
        from app.services.health import HealthService

        _mock_rag_ok(monkeypatch)

        service = HealthService()
        result = await service.rag_check()
        result_str = str(result)

        forbidden = ["api_key", "apiKey", "Bearer ", "password", "secret", "token"]
        for keyword in forbidden:
            assert keyword.lower() not in result_str.lower(), f"rag_check 泄露: '{keyword}'"
