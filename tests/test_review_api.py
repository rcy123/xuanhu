"""P7-1 医师确认 API 测试。

覆盖 confirm / modify 通过 / modify 阻断 / reject / 非法 action /
缺 formula_override / 非 review 阶段拒绝 / state_version 冲突 / 会话锁冲突。

本测试为集成测试，需要可连接的 PostgreSQL + Redis；不可用时自动跳过。
依赖 DB 中已导入的 herbs/dosage_units 种子数据（党参已存在，max_dose=30）。
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.review import DoctorReview
from app.models.safety import SafetyRuleRun

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# ---------------------------------------------------------------------------
# 模块级测试数据
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P7-1-REVIEW-"
_TEST_DOCTOR_ID = "doctor_p7_1_review"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_sessions() -> None:
    """模块结束时清理本模块创建的会话及关联数据。

    B-012: 不在 fixture setup 阶段调用 reset_session_factory()。
    只由 _check_postgres 负责引擎初始化，避免与后续测试模块的
    全局 engine 状态冲突。
    """
    yield

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            await session.execute(select(ConsultSession.id).limit(1))
        except Exception:  # noqa: BLE001
            return

        # 清理 doctor_reviews / safety_rule_runs / audit_events / consult_sessions
        session_ids_subq = select(ConsultSession.id).where(
            or_(
                ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                ConsultSession.created_by == _TEST_DOCTOR_ID,
            )
        )
        await session.execute(
            delete(DoctorReview).where(
                DoctorReview.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(SafetyRuleRun).where(
                SafetyRuleRun.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(AuditEvent).where(
                AuditEvent.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(ConsultSession).where(
                or_(
                    ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                    ConsultSession.created_by == _TEST_DOCTOR_ID,
                )
            )
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
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性，并为本模块初始化 session factory。

    B-012: 旧实现在 setup 阶段调用 reset_session_factory() 会 dispose
    全局 engine，导致与其他模块组合运行时出现"已关闭连接被复用"的
    `AttributeError: 'NoneType' object has no attribute 'send'`，被 broad
    except 包装成 skip。现在改为：先确保 settings 已加载，再获取工厂
    执行 SELECT 1；只有真正的连接失败类异常才 skip，其他异常直接抛出
    让测试失败而不是被掩盖。
    """
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    # 确保配置已加载（不 cache_clear，避免与其他模块争用）
    get_settings()

    # 若上一模块已 dispose 全局 engine，reset 重建以保证本模块可用
    await reset_session_factory()
    factory = get_session_factory()

    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except (OSError, ConnectionError) as exc:
        pytest.skip(
            f"PostgreSQL 不可用，跳过集成测试: {type(exc).__name__}: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        # 连接类以外的异常（如已关闭 engine、asyncpg 内部错误）不掩盖，
        # 让测试失败暴露真实问题。
        pytest.fail(
            f"PostgreSQL 检查出现非连接类异常，不应被 skip: "
            f"{type(exc).__name__}: {exc}",
            pytrace=True,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_review_session(
    db: AsyncSession,
    *,
    stage: str = "review",
    status: str = "pending_review",
    pending_review: bool = True,
    formula_overrides: dict[str, Any] | None = None,
    safety_passed: bool = True,
) -> ConsultSession:
    """创建一个处于 review 阶段的会话，含完整 state_snapshot。

    使用党参（已知安全药材，max_dose=30）构造一个通过/不通过安全审核的处方。
    """
    session_id = uuid.uuid4()

    # 安全处方：党参 12g（在 max_dose 30g 以内）
    safe_composition = [{"herb": "党参", "dose": 12, "unit": "g"}]
    # 不安全处方：党参 100g（严重超量，触发 blocker）
    unsafe_composition = [{"herb": "党参", "dose": 100, "unit": "g"}]

    composition = unsafe_composition if not safety_passed else safe_composition
    modified_formula = {
        "formula": {
            "name": "四君子汤" if safety_passed else "超量方",
            "composition": composition,
            "rationale": "健脾益气" if safety_passed else "测试",
        },
        "modifications": [],
    }

    snapshot: dict[str, Any] = {
        "current_stage": stage,
        "pending_review": pending_review,
        "state_version": 1,
        "rollback_counts": {},
        "patient_info": {
            "gender": "male",
            "age": 35,
            "allergies": [],
            "pregnancy_status": "no",
        },
        "modified_formula": modified_formula,
        "base_formula": modified_formula["formula"],
        "safety_rule_result": {
            "passed": safety_passed,
            "issues": [] if safety_passed else [
                {
                    "type": "dose_limit",
                    "severity": "blocker",
                    "herbs": ["党参"],
                    "rule_source": "《中国药典》",
                    "suggestion": "党参剂量 100.0g 超过上限 30.0g（严重超量）",
                }
            ],
            "normalized_formula": modified_formula["formula"],
            "warnings": [],
            "rule_version": "v1.0.0",
            "execution_order": ["normalize", "convert_dose", "dose_limit"],
        },
        "safety_review": {
            "passed": safety_passed,
            "issues": [] if safety_passed else [
                {
                    "type": "dose_limit",
                    "severity": "blocker",
                    "herbs": ["党参"],
                    "rule_source": "《中国药典》",
                    "suggestion": "党参剂量超限",
                }
            ],
            "rollback_target": "none" if safety_passed else "modification",
            "summary": "安全审核通过" if safety_passed else "安全审核未通过",
        },
    }

    if formula_overrides:
        snapshot.update(formula_overrides)

    session = ConsultSession(
        id=session_id,
        patient_ref=f"{_TEST_PATIENT_REF_PREFIX}{session_id.hex[:8]}",
        patient_info=snapshot["patient_info"],
        current_stage=stage,
        status=status,
        pending_review=pending_review,
        state_version=1,
        rollback_counts={},
        state_snapshot=snapshot,
        created_by=_TEST_DOCTOR_ID,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _insert_safety_rule_run(
    db: AsyncSession, session_id: uuid.UUID, *, passed: bool
) -> uuid.UUID:
    """为会话插入一条 safety_rule_run 记录（模拟 P6-3/P6-4 的安全审核留痕）。"""
    run = SafetyRuleRun(
        session_id=session_id,
        agent_run_id=None,
        formula_source="agent_output",
        passed=passed,
        issues=[] if passed else [
            {"type": "dose_limit", "severity": "blocker", "herbs": ["党参"]}
        ],
        formula_snapshot={
            "name": "四君子汤" if passed else "超量方",
            "composition": [{"herb": "党参", "dose": 12 if passed else 100, "unit": "g"}],
        },
        normalized_formula={
            "name": "四君子汤",
            "composition": [{"herb": "党参", "dose": 12, "unit": "g"}],
        },
        patient_snapshot={"gender": "male", "age": 35},
        rule_version="v1.0.0",
        trace_id="test-trace",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run.id


def _review_headers(
    *, doctor_id: str = _TEST_DOCTOR_ID, state_version: int | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {"X-Doctor-Id": doctor_id}
    if state_version is not None:
        headers["X-State-Version"] = str(state_version)
    return headers


async def _count_doctor_reviews(
    db: AsyncSession, session_id: str, action: str | None = None
) -> int:
    sid = uuid.UUID(session_id)
    stmt = select(DoctorReview).where(DoctorReview.session_id == sid)
    if action is not None:
        stmt = stmt.where(DoctorReview.action == action)
    result = await db.execute(stmt)
    return len(result.scalars().all())


async def _count_audit_events(
    db: AsyncSession, session_id: str, event_type: str
) -> int:
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == event_type,
        )
    )
    return len(result.scalars().all())


async def _has_no_medical_record(db: AsyncSession, session_id: str) -> bool:
    """B-013: 确认会话尚未生成 medical_records。"""
    from app.models.review import MedicalRecord

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(MedicalRecord).where(MedicalRecord.session_id == sid)
    )
    return result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# confirm 路径
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_confirm_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """confirm 成功：推进到 record 阶段，写入 doctor_reviews 和 audit。

    B-013: confirm 不生成病历，session.status 保持 active（非 done），
    供 P7-2 病历生成 Agent 从 record 阶段接续。
    """
    session = await _create_review_session(db, safety_passed=True)
    await _insert_safety_rule_run(db, session.id, passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == "SUCCESS"
        data = body["data"]
        assert data["action"] == "confirm"
        assert data["current_stage"] == "record"
        # B-013: 未生成病历，status 不得为 done
        assert data["status"] == "active"
        assert data["pending_review"] is False
        assert data["review_id"]
        assert data["state_version"] == 2

        # 验证 DB
        await db.refresh(session)
        assert session.current_stage == "record"
        assert session.status == "active"
        assert session.pending_review is False
        assert session.state_version == 2

        # B-013: 不存在 medical_records 时不得为 done
        assert await _has_no_medical_record(db, str(session.id))

        # 验证 doctor_reviews
        review_count = await _count_doctor_reviews(db, str(session.id), "confirm")
        assert review_count == 1

        # 验证 audit_events
        audit_count = await _count_audit_events(db, str(session.id), "doctor.reviewed")
        assert audit_count == 1
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_confirm_without_safety_result_rejected(
    client: AsyncClient, db: AsyncSession
) -> None:
    """confirm 时缺 safety_review/safety_rule_result → 拒绝。"""
    # 创建一个 review 阶段但 snapshot 中无安全审核结果的会话
    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref=f"{_TEST_PATIENT_REF_PREFIX}no-safety",
        patient_info={"gender": "male", "age": 35},
        current_stage="review",
        status="pending_review",
        pending_review=True,
        state_version=1,
        rollback_counts={},
        state_snapshot={
            "current_stage": "review",
            "pending_review": True,
            "state_version": 1,
        },
        created_by=_TEST_DOCTOR_ID,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "INVALID_STAGE_TRANSITION"

        # 未写 doctor_reviews
        review_count = await _count_doctor_reviews(db, str(session.id))
        assert review_count == 0
    finally:
        pass


# ---------------------------------------------------------------------------
# modify 路径
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modify_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """modify 成功：二次安全审核通过，写入 doctor_reviews，推进到 record。

    B-013: modify 不生成病历，session.status 保持 active（非 done），
    供 P7-2 病历生成 Agent 从 record 阶段接续。
    """
    session = await _create_review_session(db, safety_passed=True)
    await _insert_safety_rule_run(db, session.id, passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={
                "action": "modify",
                "formula_override": {
                    "name": "四君子汤加减",
                    "composition": [
                        {"herb": "党参", "dose": 10, "unit": "g"},
                        {"herb": "白术", "dose": 10, "unit": "g"},
                    ],
                },
                "feedback": "党参减量，加白术",
            },
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        assert data["action"] == "modify"
        assert data["current_stage"] == "record"
        # B-013: 未生成病历，status 不得为 done
        assert data["status"] == "active"
        assert data["pending_review"] is False
        assert data["formula_override"] is not None
        assert data["safety_recheck"]["passed"] is True

        # 验证 DB
        await db.refresh(session)
        assert session.current_stage == "record"
        assert session.status == "active"
        assert session.state_version == 2

        # B-013: 不存在 medical_records 时不得为 done
        assert await _has_no_medical_record(db, str(session.id))

        # 验证 doctor_reviews
        review_count = await _count_doctor_reviews(db, str(session.id), "modify")
        assert review_count == 1

        # 验证 audit
        audit_count = await _count_audit_events(db, str(session.id), "doctor.reviewed")
        assert audit_count == 1
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modify_safety_blocked(
    client: AsyncClient, db: AsyncSession
) -> None:
    """modify 阻断：formula_override 含超量药，二次安全审核未通过。"""
    session = await _create_review_session(db, safety_passed=True)
    await _insert_safety_rule_run(db, session.id, passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={
                "action": "modify",
                "formula_override": {
                    "name": "超量方",
                    "composition": [
                        {"herb": "党参", "dose": 100, "unit": "g"},
                    ],
                },
                "feedback": "测试阻断",
            },
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "SAFETY_REVIEW_BLOCKED"
        assert "issues" in body
        assert len(body["issues"]) > 0

        # 不写 doctor_reviews
        review_count = await _count_doctor_reviews(db, str(session.id))
        assert review_count == 0

        # 不写 audit_events(doctor.reviewed)
        audit_count = await _count_audit_events(db, str(session.id), "doctor.reviewed")
        assert audit_count == 0

        # 会话状态不变
        await db.refresh(session)
        assert session.current_stage == "review"
        assert session.pending_review is True
        assert session.state_version == 1
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modify_missing_formula_override(
    client: AsyncClient, db: AsyncSession
) -> None:
    """modify 缺 formula_override → FORMULA_OVERRIDE_REQUIRED。"""
    session = await _create_review_session(db, safety_passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "modify", "feedback": "忘了改方"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "FORMULA_OVERRIDE_REQUIRED"

        review_count = await _count_doctor_reviews(db, str(session.id))
        assert review_count == 0
    finally:
        pass


# ---------------------------------------------------------------------------
# reject 路径
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_reject_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """reject 成功：回退到 prescription 阶段，写入 doctor_reviews。"""
    session = await _create_review_session(db, safety_passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={
                "action": "reject",
                "feedback": "辨证结论存疑，建议重新辨证",
            },
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        assert data["action"] == "reject"
        assert data["current_stage"] == "prescription"
        assert data["status"] == "active"
        assert data["pending_review"] is False
        assert data["feedback"] == "辨证结论存疑，建议重新辨证"

        # 验证 DB
        await db.refresh(session)
        assert session.current_stage == "prescription"
        assert session.status == "active"
        assert session.pending_review is False
        assert session.state_version == 2

        # 验证 doctor_reviews
        review_count = await _count_doctor_reviews(db, str(session.id), "reject")
        assert review_count == 1

        # 验证 audit
        audit_count = await _count_audit_events(db, str(session.id), "doctor.reviewed")
        assert audit_count == 1
    finally:
        pass


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_action(
    client: AsyncClient, db: AsyncSession
) -> None:
    """非法 action → INVALID_REVIEW_ACTION（由 Pydantic 校验拦截为 422）。"""
    session = await _create_review_session(db, safety_passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "accept_risk"},
            headers=_review_headers(state_version=1),
        )
        # Pydantic Literal 校验 → 422 VALIDATION_ERROR
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_non_review_stage_rejected(
    client: AsyncClient, db: AsyncSession
) -> None:
    """非 review 阶段 → INVALID_STAGE_TRANSITION。"""
    session = await _create_review_session(
        db, stage="prescription", status="active", pending_review=False
    )
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "INVALID_STAGE_TRANSITION"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_session_not_found(client: AsyncClient) -> None:
    """会话不存在 → SESSION_NOT_FOUND。"""
    fake_id = uuid.uuid4()
    resp = await client.post(
        f"/api/v1/consult/sessions/{fake_id}/review",
        json={"action": "confirm"},
        headers=_review_headers(),
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "SESSION_NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_state_version_conflict(
    client: AsyncClient, db: AsyncSession
) -> None:
    """state_version 冲突 → INVALID_STATE_VERSION。"""
    session = await _create_review_session(db, safety_passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=999),
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "INVALID_STATE_VERSION"

        review_count = await _count_doctor_reviews(db, str(session.id))
        assert review_count == 0
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_session_lock_conflict(
    client: AsyncClient, db: AsyncSession
) -> None:
    """会话锁冲突 → SESSION_BUSY。"""
    session = await _create_review_session(db, safety_passed=True)
    redis = None
    try:
        try:
            from redis.asyncio import Redis

            from app.core.config import get_settings

            settings = get_settings()
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            await redis.ping()
            lock_key = f"xuanhu:session_lock:{session.id}"
            await redis.set(lock_key, "other-trace", nx=True, ex=90)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Redis 不可用，跳过锁冲突测试: {exc}")

        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "SESSION_BUSY"

        review_count = await _count_doctor_reviews(db, str(session.id))
        assert review_count == 0
    finally:
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.delete(f"xuanhu:session_lock:{session.id}")
            await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_terminated_session_rejected(
    client: AsyncClient, db: AsyncSession
) -> None:
    """已终止会话 → SESSION_TERMINATED。"""
    session = await _create_review_session(db, safety_passed=True)
    session.status = "terminated"
    await db.commit()
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "SESSION_TERMINATED"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modify_writes_new_safety_rule_run(
    client: AsyncClient, db: AsyncSession
) -> None:
    """modify 通过后会写入新的 safety_rule_run（formula_source=doctor_override）。"""
    session = await _create_review_session(db, safety_passed=True)
    await _insert_safety_rule_run(db, session.id, passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={
                "action": "modify",
                "formula_override": {
                    "name": "四君子汤加减",
                    "composition": [
                        {"herb": "党参", "dose": 10, "unit": "g"},
                    ],
                },
            },
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text

        # 验证新增了 doctor_override 的 safety_rule_run
        sid = uuid.UUID(str(session.id))
        result = await db.execute(
            select(SafetyRuleRun)
            .where(SafetyRuleRun.session_id == sid)
            .where(SafetyRuleRun.formula_source == "doctor_override")
        )
        runs = result.scalars().all()
        assert len(runs) == 1
        assert runs[0].passed is True

        # doctor_review 引用了该 safety_rule_run
        review_result = await db.execute(
            select(DoctorReview)
            .where(DoctorReview.session_id == sid)
            .where(DoctorReview.action == "modify")
        )
        review = review_result.scalars().one()
        assert review.safety_rule_run_id == runs[0].id
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_confirm_doctor_review_references_safety_run(
    client: AsyncClient, db: AsyncSession
) -> None:
    """confirm 的 doctor_review 引用最新的 safety_rule_run_id。"""
    session = await _create_review_session(db, safety_passed=True)
    run_id = await _insert_safety_rule_run(db, session.id, passed=True)
    try:
        resp = await client.post(
            f"/api/v1/consult/sessions/{session.id}/review",
            json={"action": "confirm"},
            headers=_review_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text

        sid = uuid.UUID(str(session.id))
        review_result = await db.execute(
            select(DoctorReview)
            .where(DoctorReview.session_id == sid)
            .where(DoctorReview.action == "confirm")
        )
        review = review_result.scalars().one()
        assert review.safety_rule_run_id == run_id
    finally:
        pass
