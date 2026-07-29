"""Recovery API runtime 分流的无基础设施回归测试。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.sql import Select

from app.db.session import get_db
from app.main import app
from app.schemas.recovery import RecoveryRequest, RecoveryResponse
from app.services.langgraph_recovery import LangGraphRecoveryService
from app.services.recovery import RecoveryService
from app.services.session_lock import SessionLock


class _ScalarResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _ReadOnlySession:
    """仅允许 dispatcher 执行一次 SELECT 的测试数据库替身。"""

    def __init__(self, runtime: str | None) -> None:
        self.runtime = runtime
        self.statements: list[Any] = []
        self.rolled_back = False

    async def execute(self, statement: Any) -> _ScalarResult:
        assert isinstance(statement, Select), "runtime 分流只能执行只读 SELECT"
        self.statements.append(statement)
        return _ScalarResult(self.runtime)

    def in_transaction(self) -> bool:
        return not self.rolled_back

    async def rollback(self) -> None:
        self.rolled_back = True


async def _post_recover(
    db: _ReadOnlySession,
    session_id: str,
) -> Any:
    async def _override_db() -> AsyncIterator[_ReadOnlySession]:
        yield db

    app.dependency_overrides[get_db] = _override_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/consult/sessions/{session_id}/recover",
                json={"action": "retry_current_stage"},
                headers={"X-Request-Id": "runtime-dispatch-test"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_langgraph_api_dispatches_to_product_recovery_without_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangGraph 只读 runtime 后仅调用产品恢复服务。"""
    session_id = str(uuid.uuid4())
    db = _ReadOnlySession("langgraph")
    legacy_calls: list[str] = []
    lock_calls: list[str] = []
    redis_calls: list[str] = []
    product_calls: list[str] = []

    async def _forbid_legacy_recover(
        self: RecoveryService,
        requested_session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> RecoveryResponse:
        del self, request, doctor_id, trace_id
        legacy_calls.append(requested_session_id)
        raise AssertionError("LangGraph 请求不得调用 Legacy RecoveryService")

    async def _forbid_lock(self: SessionLock) -> None:
        del self
        lock_calls.append("called")
        raise AssertionError("LangGraph 请求不得调用 Legacy SessionLock")

    async def _forbid_redis() -> None:
        redis_calls.append("called")
        raise AssertionError("LangGraph 请求不得访问 Legacy Redis")

    async def _product_recover(
        self: LangGraphRecoveryService,
        requested_session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        idempotency_key: str,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> RecoveryResponse:
        del self, doctor_id, trace_id, idempotency_key, shared_runtime
        assert allow_request_local_runtime is True
        product_calls.append(requested_session_id)
        return RecoveryResponse(
            session_id=requested_session_id,
            current_stage="safety",
            status="active",
            recovery_status="normal",
            action=request.action,
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(RecoveryService, "recover", _forbid_legacy_recover)
    monkeypatch.setattr(SessionLock, "acquire", _forbid_lock)
    monkeypatch.setattr(SessionLock, "release", _forbid_lock)
    monkeypatch.setattr("app.services.session_lock.get_redis", _forbid_redis)
    monkeypatch.setattr(LangGraphRecoveryService, "recover", _product_recover)
    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    try:
        response = await _post_recover(db, session_id)
    finally:
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")

    assert response.status_code == 200
    assert response.json()["data"]["current_stage"] == "safety"
    assert len(db.statements) == 1
    assert product_calls == [session_id]
    assert legacy_calls == []
    assert lock_calls == []
    assert redis_calls == []


@pytest.mark.asyncio
async def test_legacy_api_dispatches_to_existing_recovery_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy runtime 仍调用原恢复服务并返回其结果。"""
    session_id = str(uuid.uuid4())
    db = _ReadOnlySession("legacy")
    calls: list[str] = []

    async def _legacy_recover(
        self: RecoveryService,
        requested_session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> RecoveryResponse:
        del self, doctor_id, trace_id
        calls.append(requested_session_id)
        return RecoveryResponse(
            session_id=requested_session_id,
            current_stage="inquiry",
            status="active",
            recovery_status="normal",
            action=request.action,
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(RecoveryService, "recover", _legacy_recover)

    response = await _post_recover(db, session_id)

    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"
    assert calls == [session_id]
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_missing_session_returns_404_before_legacy_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读 runtime 查询无结果时返回 404，且不调用 Legacy。"""
    session_id = str(uuid.uuid4())
    db = _ReadOnlySession(None)
    calls: list[str] = []

    async def _forbid_legacy_recover(
        self: RecoveryService,
        requested_session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> RecoveryResponse:
        del self, request, doctor_id, trace_id
        calls.append(requested_session_id)
        raise AssertionError("不存在的会话不得调用 Legacy RecoveryService")

    monkeypatch.setattr(RecoveryService, "recover", _forbid_legacy_recover)

    response = await _post_recover(db, session_id)

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"
    assert body["retryable"] is False
    assert calls == []
    assert len(db.statements) == 1
