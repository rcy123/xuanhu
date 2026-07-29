"""P3-1 会话管理 API 测试。

覆盖创建、列表、详情、终止四个接口及审计事件写入。
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
from app.models.domain import GateResult, Observation, SafetyProfile

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# ---------------------------------------------------------------------------
# 模块级测试数据清理
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P3-"
_TEST_DOCTOR_ID = "doctor_p3_test"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_sessions() -> None:
    """模块结束时清理本模块创建的会话及关联审计事件。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()

    yield

    factory = get_session_factory()
    async with factory() as session:
        session_id_stmt = select(ConsultSession.id).where(
            or_(
                ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                ConsultSession.created_by == _TEST_DOCTOR_ID,
            )
        )
        try:
            await session.execute(select(ConsultSession.id).limit(1))
        except Exception:  # noqa: BLE001
            return

        await session.execute(
            delete(AuditEvent).where(
                AuditEvent.session_id.in_(session_id_stmt)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话，用于测试中断言数据库状态。"""
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


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性，不可用时跳过全部集成测试。"""
    from sqlalchemy import text

    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")


async def _create_session(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """辅助：调用创建会话接口并返回 data。"""
    response = await client.post(
        "/api/v1/consult/sessions",
        json=payload or {},
        headers=headers if headers is not None else {"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _get_session_status(
    db: AsyncSession, session_id: str
) -> ConsultSession | None:
    """辅助：从数据库读取会话对象。"""
    sid = uuid.UUID(session_id)
    result = await db.execute(select(ConsultSession).where(ConsultSession.id == sid))
    return result.scalar_one_or_none()


async def _count_audit_events(
    db: AsyncSession, session_id: str, event_type: str
) -> int:
    """辅助：统计某会话某类型的审计事件数。"""
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == event_type,
        )
    )
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# 创建会话
# ---------------------------------------------------------------------------


async def test_create_session_success(client: AsyncClient, db: AsyncSession) -> None:
    """创建会话成功，返回基本字段。"""
    payload = {
        "patient_info": {
            "name": "模拟患者",
            "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TEST001",
            "gender": "male",
            "age": 45,
            "allergies": [],
            "pregnancy_status": "unknown",
        },
        "chief_complaint": "模拟主诉：头痛一天",
    }
    response = await client.post("/api/v1/consult/sessions", json=payload)
    assert response.status_code == 201

    body = response.json()
    assert body["code"] == "SUCCESS"
    assert body["message"] == "ok"
    assert "trace_id" in body
    data = body["data"]
    assert data["current_stage"] == "inquiry"
    assert data["status"] == "active"
    assert data["agent_runtime"] == "legacy"
    assert data["patient_info"]["patient_ref"] == f"{_TEST_PATIENT_REF_PREFIX}TEST001"

    # 数据库验证
    session = await _get_session_status(db, data["session_id"])
    assert session is not None
    assert session.status == "active"
    assert session.current_stage == "inquiry"
    assert session.recovery_status == "normal"
    assert session.state_version == 1
    assert session.patient_ref == f"{_TEST_PATIENT_REF_PREFIX}TEST001"


async def test_create_session_writes_audit_event(
    client: AsyncClient, db: AsyncSession
) -> None:
    """创建会话写入 session.created 审计事件。"""
    payload = {
        "patient_info": {
            "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}AUDIT001",
            "gender": "unknown",
        },
        "chief_complaint": "audit test",
    }
    data = await _create_session(client, payload, headers={"X-Doctor-Id": "doctor_p3"})
    session_id = data["session_id"]

    count = await _count_audit_events(db, session_id, "session.created")
    assert count == 1

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.created",
        )
    )
    event = result.scalar_one()
    assert event.actor_type == "doctor"
    assert event.actor_id == "doctor_p3"
    assert event.payload["patient_ref_present"] is True
    assert "patient_ref" not in event.payload
    assert "chief_complaint" not in event.payload
    assert event.payload["initial_stage"] == "inquiry"


async def test_create_langgraph_session_seeds_identity_free_domain_state(
    client: AsyncClient,
    db: AsyncSession,
    enable_public_langgraph: None,
) -> None:
    identity_name = "不得进入Domain的姓名"
    identity_ref = f"{_TEST_PATIENT_REF_PREFIX}SEED-IDENTITY"
    data = await _create_session(
        client,
        {
            "agent_runtime": "langgraph",
            "chief_complaint": "反复头痛",
            "patient_info": {
                "name": identity_name,
                "patient_ref": identity_ref,
                "age": 36,
                "gender": "female",
                "allergies": ["青霉素"],
                "pregnancy_status": "no",
                "current_medications": [],
                "major_conditions": ["高血压"],
            },
        },
    )
    sid = uuid.UUID(data["session_id"])
    await db.rollback()
    observations = (
        await db.execute(select(Observation).where(Observation.session_id == sid).order_by(Observation.fact_key))
    ).scalars().all()
    facts = {item.fact_key: item.value for item in observations}
    assert facts == {
        "chief_complaint.symptom": "反复头痛",
        "patient.age": 36,
        "patient.sex": "female",
    }
    safety = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == sid))
    assert safety is not None
    assert safety.allergy_collection_status == "collected"
    assert safety.allergens == ["青霉素"]
    assert safety.pregnancy_value == "not_pregnant"
    assert safety.medications_collection_status == "explicitly_none"
    assert safety.major_conditions == ["高血压"]

    source = await db.scalar(
        select(ConsultMessage).where(
            ConsultMessage.session_id == sid,
            ConsultMessage.agent_name == "initial_domain_seed",
        )
    )
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "initial_domain_seed.created",
        )
    )
    assert source is not None and audit is not None
    serialized_authority = f"{facts!r}{source.structured_delta!r}{audit.payload!r}"
    assert identity_name not in serialized_authority
    assert identity_ref not in serialized_authority

    history = await client.get(f"/api/v1/consult/sessions/{sid}/messages")
    assert history.status_code == 200
    assert all(item.get("agent_name") != "initial_domain_seed" for item in history.json()["data"]["items"])


async def test_create_langgraph_session_red_flag_chief_complaint_blocks_immediately(
    client: AsyncClient,
    db: AsyncSession,
    enable_public_langgraph: None,
) -> None:
    data = await _create_session(
        client,
        {
            "agent_runtime": "langgraph",
            "chief_complaint": "突然胸痛并且呼吸困难",
            "patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}SEED-RED-FLAG"},
        },
    )
    assert data["current_stage"] == "blocked"
    assert data["status"] == "blocked"
    sid = uuid.UUID(data["session_id"])
    await db.rollback()
    session = await db.get(ConsultSession, sid)
    gate = await db.scalar(
        select(GateResult).where(GateResult.session_id == sid, GateResult.gate_name == "triage")
    )
    assert session is not None and gate is not None
    assert session.recovery_status == "manual_required"
    assert gate.decision == "blocked"
    assert (gate.details or {})["triage_precheck_version"] == "triage-raw-text-precheck.v1"


async def test_create_langgraph_session_fails_closed_when_public_rollout_is_disabled(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    patient_ref = f"{_TEST_PATIENT_REF_PREFIX}LANGGRAPH-PUBLIC-DISABLED"
    response = await client.post(
        "/api/v1/consult/sessions",
        json={
            "agent_runtime": "langgraph",
            "patient_info": {"patient_ref": patient_ref},
        },
        headers={"X-Request-Id": "trace-langgraph-public-disabled"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "LANGGRAPH_PUBLIC_DISABLED",
        "message": "LangGraph 公共会话创建尚未开放",
        "detail": (
            "agent_runtime=langgraph 的公共会话创建未启用；"
            "请使用 legacy 或由运维启用 XUANHU_LANGGRAPH_PUBLIC_ENABLED"
        ),
        "retryable": False,
        "stage": None,
        "trace_id": "trace-langgraph-public-disabled",
    }
    assert await db.scalar(select(ConsultSession.id).where(ConsultSession.patient_ref == patient_ref)) is None


async def test_default_langgraph_runtime_is_blocked_but_explicit_legacy_remains_available(
    client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    blocked_ref = f"{_TEST_PATIENT_REF_PREFIX}DEFAULT-LANGGRAPH-DISABLED"
    legacy_ref = f"{_TEST_PATIENT_REF_PREFIX}EXPLICIT-LEGACY-ALLOWED"
    monkeypatch.setenv("AGENT_RUNTIME_VERSION", "langgraph")
    monkeypatch.setenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", "false")
    get_settings.cache_clear()
    try:
        blocked = await client.post(
            "/api/v1/consult/sessions",
            json={"patient_info": {"patient_ref": blocked_ref}},
        )
        legacy = await client.post(
            "/api/v1/consult/sessions",
            json={
                "agent_runtime": "legacy",
                "patient_info": {"patient_ref": legacy_ref},
            },
        )
    finally:
        get_settings.cache_clear()

    assert blocked.status_code == 403
    assert blocked.json()["code"] == "LANGGRAPH_PUBLIC_DISABLED"
    assert legacy.status_code == 201
    assert await db.scalar(select(ConsultSession.id).where(ConsultSession.patient_ref == blocked_ref)) is None
    legacy_session = await db.scalar(select(ConsultSession).where(ConsultSession.patient_ref == legacy_ref))
    assert legacy_session is not None
    assert legacy_session.agent_runtime == "legacy"


async def test_create_session_default_values(client: AsyncClient, db: AsyncSession) -> None:
    """空请求体时，会话使用默认值。"""
    data = await _create_session(client, {})
    assert data["status"] == "active"
    assert data["current_stage"] == "inquiry"


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------


async def test_list_sessions_pagination_and_sort(client: AsyncClient, db: AsyncSession) -> None:
    """列表分页与排序。"""
    await _create_session(
        client,
        {
            "patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}PAGE001"},
            "chief_complaint": "first",
        },
    )
    s2 = await _create_session(
        client,
        {
            "patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}PAGE002"},
            "chief_complaint": "second",
        },
    )

    response = await client.get("/api/v1/consult/sessions?page=1&page_size=1&sort=created_at:desc")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert len(data["items"]) == 1
    assert data["total"] >= 2
    assert data["page"] == 1
    assert data["page_size"] == 1

    # 默认排序为 created_at:desc，第一条应为最新创建的 s2
    assert data["items"][0]["session_id"] == s2["session_id"]
    assert data["items"][0]["agent_runtime"] == "legacy"


async def test_list_sessions_status_filter(client: AsyncClient, db: AsyncSession) -> None:
    """按 status 过滤列表。"""
    unique_ref = f"{_TEST_PATIENT_REF_PREFIX}STATUS{datetime.now(UTC).strftime('%H%M%S%f')}"
    s = await _create_session(
        client,
        {"patient_info": {"patient_ref": unique_ref}},
    )

    response = await client.get(f"/api/v1/consult/sessions?status=active&patient_ref={unique_ref}")
    assert response.status_code == 200
    body = response.json()
    session_ids = [item["session_id"] for item in body["data"]["items"]]
    assert s["session_id"] in session_ids

    response = await client.get(
        f"/api/v1/consult/sessions?status=terminated&patient_ref={unique_ref}"
    )
    assert response.status_code == 200
    body = response.json()
    session_ids = [item["session_id"] for item in body["data"]["items"]]
    assert s["session_id"] not in session_ids


async def test_list_sessions_patient_ref_search(client: AsyncClient, db: AsyncSession) -> None:
    """按 patient_ref 模糊搜索。"""
    unique_ref = f"{_TEST_PATIENT_REF_PREFIX}SEARCH{datetime.now(UTC).strftime('%H%M%S%f')}"
    s = await _create_session(
        client,
        {"patient_info": {"patient_ref": unique_ref}},
    )

    # 部分匹配
    response = await client.get(f"/api/v1/consult/sessions?patient_ref={unique_ref[4:8]}")
    assert response.status_code == 200
    session_ids = [item["session_id"] for item in response.json()["data"]["items"]]
    assert s["session_id"] in session_ids

    # 不匹配
    response = await client.get("/api/v1/consult/sessions?patient_ref=NOTEXIST")
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 0


async def test_list_sessions_sort_updated_at(client: AsyncClient, db: AsyncSession) -> None:
    """按 updated_at:desc 排序。"""
    await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}UPD001"}},
    )
    s2 = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}UPD002"}},
    )

    response = await client.get("/api/v1/consult/sessions?sort=updated_at:desc&page_size=2")
    items = response.json()["data"]["items"]
    assert items[0]["session_id"] == s2["session_id"]


# ---------------------------------------------------------------------------
# 会话详情
# ---------------------------------------------------------------------------


async def test_get_session_detail_success(client: AsyncClient, db: AsyncSession) -> None:
    """获取会话详情成功，包含预留字段。"""
    payload = {
        "patient_info": {
            "name": "Detail",
            "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}DETAIL001",
            "gender": "female",
            "age": 30,
        },
        "chief_complaint": "detail test",
    }
    created = await _create_session(client, payload)

    response = await client.get(f"/api/v1/consult/sessions/{created['session_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["session_id"] == created["session_id"]
    assert data["current_stage"] == "inquiry"
    assert data["status"] == "active"
    assert data["patient_info"]["patient_ref"] == f"{_TEST_PATIENT_REF_PREFIX}DETAIL001"
    # P3-1 未实现字段应为 null 或空结构
    assert data["sufficiency_report"] is None
    assert data["syndrome_result"] is None
    assert data["safety_review"] is None
    assert data["medical_record"] is None


async def test_get_session_not_found(client: AsyncClient, db: AsyncSession) -> None:
    """获取不存在会话返回 404 SESSION_NOT_FOUND。"""
    fake_id = str(uuid.uuid4())
    response = await client.get(f"/api/v1/consult/sessions/{fake_id}")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"
    assert body["retryable"] is False
    assert body["trace_id"]


# ---------------------------------------------------------------------------
# 终止会话
# ---------------------------------------------------------------------------


async def test_terminate_active_session_success(client: AsyncClient, db: AsyncSession) -> None:
    """终止 active 会话成功。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERM001"}},
    )
    session_id = created["session_id"]

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json={"reason": "测试终止"},
        headers={"X-Doctor-Id": "doctor_term"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["status"] == "terminated"
    assert data["current_stage"] == "blocked"
    assert data["blocked_reason"] == "terminated_by_doctor"

    session = await _get_session_status(db, session_id)
    assert session is not None
    assert session.status == "terminated"
    assert session.blocked_reason == "terminated_by_doctor"
    assert session.blocked_at is not None


async def test_terminate_writes_audit_event(client: AsyncClient, db: AsyncSession) -> None:
    """终止会话写入 session.terminated 审计事件。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERMAUDIT"}},
    )
    session_id = created["session_id"]

    await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json={"reason": "审计测试终止"},
    )

    count = await _count_audit_events(db, session_id, "session.terminated")
    assert count == 1

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.terminated",
        )
    )
    event = result.scalar_one()
    assert event.payload["reason"] == "审计测试终止"
    assert event.payload["previous_status"] == "active"
    assert "terminated_at" in event.payload


async def test_terminate_done_session_fails(client: AsyncClient, db: AsyncSession) -> None:
    """done 会话不可终止。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}DONETERM"}},
    )
    session_id = created["session_id"]

    # 直接修改数据库状态为 done（P3-1 无 advance 接口，故直接改库）
    session = await _get_session_status(db, session_id)
    assert session is not None
    session.status = "done"
    await db.commit()

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json={"reason": "不应成功"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVALID_STAGE_TRANSITION"
    assert body["retryable"] is False


async def test_terminate_terminated_session_fails(client: AsyncClient, db: AsyncSession) -> None:
    """terminated 会话不可再次终止。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERMTWICE"}},
    )
    session_id = created["session_id"]

    # 第一次终止
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json={"reason": "第一次终止"},
    )
    assert response.status_code == 200

    # 第二次终止
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/terminate",
        json={"reason": "第二次终止"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVALID_STAGE_TRANSITION"


# ---------------------------------------------------------------------------
# 请求校验
# ---------------------------------------------------------------------------


async def test_create_session_invalid_age(client: AsyncClient, db: AsyncSession) -> None:
    """年龄越界返回 VALIDATION_ERROR。"""
    payload = {"patient_info": {"age": 200}}
    response = await client.post("/api/v1/consult/sessions", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["retryable"] is False
