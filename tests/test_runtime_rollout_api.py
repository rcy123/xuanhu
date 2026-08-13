"""HTTP envelope regressions for L9 terminal rollout phases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.session import get_db
from app.main import app


async def _unused_db() -> AsyncIterator[Any]:
    yield object()


@pytest.mark.asyncio
async def test_full_phase_rejects_new_legacy_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_VERSION", "langgraph")
    monkeypatch.setenv("AGENT_RUNTIME_ROLLOUT_PHASE", "full")
    monkeypatch.setenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", "true")
    monkeypatch.setenv("XUANHU_LANGGRAPH_PRODUCT_READY", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = _unused_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/consult/sessions",
                json={"agent_runtime": "legacy", "chief_complaint": "test"},
                headers={"X-Request-Id": "rollout-full-test"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()

    assert response.status_code == 409
    assert response.json() == {
        "code": "LEGACY_RUNTIME_CREATION_DISABLED",
        "message": "全量切流后不再允许新建 Legacy 会话",
        "detail": None,  # 阶段2 T2.7：detail 不回传客户端
        "retryable": False,
        "stage": None,
        "trace_id": "rollout-full-test",
    }


@pytest.mark.asyncio
async def test_rollback_phase_rejects_only_new_langgraph_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_VERSION", "legacy")
    monkeypatch.setenv("AGENT_RUNTIME_ROLLOUT_PHASE", "rollback")
    monkeypatch.setenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", "true")
    monkeypatch.setenv("XUANHU_LANGGRAPH_PRODUCT_READY", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = _unused_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/consult/sessions",
                json={"agent_runtime": "langgraph", "chief_complaint": "test"},
                headers={"X-Request-Id": "rollout-rollback-test"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "RUNTIME_ROLLOUT_NOT_READY"
    assert body["message"] == "回滚阶段暂停新建 LangGraph 会话"
    assert body["retryable"] is False
    assert body["trace_id"] == "rollout-rollback-test"
