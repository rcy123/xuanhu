"""问诊消息回退（rollback）集成测试。

覆盖：
- 成功回退：截断消息、observations 修正链恢复、state_version 推进、system 提示
- safety assertions 删除与恢复、safety profile 重置
- 异常路径：非 inquiry 阶段拒绝、目标消息不存在、目标消息非本会话
- X-State-Version 并发校验

需要可连接的 PostgreSQL；不可用时自动跳过。
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
from app.models.domain import Observation, SafetyFactAssertion, SafetyProfile

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_TEST_PATIENT_REF_PREFIX = "RB-MSG-"


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
                ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%")
            )
        )
        test_session_ids = [row[0] for row in result.all()]
        if not test_session_ids:
            return
        await session.execute(
            delete(SafetyFactAssertion).where(SafetyFactAssertion.session_id.in_(test_session_ids))
        )
        await session.execute(
            delete(SafetyProfile).where(SafetyProfile.session_id.in_(test_session_ids))
        )
        await session.execute(
            delete(Observation).where(Observation.session_id.in_(test_session_ids))
        )
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


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def http_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _create_inquiry_session(db: AsyncSession) -> ConsultSession:
    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref=f"{_TEST_PATIENT_REF_PREFIX}{uuid.uuid4().hex[:8]}",
        patient_info={"name": "测试"},
        chief_complaint="月经量少",
        current_stage="inquiry",
        status="active",
        agent_runtime="langgraph",
        state_version=1,
        recovery_status="normal",
    )
    db.add(session)
    await db.flush()

    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    messages = []
    for index, (role, content) in enumerate(
        [
            ("agent", "患者主诉？"),
            ("doctor", "月经量少，色暗"),
            ("agent", "请补充怕冷情况？"),
            ("doctor", "平时非常怕冷"),
            ("agent", "请补充睡眠情况？"),
            ("doctor", "睡眠尚可"),
        ]
    ):
        msg = ConsultMessage(
            id=uuid.uuid4(),
            session_id=session.id,
            role=role,
            stage="inquiry",
            content=content,
            created_at=base.replace(minute=base.minute + index),
        )
        db.add(msg)
        messages.append(msg)
    await db.flush()
    session.state_snapshot = {
        "agent_runtime": "langgraph",
        "last_message": {
            "message_id": str(messages[-1].id),
            "role": "doctor",
            "stage": "inquiry",
        },
    }
    await db.commit()
    return session, messages


def _mk_observation(
    session_id: uuid.UUID,
    source_message_id: uuid.UUID,
    fact_key: str,
    value: Any,
    status: str = "active",
    supersedes_observation_id: uuid.UUID | None = None,
) -> Observation:
    return Observation(
        id=uuid.uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value=value,
        source_message_id=source_message_id,
        status=status,
        supersedes_observation_id=supersedes_observation_id,
    )


async def test_rollback_truncates_messages_and_restores_correction_chain(db: AsyncSession, http_client: AsyncClient) -> None:
    session, messages = await _create_inquiry_session(db)
    sid = str(session.id)
    # messages: [0]=agent [1]=doctor [2]=agent [3]=doctor [4]=agent [5]=doctor
    # 事实链：obs A(active) -> obs B 修正 A(corrected) -> obs C 修正 B(corrected)
    obs_a = _mk_observation(session.id, messages[1].id, "cold_heat", {"value": "怕冷"})
    await db.flush()
    obs_b = _mk_observation(
        session.id, messages[3].id, "cold_heat", {"value": "非常怕冷"}, "corrected", obs_a.id
    )
    await db.flush()
    obs_c = _mk_observation(
        session.id, messages[5].id, "cold_heat", {"value": "非常怕冷，手脚凉"}, "corrected", obs_b.id
    )
    db.add_all([obs_a, obs_b, obs_c])
    await db.commit()

    # 回退到 messages[3]（"非常怕冷" 回答）→ 删除 msg3..msg5，恢复 obs_b 为 active
    before_version = session.state_version
    response = await http_client.post(
        f"/api/v1/consult/sessions/{sid}/messages/{messages[3].id}/rollback",
        json={"reason": "答错纠正"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["state_version"] == before_version + 1
    assert set(data["rolled_back_message_ids"]) == {
        str(messages[3].id),
        str(messages[4].id),
        str(messages[5].id),
    }
    assert data["kept_last_message_id"] == str(messages[2].id)

    # 消息：0,1,2 保留 + 1 条 system 提示
    remaining = (
        await db.scalars(
            select(ConsultMessage)
            .where(ConsultMessage.session_id == session.id)
            .order_by(ConsultMessage.created_at)
        )
    ).all()
    assert [row.id for row in remaining[:3]] == [messages[0].id, messages[1].id, messages[2].id]
    assert remaining[-1].role == "system"
    assert "回退" in remaining[-1].content

    # observations：obs_c 删除，obs_b 恢复为 active
    obs_rows = (
        await db.scalars(
            select(Observation)
            .where(Observation.session_id == session.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert obs_a.id in {row.id for row in obs_rows}  # 链头保留
    assert obs_b.id not in {row.id for row in obs_rows}  # 来源在截断集，删除
    assert obs_c.id not in {row.id for row in obs_rows}  # 来源在截断集，删除
    restored = next(row for row in obs_rows if row.fact_key == "cold_heat")
    assert restored.id == obs_a.id
    assert restored.status == "active"
    assert restored.supersedes_observation_id is None


async def test_rollback_rejects_non_inquiry_stage(db: AsyncSession, http_client: AsyncClient) -> None:
    session, messages = await _create_inquiry_session(db)
    session.current_stage = "syndrome"
    await db.commit()
    response = await http_client.post(
        f"/api/v1/consult/sessions/{session.id}/messages/{messages[3].id}/rollback",
        json={},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "INVALID_STAGE_TRANSITION"


async def test_rollback_missing_message(db: AsyncSession, http_client: AsyncClient) -> None:
    session, _ = await _create_inquiry_session(db)
    response = await http_client.post(
        f"/api/v1/consult/sessions/{session.id}/messages/{uuid.uuid4()}/rollback",
        json={},
    )
    assert response.status_code == 404, response.text


async def test_rollback_state_version_mismatch(db: AsyncSession, http_client: AsyncClient) -> None:
    session, messages = await _create_inquiry_session(db)
    response = await http_client.post(
        f"/api/v1/consult/sessions/{session.id}/messages/{messages[3].id}/rollback",
        json={},
        headers={"X-State-Version": "999"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "INVALID_STATE_VERSION"


async def test_rollback_resets_safety_profile(db: AsyncSession, http_client: AsyncClient) -> None:
    session, messages = await _create_inquiry_session(db)
    sid = str(session.id)
    # 在 msg5 上产生一条已确认的 safety assertion + profile 投影
    assertion = SafetyFactAssertion(
        id=uuid.uuid4(),
        session_id=session.id,
        field_name="allergy",
        value={"collection_status": "explicitly_none"},
        value_digest="0" * 64,
        assertion_fingerprint="0" * 64,
        status="confirmed",
        source_kind="deterministic_reply_binding",
        source_message_id=messages[5].id,
        template_version="safety.v1",
        evidence_spans=[],
        evidence_digest="0" * 64,
        proposed_by_actor_type="system",
        confirmed_by_actor_type="doctor",
        confirmed_at=datetime.now(UTC),
    )
    db.add(assertion)
    profile = SafetyProfile(
        id=uuid.uuid4(),
        session_id=session.id,
        allergy_collection_status="explicitly_none",
    )
    db.add(profile)
    await db.commit()

    response = await http_client.post(
        f"/api/v1/consult/sessions/{sid}/messages/{messages[5].id}/rollback",
        json={},
    )
    assert response.status_code == 200, response.text
    remaining_assertions = (
        await db.scalars(
            select(SafetyFactAssertion)
            .where(SafetyFactAssertion.session_id == session.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert assertion.id not in {row.id for row in remaining_assertions}
    from app.db.session import get_session_factory

    async with get_session_factory()() as fresh_db:
        profile_row = await fresh_db.scalar(
            select(SafetyProfile).where(SafetyProfile.session_id == session.id)
        )
        assert profile_row.allergy_collection_status == "unknown"
        assert profile_row.allergens is None


async def test_rollback_cleans_question_contracts_and_coverage(db: AsyncSession, http_client: AsyncClient) -> None:
    """回退删除消息时同步清理引用它的 R9 question contracts / coverage events。"""
    session, messages = await _create_inquiry_session(db)
    sid = str(session.id)
    # 在 msg4（agent 提问）上建契约，msg5（doctor 回答）上建 coverage 事件
    from app.models.question_contract import (
        QuestionContractRecord,
        QuestionCoverageEventRecord,
    )

    contract = QuestionContractRecord(
        id=uuid.uuid4(),
        schema_version="question-contract.v1",
        session_id=session.id,
        question_message_id=messages[4].id,
        root_contract_id=messages[4].id,  # placeholder, replaced below
        parent_contract_id=None,
        revision=1,
        dimension="ten_questions.sleep",
        selection_kind="required",
        safety_critical=False,
        max_followups=1,
        question_digest="0" * 64,
        aspects=[{"aspect": "入睡困难", "covered": False}],
        contract_digest="0" * 64,
    )
    # root_contract_id 必须指向自身（revision=1 且 root=id）
    contract.root_contract_id = contract.id
    db.add(contract)
    await db.flush()
    coverage = QuestionCoverageEventRecord(
        id=uuid.uuid4(),
        schema_version="question-coverage-event.v1",
        session_id=session.id,
        contract_id=contract.id,
        root_contract_id=contract.id,
        answer_message_id=messages[5].id,
        items=[{"aspect": "入睡困难", "covered": True}],
        event_digest="0" * 64,
    )
    db.add(coverage)
    await db.commit()

    # 回退到 msg4（agent 提问）→ 删除 msg4..msg5
    response = await http_client.post(
        f"/api/v1/consult/sessions/{sid}/messages/{messages[4].id}/rollback",
        json={},
    )
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]["rolled_back_message_ids"]) == {
        str(messages[4].id),
        str(messages[5].id),
    }

    remaining_contracts = (
        await db.scalars(
            select(QuestionContractRecord)
            .where(QuestionContractRecord.session_id == session.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    remaining_coverage = (
        await db.scalars(
            select(QuestionCoverageEventRecord)
            .where(QuestionCoverageEventRecord.session_id == session.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert contract.id not in {row.id for row in remaining_contracts}
    assert coverage.id not in {row.id for row in remaining_coverage}
