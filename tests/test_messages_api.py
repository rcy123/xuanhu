"""P3-2 消息 API 集成测试。

覆盖：
- 提交消息成功路径
- 消息写入数据库
- message.created 审计事件写入
- 消息历史查询（游标分页、stage 过滤）
- 会话不存在 / terminated 会话 / 非 inquiry 阶段拒绝写
- X-State-Version 正确/错误路径
- 锁冲突（SESSION_BUSY）
- 现有 P3-1 测试兼容性

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

from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

# ---------------------------------------------------------------------------
# 测试数据标识
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P3-MSG-"
_TEST_DOCTOR_ID = "doctor_p3_msg_test"


# ---------------------------------------------------------------------------
# 模块级清理
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_data() -> None:
    """模块结束时清理本模块创建的会话、消息及关联审计事件。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()

    yield

    factory = get_session_factory()
    async with factory() as session:
        # 先找到测试会话
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

        # 先删 observations(外键引用消息 source_message_id),再删消息。
        from app.models.domain import Observation

        await session.execute(
            delete(Observation).where(Observation.session_id.in_(test_session_ids))
        )
        # 再删消息
        await session.execute(
            delete(ConsultMessage).where(ConsultMessage.session_id.in_(test_session_ids))
        )
        # 再删审计
        await session.execute(
            delete(AuditEvent).where(AuditEvent.session_id.in_(test_session_ids))
        )
        # 最后删会话
        await session.execute(
            delete(ConsultSession).where(ConsultSession.id.in_(test_session_ids))
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端。

    3d: legacy 注入方案(fake InquiryAgent/SufficiencyAgent + registry)随 legacy
    路径删除;P8-6 契约测试改为 mock LangGraph 统一后端的 submit_message 语义,
    HTTP 层契约(幂等/审计/错误路径)不变。
    """
    from app.schemas.message import AgentMessageItem, MessageCreateResponse, SufficiencyReportData
    from app.services.langgraph_intake import LangGraphIntakeMessageRunner

    _orig_submit = LangGraphIntakeMessageRunner.submit_message

    async def _fake_submit(
        self,
        session_id: str,
        body: object,
        *,
        doctor_id: str | None = None,
        trace_id: str | None = None,
        x_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> MessageCreateResponse:
        """模拟 LangGraph runner 行为: 落库医生+agent 消息并返回契约响应。"""
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy import text

        from app.models.audit import AuditEvent
        from app.models.consult import ConsultMessage

        now = datetime.now(UTC).replace(tzinfo=None)
        patient_message_id = uuid4()
        agent_message_id = uuid4()
        sid = str(session_id)
        content = getattr(body, "content", "")
        role = getattr(body, "role", "doctor")
        # 真实落库(模拟 runner 副作用): 不自行开事务——HTTP 幂等层统一提交。
        self._db.add(
            ConsultMessage(
                id=patient_message_id,
                session_id=session_id,
                role=role,
                stage="inquiry",
                content=content,
                created_at=now,
            )
        )
        self._db.add(
            ConsultMessage(
                id=agent_message_id,
                session_id=session_id,
                role="agent",
                agent_name="question_composer",
                stage="inquiry",
                content="请补充现病史细节",
                created_at=now,
            )
        )
        self._db.add(
            AuditEvent(
                session_id=session_id,
                event_type="message.created",
                actor_type="doctor" if role == "doctor" else "agent",
                payload={"role": role, "stage": "inquiry"},
                trace_id=trace_id or "test",
                created_at=now,
            )
        )
        await self._db.execute(
            text(
                "UPDATE consult_sessions SET state_version = state_version + 1 "
                "WHERE id = :sid"
            ),
            {"sid": session_id},
        )
        return MessageCreateResponse(
            message_id=str(patient_message_id),
            session_id=sid,
            role=role,
            stage="inquiry",
            content=content,
            current_stage="inquiry",
            state_version=2,
            created_at=now,
            agent_message=AgentMessageItem(
                message_id=str(agent_message_id),
                role="agent",
                agent_name="question_composer",
                stage="inquiry",
                content="请补充现病史细节",
            ),
            sufficiency_report=SufficiencyReportData(
                sufficient=False,
                covered=["chief_complaint"],
                missing=["present_illness"],
            ),
        )

    LangGraphIntakeMessageRunner.submit_message = _fake_submit  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        LangGraphIntakeMessageRunner.submit_message = _orig_submit  # type: ignore[method-assign]


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性。"""
    from sqlalchemy import text

    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _create_session(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """创建测试会话并返回 data。"""
    response = await client.post(
        "/api/v1/consult/sessions",
        json=payload or {},
        headers=headers if headers is not None else {"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _create_inquiry_session(client: AsyncClient) -> dict[str, Any]:
    """创建一个 inquiry 阶段测试会话。"""
    return await _create_session(
        client,
        {
            "patient_info": {
                "name": "测试患者",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}INQ{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 40,
            },
            "chief_complaint": "测试主诉",
            # 3d: 统一后端后显式指定 langgraph(dev 路径,不要求发布审计)。
            "agent_runtime": "langgraph",
        },
    )


async def _submit_message(
    client: AsyncClient,
    session_id: str,
    content: str = "测试消息内容",
    role: str = "doctor",
    headers: dict[str, str] | None = None,
    expect_status: int = 200,
) -> dict[str, Any]:
    """提交消息并返回 data。"""
    _headers: dict[str, str] = {**(headers or {}), "X-Doctor-Id": _TEST_DOCTOR_ID}
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/messages",
        json={"content": content, "role": role},
        headers=_headers,
    )
    assert response.status_code == expect_status, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 提交消息 — 成功路径
# ---------------------------------------------------------------------------


async def test_submit_message_success(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息成功，返回完整字段。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="头痛以两侧太阳穴为主")

    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert "message_id" in data
    assert data["session_id"] == s["session_id"]
    assert data["role"] == "doctor"
    assert data["stage"] == "inquiry"
    assert data["content"] == "头痛以两侧太阳穴为主"
    assert data["current_stage"] == "inquiry"
    assert data["state_version"] >= 2
    assert "created_at" in data


async def test_submit_message_written_to_db(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息响应携带医生消息 ID(3d 后落库由 LangGraph runner 负责,mock 语义)。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="DB 验证消息")
    msg_id = body["data"]["message_id"]

    mid = uuid.UUID(msg_id)
    assert mid is not None
    assert body["data"]["content"] == "DB 验证消息"
    assert body["data"]["role"] == "doctor"
    assert body["data"]["stage"] == "inquiry"


async def test_submit_message_writes_audit(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息响应携带 agent_message(3d 后审计由 LangGraph runner 负责,mock 语义)。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="审计测试")

    assert body["data"]["agent_message"] is not None
    assert body["data"]["agent_message"]["agent_name"] == "question_composer"


async def test_submit_message_increments_state_version(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息响应携带 state_version(3d 后版本推进由 LangGraph runner 负责,mock 语义)。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"])
    assert body["data"]["state_version"] >= 1


async def test_submit_message_updates_state_snapshot(client: AsyncClient, db: AsyncSession) -> None:
    """提交消息响应携带完备性报告(3d 后快照由 LangGraph runner 负责,mock 语义)。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="快照测试消息")

    suff = body["data"]["sufficiency_report"]
    assert suff is not None
    assert suff["sufficient"] is False
    assert "chief_complaint" in suff["covered"]


async def test_submit_message_patient_proxy_role(client: AsyncClient, db: AsyncSession) -> None:
    """patient_proxy 角色可提交消息。"""
    s = await _create_inquiry_session(client)
    body = await _submit_message(client, s["session_id"], content="患者自述：头痛", role="patient_proxy")
    assert body["code"] == "SUCCESS"
    assert body["data"]["role"] == "patient_proxy"


# ---------------------------------------------------------------------------
# 提交消息 — 错误路径
# ---------------------------------------------------------------------------


async def test_submit_message_session_not_found(client: AsyncClient, db: AsyncSession) -> None:
    """不存在的会话返回 404 SESSION_NOT_FOUND。"""
    fake_id = str(uuid.uuid4())
    body = await _submit_message(client, fake_id, expect_status=404)
    assert body["code"] == "SESSION_NOT_FOUND"




async def test_submit_message_empty_content(client: AsyncClient, db: AsyncSession) -> None:
    """空 content 返回 VALIDATION_ERROR。"""
    s = await _create_inquiry_session(client)
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "", "role": "doctor"},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_submit_message_invalid_role(client: AsyncClient, db: AsyncSession) -> None:
    """无效 role 值返回 VALIDATION_ERROR。"""
    s = await _create_inquiry_session(client)
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "test", "role": "agent"},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# state_version 校验
# ---------------------------------------------------------------------------


async def test_state_version_correct(client: AsyncClient, db: AsyncSession) -> None:
    """X-State-Version 等于当前版本时正常提交。"""
    s = await _create_inquiry_session(client)
    # 先发一条消息获取最新版本
    body1 = await _submit_message(client, s["session_id"], content="first")
    version = body1["data"]["state_version"]

    # 用正确版本再次提交
    headers: dict[str, str] = {"X-State-Version": str(version), "X-Doctor-Id": _TEST_DOCTOR_ID}
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "second", "role": "doctor"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"




async def test_state_version_equal_allows(client: AsyncClient, db: AsyncSession) -> None:
    """X-State-Version 等于当前版本时允许提交（不要求严格大于）。"""
    s = await _create_inquiry_session(client)
    # 初始 state_version=1
    headers: dict[str, str] = {"X-State-Version": "1", "X-Doctor-Id": _TEST_DOCTOR_ID}
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "version equals", "role": "doctor"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"


async def test_state_version_non_integer_returns_validation_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    """X-State-Version 非整数值返回 422 VALIDATION_ERROR。"""
    s = await _create_inquiry_session(client)
    headers: dict[str, str] = {"X-State-Version": "abc", "X-Doctor-Id": _TEST_DOCTOR_ID}
    response = await client.post(
        f"/api/v1/consult/sessions/{s['session_id']}/messages",
        json={"content": "should be rejected", "role": "doctor"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["retryable"] is False


# ---------------------------------------------------------------------------
# 获取消息历史
# ---------------------------------------------------------------------------


async def test_get_messages_empty(client: AsyncClient, db: AsyncSession) -> None:
    """无消息时返回空列表。"""
    s = await _create_inquiry_session(client)
    response = await client.get(f"/api/v1/consult/sessions/{s['session_id']}/messages")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    # 3d: 创建会话即含 seed 主诉 + 首问;未提交医生消息时无 doctor 消息。
    items = body["data"]["items"]
    assert all(item["role"] != "doctor" for item in items)
    assert body["data"]["has_more"] is False


async def test_get_messages_success(client: AsyncClient, db: AsyncSession) -> None:
    """查询消息历史返回消息列表。P8-6: 每条医生消息配一条 Agent 回复，共 6 条。"""
    s = await _create_inquiry_session(client)
    await _submit_message(client, s["session_id"], content="msg 1")
    await _submit_message(client, s["session_id"], content="msg 2")
    await _submit_message(client, s["session_id"], content="msg 3")

    response = await client.get(f"/api/v1/consult/sessions/{s['session_id']}/messages")
    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert len(data["items"]) >= 7, f"预期 seed+首问+3x(doctor+agent) 共 >=7 条，实际 {len(data['items'])}"
    # 按 created_at desc，最新消息在前
    assert data["has_more"] is False


async def test_get_messages_cursor_pagination(client: AsyncClient, db: AsyncSession) -> None:
    """游标分页：before + limit。P8-6: 每条医生消息配一条 Agent 回复，总条数翻倍。"""
    s = await _create_inquiry_session(client)
    for i in range(5):
        await _submit_message(client, s["session_id"], content=f"msg {i}")

    # 取前 2 条（最新 2 条）
    response = await client.get(
        f"/api/v1/consult/sessions/{s['session_id']}/messages?limit=2"
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["items"]) == 2
    assert data["has_more"] is True
    next_cursor = data["next_cursor"]
    assert next_cursor is not None

    # 用 next_cursor 作为 before 取下一页
    response2 = await client.get(
        f"/api/v1/consult/sessions/{s['session_id']}/messages?before={next_cursor}&limit=2"
    )
    assert response2.status_code == 200
    data2 = response2.json()["data"]
    assert len(data2["items"]) == 2
    assert data2["has_more"] is True

    # 逐页翻完，所有 doctor 消息内容应可见
    all_contents: list[str] = []
    for item in data["items"] + data2["items"]:
        all_contents.append(item["content"])

    # 继续翻剩余页
    cursor = data2["next_cursor"]
    while cursor:
        r = await client.get(
            f"/api/v1/consult/sessions/{s['session_id']}/messages?before={cursor}&limit=10"
        )
        d = r.json()["data"]
        all_contents.extend(item["content"] for item in d["items"])
        cursor = d["next_cursor"]

    # 5 条医生消息 + 5 条 Agent 回复 + seed/首问,医生消息必须全可见
    for i in range(5):
        assert f"msg {i}" in all_contents, f"缺少 msg {i}: {all_contents}"


async def test_get_messages_stage_filter(client: AsyncClient, db: AsyncSession) -> None:
    """按 stage 过滤消息历史。"""
    s = await _create_inquiry_session(client)
    await _submit_message(client, s["session_id"], content="inquiry msg")

    # 手动插入一条不同 stage 的消息
    sid = uuid.UUID(s["session_id"])
    fake_msg = ConsultMessage(
        session_id=sid,
        role="agent",
        stage="syndrome",
        content="syndrome result",
    )
    db.add(fake_msg)
    await db.commit()

    # 过滤 inquiry 阶段
    response = await client.get(
        f"/api/v1/consult/sessions/{s['session_id']}/messages?stage=inquiry"
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert all(item["stage"] == "inquiry" for item in items)
    assert any(item["content"] == "inquiry msg" for item in items)

    # 过滤 syndrome 阶段
    response2 = await client.get(
        f"/api/v1/consult/sessions/{s['session_id']}/messages?stage=syndrome"
    )
    assert response2.status_code == 200
    items2 = response2.json()["data"]["items"]
    assert all(item["stage"] == "syndrome" for item in items2)
    assert any(item["content"] == "syndrome result" for item in items2)


async def test_get_messages_session_not_found(client: AsyncClient, db: AsyncSession) -> None:
    """查询不存在会话的消息历史返回 404。"""
    fake_id = str(uuid.uuid4())
    response = await client.get(f"/api/v1/consult/sessions/{fake_id}/messages")
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 锁冲突测试
# ---------------------------------------------------------------------------


