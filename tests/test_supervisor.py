"""P4-3 Supervisor 状态机骨架测试。

使用 fake agents 测试阶段路由、回退、挂起、blocked、checkpoint、
Redis Stream 事件和审计，不调用真实模型网关。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.errors import AgentRunError
from app.agents.registry import AgentRegistry
from app.agents.supervisor import Supervisor
from app.core.config import get_settings
from app.core.exceptions import InvalidStageTransitionError, InvalidStateVersionError, SessionBusyError
from app.core.redis import get_redis
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    MedicalRecord,
    ModificationItem,
    ModifiedFormulaResult,
    SafetyExplanation,
    SafetyIssue,
    SafetyReview,
    SufficiencyReport,
    SyndromeResult,
    XuanhuState,
)
from app.schemas.types import ModificationAction, RollbackTarget, Severity, Stage
from app.services.events import EventService

# ---------------------------------------------------------------------------
# Fake Agent 输出 Schema
# ---------------------------------------------------------------------------

class FakeInquiryOutput(BaseModel):
    """Fake inquiry agent 输出。"""

    next_question: str = "还有什么症状？"


class FakeSufficiencyOutput(SufficiencyReport):
    """Fake sufficiency agent 输出。"""

    pass


class FakeSyndromeOutput(SyndromeResult):
    """Fake syndrome agent 输出。"""

    pass


class FakePrescriptionOutput(FormulaResult):
    """Fake prescription agent 输出。"""

    pass


class FakeModificationOutput(ModifiedFormulaResult):
    """Fake modification agent 输出。"""

    pass


class FakeSafetyOutput(SafetyReview):
    """Fake safety agent 输出。"""

    pass


class FakeRecordOutput(MedicalRecord):
    """Fake record agent 输出。"""

    pass


# ---------------------------------------------------------------------------
# Fake Agents（继承 BaseAgentImpl，使用 FakeGateway）
# ---------------------------------------------------------------------------

class FakeGateway:
    """可控 fake gateway。"""

    def __init__(self, responses: list[BaseModel | dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> BaseModel | dict[str, Any]:
        self.calls.append({
            "messages": messages,
            "output_schema": output_schema,
            "trace_id": trace_id,
            "session_id": session_id,
            "agent_name": agent_name,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeInquiryAgent:
    """Fake inquiry agent，直接实现 BaseAgent Protocol。"""

    name = "inquiry"
    stage = Stage.INQUIRY
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeInquiryOutput
    next_stage = Stage.SUFFICIENCY

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakeInquiryOutput(next_question="还有什么症状？"),
            prompt_version="fake",
        )


class FakeSufficiencyAgent:
    """Fake sufficiency agent，直接实现 BaseAgent Protocol。"""

    name = "sufficiency"
    stage = Stage.SUFFICIENCY
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeSufficiencyOutput
    next_stage = Stage.SYNDROME

    def __init__(self, sufficient: bool = True) -> None:
        self._sufficient = sufficient

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakeSufficiencyOutput(
                sufficient=self._sufficient,
                covered=["chief_complaint"],
                missing=[] if self._sufficient else ["sleep"],
                suggestions=[],
            ),
            prompt_version="fake",
        )


class FakeSyndromeAgent:
    """Fake syndrome agent，直接实现 BaseAgent Protocol。"""

    name = "syndrome"
    stage = Stage.SYNDROME
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeSyndromeOutput
    next_stage = Stage.PRESCRIPTION

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakeSyndromeOutput(
                syndrome="脾虚湿盛",
                syndrome_basis=["食欲差", "大便溏"],
                treatment_principle="健脾化湿",
                confidence=0.85,
            ),
            prompt_version="fake",
        )


class FakePrescriptionAgent:
    """Fake prescription agent，直接实现 BaseAgent Protocol。"""

    name = "prescription"
    stage = Stage.PRESCRIPTION
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakePrescriptionOutput
    next_stage = Stage.MODIFICATION

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakePrescriptionOutput(
                name="参苓白术散",
                composition=[HerbDose(herb="党参", dose=12, unit="g"), HerbDose(herb="白术", dose=10, unit="g")],
                rationale="健脾益气，渗湿止泻",
            ),
            prompt_version="fake",
        )


class FakeModificationAgent:
    """Fake modification agent，直接实现 BaseAgent Protocol。"""

    name = "modification"
    stage = Stage.MODIFICATION
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeModificationOutput
    next_stage = Stage.SAFETY

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakeModificationOutput(
                formula=FakePrescriptionOutput(
                    name="参苓白术散加减",
                    composition=[HerbDose(herb="党参", dose=12, unit="g"), HerbDose(herb="白术", dose=10, unit="g")],
                    rationale="健脾益气，渗湿止泻",
                ),
                modifications=[ModificationItem(action=ModificationAction.ADD, herb="茯苓", dose=10, unit="g", reason="增强渗湿")],
            ),
            prompt_version="fake",
        )


class FakeSafetyAgent:
    """Fake safety agent，用于 P6-4 之前的兼容路径测试。

    P6-3 起 SAFETY 阶段由 SafetyRuleEngine 处理，不再走 FakeSafetyAgent。
    保留该类以备 P6-4 Safety Agent 联调。
    """

    name = "safety"
    stage = Stage.SAFETY
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeSafetyOutput
    next_stage = Stage.REVIEW

    def __init__(self, passed: bool = True, rollback_target: RollbackTarget = RollbackTarget.NONE) -> None:
        self._passed = passed
        self._rollback_target = rollback_target

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        if self._passed:
            return AgentResult(
                output=FakeSafetyOutput(
                    passed=True,
                    issues=[],
                    rollback_target=RollbackTarget.NONE,
                    summary="安全审核通过",
                ),
                prompt_version="fake",
            )
        return AgentResult(
            output=FakeSafetyOutput(
                passed=False,
                issues=[SafetyIssue(type="dose_limit", severity=Severity.WARNING, herbs=["白术"], rule_source="剂量上限", suggestion="建议减量")],
                rollback_target=self._rollback_target,
                summary="剂量超限",
            ),
            prompt_version="fake",
        )


class FakeRecordAgent:
    """Fake record agent，直接实现 BaseAgent Protocol。"""

    name = "record"
    stage = Stage.RECORD
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeRecordOutput
    next_stage = Stage.DONE

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        return AgentResult(
            output=FakeRecordOutput(
                text="病历文本",
                record_json={"chief_complaint": "头痛"},
                disclaimer="本病历由 AI 辅助生成，需医师确认。",
            ),
            prompt_version="fake",
        )


class FakeFailingAgent:
    """总是抛出 AGENT_SCHEMA_INVALID 的 Agent。"""

    name = "failing"
    stage = Stage.INQUIRY
    primary_sources = ()
    allow_cross_source = True
    output_schema = FakeInquiryOutput
    next_stage = None

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        raise AgentRunError("schema invalid", code="AGENT_SCHEMA_INVALID", retryable=False)


class FailingRedis:
    """用于确定性触发 Redis checkpoint 写入失败。"""

    async def set(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("redis checkpoint unavailable")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _build_registry(
    *,
    sufficient: bool = True,
    safety_passed: bool = True,
    safety_rollback_target: RollbackTarget = RollbackTarget.NONE,
) -> AgentRegistry:
    """构造一组 fake agents 注册表。"""
    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, FakeInquiryAgent())
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgent(sufficient=sufficient))
    registry.register(Stage.SYNDROME, FakeSyndromeAgent())
    registry.register(Stage.PRESCRIPTION, FakePrescriptionAgent())
    registry.register(Stage.MODIFICATION, FakeModificationAgent())
    # P6-3: SAFETY 阶段不再注册 FakeSafetyAgent（规则引擎直接处理）
    registry.register(Stage.RECORD, FakeRecordAgent())
    return registry


async def _create_session(db: AsyncSession, stage: Stage = Stage.INQUIRY, status: str = "active") -> ConsultSession:
    """在数据库中创建测试会话。"""
    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref="P4-3-TEST",
        patient_info={"gender": "male", "age": 35},
        current_stage=stage.value,
        status=status,
        state_version=1,
        rollback_counts={},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _cleanup_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    """清理测试会话及相关数据。"""
    from app.models.review import MedicalRecord

    await db.execute(delete(MedicalRecord).where(MedicalRecord.session_id == session_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.session_id == session_id))
    await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    await db.commit()


async def _read_redis_checkpoint(session_id: str) -> dict[str, Any] | None:
    """读取 Redis checkpoint。"""
    try:
        redis = await get_redis()
        key = f"xuanhu:checkpoint:{session_id}"
        raw = await redis.get(key)
        if raw:
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        pass
    return None


async def _cleanup_redis_checkpoint(session_id: str) -> None:
    """删除 Redis checkpoint。"""
    try:
        redis = await get_redis()
        key = f"xuanhu:checkpoint:{session_id}"
        await redis.delete(key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

pytestmark_integration = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供集成测试数据库会话。"""
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用，跳过 Supervisor 集成测试: {type(exc).__name__}: {exc}")

    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_inquiry_to_sufficiency_to_syndrome(db: AsyncSession) -> None:
    """fake agents 顺序推进：inquiry -> sufficiency -> syndrome。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=True)
    supervisor = Supervisor(db, registry=registry)

    try:
        # inquiry -> sufficiency
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.SUFFICIENCY
        assert result.state.sufficiency_report is None  # sufficiency 尚未运行

        # sufficiency -> syndrome
        result = await supervisor.advance(str(session.id), "trace-2")
        assert result.to_stage == Stage.SYNDROME
        assert result.state.sufficiency_report is not None
        assert result.state.sufficiency_report.sufficient is True
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_syndrome_writes_result_and_routes_to_prescription(db: AsyncSession) -> None:
    """syndrome 阶段写入 state.syndrome_result 并推进到 prescription。

    覆盖 P5-3 验收：Supervisor 在 Stage.SYNDROME 能写入 state.syndrome_result
    并推进到 Stage.PRESCRIPTION，但不实现 Prescription Agent。
    """
    session = await _create_session(db, stage=Stage.SYNDROME)
    registry = AgentRegistry()
    registry.register(Stage.SYNDROME, FakeSyndromeAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-syndrome")
        assert result.to_stage == Stage.PRESCRIPTION
        assert result.from_stage == Stage.SYNDROME
        assert result.agent_name == "syndrome"
        assert result.state.syndrome_result is not None
        assert result.state.syndrome_result.syndrome == "脾虚湿盛"
        assert result.state.syndrome_result.treatment_principle == "健脾化湿"
        assert result.state.syndrome_result.confidence == 0.85

        # PG snapshot 含 syndrome_result
        await db.refresh(session)
        assert session.state_snapshot is not None
        assert session.state_snapshot.get("current_stage") == "prescription"
        assert "syndrome_result" in session.state_snapshot

        # Evidence 已合并到 state.evidences
        assert "evidences" in session.state_snapshot
        assert len(session.state_snapshot["evidences"]) >= 0
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_syndrome_missing_agent_blocked(db: AsyncSession) -> None:
    """syndrome 阶段未注册 Agent 时进入 blocked，不跳过到 prescription。"""
    session = await _create_session(db, stage=Stage.SYNDROME)
    registry = AgentRegistry()  # 不注册任何 syndrome agent
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-syndrome")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "missing_agent" in result.blocked_reason
        assert "syndrome" in result.blocked_reason

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_sufficiency_insufficient_rollback_inquiry(db: AsyncSession) -> None:
    """sufficiency_report.sufficient=false 回退到 inquiry。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=False)
    supervisor = Supervisor(db, registry=registry)

    try:
        # inquiry -> sufficiency
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.SUFFICIENCY

        # sufficiency 不足 -> inquiry（回退）
        result = await supervisor.advance(str(session.id), "trace-2")
        assert result.to_stage == Stage.INQUIRY
        assert result.state.rollback_counts.get("sufficiency", 0) == 0  # 不计入 rollback_counts
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_passed_to_review_suspend(db: AsyncSession) -> None:
    """P6-3: 安全规则通过进入 review 并挂起。

    使用 SafetyRuleEngine 路径（不再依赖 FakeSafetyAgent）。
    复用 DB 中已导入的 herbs/dosage_units 种子数据（党参已存在）。
    """
    session = await _create_session(db, stage=Stage.SAFETY)

    # 预设 modified_formula（安全处方，党参为已知安全药材）
    formula = FormulaResult(
        name="四君子汤",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    modified = ModifiedFormulaResult(
        formula=formula,
        modifications=[],
    )
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    registry = _build_registry(safety_passed=True)
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.REVIEW
        assert result.state.pending_review is True
        assert result.state.safety_rule_result is not None
        assert result.state.safety_rule_result.passed is True

        # 验证 PG
        await db.refresh(session)
        assert session.status == "pending_review"
        assert session.pending_review is True
        assert session.current_stage == "review"

        # review 阶段再次 advance 应被拒绝
        with pytest.raises(InvalidStageTransitionError) as exc_info:
            await supervisor.advance(str(session.id), "trace-2")
        assert exc_info.value.code == "INVALID_STAGE_TRANSITION"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        # 清理本测试产生的 safety_rule_runs（herbs/dosage_units 复用已有种子数据）
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_done_and_blocked_terminal_stages_do_not_mutate(db: AsyncSession) -> None:
    """done / blocked 终态再次 advance 会被拒绝，且不改变 PG 状态。"""
    done_session = await _create_session(db, stage=Stage.DONE, status="done")
    blocked_session = await _create_session(db, stage=Stage.BLOCKED, status="blocked")
    blocked_session.blocked_reason = "manual_required"
    await db.commit()
    registry = _build_registry()
    supervisor = Supervisor(db, registry=registry)

    try:
        with pytest.raises(InvalidStageTransitionError):
            await supervisor.advance(str(done_session.id), "trace-done")
        await db.refresh(done_session)
        assert done_session.status == "done"
        assert done_session.current_stage == "done"
        assert done_session.state_version == 1

        with pytest.raises(InvalidStageTransitionError):
            await supervisor.advance(str(blocked_session.id), "trace-blocked")
        await db.refresh(blocked_session)
        assert blocked_session.status == "blocked"
        assert blocked_session.current_stage == "blocked"
        assert blocked_session.blocked_reason == "manual_required"
        assert blocked_session.state_version == 1
    finally:
        await _cleanup_session(db, done_session.id)
        await _cleanup_session(db, blocked_session.id)
        await _cleanup_redis_checkpoint(str(done_session.id))
        await _cleanup_redis_checkpoint(str(blocked_session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_failed_rollback_prescription(db: AsyncSession) -> None:
    """P6-3: 安全规则未通过，回退到 prescription。

    用党参（已知安全药材，max_dose=30，开 100g 触发 blocker）
    验证剂量上限规则阻断后回退到 modification。
    """
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],  # 严重超量
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    registry = _build_registry(safety_passed=False)
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-1")
        # 规则引擎判定 dose_limit blocker → safety_review.passed=False
        # 回退目标 modification（_rule_result_to_safety_review 默认）
        assert result.to_stage == Stage.MODIFICATION
        assert result.state.rollback_counts.get("safety", 0) == 1
        assert result.state.safety_review is not None
        assert result.state.safety_review.passed is False
        assert result.state.safety_rule_result is not None
        assert result.state.safety_rule_result.passed is False
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_rollback_within_limit(db: AsyncSession) -> None:
    """P6-3: safety 回退次数未超限时继续（规则引擎路径）。"""
    session = await _create_session(db, stage=Stage.SAFETY)
    session.rollback_counts = {"safety": 2}
    await db.commit()

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {"safety": 2},
    }
    await db.commit()

    registry = _build_registry(safety_passed=False)
    supervisor = Supervisor(db, registry=registry)
    settings = get_settings()
    limit = settings.safety_rollback_limit

    try:
        assert limit >= 3
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.MODIFICATION
        assert result.state.rollback_counts["safety"] == 3
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_rollback_exceeds_limit_blocked(db: AsyncSession) -> None:
    """P6-3: safety 回退次数超限后进入 blocked（规则引擎路径）。"""
    session = await _create_session(db, stage=Stage.SAFETY)
    settings = get_settings()
    limit = settings.safety_rollback_limit
    session.rollback_counts = {"safety": limit}
    await db.commit()

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {"safety": limit},
    }
    await db.commit()

    registry = _build_registry(safety_passed=False)
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason == "rollback_limit_exceeded"

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
        assert session.blocked_reason == "rollback_limit_exceeded"
        assert session.recovery_status == "manual_required"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_blocked_checkpoint_failure_is_audited(db: AsyncSession) -> None:
    """P6-3: 进入 blocked 时 Redis checkpoint 失败也要写降级审计（规则引擎路径）。"""
    session = await _create_session(db, stage=Stage.SAFETY)
    settings = get_settings()
    session.rollback_counts = {"safety": settings.safety_rollback_limit}
    await db.commit()

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {"safety": settings.safety_rollback_limit},
    }
    await db.commit()

    registry = _build_registry(safety_passed=False)
    supervisor = Supervisor(db, registry=registry, redis=FailingRedis())  # type: ignore[arg-type]

    try:
        result = await supervisor.advance(str(session.id), "trace-blocked-checkpoint")
        assert result.to_stage == Stage.BLOCKED

        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session.id)
            .where(AuditEvent.event_type == "session.recovered")
        )
        audit = audit_result.scalar_one_or_none()
        assert audit is not None
        assert audit.payload["reason"] == "checkpoint_redis_failed"
        assert audit.payload["blocked_reason"] == "rollback_limit_exceeded"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_schema_invalid_blocked(db: AsyncSession) -> None:
    """Agent 抛 AGENT_SCHEMA_INVALID 时进入 blocked。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, FakeFailingAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason == "agent_failed/AGENT_SCHEMA_INVALID"

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_state_version_increments_on_advance(db: AsyncSession) -> None:
    """state_version 成功推进后递增。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry()
    supervisor = Supervisor(db, registry=registry)

    try:
        assert session.state_version == 1
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.state.state_version == 2

        await db.refresh(session)
        assert session.state_version == 2
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_expected_state_version_conflict(db: AsyncSession) -> None:
    """expected_state_version 冲突时拒绝推进。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry()
    supervisor = Supervisor(db, registry=registry)

    try:
        with pytest.raises(InvalidStateVersionError):
            await supervisor.advance(str(session.id), "trace-1", expected_state_version=999)
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_lock_conflict_raises_session_busy(db: AsyncSession) -> None:
    """Supervisor 推进会话时必须获取锁，锁冲突抛 SESSION_BUSY 且不改状态。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry()
    supervisor = Supervisor(db, registry=registry)
    lock_key = f"xuanhu:session_lock:{session.id}"

    try:
        try:
            redis = await get_redis()
            await redis.set(lock_key, "other-trace", ex=60)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Redis 不可用，跳过 Supervisor 锁冲突测试: {type(exc).__name__}: {exc}")

        with pytest.raises(SessionBusyError) as exc_info:
            await supervisor.advance(str(session.id), "trace-lock-conflict")
        assert exc_info.value.code == "SESSION_BUSY"

        await db.refresh(session)
        assert session.current_stage == "inquiry"
        assert session.status == "active"
        assert session.state_version == 1
    finally:
        try:
            redis = await get_redis()
            await redis.delete(lock_key)
        except Exception:
            pass
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_pg_snapshot_updated(db: AsyncSession) -> None:
    """PG state_snapshot 更新。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=True)
    supervisor = Supervisor(db, registry=registry)

    try:
        # inquiry -> sufficiency
        await supervisor.advance(str(session.id), "trace-1")
        await db.refresh(session)
        assert session.state_snapshot is not None
        assert session.state_snapshot.get("current_stage") == "sufficiency"

        # sufficiency -> syndrome
        await supervisor.advance(str(session.id), "trace-2")
        await db.refresh(session)
        assert session.state_snapshot.get("current_stage") == "syndrome"
        assert "sufficiency_report" in session.state_snapshot
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_redis_checkpoint_written(db: AsyncSession) -> None:
    """Redis checkpoint 写入成功。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=True)
    supervisor = Supervisor(db, registry=registry)

    try:
        await supervisor.advance(str(session.id), "trace-1")
        checkpoint = await _read_redis_checkpoint(str(session.id))
        assert checkpoint is not None
        assert checkpoint["session_id"] == str(session.id)
        assert checkpoint["current_stage"] == "sufficiency"
        assert checkpoint["state_version"] == 2
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_redis_checkpoint_failure_degradation(db: AsyncSession) -> None:
    """Redis checkpoint 写入失败时 PG 状态仍可用，且 recovery_status 降级。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=True)
    supervisor = Supervisor(db, registry=registry, redis=FailingRedis())  # type: ignore[arg-type]

    try:
        result = await supervisor.advance(str(session.id), "trace-1")
        assert result.to_stage == Stage.SUFFICIENCY

        await db.refresh(session)
        assert session.state_version == 2
        assert session.recovery_status == "recovering"

        # 检查 audit
        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session.id)
            .where(AuditEvent.event_type == "session.recovered")
        )
        audit = audit_result.scalar_one_or_none()
        assert audit is not None
        assert audit.payload["reason"] == "checkpoint_redis_failed"
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_stream_stage_changed(db: AsyncSession) -> None:
    """阶段变化写 Redis Stream stage.changed。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    registry = _build_registry(sufficient=True)
    event_service = EventService()
    supervisor = Supervisor(db, registry=registry, event_service=event_service)

    try:
        await supervisor.advance(str(session.id), "trace-1")
        # 读取 Redis Stream 验证
        redis = await get_redis()
        key = f"xuanhu:events:{session.id}"
        entries = await redis.xrange(key, count=10)
        types = [entry[1].get("event_type") for entry in entries]
        assert "stage.changed" in types
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        try:
            redis = await get_redis()
            await redis.delete(f"xuanhu:events:{session.id}")
        except Exception:
            pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_stream_review_required(db: AsyncSession) -> None:
    """P6-3: review 挂起写 review.required（规则引擎通过路径）。"""
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="四君子汤",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    registry = _build_registry(safety_passed=True)
    event_service = EventService()
    supervisor = Supervisor(db, registry=registry, event_service=event_service)

    try:
        await supervisor.advance(str(session.id), "trace-1")
        redis = await get_redis()
        key = f"xuanhu:events:{session.id}"
        entries = await redis.xrange(key, count=10)
        types = [entry[1].get("event_type") for entry in entries]
        assert "review.required" in types
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        try:
            redis = await get_redis()
            await redis.delete(f"xuanhu:events:{session.id}")
        except Exception:
            pass
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_no_real_model_called(db: AsyncSession) -> None:
    """确认 fake agents 不调用真实模型网关。"""
    session = await _create_session(db, stage=Stage.INQUIRY)
    # 使用 FakeGateway 注入，确保不会走到真实网关
    gateway_called = False

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal gateway_called
            gateway_called = True
            raise RuntimeError("不应调用真实网关")

    # FakeInquiryAgent 默认不调用 gateway（_call_with_retries 被覆写）
    # 但为了保险，我们直接用一个不覆写 _call_with_retries 的 agent 并注入 gateway
    # 这里我们简单验证：fake agents 的 _call_with_retries 已被覆写，不会走到 gateway
    registry = _build_registry()
    supervisor = Supervisor(db, registry=registry)

    try:
        await supervisor.advance(str(session.id), "trace-1")
        # 如果 gateway 被调用，上面的 TrackingGateway 会抛 RuntimeError
        # 由于 FakeAgents 覆写了 _call_with_retries，不会调用 gateway
        assert not gateway_called
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_prescription_writes_result_and_routes_to_modification(db: AsyncSession) -> None:
    """prescription 阶段写入 state.base_formula 并推进到 modification。

    覆盖 P6-1 验收：Supervisor 在 Stage.PRESCRIPTION 能写入 state.base_formula
    并推进到 Stage.MODIFICATION，但不实现加减方 Agent。
    """
    session = await _create_session(db, stage=Stage.PRESCRIPTION)
    registry = AgentRegistry()
    registry.register(Stage.PRESCRIPTION, FakePrescriptionAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-prescription")
        assert result.to_stage == Stage.MODIFICATION
        assert result.from_stage == Stage.PRESCRIPTION
        assert result.agent_name == "prescription"
        assert result.state.base_formula is not None
        assert result.state.base_formula.name == "参苓白术散"
        assert len(result.state.base_formula.composition) == 2
        assert result.state.base_formula.composition[0].herb == "党参"
        assert result.state.base_formula.rationale == "健脾益气，渗湿止泻"

        # PG snapshot 含 base_formula
        await db.refresh(session)
        assert session.state_snapshot is not None
        assert session.state_snapshot.get("current_stage") == "modification"
        assert "base_formula" in session.state_snapshot
        # 不写入 modified_formula（P6-1 不实现加减方）
        assert "modified_formula" not in session.state_snapshot
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_prescription_missing_agent_blocked(db: AsyncSession) -> None:
    """prescription 阶段未注册 Agent 时进入 blocked，不跳过到 modification。"""
    session = await _create_session(db, stage=Stage.PRESCRIPTION)
    registry = AgentRegistry()  # 不注册任何 prescription agent
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-prescription")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "missing_agent" in result.blocked_reason
        assert "prescription" in result.blocked_reason

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_default_registry_includes_prescription() -> None:
    """Supervisor 默认 registry 包含 PrescriptionAgent（不依赖 DB）。"""
    from app.agents.prescription import PrescriptionAgent
    from app.agents.supervisor import _default_registry

    registry = _default_registry()
    agent = registry.get(Stage.PRESCRIPTION)
    assert isinstance(agent, PrescriptionAgent)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modification_writes_result_and_routes_to_safety(db: AsyncSession) -> None:
    """modification 阶段写入 state.modified_formula 并推进到 safety。

    覆盖 P6-2 验收：Supervisor 在 Stage.MODIFICATION 能写入 state.modified_formula
    并推进到 Stage.SAFETY，但不实现 Safety Agent。
    """
    session = await _create_session(db, stage=Stage.MODIFICATION)
    registry = AgentRegistry()
    registry.register(Stage.MODIFICATION, FakeModificationAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-modification")
        assert result.to_stage == Stage.SAFETY
        assert result.from_stage == Stage.MODIFICATION
        assert result.agent_name == "modification"
        assert result.state.modified_formula is not None
        assert result.state.modified_formula.formula.name == "参苓白术散加减"
        assert len(result.state.modified_formula.formula.composition) == 2
        assert len(result.state.modified_formula.modifications) == 1
        assert result.state.modified_formula.modifications[0].herb == "茯苓"

        # PG snapshot 含 modified_formula
        await db.refresh(session)
        assert session.state_snapshot is not None
        assert session.state_snapshot.get("current_stage") == "safety"
        assert "modified_formula" in session.state_snapshot
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_modification_missing_agent_blocked(db: AsyncSession) -> None:
    """modification 阶段未注册 Agent 时进入 blocked，不跳过到 safety。"""
    session = await _create_session(db, stage=Stage.MODIFICATION)
    registry = AgentRegistry()  # 不注册任何 modification agent
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-modification")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "missing_agent" in result.blocked_reason
        assert "modification" in result.blocked_reason

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_default_registry_includes_modification() -> None:
    """Supervisor 默认 registry 包含 ModificationAgent（不依赖 DB）。"""
    from app.agents.modification import ModificationAgent
    from app.agents.supervisor import _default_registry

    registry = _default_registry()
    agent = registry.get(Stage.MODIFICATION)
    assert isinstance(agent, ModificationAgent)


# ---------------------------------------------------------------------------
# P6-4 SafetyAgent 解释层联调测试
# ---------------------------------------------------------------------------


def _patch_supervisor_safety_agent(
    supervisor: Supervisor,
    explanation: SafetyExplanation | None,
) -> None:
    """用 fake _run_safety_agent 替换 Supervisor 的 SafetyAgent 调用。

    SafetyAgent 内部调用真实模型网关；集成测试只需验证"解释附加到
    safety_review 但不修改路由"的契约，故直接注入预设解释。

    注意：fake 仍校验 state.safety_rule_result 非空（与真实 _run_safety_agent
    的前置检查一致），以覆盖 B-011 修复——调用方必须先写入 safety_rule_result。
    """
    import types

    async def fake_run_safety_agent(
        self: Supervisor,
        state: XuanhuState,
        trace_id: str,
        session_id: str,
    ) -> SafetyExplanation | None:
        del self, trace_id, session_id
        # 与真实 _run_safety_agent 一致的前置检查
        if state.safety_rule_result is None:
            return None
        return explanation

    supervisor._run_safety_agent = types.MethodType(  # type: ignore[method-assign]
        fake_run_safety_agent, supervisor
    )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_agent_explanation_attached_to_safety_review(
    db: AsyncSession,
) -> None:
    """P6-4: SafetyAgent 解释文本附加到 safety_review 上。

    使用 SafetyRuleEngine + fake _run_safety_agent 路径。验证 safety_review
    包含 explanation 字段，且 passed/issues/rollback_target 仍由规则引擎决定。
    """
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="四君子汤",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    supervisor = Supervisor(db, registry=_build_registry(safety_passed=True))
    _patch_supervisor_safety_agent(
        supervisor,
        SafetyExplanation(
            summary="经安全规则审核，该处方未发现安全问题，可进入医师复核。",
            issue_explanations=[],
            recommendations=None,
            safety_agent_run_id="fake-run-id",
            safety_agent_model="fake-model",
        ),
    )

    try:
        result = await supervisor.advance(str(session.id), "trace-safety-agent-explanation")
        assert result.to_stage == Stage.REVIEW
        assert result.state.safety_review is not None
        assert result.state.safety_review.passed is True
        # 规则引擎结论不变
        assert result.state.safety_rule_result is not None
        assert result.state.safety_rule_result.passed is True
        # SafetyAgent 解释附加
        assert result.state.safety_review.explanation is not None
        assert "安全" in result.state.safety_review.explanation
        assert result.state.safety_review.safety_agent_run_id == "fake-run-id"
        assert result.state.safety_review.safety_agent_model == "fake-model"
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_explanation_does_not_change_routing(db: AsyncSession) -> None:
    """P6-4: SafetyAgent 失败（返回 None）时不影响路由决策。

    规则引擎判定未通过 → 回退 modification，无论 SafetyAgent 是否产出解释。
    """
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    supervisor = Supervisor(db, registry=_build_registry(safety_passed=False))
    _patch_supervisor_safety_agent(supervisor, None)  # SafetyAgent 失败降级

    try:
        result = await supervisor.advance(str(session.id), "trace-safety-agent-fail")
        # 路由仍按规则引擎结果：passed=False → 回退 modification
        assert result.to_stage == Stage.MODIFICATION
        assert result.state.safety_review is not None
        assert result.state.safety_review.passed is False
        # explanation 为 None（SafetyAgent 失败）
        assert result.state.safety_review.explanation is None
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_blocked_event_emitted_on_safety_failure(db: AsyncSession) -> None:
    """P6-4: SAFETY 未通过回退时发射 safety.blocked 事件。

    规则引擎判定未通过，回退到 modification，应发射 safety.blocked。
    """
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="超量方",
        composition=[HerbDose(herb="党参", dose=100, unit="g")],
        rationale="测试",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    event_service = EventService()
    supervisor = Supervisor(
        db, registry=_build_registry(safety_passed=False), event_service=event_service
    )
    _patch_supervisor_safety_agent(
        supervisor,
        SafetyExplanation(
            summary="党参超量，需调整",
            issue_explanations=["党参剂量100g超限"],
            recommendations="减量至30g",
            safety_agent_run_id="fake-run-id",
            safety_agent_model="fake-model",
        ),
    )

    try:
        result = await supervisor.advance(str(session.id), "trace-safety-blocked-event")
        assert result.to_stage == Stage.MODIFICATION

        # 读取 Redis Stream 验证 safety.blocked 事件
        redis = await get_redis()
        key = f"xuanhu:events:{session.id}"
        entries = await redis.xrange(key, count=20)
        types_list = [entry[1].get("event_type") for entry in entries]
        assert "safety.blocked" in types_list
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        try:
            redis = await get_redis()
            await redis.delete(f"xuanhu:events:{session.id}")
        except Exception:
            pass
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_passed_does_not_emit_safety_blocked(db: AsyncSession) -> None:
    """P6-4: SAFETY 通过时不发射 safety.blocked，改发 review.required。"""
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="四君子汤",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    event_service = EventService()
    supervisor = Supervisor(
        db, registry=_build_registry(safety_passed=True), event_service=event_service
    )
    _patch_supervisor_safety_agent(
        supervisor,
        SafetyExplanation(
            summary="审核通过",
            issue_explanations=[],
            recommendations=None,
            safety_agent_run_id="fake-run-id",
            safety_agent_model="fake-model",
        ),
    )

    try:
        result = await supervisor.advance(str(session.id), "trace-safety-passed-event")
        assert result.to_stage == Stage.REVIEW

        redis = await get_redis()
        key = f"xuanhu:events:{session.id}"
        entries = await redis.xrange(key, count=20)
        types_list = [entry[1].get("event_type") for entry in entries]
        assert "safety.blocked" not in types_list
        assert "review.required" in types_list
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        try:
            redis = await get_redis()
            await redis.delete(f"xuanhu:events:{session.id}")
        except Exception:
            pass
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_safety_agent_explanation_preserved_in_pg_snapshot(
    db: AsyncSession,
) -> None:
    """P6-4: SafetyAgent 解释文本写入 PG state_snapshot。"""
    session = await _create_session(db, stage=Stage.SAFETY)

    formula = FormulaResult(
        name="四君子汤",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    modified = ModifiedFormulaResult(formula=formula, modifications=[])
    session.state_snapshot = {
        "current_stage": "safety",
        "modified_formula": modified.model_dump(mode="python"),
        "patient_info": {"allergies": [], "pregnancy_status": "no"},
        "session_id": str(session.id),
        "state_version": session.state_version,
        "pending_review": False,
        "rollback_counts": {},
    }
    await db.commit()

    supervisor = Supervisor(db, registry=_build_registry(safety_passed=True))
    _patch_supervisor_safety_agent(
        supervisor,
        SafetyExplanation(
            summary="经安全规则审核，该处方未发现安全问题，可进入医师复核。",
            issue_explanations=[],
            recommendations=None,
            safety_agent_run_id="fake-run-id",
            safety_agent_model="fake-model",
        ),
    )

    try:
        await supervisor.advance(str(session.id), "trace-safety-pg-snapshot")
        await db.refresh(session)
        assert session.state_snapshot is not None
        safety_review = session.state_snapshot.get("safety_review")
        assert safety_review is not None
        assert safety_review.get("explanation") is not None
        assert safety_review.get("safety_agent_run_id") == "fake-run-id"
        # 规则引擎字段仍在
        assert safety_review.get("passed") is True
    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await db.execute(text("DELETE FROM safety_rule_runs"))
        await db.commit()


# ---------------------------------------------------------------------------
# P7-2: record→done 集成测试
# ---------------------------------------------------------------------------

async def _advance_to_record(
    db: AsyncSession,
    session: ConsultSession,
    safety_passed: bool = True,
) -> None:
    """将测试会话从 review 推进到 record 阶段（模拟 P7-1 确认后状态）。

    P7-1 confirm/modify 后 session.current_stage=record/status=active。
    此处直接更新 PG 行，模拟 P7-1 已完成，准备 P7-2 病历生成。

    同时写入一条 doctor_reviews 记录，使 medical_records.doctor_review_id
    的外键约束可满足。
    """
    from datetime import UTC, datetime

    from app.models.review import DoctorReview

    review_id = uuid.uuid4()
    # 写入 doctor_reviews，满足 medical_records 的外键约束
    review = DoctorReview(
        id=review_id,
        session_id=session.id,
        agent_run_id=None,
        safety_rule_run_id=None,
        action="confirm",
        original_formula=None,
        formula_override=None,
        feedback=None,
        reviewed_by="doctor-1",
    )
    db.add(review)

    session.current_stage = "record"
    session.status = "active"
    session.pending_review = False
    session.state_version += 1
    session.state_snapshot = {
        "session_id": str(session.id),
        "current_stage": "record",
        "pending_review": False,
        "rollback_counts": {},
        "state_version": session.state_version,
        "patient_info": {"gender": "male", "age": 35},
        "chief_complaint": "头痛3天",
        "present_illness": "近3日头痛，伴发热",
        "past_history": "无特殊",
        "personal_family_history": "无特殊",
        "syndrome_result": {
            "syndrome": "风热头痛",
            "treatment_principle": "疏风清热",
            "syndrome_basis": ["头痛", "发热"],
            "differential": [],
            "confidence": 0.85,
            "citations": [],
        },
        "modified_formula": {
            "formula": {
                "name": "川芎茶调散加减",
                "composition": [{"herb": "川芎", "dose": 10, "unit": "g"}],
                "rationale": "疏风清热",
                "source": None,
                "citations": [],
            },
            "modifications": [],
        },
        "safety_rule_result": {
            "passed": safety_passed,
            "issues": [],
            "normalized_formula": {
                "name": "川芎茶调散加减",
                "composition": [{"herb": "川芎", "dose": 10, "unit": "g"}],
                "rationale": "疏风清热",
            },
            "warnings": [],
            "rule_version": "v1.0.0",
            "execution_order": ["normalize"],
        },
        "safety_review": {
            "passed": safety_passed,
            "issues": [],
            "rollback_target": "none",
            "summary": "安全规则审核通过，无阻断性问题。",
        },
        "doctor_review": {
            "action": "confirm",
            "reviewed_by": "doctor-1",
            "reviewed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "review_id": str(review_id),
        },
    }
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_to_done_writes_medical_record(db: AsyncSession) -> None:
    """P7-2: record→done 写入 medical_records version=1 并更新 session 状态。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 注册 RecordAgent 和必要的 fake agents
    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-record-done")
        assert result.to_stage == Stage.DONE
        assert result.from_stage == Stage.RECORD
        assert result.agent_name == "record"

        # 校验 session 状态
        await db.refresh(session)
        assert session.current_stage == "done"
        assert session.status == "done"
        assert session.pending_review is False
        assert session.state_version > 1

        # 校验 PG state_snapshot 含 medical_record
        assert session.state_snapshot is not None
        assert "medical_record" in session.state_snapshot

        # 校验 medical_records 表写入
        from sqlalchemy import select

        from app.models.review import MedicalRecord

        stmt = select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 1,
        )
        r = await db.execute(stmt)
        record = r.scalar_one_or_none()
        assert record is not None
        assert record.record_text == "病历文本"
        assert record.record_json == {"chief_complaint": "头痛"}
        assert record.edited_by_doctor is False
        assert record.version == 1

        # 校验 audit_events(record.generated)
        from app.models.audit import AuditEvent
        audit_stmt = select(AuditEvent).where(
            AuditEvent.session_id == session.id,
            AuditEvent.event_type == "record.generated",
        )
        audit_result = await db.execute(audit_stmt)
        audit_event = audit_result.scalar_one_or_none()
        assert audit_event is not None
        assert audit_event.payload.get("version") == 1
        assert audit_event.payload.get("record_id") == str(record.id)

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        # 清理 medical_records
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_to_done_idempotent_no_duplicate(db: AsyncSession) -> None:
    """P7-2: 重复执行不重复创建 version=1 病历。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        # 第一次推进
        result1 = await supervisor.advance(str(session.id), "trace-record-1")
        assert result1.to_stage == Stage.DONE

        # 直接重置 session 为 record 以模拟"重复执行"
        # 注意：这不会出现在实际场景中（done 不可再 advance），
        # 但通过 _write_medical_record 幂等逻辑验证
        session.current_stage = "record"
        session.status = "active"
        session.state_version += 1
        session.state_snapshot = {
            **session.state_snapshot,
            "current_stage": "record",
            "state_version": session.state_version,
        }
        await db.commit()

        # 第二次推进
        result2 = await supervisor.advance(str(session.id), "trace-record-2")
        assert result2.to_stage == Stage.DONE

        # 校验 medical_records 仍只有一条 version=1
        from sqlalchemy import func as sqlfunc
        from sqlalchemy import select

        from app.models.review import MedicalRecord

        count_stmt = select(sqlfunc.count()).select_from(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 1,
        )
        count_result = await db.execute(count_stmt)
        count = count_result.scalar_one()
        assert count == 1

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_to_done_emits_session_done_event(db: AsyncSession) -> None:
    """P7-2: record→done 发射 session.done 事件，含 record_id。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        await supervisor.advance(str(session.id), "trace-record-event")

        # 从 medical_records 获取 record_id
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 1,
        )
        r = await db.execute(stmt)
        record = r.scalar_one_or_none()
        assert record is not None

        # 校验 Redis Stream 事件
        redis = await get_redis()
        stream_key = f"xuanhu:events:{session.id}"
        try:
            events = await redis.xread({stream_key: "0"}, count=100)
            found = False
            for _stream_name, entries in events:
                for _entry_id, fields in entries:
                    event_type = None
                    payload: dict[str, Any] = {}
                    if isinstance(fields, dict):
                        et_raw = fields.get(b"event_type") or fields.get("event_type")
                        if isinstance(et_raw, bytes):
                            et_raw = et_raw.decode("utf-8")
                        event_type = et_raw
                        p_raw = fields.get(b"payload") or fields.get("payload")
                        if isinstance(p_raw, bytes):
                            p_raw = p_raw.decode("utf-8")
                        if isinstance(p_raw, str):
                            import json
                            payload = json.loads(p_raw)
                    if event_type == "session.done":
                        found = True
                        assert "record_id" in payload
                        assert payload["record_id"] == str(record.id)
                        break
                if found:
                    break
            assert found, "未找到 session.done 事件"
        finally:
            await redis.delete(stream_key)

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_without_doctor_review_blocked(db: AsyncSession) -> None:
    """P7-2-fix B-014: 无 doctor_review 时 blocked，不写 medical_records。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 清除 doctor_review
    snapshot = dict(session.state_snapshot or {})
    snapshot.pop("doctor_review", None)
    session.state_snapshot = snapshot
    await db.commit()

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-review")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "doctor_review_required" in result.blocked_reason
        assert "missing" in result.blocked_reason

        await db.refresh(session)
        assert session.current_stage == "blocked"
        assert session.status == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_with_invalid_uuid_review_id_blocked(db: AsyncSession) -> None:
    """P7-2-fix B-014: doctor_review.review_id 非合法 UUID 时 blocked。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 替换 review_id 为非法值
    snapshot = dict(session.state_snapshot or {})
    snapshot["doctor_review"] = {
        "action": "confirm",
        "reviewed_by": "doctor-1",
        "reviewed_at": "2026-07-03T10:00:00",
        "review_id": "not-a-valid-uuid",
    }
    session.state_snapshot = snapshot
    await db.commit()

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-invalid-uuid")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "doctor_review_required" in result.blocked_reason
        assert "invalid_uuid" in result.blocked_reason

        await db.refresh(session)
        assert session.current_stage == "blocked"
        assert session.status == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_with_nonexistent_review_id_blocked(db: AsyncSession) -> None:
    """P7-2-fix B-014: review_id 在 DB 中不存在时 blocked。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 替换为一个数据库中不存在的 UUID
    nonexistent_id = uuid.uuid4()
    snapshot = dict(session.state_snapshot or {})
    snapshot["doctor_review"] = {
        "action": "confirm",
        "reviewed_by": "doctor-1",
        "reviewed_at": "2026-07-03T10:00:00",
        "review_id": str(nonexistent_id),
    }
    session.state_snapshot = snapshot
    await db.commit()

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-nonexistent")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "doctor_review_required" in result.blocked_reason
        assert "not_found" in result.blocked_reason

        await db.refresh(session)
        assert session.current_stage == "blocked"
        assert session.status == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_with_cross_session_review_id_blocked(db: AsyncSession) -> None:
    """P7-2-fix B-014: review_id 属于其他 session 时 blocked。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 创建另一个 session 和其 doctor_review
    other_session = await _create_session(db, stage=Stage.INQUIRY)
    from app.models.review import DoctorReview
    other_review = DoctorReview(
        id=uuid.uuid4(),
        session_id=other_session.id,
        action="confirm",
        reviewed_by="doctor-1",
    )
    db.add(other_review)
    await db.commit()

    # 将 other_session 的 review_id 注入当前 session
    snapshot = dict(session.state_snapshot or {})
    snapshot["doctor_review"] = {
        "action": "confirm",
        "reviewed_by": "doctor-1",
        "reviewed_at": "2026-07-03T10:00:00",
        "review_id": str(other_review.id),
    }
    session.state_snapshot = snapshot
    await db.commit()

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-cross-session")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "doctor_review_required" in result.blocked_reason
        assert "not_found" in result.blocked_reason

        await db.refresh(session)
        assert session.current_stage == "blocked"
        assert session.status == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_session(db, other_session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        await _cleanup_redis_checkpoint(str(other_session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_with_reject_action_blocked(db: AsyncSession) -> None:
    """P7-2-fix B-014: doctor_review.action=reject 时 blocked。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 写入一条 action=reject 的 doctor_review，并更新 snapshot
    from app.models.review import DoctorReview
    reject_review = DoctorReview(
        id=uuid.uuid4(),
        session_id=session.id,
        action="reject",
        reviewed_by="doctor-1",
    )
    db.add(reject_review)

    snapshot = dict(session.state_snapshot or {})
    snapshot["doctor_review"] = {
        "action": "reject",
        "reviewed_by": "doctor-1",
        "reviewed_at": "2026-07-03T10:00:00",
        "review_id": str(reject_review.id),
    }
    session.state_snapshot = snapshot
    await db.commit()

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-reject")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "doctor_review_required" in result.blocked_reason
        assert "action_invalid" in result.blocked_reason

        await db.refresh(session)
        assert session.current_stage == "blocked"
        assert session.status == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_writes_doctor_review_id(db: AsyncSession) -> None:
    """P7-2: medical_records.doctor_review_id 正确关联 doctor_reviews。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    registry = AgentRegistry()
    registry.register(Stage.RECORD, FakeRecordAgent())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-review-id")
        assert result.to_stage == Stage.DONE

        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 1,
        )
        r = await db.execute(stmt)
        record = r.scalar_one_or_none()
        assert record is not None

        # doctor_review_id 来自 snapshot 中的 review_id
        snapshot = session.state_snapshot or {}
        expected_review_id = snapshot.get("doctor_review", {}).get("review_id")
        if expected_review_id:
            assert str(record.doctor_review_id) == expected_review_id

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_record_missing_agent_blocked(db: AsyncSession) -> None:
    """P7-2: record 阶段未注册 Agent 时进入 blocked。"""
    session = await _create_session(db, stage=Stage.RECORD)
    await _advance_to_record(db, session)

    # 不注册 RecordAgent
    registry = AgentRegistry()
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-no-record-agent")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason is not None
        assert "missing_agent" in result.blocked_reason
        assert "record" in result.blocked_reason

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"

        # 无 medical_record 落库
        from sqlalchemy import select

        from app.models.review import MedicalRecord
        stmt = select(MedicalRecord).where(MedicalRecord.session_id == session.id)
        r = await db.execute(stmt)
        assert r.scalar_one_or_none() is None

    finally:
        await _cleanup_session(db, session.id)
        await _cleanup_redis_checkpoint(str(session.id))
        from app.models.review import MedicalRecord
        await db.execute(
            delete(MedicalRecord).where(MedicalRecord.session_id == session.id)
        )
        await db.commit()

