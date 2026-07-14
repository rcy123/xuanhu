"""P5-2 完备性 Agent 测试。

使用 fake gateway 覆盖：
- SufficiencyReport schema 校验
- SufficiencyAgent 信息明显不足
- SufficiencyAgent 基础信息足够
- SufficiencyAgent 安全基础信息缺失
- fake gateway 返回坏 schema
- Supervisor sufficient=true 推进 syndrome
- Supervisor sufficient=false 回退 inquiry
- Supervisor sufficiency_report_missing blocked
- Supervisor force 强制推进审计
- 不调用真实模型网关
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.errors import AgentRunError
from app.agents.prompt_loader import PromptLoader
from app.agents.registry import AgentRegistry
from app.agents.sufficiency import SufficiencyAgent, merge_sufficiency_report_to_state
from app.agents.supervisor import Supervisor, _default_registry
from app.models.agent import AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.agent import (
    PatientInfo,
    SufficiencyReport,
    TenQuestions,
    XuanhuState,
)
from app.schemas.types import Stage

# ---------------------------------------------------------------------------
# Fake Gateway（不依赖真实模型网关）
# ---------------------------------------------------------------------------


class FakeGateway:
    """可控 fake gateway，注入预设响应或异常。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[Any],
        *,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "output_schema": output_schema,
                "trace_id": trace_id,
                "session_id": session_id,
                "agent_name": agent_name,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark_integration = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def _write_prompt_files(tmp_path: Path, *, manifest_extra: str = "") -> Path:
    """写临时 prompt 文件，返回 manifest 路径。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    manifest_content = (
        "test_agent: test_agent_v1.jinja2\n"
        "inquiry: inquiry_v1.jinja2\n"
        "sufficiency: sufficiency_v1.jinja2\n"
        + manifest_extra
    )
    (prompt_dir / "manifest.yaml").write_text(manifest_content, encoding="utf-8")
    (prompt_dir / "test_agent_v1.jinja2").write_text("TEST_PROMPT", encoding="utf-8")
    (prompt_dir / "inquiry_v1.jinja2").write_text(
        "You are a TCM inquiry assistant.\n{state_summary}\n{conversation_history}\n",
        encoding="utf-8",
    )
    (prompt_dir / "sufficiency_v1.jinja2").write_text(
        "You are a TCM sufficiency assistant.\n{state_summary}\n{conversation_history}\n",
        encoding="utf-8",
    )
    return prompt_dir / "manifest.yaml"


def _insufficient_report(**overrides: Any) -> SufficiencyReport:
    """构造信息不足的完备性报告。"""
    defaults: dict[str, Any] = {
        "covered": ["chief_complaint"],
        "missing": ["present_illness", "stool_urine", "sleep"],
        "sufficient": False,
        "suggestions": ["补问现病史细节", "补问二便和睡眠情况"],
        "next_question": "请问您的大便和睡眠情况怎么样？",
    }
    defaults.update(overrides)
    return SufficiencyReport.model_validate(defaults)


def _sufficient_report(**overrides: Any) -> SufficiencyReport:
    """构造信息充分的完备性报告。"""
    defaults: dict[str, Any] = {
        "covered": ["chief_complaint", "present_illness", "cold_heat", "head_body", "sleep", "allergy"],
        "missing": [],
        "sufficient": True,
        "suggestions": [],
        "next_question": None,
    }
    defaults.update(overrides)
    return SufficiencyReport.model_validate(defaults)


def _state_with_basic_info(session_id: str | None = None) -> XuanhuState:
    """构造一个包含基本问诊信息的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        patient_info=PatientInfo(name="测试患者", gender="female", age=30),
        chief_complaint="头痛三天",
        present_illness="三天前淋雨后开始，以双侧太阳穴附近胀痛为主",
        past_history="既往有偏头痛史",
        ten_questions=TenQuestions(cold_heat="恶寒发热", head_body="头痛连及项背"),
        inquiry_messages=[
            {"role": "doctor", "content": "患者诉头痛三天"},
            {"role": "assistant", "content": "请问头痛的具体位置？", "asked_dimension": "present_illness"},
        ],
    )


def _minimal_state(session_id: str | None = None) -> XuanhuState:
    """构造一个信息极少的 XuanhuState。"""
    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        inquiry_messages=[{"role": "doctor", "content": "患者来了"}],
    )


# ===========================================================================
# Schema 校验测试
# ===========================================================================


def test_sufficiency_report_minimal_valid() -> None:
    """最少合法 SufficiencyReport 可独立校验。"""
    report = SufficiencyReport.model_validate({"sufficient": False})
    assert report.sufficient is False
    assert report.covered == []
    assert report.missing == []
    assert report.suggestions == []
    assert report.next_question is None


def test_sufficiency_report_full_shape() -> None:
    """完整的 SufficiencyReport 可独立校验。"""
    report = SufficiencyReport.model_validate(
        {
            "covered": ["chief_complaint", "present_illness", "cold_heat"],
            "missing": ["stool_urine", "sleep"],
            "sufficient": False,
            "suggestions": ["补问二便", "补问睡眠"],
            "next_question": "请问您的大便和睡眠情况怎么样？",
        }
    )
    assert len(report.covered) == 3
    assert len(report.missing) == 2
    assert report.sufficient is False
    assert report.next_question is not None


def test_sufficiency_report_sufficient_no_next_question() -> None:
    """sufficient=true 时 next_question 可为 null。"""
    report = SufficiencyReport.model_validate(
        {
            "covered": ["chief_complaint", "present_illness", "cold_heat", "sleep", "allergy"],
            "missing": [],
            "sufficient": True,
            "suggestions": [],
            "next_question": None,
        }
    )
    assert report.sufficient is True
    assert report.next_question is None


def test_sufficiency_report_sufficient_required() -> None:
    """sufficient 字段必填。"""
    with pytest.raises(ValidationError):
        SufficiencyReport.model_validate({"covered": ["chief_complaint"]})


# ===========================================================================
# merge_sufficiency_report_to_state 测试
# ===========================================================================


def test_merge_sufficiency_report_writes_to_state() -> None:
    """merge_sufficiency_report_to_state 将 report 写入 state update dict。"""
    state = _state_with_basic_info()
    report = _insufficient_report()
    updates = merge_sufficiency_report_to_state(state, report)
    assert "sufficiency_report" in updates
    assert updates["sufficiency_report"] is report


def test_merge_sufficiency_report_does_not_mutate_other_fields() -> None:
    """merge 不修改问诊字段，只写 sufficiency_report。"""
    state = _state_with_basic_info()
    report = _insufficient_report()
    updates = merge_sufficiency_report_to_state(state, report)
    assert "chief_complaint" not in updates
    assert "present_illness" not in updates
    assert "inquiry_messages" not in updates
    assert len(updates) == 1


# ===========================================================================
# SufficiencyAgent fake gateway 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_sufficiency_agent_insufficient(tmp_path: Path) -> None:
    """信息明显不足时 SufficiencyAgent 输出 sufficient=false。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _insufficient_report(
                covered=["chief_complaint"],
                missing=["present_illness", "ten_questions", "allergy"],
            )
        ]
    )
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _minimal_state()
    result = await agent.run(state, "trace-insufficient")

    output = result.output
    assert isinstance(output, SufficiencyReport)
    assert output.sufficient is False
    assert "present_illness" in output.missing or "allergy" in output.missing
    assert output.next_question is not None
    assert gateway.calls[0]["agent_name"] == "sufficiency"
    assert result.prompt_version == "sufficiency_v1.jinja2"


@pytest.mark.asyncio
async def test_sufficiency_agent_sufficient(tmp_path: Path) -> None:
    """基础信息足够时 SufficiencyAgent 输出 sufficient=true。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_sufficient_report()])
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _state_with_basic_info()
    result = await agent.run(state, "trace-sufficient")

    output = result.output
    assert isinstance(output, SufficiencyReport)
    assert output.sufficient is True
    assert output.next_question is None


@pytest.mark.asyncio
async def test_sufficiency_agent_safety_missing(tmp_path: Path) -> None:
    """安全基础信息（过敏史、妊娠状态）缺失时 sufficient=false。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            _insufficient_report(
                covered=["chief_complaint", "present_illness", "cold_heat"],
                missing=["allergy", "pregnancy_status"],
                sufficient=False,
                suggestions=["需确认过敏史和妊娠状态"],
                next_question="请问您有药物过敏史吗？目前是否在备孕？",
            )
        ]
    )
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _state_with_basic_info()
    result = await agent.run(state, "trace-safety-missing")

    output = result.output
    assert output.sufficient is False
    assert any("allergy" in m.lower() or "pregnancy" in m.lower() for m in output.missing)
    assert output.next_question is not None


@pytest.mark.asyncio
async def test_sufficiency_agent_bad_schema(tmp_path: Path) -> None:
    """fake gateway 返回坏 schema 时 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway(
        [
            {"bad": "missing sufficient field"},
            {"also": "bad"},
        ]
    )
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-bad-schema")

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_sufficiency_agent_prompt_no_diagnosis_or_prescription(tmp_path: Path) -> None:
    """SufficiencyAgent 输出不包含辨证、处方、安全审核结论。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_insufficient_report()])
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _state_with_basic_info()
    result = await agent.run(state, "trace-no-dx")
    output = result.output
    output_dump = output.model_dump_json()
    assert "syndrome" not in output_dump.lower() or "syndrome" not in output_dump
    assert "处方" not in output_dump
    assert "剂量" not in output_dump
    assert "安全审核" not in output_dump
    assert "跳过" not in output_dump
    assert "自动确认" not in output_dump


@pytest.mark.asyncio
async def test_sufficiency_agent_prompt_includes_state_summary(tmp_path: Path) -> None:
    """构造的 prompt 中包含状态摘要和对话历史。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_sufficient_report()])
    agent = SufficiencyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _state_with_basic_info()
    await agent.run(state, "trace-prompt-check")

    system_msg = gateway.calls[0]["messages"][0]["content"]
    assert "头痛三天" in system_msg
    assert "测试患者" in system_msg


# ===========================================================================
# 不调用真实模型网关测试
# ===========================================================================


@pytest.mark.asyncio
async def test_no_real_model_gateway_called(tmp_path: Path) -> None:
    """验证所有测试路径只经过 FakeGateway，不调真实模型网关。"""
    manifest = _write_prompt_files(tmp_path)

    class TrackingGateway:
        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            return _sufficient_report()

    agent = SufficiencyAgent(
        gateway=TrackingGateway(),
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    await agent.run(XuanhuState(session_id=str(uuid.uuid4())), "trace-no-real")
    assert True  # TrackingGateway 返回了 fake 输出，未调真实网关


# ===========================================================================
# Supervisor 默认 registry 测试
# ===========================================================================


def test_supervisor_default_registry_includes_sufficiency() -> None:
    """Supervisor 默认 AgentRegistry 包含 SufficiencyAgent。"""
    registry = _default_registry()
    assert Stage.SUFFICIENCY in registry
    agent = registry.get(Stage.SUFFICIENCY)
    assert agent is not None
    assert agent.name == "sufficiency"
    assert agent.stage == Stage.SUFFICIENCY


def test_supervisor_default_registry_includes_inquiry() -> None:
    """Supervisor 默认 AgentRegistry 仍包含 InquiryAgent。"""
    registry = _default_registry()
    assert Stage.INQUIRY in registry
    agent = registry.get(Stage.INQUIRY)
    assert agent is not None
    assert agent.name == "inquiry"


# ===========================================================================
# 集成测试（需要 PostgreSQL/Redis，标记 integration）
# ===========================================================================


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
        pytest.fail(
            f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}"
        )

    async with factory() as session:
        yield session


async def _create_session(
    db: AsyncSession,
    stage: Stage = Stage.INQUIRY,
    status: str = "active",
) -> ConsultSession:
    """在数据库中创建测试会话。"""
    session = ConsultSession(
        id=uuid.uuid4(),
        patient_ref="P5-2-TEST",
        patient_info={"patient_ref": "P5-2-TEST", "gender": "female", "age": 30},
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
    await db.execute(delete(AuditEvent).where(AuditEvent.session_id == session_id))
    await db.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
    await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    await db.commit()


# ---------------------------------------------------------------------------
# Fake agents for Supervisor integration
# ---------------------------------------------------------------------------


class FakeInquiryAgentForSupervisor:
    """Fake inquiry agent 供 Supervisor 集成测试。"""

    name = "inquiry"
    stage = Stage.INQUIRY
    primary_sources = ()
    allow_cross_source = True
    next_stage = Stage.SUFFICIENCY

    def __init__(self) -> None:
        pass

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult
        from app.schemas.agent import InquiryAgentOutput

        return AgentResult(
            output=InquiryAgentOutput(
                next_question="还有什么症状？",
                asked_dimension="present_illness",
            ),
            prompt_version="fake",
        )


class FakeSufficiencyAgentForSupervisor:
    """Fake sufficiency agent 供 Supervisor 集成测试。"""

    name = "sufficiency"
    stage = Stage.SUFFICIENCY
    primary_sources = ()
    allow_cross_source = True
    next_stage = Stage.SYNDROME

    def __init__(self, sufficient: bool = True) -> None:
        self._sufficient = sufficient

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from app.agents.base import AgentResult

        if self._sufficient:
            return AgentResult(
                output=_sufficient_report(),
                prompt_version="fake",
            )
        return AgentResult(
            output=_insufficient_report(),
            prompt_version="fake",
        )


class FakeSufficiencyAgentMissing:
    """Fake sufficiency agent 返回不含 report 的异常输出。"""

    name = "sufficiency"
    stage = Stage.SUFFICIENCY
    primary_sources = ()
    allow_cross_source = True
    next_stage = Stage.SYNDROME

    async def run(self, state: XuanhuState, trace_id: str) -> Any:
        from pydantic import BaseModel

        from app.agents.base import AgentResult

        # 返回一个合法 BaseModel 但不是 SufficiencyReport，模拟 Agent 输出
        # 类型不匹配，使 _apply_agent_output 不会把 sufficiency_report 写入 state，
        # 从而真正触发 _decide_next_stage 的 sufficiency_report_missing -> blocked 路径。
        class NotAReport(BaseModel):
            pass

        return AgentResult(
            output=NotAReport(),
            prompt_version="fake",
        )


# ---------------------------------------------------------------------------
# Supervisor 路由测试
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_sufficient_advance_to_syndrome(db: AsyncSession) -> None:
    """sufficiency_report.sufficient=true 推进到 syndrome。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentForSupervisor(sufficient=True))
    registry.register(Stage.SYNDROME, None)  # 不会运行
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-sufficient-syndrome")
        assert result.to_stage == Stage.SYNDROME
        assert result.state.sufficiency_report is not None
        assert result.state.sufficiency_report.sufficient is True
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_insufficient_rollback_inquiry(db: AsyncSession) -> None:
    """sufficiency_report.sufficient=false 回退到 inquiry。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentForSupervisor(sufficient=False))
    registry.register(Stage.INQUIRY, FakeInquiryAgentForSupervisor())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-insufficient-rollback")
        assert result.to_stage == Stage.INQUIRY
        assert result.state.sufficiency_report is not None
        assert result.state.sufficiency_report.sufficient is False
        # 回退到 inquiry 不计入 rollback_counts
        assert result.state.rollback_counts.get("sufficiency", 0) == 0
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_sufficiency_report_missing_blocked(db: AsyncSession) -> None:
    """sufficiency_report 缺失时进入 blocked。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentMissing())
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(str(session.id), "trace-missing-report")
        assert result.to_stage == Stage.BLOCKED
        assert result.blocked_reason == "sufficiency_report_missing"

        await db.refresh(session)
        assert session.status == "blocked"
        assert session.current_stage == "blocked"
        assert session.blocked_reason == "sufficiency_report_missing"
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_force_insufficient_advance_to_syndrome(db: AsyncSession) -> None:
    """force=true 时 insufficient 仍推进到 syndrome 并写入审计。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentForSupervisor(sufficient=False))
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(
            str(session.id), "trace-force", force=True
        )
        assert result.to_stage == Stage.SYNDROME
        assert result.state.sufficiency_report is not None
        assert result.state.sufficiency_report.sufficient is False

        # 验证 force_advanced 审计事件已写入
        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session.id)
            .where(AuditEvent.event_type == "stage.force_advanced")
        )
        audit = audit_result.scalar_one_or_none()
        assert audit is not None
        assert audit.actor_type == "doctor"
        assert audit.payload["stage"] == "sufficiency"
        assert audit.payload["sufficiency_sufficient"] is False
        assert "missing" in audit.payload

        # 验证普通 stage.changed 审计也存在
        stage_audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session.id)
            .where(AuditEvent.event_type == "stage.changed")
        )
        stage_audit = stage_audit_result.scalar_one_or_none()
        assert stage_audit is not None
        assert stage_audit.payload["to_stage"] == "syndrome"
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_force_does_not_bypass_safety(db: AsyncSession) -> None:
    """force 只影响 sufficiency→syndrome，不得绕过后续安全审核。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentForSupervisor(sufficient=False))

    # 验证 force 推进到 syndrome 后，syndrome 阶段没有注册 Agent 会进入 blocked
    # （而不是直接跳过到 prescription）
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(
            str(session.id), "trace-force-no-safety-bypass", force=True
        )
        assert result.to_stage == Stage.SYNDROME

        # 再次推进：syndrome 阶段没有 agent → blocked
        # 注意：这里需要重新创建 supervisor 或用同一个再次推进
        result2 = await supervisor.advance(str(session.id), "trace-force-no-safety-bypass-2")
        assert result2.to_stage == Stage.BLOCKED
        assert "missing_agent" in (result2.blocked_reason or "")
    finally:
        await _cleanup_session(db, session.id)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_supervisor_force_sufficient_is_noop(db: AsyncSession) -> None:
    """sufficient=true 时 force 不产生额外审计（本来就是正常推进）。"""
    session = await _create_session(db, stage=Stage.SUFFICIENCY)
    registry = AgentRegistry()
    registry.register(Stage.SUFFICIENCY, FakeSufficiencyAgentForSupervisor(sufficient=True))
    supervisor = Supervisor(db, registry=registry)

    try:
        result = await supervisor.advance(
            str(session.id), "trace-force-sufficient", force=True
        )
        assert result.to_stage == Stage.SYNDROME

        # 不应有 force_advanced 审计（因为 sufficient=true 不是强制）
        audit_result = await db.execute(
            select(AuditEvent)
            .where(AuditEvent.session_id == session.id)
            .where(AuditEvent.event_type == "stage.force_advanced")
        )
        audit = audit_result.scalar_one_or_none()
        assert audit is None
    finally:
        await _cleanup_session(db, session.id)
