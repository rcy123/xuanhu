"""P3-3 SSE 事件流 API 测试。"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis, reset_redis
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.services.events import EventService, session_event_stream_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_TEST_PATIENT_REF_PREFIX = "P3-SSE-"
_TEST_DOCTOR_ID = "doctor_p3_sse_test"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_data() -> None:
    """模块结束时清理本模块创建的数据和 Redis 事件。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()
    with contextlib.suppress(Exception):
        await reset_redis()

    yield

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ConsultSession.id).where(
                or_(
                    ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                    ConsultSession.created_by == _TEST_DOCTOR_ID,
                )
            )
        )
        test_session_ids = [row[0] for row in result.all()]
        if test_session_ids:
            await session.execute(
                delete(ConsultMessage).where(ConsultMessage.session_id.in_(test_session_ids))
            )
            await session.execute(
                delete(AuditEvent).where(AuditEvent.session_id.in_(test_session_ids))
            )
            await session.execute(
                delete(ConsultSession).where(ConsultSession.id.in_(test_session_ids))
            )
            await session.commit()

    with contextlib.suppress(Exception, RuntimeError):
        redis = await get_redis()
        for sid in test_session_ids:
            await redis.delete(session_event_stream_key(str(sid)))
        await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端（注入 fake inquiry/sufficiency agent 绕过真实模型网关）。"""
    from app.agents.base import AgentResult
    from app.agents.registry import AgentRegistry
    from app.schemas.agent import InquiryAgentOutput, SufficiencyReport
    from app.schemas.types import Stage

    class _FakeInquiry:
        name = "inquiry"
        stage = "inquiry"
        primary_sources = ()
        allow_cross_source = True
        output_schema = InquiryAgentOutput

        async def run(self, state: Any, trace_id: str) -> AgentResult:
            return AgentResult(
                output=InquiryAgentOutput(
                    next_question="请补充现病史细节",
                    asked_dimension="chief_complaint",
                ),
                prompt_version="fake",
            )

    class _FakeSufficiency:
        name = "sufficiency"
        stage = "sufficiency"
        primary_sources = ()
        allow_cross_source = True
        output_schema = SufficiencyReport

        async def run(self, state: Any, trace_id: str) -> AgentResult:
            return AgentResult(
                output=SufficiencyReport(
                    covered=["chief_complaint"],
                    missing=["present_illness"],
                    sufficient=False,
                    suggestions=["请补充现病史"],
                ),
                prompt_version="fake",
            )

    reg = AgentRegistry()
    reg.register(Stage.INQUIRY, _FakeInquiry())  # type: ignore[arg-type]
    reg.register(Stage.SUFFICIENCY, _FakeSufficiency())  # type: ignore[arg-type]

    import app.services.message as msg_module

    _orig_registry = msg_module._default_inquiry_registry
    msg_module._default_inquiry_registry = lambda: reg  # type: ignore[assignment]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    msg_module._default_inquiry_registry = _orig_registry  # type: ignore[assignment]


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres_and_redis() -> None:
    """检查 PostgreSQL 和 Redis 可用性。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Redis integration dependency unavailable: {type(exc).__name__}: {exc}")


async def _create_session(client: AsyncClient) -> dict[str, Any]:
    """创建测试会话。"""
    response = await client.post(
        "/api/v1/consult/sessions",
        json={
            "patient_info": {
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "unknown",
            },
            "chief_complaint": "SSE 测试主诉",
        },
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _finite_sse(
    self: EventService,
    session_id: str,
    *,
    last_event_id: str | None,
    heartbeat_interval_seconds: float,
) -> AsyncIterator[str]:
    """测试用有限 SSE 流。"""
    del heartbeat_interval_seconds
    event = self.resync_event(session_id, last_event_id or "initial")
    yield self.format_sse_event(event)


async def test_stream_endpoint_returns_text_event_stream(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE endpoint 返回 text/event-stream 和标准事件文本。"""
    session = await _create_session(client)
    monkeypatch.setattr(EventService, "iter_sse", _finite_sse)

    response = await client.get(f"/api/v1/consult/sessions/{session['session_id']}/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: resync\n" in response.text
    assert "\nid: resync-" in response.text
    assert "\ndata: " in response.text


async def test_stream_endpoint_passes_last_event_id(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """last_event_id query 参数会传给事件服务。"""
    session = await _create_session(client)
    seen: dict[str, str | None] = {}

    async def finite_with_capture(
        self: EventService,
        session_id: str,
        *,
        last_event_id: str | None,
        heartbeat_interval_seconds: float,
    ) -> AsyncIterator[str]:
        del heartbeat_interval_seconds
        seen["last_event_id"] = last_event_id
        yield self.format_sse_event(self.resync_event(session_id, "capture"))

    monkeypatch.setattr(EventService, "iter_sse", finite_with_capture)

    response = await client.get(
        f"/api/v1/consult/sessions/{session['session_id']}/stream?last_event_id=12-0"
    )

    assert response.status_code == 200
    assert seen["last_event_id"] == "12-0"


async def test_stream_endpoint_session_not_found(client: AsyncClient) -> None:
    """不存在会话返回 SESSION_NOT_FOUND。"""
    response = await client.get(f"/api/v1/consult/sessions/{uuid.uuid4()}/stream")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"


async def test_message_submit_writes_message_created_stream_event(
    client: AsyncClient,
) -> None:
    """P3-2 消息提交成功后写入 message.created Redis Stream 事件。P8-6: 医生消息 + Agent 消息各一条。"""
    session = await _create_session(client)
    session_id = session["session_id"]

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/messages",
        json={"content": "SSE message event", "role": "doctor"},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 200, response.text
    doctor_message_id = response.json()["data"]["message_id"]

    events, needs_resync = await EventService().read_events_after(session_id, "0-0")

    assert needs_resync is False
    message_events = [event for event in events if event.event_type == "message.created"]
    assert len(message_events) >= 1
    # 至少有一条 doctor 消息事件
    doctor_events = [
        e for e in message_events
        if e.payload.get("message_id") == doctor_message_id
        and e.payload.get("role") == "doctor"
    ]
    assert len(doctor_events) >= 1, f"应有 doctor 消息事件: {[e.payload for e in message_events]}"
    assert doctor_events[0].payload["session_id"] == session_id
    assert doctor_events[0].payload["content"] == "SSE message event"
