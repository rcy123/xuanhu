"""P9-1 后端 E2E 测试共享夹具。

集中管理：
- 模块级测试数据清理
- PostgreSQL / Redis 可用性检查（不可用自动跳过，但不掩盖非连接异常）
- 注入 fake agents 的 AsyncClient（绕过真实模型网关）
- 独立 DB 会话
- 事件流读取辅助

不污染真实试用数据：所有测试会话以专属 patient_ref 前缀 + doctor_id 标识，
模块结束时级联清理 consult_sessions / consult_messages / audit_events /
doctor_reviews / medical_records / safety_rule_runs / agent_runs。
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.agent import AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.knowledge import DosageUnit, Herb
from app.models.review import DoctorReview, MedicalRecord
from app.models.safety import SafetyRuleRun

# ---------------------------------------------------------------------------
# 测试数据标识——所有 E2E 会话以此前缀/doctor 标识，便于级联清理
# ---------------------------------------------------------------------------

E2E_PATIENT_REF_PREFIX = "P9-1-E2E-"
E2E_DOCTOR_ID = "doctor_p9_1_e2e"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# 用于断言前刷新的辅助：每个测试需要读取 DB 最新状态时显式获取新会话
async def new_db_session() -> AsyncSession:
    """创建一个全新 AsyncSession，用于读取请求写入后的最新状态。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    return factory()


# ---------------------------------------------------------------------------
# Fake Agents —— 实现 BaseAgent Protocol，不调用真实模型网关
# ---------------------------------------------------------------------------


class FakeInquiryAgent:
    """问诊 Agent：返回单条补问，next_question 校验通过。"""

    name = "inquiry"
    stage = "inquiry"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.agents.errors import AgentRunError

        if self._fail:
            raise AgentRunError(
                "fake inquiry agent failed",
                code="AGENT_FAILED",
                retryable=False,
            )
        from app.schemas.agent import InquiryAgentOutput

        return AgentResult(
            output=InquiryAgentOutput(
                next_question="请补充现病史细节？",
                asked_dimension="chief_complaint",
            ),
            prompt_version="fake",
        )


class FakeSufficiencyAgent:
    """完备性 Agent：可控制 sufficient 标志。"""

    name = "sufficiency"
    stage = "sufficiency"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    def __init__(self, *, sufficient: bool = True) -> None:
        self._sufficient = sufficient

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import SufficiencyReport

        return AgentResult(
            output=SufficiencyReport(
                covered=["chief_complaint", "present_illness"],
                missing=[] if self._sufficient else ["present_illness"],
                sufficient=self._sufficient,
                suggestions=[] if self._sufficient else ["请补充现病史"],
            ),
            prompt_version="fake",
        )


class FakeSyndromeAgent:
    """辨证 Agent：返回固定证型与治法。"""

    name = "syndrome"
    stage = "syndrome"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import SyndromeResult

        return AgentResult(
            output=SyndromeResult(
                syndrome="脾虚湿盛证",
                syndrome_basis=["食欲不振", "大便溏薄"],
                differential=["肝郁脾虚"],
                treatment_principle="健脾益气，渗湿止泻",
                confidence=0.85,
                citations=["fake-citation"],
            ),
            prompt_version="fake",
        )


class FakePrescriptionAgent:
    """开方 Agent：返回党参 12g 安全处方（党参 max_dose=30 已在种子数据）。"""

    name = "prescription"
    stage = "prescription"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import FormulaResult, HerbDose

        return AgentResult(
            output=FormulaResult(
                name="四君子汤",
                composition=[
                    HerbDose(herb="党参", dose=12, unit="g"),
                    HerbDose(herb="白术", dose=10, unit="g"),
                ],
                rationale="健脾益气",
            ),
            prompt_version="fake",
        )


class FakeModificationAgent:
    """加减 Agent：原方追加茯苓。"""

    name = "modification"
    stage = "modification"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import (
            FormulaResult,
            HerbDose,
            ModificationAction,
            ModificationItem,
            ModifiedFormulaResult,
        )

        return AgentResult(
            output=ModifiedFormulaResult(
                formula=FormulaResult(
                    name="四君子汤加减",
                    composition=[
                        HerbDose(herb="党参", dose=12, unit="g"),
                        HerbDose(herb="白术", dose=10, unit="g"),
                        HerbDose(herb="茯苓", dose=10, unit="g"),
                    ],
                    rationale="健脾益气，渗湿止泻",
                ),
                modifications=[
                    ModificationItem(
                        action=ModificationAction.ADD,
                        herb="茯苓",
                        dose=10,
                        unit="g",
                        reason="增强渗湿",
                    )
                ],
            ),
            prompt_version="fake",
        )


class FakeRecordAgent:
    """病历 Agent：返回固定病历文本。"""

    name = "record"
    stage = "record"
    primary_sources: tuple[str, ...] = ()
    allow_cross_source = True

    async def run(self, state: Any, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import MedicalRecord

        return AgentResult(
            output=MedicalRecord(
                text="【主诉】食欲不振伴大便溏薄3天\n【辨证】脾虚湿盛证\n【处方】四君子汤加减",
                record_json={
                    "chief_complaint": "食欲不振伴大便溏薄3天",
                    "syndrome": "脾虚湿盛证",
                    "formula": {
                        "name": "四君子汤加减",
                        "composition": [
                            {"herb": "党参", "dose": 12, "unit": "g"},
                            {"herb": "白术", "dose": 10, "unit": "g"},
                            {"herb": "茯苓", "dose": 10, "unit": "g"},
                        ],
                    },
                },
                disclaimer="本记录由悬壶AI辅助生成，经医师审核确认。仅供参考。",
            ),
            prompt_version="fake",
        )


def build_fake_registry(
    *,
    sufficient: bool = True,
    inquiry_fail: bool = False,
) -> Any:
    """构造覆盖全阶段（除 SAFETY 规则引擎）的 fake registry。"""
    from app.agents.registry import AgentRegistry
    from app.schemas.types import Stage

    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, FakeInquiryAgent(fail=inquiry_fail))  # type: ignore[arg-type]
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgent(sufficient=sufficient))  # type: ignore[arg-type]
    registry.register(Stage.SYNDROME, FakeSyndromeAgent())  # type: ignore[arg-type]
    registry.register(Stage.PRESCRIPTION, FakePrescriptionAgent())  # type: ignore[arg-type]
    registry.register(Stage.MODIFICATION, FakeModificationAgent())  # type: ignore[arg-type]
    registry.register(Stage.RECORD, FakeRecordAgent())  # type: ignore[arg-type]
    return registry


# ---------------------------------------------------------------------------
# 模块级清理
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_e2e_data(_check_infra: None) -> None:
    """模块结束时级联清理本模块创建的所有会话及关联数据。"""
    from app.db.session import get_session_factory

    yield

    factory = get_session_factory()
    async with factory() as session:
        try:
            await session.execute(select(ConsultSession.id).limit(1))
        except Exception:  # noqa: BLE001
            return

        session_ids_subq = select(ConsultSession.id).where(
            or_(
                ConsultSession.patient_ref.like(f"{E2E_PATIENT_REF_PREFIX}%"),
                ConsultSession.created_by == E2E_DOCTOR_ID,
            )
        )

        await session.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(DoctorReview).where(DoctorReview.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(SafetyRuleRun).where(SafetyRuleRun.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(AgentRun).where(AgentRun.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(ConsultMessage).where(ConsultMessage.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(AuditEvent).where(AuditEvent.session_id.in_(session_ids_subq))
        )
        await session.execute(
            delete(ConsultSession).where(
                or_(
                    ConsultSession.patient_ref.like(f"{E2E_PATIENT_REF_PREFIX}%"),
                    ConsultSession.created_by == E2E_DOCTOR_ID,
                )
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# DB / Redis 可用性检查
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _check_infra() -> None:
    """检查 PostgreSQL / Redis 可用性。

    任何连接异常都直接失败，防止 integration 门禁假绿。
    """
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    get_settings()
    await reset_session_factory()
    factory = get_session_factory()

    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except (OSError, ConnectionError) as exc:
        pytest.fail(
            f"PostgreSQL E2E dependency unavailable: {type(exc).__name__}: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"PostgreSQL 检查出现非连接类异常: {type(exc).__name__}: {exc}",
            pytrace=True,
        )

    async with factory() as session:
        existing_herbs = set((await session.scalars(select(Herb.name))).all())
        for name, aliases, max_dose, doc_text in (
            ("党参", ["潞党参"], 30.0, "党参 补中益气"),
            ("白术", ["于术"], 15.0, "白术 补气健脾"),
            ("茯苓", [], 30.0, "茯苓 利水渗湿"),
        ):
            if name not in existing_herbs:
                session.add(
                    Herb(
                        name=name,
                        aliases=aliases,
                        max_dose=max_dose,
                        pregnancy_contraindication="none",
                        doc_text=doc_text,
                    )
                )
        dosage_unit = await session.scalar(select(DosageUnit).where(DosageUnit.unit_name == "g"))
        if dosage_unit is None:
            session.add(
                DosageUnit(
                    unit_name="g",
                    aliases=["克"],
                    to_grams=1.0,
                    conversion_type="standard",
                    is_standard=True,
                    enabled=True,
                )
            )
        await session.commit()

    try:
        yield
    finally:
        await reset_session_factory()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话（与路由请求会话隔离）。

    每个 AsyncSession 对应独立的 identity map。为避免读到请求会话
    写入后未刷新的旧实例，提供 refresh_db 让测试在请求完成后获取
    全新会话读取最新状态。
    """
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def fresh_db() -> AsyncSession:
    """函数级全新 AsyncSession，用于在 HTTP 请求后读取最新 DB 状态。

    每个测试函数获取一个独立会话，identity map 干净，避免读到
    请求会话或模块级会话缓存的旧值。
    """
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# 注入 fake agents 的 AsyncClient
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端，注入 fake agent 绕过真实模型网关。

    monkeypatch 三项：
    1. MessageService._default_inquiry_registry → fake agents
    2. Supervisor._default_registry → fake agents
    3. Supervisor._run_safety_agent → 返回 fake SafetyExplanation，不调用真实
       SafetyAgent/模型网关。SafetyRuleEngine 的确定性安全审核仍真实运行。
    """
    import app.agents.supervisor as sup_module
    import app.services.message as msg_module
    from app.schemas.agent import SafetyExplanation

    fake_msg_registry = build_fake_registry()
    fake_sup_registry = build_fake_registry()

    _orig_msg = msg_module._default_inquiry_registry
    _orig_sup = sup_module._default_registry
    _orig_safety_agent = sup_module.Supervisor._run_safety_agent  # type: ignore[attr-defined]

    msg_module._default_inquiry_registry = lambda: fake_msg_registry  # type: ignore[assignment]
    sup_module._default_registry = lambda: fake_sup_registry  # type: ignore[assignment]

    # P9-1-fix: monkeypatch _run_safety_agent 返回 fake 解释，不调用真实模型网关。
    # SafetyRuleEngine 的确定性安全审核（规则引擎）仍真实运行，仅 GPU 解释层被替换。
    async def _fake_run_safety_agent(
        self: sup_module.Supervisor,
        state: Any,
        trace_id: str,
        session_id: str,
    ) -> SafetyExplanation | None:
        del self, trace_id, session_id
        if state.safety_rule_result is None:
            return None
        return SafetyExplanation(
            summary="经安全规则审核，该处方未发现安全问题，可进入医师复核。",
            issue_explanations=[],
            recommendations=None,
            safety_agent_run_id="e2e-fake-run-id",
            safety_agent_model="e2e-fake-model",
        )

    sup_module.Supervisor._run_safety_agent = _fake_run_safety_agent  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    msg_module._default_inquiry_registry = _orig_msg  # type: ignore[assignment]
    sup_module._default_registry = _orig_sup  # type: ignore[assignment]
    sup_module.Supervisor._run_safety_agent = _orig_safety_agent  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------


def _headers(state_version: int | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"X-Doctor-Id": E2E_DOCTOR_ID}
    if state_version is not None:
        headers["X-State-Version"] = str(state_version)
    return headers


async def create_session(
    client: AsyncClient,
    *,
    chief_complaint: str = "食欲不振伴大便溏薄3天",
) -> dict[str, Any]:
    """通过 POST /sessions 创建会话，返回 data 字典（含 session_id / state_version）。"""
    from datetime import UTC, datetime

    payload = {
        "patient_info": {
            "name": "P9-1E2E患者",
            "patient_ref": f"{E2E_PATIENT_REF_PREFIX}{datetime.now(UTC).strftime('%H%M%S%f')}",
            "gender": "male",
            "age": 45,
            "allergies": [],
            "pregnancy_status": "no",
        },
        "chief_complaint": chief_complaint,
    }
    resp = await client.post(
        "/api/v1/consult/sessions",
        json=payload,
        headers={"X-Doctor-Id": E2E_DOCTOR_ID},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    # state_version 不在 SessionCreateResponse 中，通过 detail 获取
    # 创建后 version=2（P3-1 创建即递增一次）
    detail_resp = await client.get(
        f"/api/v1/consult/sessions/{data['session_id']}",
        headers={"X-Doctor-Id": E2E_DOCTOR_ID},
    )
    detail = detail_resp.json()["data"]
    data["state_version"] = detail.get("state_version", 2)
    return data


async def submit_message(
    client: AsyncClient,
    session_id: str,
    *,
    content: str = "患者诉近三日食欲明显减退，饭后腹胀，大便溏薄日两次，无发热。",
    expect_status: int = 200,
    state_version: int | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/consult/sessions/{session_id}/messages",
        json={"content": content, "role": "doctor"},
        headers=_headers(state_version),
    )
    assert resp.status_code == expect_status, resp.text
    return resp.json()


async def post_advance(
    client: AsyncClient,
    session_id: str,
    *,
    body: dict[str, Any] | None = None,
    expect_status: int = 200,
    state_version: int | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/consult/sessions/{session_id}/advance",
        json=body or {},
        headers=_headers(state_version),
    )
    assert resp.status_code == expect_status, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# DB / Redis 查询辅助
# ---------------------------------------------------------------------------


async def count_audit_events(
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


async def fetch_session(db: AsyncSession, session_id: str) -> ConsultSession:
    """读取会话最新状态。

    使用 expire 然后 refresh 确保读到最新 DB 值（绕过 identity map）。
    """
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(ConsultSession).where(ConsultSession.id == sid)
    )
    session = result.scalar_one()
    await db.refresh(session)
    return session


async def fetch_session_fresh(session_id: str) -> ConsultSession:
    """在全新 AsyncSession 中读取会话，完全隔离缓存。"""
    async with await new_db_session() as db:
        sid = uuid.UUID(session_id)
        result = await db.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        return result.scalar_one()


async def read_stream_event_types(session_id: str) -> list[str]:
    """读取 Redis Stream 全部事件类型，Redis 不可用时返回空列表。"""
    try:
        from app.core.redis import get_redis

        redis = await get_redis()
        key = f"xuanhu:events:{session_id}"
        entries = await redis.xrange(key, count=200)
        types = [entry[1].get("event_type") for entry in entries]
        return [t for t in types if t]
    except Exception:  # noqa: BLE001
        return []


async def read_stream_events(session_id: str) -> list[dict[str, Any]]:
    """读取 Redis Stream 全部事件（含 payload），Redis 不可用时返回空。"""
    try:
        from app.core.redis import get_redis

        redis = await get_redis()
        key = f"xuanhu:events:{session_id}"
        entries = await redis.xrange(key, count=200)
        events: list[dict[str, Any]] = []
        for _eid, fields in entries:
            et = fields.get("event_type")
            payload_raw = fields.get("payload", "{}")
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
            events.append({"event_type": et, "payload": payload})
        return events
    except Exception:  # noqa: BLE001
        return []


async def cleanup_stream(session_id: str) -> None:
    """清理测试会话的 Redis Stream 与 checkpoint。"""
    with contextlib.suppress(Exception):
        from app.core.redis import get_redis

        redis = await get_redis()
        await redis.delete(f"xuanhu:events:{session_id}")
        await redis.delete(f"xuanhu:checkpoint:{session_id}")


async def cleanup_session_lock(session_id: str) -> None:
    """清理测试残留的会话锁。"""
    with contextlib.suppress(Exception):
        from app.core.redis import get_redis

        redis = await get_redis()
        await redis.delete(f"xuanhu:session_lock:{session_id}")
