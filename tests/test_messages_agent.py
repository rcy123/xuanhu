"""P8-6 消息提交触发 Agent 回复集成测试。

使用 fake agent 注入 MessageService registry，覆盖：
- 提交消息后出现 Agent 回复（agent_message 在响应中）
- 医生消息和 Agent 回复均落库 consult_messages
- state_version 递增两次（医生消息 + Agent 消息）
- message.created 事件两条（医生 + Agent）
- Agent 失败时不伪造回复，返回 AGENT_TRIGGER_FAILED
- 非 inquiry 阶段拒绝提交

本测试为集成测试，需要可连接的 PostgreSQL；不可用时自动跳过。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentResult
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

# ---------------------------------------------------------------------------
# 测试数据标识
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P8-MSG6-"
_TEST_DOCTOR_ID = "doctor_p8_msg6_test"


# ---------------------------------------------------------------------------
# 模块级清理
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_data() -> None:
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()

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
        if not test_session_ids:
            return
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端（注入 fake agent 绕过真实模型网关）。"""

    # 注入 fake agents 到 MessageService
    from app.agents.registry import AgentRegistry
    from app.schemas.types import Stage

    reg = AgentRegistry()
    reg.register(Stage.INQUIRY, FakeInquiryAgent())  # type: ignore[arg-type]
    reg.register(Stage.SUFFICIENCY, FakeSufficiencyAgent())  # type: ignore[arg-type]

    # monkeypatch: 覆盖 _default_inquiry_registry 返回 fake agents
    import app.services.message as msg_module

    _orig_registry = msg_module._default_inquiry_registry

    def _fake_registry():
        return reg

    msg_module._default_inquiry_registry = _fake_registry  # type: ignore[assignment]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    msg_module._default_inquiry_registry = _orig_registry  # type: ignore[assignment]


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    from sqlalchemy import text

    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用，跳过集成测试: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Fake Agents（实现 BaseAgent Protocol，用于注入）
# ---------------------------------------------------------------------------


class FakeInquiryAgent:
    """Fake inquiry agent，不调用真实模型网关。"""

    name = "inquiry"
    stage = "inquiry"
    primary_sources = ()
    allow_cross_source = True

    def __init__(self, next_question: str = "请补充现病史细节", *, fail: bool = False) -> None:
        self._next_question = next_question
        self._fail = fail

    async def run(self, state: Any, trace_id: str) -> AgentResult:
        if self._fail:
            from app.agents.errors import AgentRunError

            raise AgentRunError(
                "Agent 执行失败",
                code="AGENT_FAILED",
                retryable=False,
            )
        from app.schemas.agent import InquiryAgentOutput

        return AgentResult(
            output=InquiryAgentOutput(
                next_question=self._next_question,
                asked_dimension="chief_complaint",
            ),
            prompt_version="fake",
        )


class FakeSufficiencyAgent:
    """Fake sufficiency agent，不调用真实模型网关。"""

    name = "sufficiency"
    stage = "sufficiency"
    primary_sources = ()
    allow_cross_source = True

    def __init__(self, sufficient: bool = False) -> None:
        self._sufficient = sufficient

    async def run(self, state: Any, trace_id: str) -> AgentResult:
        from app.schemas.agent import SufficiencyReport

        return AgentResult(
            output=SufficiencyReport(
                covered=["chief_complaint"],
                missing=[] if self._sufficient else ["present_illness", "sleep"],
                sufficient=self._sufficient,
                suggestions=[] if self._sufficient else ["请补充现病史", "请补充睡眠情况"],
                next_question=None if self._sufficient else "请问现病史和睡眠情况？",
            ),
            prompt_version="fake",
        )


class FakeFailingSufficiencyAgent:
    """Sufficiency agent that always fails with AGENT_TRIGGER_EXCEPTION."""

    name = "sufficiency"
    stage = "sufficiency"
    primary_sources = ()
    allow_cross_source = True

    async def run(self, state: Any, trace_id: str) -> AgentResult:
        raise RuntimeError("sufficiency agent crashed")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _create_session(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/consult/sessions",
        json=payload or {},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _create_inquiry_session(client: AsyncClient) -> dict[str, Any]:
    return await _create_session(
        client,
        {
            "patient_info": {
                "name": "P8-6测试患者",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}INQ{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 40,
            },
            "chief_complaint": "P8-6测试主诉",
        },
    )


async def _submit_message(
    client: AsyncClient,
    session_id: str,
    content: str = "头痛持续三天",
    expect_status: int = 200,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/messages",
        json={"content": content, "role": "doctor"},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == expect_status, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 提交消息 → Agent 回复成功路径
# ---------------------------------------------------------------------------


async def test_submit_message_returns_agent_reply(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息后响应包含 agent_message 和 sufficiency_report。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="头痛以两侧太阳穴为主")

    assert body["code"] == "SUCCESS"
    data = body["data"]

    # 医生消息
    assert data["role"] == "doctor"
    assert data["content"] == "头痛以两侧太阳穴为主"

    # Agent 回复
    agent_msg = data.get("agent_message")
    assert agent_msg is not None, f"响应缺少 agent_message: {list(data.keys())}"
    assert agent_msg["role"] == "agent"
    assert agent_msg["agent_name"] == "inquiry"
    assert "请补充现病史细节" in agent_msg["content"]

    # 完备性报告
    suff = data.get("sufficiency_report")
    assert suff is not None, f"响应缺少 sufficiency_report: {list(data.keys())}"
    assert "sufficient" in suff


async def test_both_messages_saved_to_db(client: AsyncClient, db: AsyncSession) -> None:
    """医生消息和 Agent 消息均落库 consult_messages。"""
    s = await _create_inquiry_session(client)
    await _submit_message(client, s["session_id"], content="DB验证消息")

    sid = uuid.UUID(s["session_id"])
    result = await db.execute(
        select(ConsultMessage)
        .where(ConsultMessage.session_id == sid)
        .order_by(ConsultMessage.created_at.asc())
    )
    messages = result.scalars().all()

    # 至少两条消息：医生 + Agent
    assert len(messages) >= 2, f"预期 >=2 条消息，实际 {len(messages)}"
    roles = [m.role for m in messages]
    assert "doctor" in roles, f"缺少 doctor 消息: {roles}"
    assert "agent" in roles, f"缺少 agent 消息: {roles}"

    agent_msg = [m for m in messages if m.role == "agent"][0]
    assert agent_msg.agent_name == "inquiry"
    assert agent_msg.content != ""
    assert agent_msg.stage == "inquiry"


async def test_state_version_incremented_twice(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息后 state_version 至少递增 2（医生消息 + Agent 消息）。"""
    s = await _create_inquiry_session(client)
    initial_version = s.get("state_version", 1)

    body = await _submit_message(client, s["session_id"])
    final_version = body["data"]["state_version"]
    assert final_version >= initial_version + 2, (
        f"预期 state_version >= {initial_version + 2}，实际 {final_version}"
    )


async def test_agent_message_has_agent_name(client: AsyncClient, db: AsyncSession) -> None:
    """Agent 消息的 agent_name 为 inquiry。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"])
    assert body["data"]["agent_message"]["agent_name"] == "inquiry"


async def test_sufficiency_report_in_response(client: AsyncClient, db: AsyncSession) -> None:
    """响应中的 sufficiency_report 包含 sufficient 字段。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"])
    suff = body["data"]["sufficiency_report"]
    assert suff is not None
    assert "sufficient" in suff
    assert "covered" in suff
    assert "missing" in suff
    assert "suggestions" in suff


# ---------------------------------------------------------------------------
# Agent 失败路径
# ---------------------------------------------------------------------------


async def test_agent_failure_does_not_forge_reply(client: AsyncClient, db: AsyncSession) -> None:
    """Agent 失败时返回错误，不伪造 Agent 回复。"""

    # 注入总是失败的 fake agent
    fake_inquiry = FakeInquiryAgent(fail=True)
    from app.agents.registry import AgentRegistry
    from app.schemas.types import Stage

    reg = AgentRegistry()
    reg.register(Stage.INQUIRY, fake_inquiry)  # type: ignore[arg-type]
    reg.register(Stage.SUFFICIENCY, FakeSufficiencyAgent())  # type: ignore[arg-type]

    s = await _create_inquiry_session(client)
    try:
        # 通过 monkeypatch 替换 registry 构造
        import app.services.message as msg_module

        _orig_init = msg_module.MessageService.__init__

        def patched_init(self, db, *, registry=None, event_service=None):
            _orig_init(self, db, registry=reg, event_service=event_service)

        msg_module.MessageService.__init__ = patched_init  # type: ignore[method-assign]

        body = await _submit_message(
            client, s["session_id"], content="这条消息应该触发 Agent 失败", expect_status=503
        )
        assert body["code"] == "AGENT_TRIGGER_FAILED"
        assert body["retryable"] is False  # AGENT_FAILED is not retryable

        # 医生消息已落库
        sid = uuid.UUID(s["session_id"])
        result = await db.execute(
            select(ConsultMessage)
            .where(ConsultMessage.session_id == sid)
            .order_by(ConsultMessage.created_at.asc())
        )
        messages = result.scalars().all()
        roles = [m.role for m in messages]
        assert "doctor" in roles, f"医生消息应已落库: {roles}"
        # 没有 agent 消息
        agent_msgs = [m for m in messages if m.role == "agent"]
        assert len(agent_msgs) == 0, f"不应有 agent 消息: {agent_msgs}"
    finally:
        msg_module.MessageService.__init__ = _orig_init  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 非 inquiry 阶段拒绝
# ---------------------------------------------------------------------------


async def test_submit_message_non_inquiry_stage(client: AsyncClient, db: AsyncSession) -> None:
    """非 inquiry 阶段不可提交消息（已有测试复现，P8-6 不改变此行为）。"""
    s = await _create_inquiry_session(client)
    session_id = s["session_id"]

    sid = uuid.UUID(session_id)
    result = await db.execute(select(ConsultSession).where(ConsultSession.id == sid))
    session = result.scalar_one()
    session.current_stage = "syndrome"
    await db.commit()

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/messages",
        json={"content": "不应成功", "role": "doctor"},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_STAGE_TRANSITION"


# ---------------------------------------------------------------------------
# state_version 兼容性
# ---------------------------------------------------------------------------


async def test_state_version_signature_unchanged(client: AsyncClient, db: AsyncSession) -> None:
    """X-State-Version 校验逻辑与 P3-2 一致。"""
    s = await _create_inquiry_session(client)

    # 正确版本
    headers: dict[str, str] = {"X-State-Version": "1", "X-Doctor-Id": _TEST_DOCTOR_ID}
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "version ok", "role": "doctor"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "SUCCESS"

    # 落后版本
    headers2: dict[str, str] = {"X-State-Version": "0", "X-Doctor-Id": _TEST_DOCTOR_ID}
    response2 = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "version behind", "role": "doctor"},
        headers=headers2,
    )
    assert response2.status_code == 409
    assert response2.json()["code"] == "INVALID_STATE_VERSION"
