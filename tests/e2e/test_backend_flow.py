"""P9-1 后端端到端测试。

覆盖主链路状态推进、关键异常流程、Redis Stream 事件、审计与状态版本一致性。
全部通过 fake agent / monkeypatch 运行，不依赖真实模型网关。

主流程：创建会话 → 提交问诊消息（Agent 回复落库）→ 完备性足够后 advance
→ 辨证 / 开方 / 加减 / 安全 → review 挂起 → 医师 confirm/modify → record → done
→ 查询/导出病历。

异常流程：
- 完备性不足不可 advance（回退 inquiry）
- 模型网关/Agent 失败时医生消息已保存但不伪造 Agent 回复
- 安全审核阻断或回退
- review 阶段不能绕过医生确认
- record 阶段缺少有效 doctor_review 不得生成最终病历
- state_version 冲突返回 INVALID_STATE_VERSION
- session lock 冲突返回 SESSION_BUSY
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.e2e.conftest import (
    E2E_DOCTOR_ID,
    build_fake_registry,
    cleanup_session_lock,
    cleanup_stream,
    count_audit_events,
    create_session,
    fetch_session_fresh,
    post_advance,
    read_stream_event_types,
    read_stream_events,
    submit_message,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# ===========================================================================
# 辅助：连续 advance 推进会话到 review 阶段
# ===========================================================================


async def _advance_to_review(client: AsyncClient, session_id: str) -> dict[str, Any]:
    """连续 advance，推进会话到 review 挂起状态，返回最后一次 advance 的 data。

    fake agents 链路：inquiry→sufficiency→syndrome→prescription→
    modification→safety→review。需调用多次 advance。
    """
    last_data: dict[str, Any] = {}
    for _ in range(10):
        resp = await post_advance(client, session_id)
        last_data = resp["data"]
        if last_data["current_stage"] == "review":
            return last_data
        if last_data["current_stage"] in ("blocked", "done"):
            return last_data
    return last_data


# ===========================================================================
# 主流程：完整问诊闭环
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_happy_path_full_pipeline(client: AsyncClient, db: AsyncSession) -> None:
    """端到端主流程：问诊 → 辨证 → 开方 → 加减 → 安全 → 复核 → 确认 → 病历 → 完成。

    校验：
    - 每阶段 state_version 单调递增
    - message.created / stage.changed / review.required / session.done 事件
    - 关键写操作有审计（message.created / doctor.reviewed / record.generated）
    - 最终病历落库并可导出
    """
    session_data = await create_session(client)
    session_id = session_data["session_id"]
    initial_version = 2  # create 后 state_version=2（P3-1 创建即递增一次）

    try:
        # 1. 提交问诊消息 → Agent 回复落库
        msg_resp = await submit_message(client, session_id)
        assert msg_resp["code"] == "SUCCESS"
        msg_data = msg_resp["data"]
        assert msg_data["agent_message"] is not None
        assert msg_data["agent_message"]["role"] == "agent"
        assert msg_data["agent_message"]["agent_name"] == "inquiry"
        # 完备性报告（fake sufficient=True）
        assert msg_data["sufficiency_report"]["sufficient"] is True
        version_after_msg = msg_data["state_version"]
        assert version_after_msg > initial_version

        # message.created 审计（医生 + agent 两条）
        audit_count = await count_audit_events(db, session_id, "message.created")
        assert audit_count >= 2

        # 2. advance 推进至 review 挂起
        adv_data = await _advance_to_review(client, session_id)
        assert adv_data["current_stage"] == "review"
        version_after_advance = adv_data["state_version"]
        assert version_after_advance > version_after_msg

        # review.required 事件 + stage.changed 事件
        event_types = await read_stream_event_types(session_id)
        assert "review.required" in event_types
        assert "stage.changed" in event_types

        # 3. 医师确认处方 → record 阶段
        review_resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/review",
            json={"action": "confirm"},
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert review_resp.status_code == 200, review_resp.text
        review_data = review_resp.json()["data"]
        assert review_data["action"] == "confirm"
        assert review_data["current_stage"] == "record"
        assert review_data["pending_review"] is False
        review_id = review_data["review_id"]
        version_after_review = review_data["state_version"]
        assert version_after_review > version_after_advance

        # doctor.reviewed 审计
        assert await count_audit_events(db, session_id, "doctor.reviewed") >= 1

        # 4. advance: record → done（生成病历）
        done_resp = await post_advance(
            client, session_id, state_version=version_after_review
        )
        assert done_resp["code"] == "SUCCESS"
        done_data = done_resp["data"]
        assert done_data["current_stage"] == "done"

        # session.done 事件
        events = await read_stream_events(session_id)
        done_events = [e for e in events if e["event_type"] == "session.done"]
        assert len(done_events) >= 1
        assert done_events[0]["payload"].get("record_id") is not None

        # record.generated 审计
        assert await count_audit_events(db, session_id, "record.generated") >= 1

        # 5. 查询病历
        record_resp = await client.get(
            f"/api/v1/consult/sessions/{session_id}/record",
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert record_resp.status_code == 200, record_resp.text
        record_data = record_resp.json()["data"]
        assert record_data["version"] == 1
        assert record_data["record_text"]
        assert record_data["disclaimer"]
        assert record_data["doctor_review_id"] == review_id

        # 6. 导出 txt / json / md
        for fmt in ("txt", "json", "md"):
            export_resp = await client.get(
                f"/api/v1/consult/sessions/{session_id}/record/export?format={fmt}",
                headers={"X-Doctor-Id": E2E_DOCTOR_ID},
            )
            assert export_resp.status_code == 200, f"export {fmt} failed: {export_resp.text}"
            assert export_resp.text

        # 最终会话状态校验
        session = await fetch_session_fresh(session_id)
        assert session.current_stage == "done"
        assert session.status == "done"
        assert session.state_version > initial_version
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 异常流程 1：完备性不足不可 advance（回退 inquiry）
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_insufficient_inquiry_rollback(client: AsyncClient, db: AsyncSession) -> None:
    """完备性不足时 advance 应拒绝推进（INSUFFICIENT_INQUIRY），不直接进入辨证。

    通过 monkeypatch 切换 fake registry 为 sufficient=False，
    验证 advance 后仍停留在 inquiry 阶段。
    """
    import app.agents.supervisor as sup_module
    import app.services.message as msg_module

    fake_registry = build_fake_registry(sufficient=False)
    _orig_msg = msg_module._default_inquiry_registry
    _orig_sup = sup_module._default_registry
    msg_module._default_inquiry_registry = lambda: fake_registry  # type: ignore[assignment]
    sup_module._default_registry = lambda: fake_registry  # type: ignore[assignment]

    try:
        session_data = await create_session(client)
        session_id = session_data["session_id"]

        try:
            # 提交问诊消息（fake sufficient=False）
            msg_resp = await submit_message(client, session_id)
            assert msg_resp["code"] == "SUCCESS"
            assert msg_resp["data"]["sufficiency_report"]["sufficient"] is False

            # advance：完备性不足，预校验直接拒绝（INSUFFICIENT_INQUIRY，400）
            adv_resp = await post_advance(
                client, session_id, expect_status=400
            )
            assert adv_resp["code"] == "INSUFFICIENT_INQUIRY"

            session = await fetch_session_fresh(session_id)
            # 不应进入 syndrome
            assert session.current_stage != "syndrome"
            assert session.current_stage == "inquiry"
        finally:
            await cleanup_stream(session_id)
            await cleanup_session_lock(session_id)
    finally:
        msg_module._default_inquiry_registry = _orig_msg  # type: ignore[assignment]
        sup_module._default_registry = _orig_sup  # type: ignore[assignment]


# ===========================================================================
# 异常流程 2：模型网关/Agent 失败时医生消息已保存但不伪造 Agent 回复
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_agent_failure_keeps_doctor_message(
    client: AsyncClient, db: AsyncSession
) -> None:
    """InquiryAgent 失败时：医生消息已落库，返回 AGENT_TRIGGER_FAILED，不伪造 Agent 回复。"""
    import app.services.message as msg_module

    fake_registry = build_fake_registry(inquiry_fail=True)
    _orig = msg_module._default_inquiry_registry
    msg_module._default_inquiry_registry = lambda: fake_registry  # type: ignore[assignment]

    try:
        session_data = await create_session(client)
        session_id = session_data["session_id"]

        try:
            # 提交消息 → Agent 失败
            resp = await client.post(
                f"/api/v1/consult/sessions/{session_id}/messages",
                json={
                    "content": "患者诉头痛3天，伴恶心",
                    "role": "doctor",
                },
                headers={"X-Doctor-Id": E2E_DOCTOR_ID},
            )
            assert resp.status_code == 503, resp.text
            body = resp.json()
            assert body["code"] == "AGENT_TRIGGER_FAILED"

            # 医生消息已落库（段 A 已 commit）
            from sqlalchemy import select

            from app.models.consult import ConsultMessage

            sid = uuid.UUID(session_id)
            result = await db.execute(
                select(ConsultMessage).where(
                    ConsultMessage.session_id == sid,
                    ConsultMessage.role == "doctor",
                )
            )
            doctor_msgs = result.scalars().all()
            assert len(doctor_msgs) >= 1
            assert any("头痛" in m.content for m in doctor_msgs)

            # 不存在伪造的 agent 回复
            agent_result = await db.execute(
                select(ConsultMessage).where(
                    ConsultMessage.session_id == sid,
                    ConsultMessage.role == "agent",
                )
            )
            agent_msgs = agent_result.scalars().all()
            assert len(agent_msgs) == 0

            # agent.failed 审计
            assert await count_audit_events(db, session_id, "agent.failed") >= 1
        finally:
            await cleanup_stream(session_id)
            await cleanup_session_lock(session_id)
    finally:
        msg_module._default_inquiry_registry = _orig  # type: ignore[assignment]


# ===========================================================================
# 异常流程 3：安全审核阻断或回退
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_safety_block_rollback(client: AsyncClient, db: AsyncSession) -> None:
    """安全审核未通过时回退 modification，不进入 review。

    直接构造一个 SAFETY 阶段会话，处方含党参 100g（超 max_dose=30），
    advance 后应回退 modification 并发射 safety.blocked 事件。
    """
    from app.models.consult import ConsultSession
    from app.schemas.agent import (
        FormulaResult,
        HerbDose,
        ModifiedFormulaResult,
    )

    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref="P9-1-E2E-SAFETY-BLOCK",
        patient_info={"gender": "male", "age": 45, "allergies": [], "pregnancy_status": "no"},
        current_stage="safety",
        status="active",
        state_version=1,
        rollback_counts={},
        created_by=E2E_DOCTOR_ID,
    )
    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试超量",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": 1,
        "pending_review": False,
        "rollback_counts": {},
    }
    db.add(session)
    await db.commit()

    session_id = str(session.id)
    try:
        # advance safety → 回退 modification，且不应调用真实模型网关
        adv_resp = await post_advance(client, session_id)
        assert adv_resp["code"] == "SUCCESS"
        adv_data = adv_resp["data"]
        assert adv_data["current_stage"] == "modification"

        # safety.blocked 事件
        event_types = await read_stream_event_types(session_id)
        assert "safety.blocked" in event_types

        # 不应进入 review
        assert "review.required" not in event_types

        # state_version 递增
        refreshed = await fetch_session_fresh(session_id)
        assert refreshed.state_version == 2
        assert refreshed.current_stage == "modification"
        # 回退后 safety rollback 计数已计入 snapshot（state.rollback_counts）
        snap = refreshed.state_snapshot or {}
        assert (snap.get("rollback_counts") or {}).get("safety", 0) >= 1
    finally:
        from sqlalchemy import delete

        from app.models.safety import SafetyRuleRun

        await db.execute(
            delete(SafetyRuleRun).where(SafetyRuleRun.session_id == session.id)
        )
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session.id))
        await db.commit()
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 异常流程 4：review 阶段不能绕过医生确认
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_review_cannot_bypass_doctor(client: AsyncClient, db: AsyncSession) -> None:
    """review 挂起后 advance 应被拒绝（PENDING_DOCTOR_REVIEW），不可绕过医生确认。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]

    try:
        # 推进至 review 挂起
        await submit_message(client, session_id)
        adv_data = await _advance_to_review(client, session_id)
        assert adv_data["current_stage"] == "review"

        # review 阶段再次 advance → 拒绝
        bypass_resp = await post_advance(
            client, session_id, expect_status=409
        )
        assert bypass_resp["code"] == "PENDING_DOCTOR_REVIEW"

        # 状态未改变
        session = await fetch_session_fresh(session_id)
        assert session.current_stage == "review"
        assert session.pending_review is True
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 异常流程 5：record 阶段缺少有效 doctor_review 不得生成最终病历
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_record_without_doctor_review_blocked(
    client: AsyncClient, db: AsyncSession
) -> None:
    """record 阶段无有效 doctor_review 时进入 blocked，不生成病历。"""
    from app.models.consult import ConsultSession

    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref="P9-1-E2E-NO-REVIEW",
        patient_info={"gender": "male", "age": 45},
        current_stage="record",
        status="active",
        state_version=1,
        rollback_counts={},
        created_by=E2E_DOCTOR_ID,
    )
    # 不写 doctor_review
    session.state_snapshot = {
        "current_stage": "record",
        "session_id": str(session.id),
        "state_version": 1,
        "pending_review": False,
        "rollback_counts": {},
        "patient_info": {"gender": "male", "age": 45},
    }
    db.add(session)
    await db.commit()

    session_id = str(session.id)
    try:
        adv_resp = await post_advance(client, session_id)
        assert adv_resp["code"] == "SUCCESS"
        adv_data = adv_resp["data"]
        assert adv_data["current_stage"] == "blocked"

        # 无病历落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord

        sid = uuid.UUID(session_id)
        result = await db.execute(
            select(MedicalRecord).where(MedicalRecord.session_id == sid)
        )
        assert result.scalar_one_or_none() is None

        # session.blocked 事件
        event_types = await read_stream_event_types(session_id)
        assert "session.blocked" in event_types
    finally:
        from sqlalchemy import delete

        await db.execute(delete(ConsultSession).where(ConsultSession.id == session.id))
        await db.commit()
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 异常流程 6：state_version 冲突返回 INVALID_STATE_VERSION
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_state_version_conflict(client: AsyncClient, db: AsyncSession) -> None:
    """X-State-Version 与服务端不一致时返回 INVALID_STATE_VERSION（409）。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]

    try:
        # 提交一个明显过时的版本号（服务端为 2，客户端传 999）
        resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json={"content": "测试版本冲突", "role": "doctor"},
            headers={
                "X-Doctor-Id": E2E_DOCTOR_ID,
                "X-State-Version": "999",
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "INVALID_STATE_VERSION"

        # 状态未改变
        session = await fetch_session_fresh(session_id)
        assert session.current_stage == "inquiry"
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 异常流程 7：session lock 冲突返回 SESSION_BUSY
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_session_lock_conflict(client: AsyncClient, db: AsyncSession) -> None:
    """会话锁被占用时返回 SESSION_BUSY（409），且不改状态。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]
    initial_version = session_data.get("state_version") or 2

    try:
        # 预占锁
        from app.core.redis import get_redis

        try:
            redis = await get_redis()
            await redis.set(
                f"xuanhu:session_lock:{session_id}", "other-trace", ex=60
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Redis integration dependency unavailable: {type(exc).__name__}: {exc}")

        # 提交消息 → SESSION_BUSY
        resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json={"content": "锁冲突测试", "role": "doctor"},
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "SESSION_BUSY"

        # 状态未变
        session = await fetch_session_fresh(session_id)
        assert session.state_version == initial_version
        assert session.current_stage == "inquiry"
    finally:
        await cleanup_session_lock(session_id)
        await cleanup_stream(session_id)


# ===========================================================================
# 主流程变体：医师修改处方（modify）路径
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_modify_formula_path(client: AsyncClient, db: AsyncSession) -> None:
    """医师修改处方后二次安全审核通过 → record → done。

    覆盖 PRD 验收样例"医师修改处方"。
    """
    session_data = await create_session(client)
    session_id = session_data["session_id"]

    try:
        await submit_message(client, session_id)
        adv_data = await _advance_to_review(client, session_id)
        assert adv_data["current_stage"] == "review"

        # modify：提供一个安全处方（党参 12g）
        modify_resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/review",
            json={
                "action": "modify",
                "formula_override": {
                    "name": "医师修改方",
                    "composition": [
                        {"herb": "党参", "dose": 12, "unit": "g"},
                        {"herb": "白术", "dose": 10, "unit": "g"},
                    ],
                    "rationale": "医师调整",
                },
                "feedback": "减量以保安全",
            },
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert modify_resp.status_code == 200, modify_resp.text
        modify_data = modify_resp.json()["data"]
        assert modify_data["action"] == "modify"
        assert modify_data["current_stage"] == "record"
        # modify 路径 safety_recheck.passed=True（二次审核通过）
        assert modify_data["safety_recheck"]["passed"] is True
        version_after_modify = modify_data["state_version"]

        # record → done
        done_resp = await post_advance(
            client, session_id, state_version=version_after_modify
        )
        assert done_resp["data"]["current_stage"] == "done"

        # 病历含修改记录
        record_resp = await client.get(
            f"/api/v1/consult/sessions/{session_id}/record",
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert record_resp.status_code == 200
        assert record_resp.json()["data"]["version"] == 1
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 主流程变体：医师否决处方（reject）回退
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_reject_formula_rollback(client: AsyncClient, db: AsyncSession) -> None:
    """医师否决处方后回退 prescription，pending_review 清除。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]

    try:
        await submit_message(client, session_id)
        adv_data = await _advance_to_review(client, session_id)
        assert adv_data["current_stage"] == "review"

        reject_resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/review",
            json={"action": "reject", "feedback": "证型不符，重新开方"},
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        assert reject_resp.status_code == 200, reject_resp.text
        reject_data = reject_resp.json()["data"]
        assert reject_data["action"] == "reject"
        assert reject_data["current_stage"] == "prescription"
        assert reject_data["pending_review"] is False

        session = await fetch_session_fresh(session_id)
        assert session.current_stage == "prescription"
        assert session.pending_review is False
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# 审计与状态版本一致性
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_audit_and_state_version_consistency(
    client: AsyncClient, db: AsyncSession
) -> None:
    """关键写操作必须有审计，state_version 单调递增。"""
    from sqlalchemy import select

    from app.models.audit import AuditEvent

    session_data = await create_session(client)
    session_id = session_data["session_id"]
    versions: list[int] = [session_data["state_version"]]

    try:
        # 提交消息
        msg_resp = await submit_message(client, session_id)
        versions.append(msg_resp["data"]["state_version"])

        # advance 至 review
        adv_data = await _advance_to_review(client, session_id)
        versions.append(adv_data["state_version"])

        # confirm
        review_resp = await client.post(
            f"/api/v1/consult/sessions/{session_id}/review",
            json={"action": "confirm"},
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        versions.append(review_resp.json()["data"]["state_version"])

        # record → done
        done_resp = await post_advance(
            client, session_id, state_version=versions[-1]
        )
        versions.append(done_resp["data"]["state_version"])

        # state_version 单调递增
        for i in range(1, len(versions)):
            assert versions[i] > versions[i - 1], f"版本非递增: {versions}"

        # 关键审计事件存在
        sid = uuid.UUID(session_id)
        result = await db.execute(
            select(AuditEvent.event_type).where(AuditEvent.session_id == sid)
        )
        audit_types = {r[0] for r in result.all()}
        assert "message.created" in audit_types
        assert "doctor.reviewed" in audit_types
        assert "record.generated" in audit_types
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)


# ===========================================================================
# Redis Stream 关键事件覆盖
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_redis_stream_key_events(client: AsyncClient, db: AsyncSession) -> None:
    """校验 Redis Stream 关键事件：message.created / stage.changed / review.required / session.done。"""
    session_data = await create_session(client)
    session_id = session_data["session_id"]

    try:
        await submit_message(client, session_id)
        await _advance_to_review(client, session_id)
        await client.post(
            f"/api/v1/consult/sessions/{session_id}/review",
            json={"action": "confirm"},
            headers={"X-Doctor-Id": E2E_DOCTOR_ID},
        )
        # confirm 后进入 record，再 advance 至 done
        session = await fetch_session_fresh(session_id)
        await post_advance(client, session_id, state_version=session.state_version)

        event_types = await read_stream_event_types(session_id)
        assert "message.created" in event_types
        assert "stage.changed" in event_types
        assert "review.required" in event_types
        assert "session.done" in event_types
    finally:
        await cleanup_stream(session_id)
        await cleanup_session_lock(session_id)
