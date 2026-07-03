"""P6-4 Safety Agent 测试。

使用 fake gateway 覆盖：
- SafetyAgent 产生 SafetyExplanation
- 通过处方：summary 肯定，issue_explanations 为空
- 未通过处方：summary 描述问题，issue_explanations 与 issues 顺序一致
- passed/issues/rollback_target 不被 SafetyAgent 修改
- 缺少 safety_rule_result → 错误
- bad schema → AGENT_SCHEMA_INVALID
- prompt 包含规则 issues + 处方上下文
- 不调用 RAG
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.errors import AgentRunError
from app.agents.prompt_loader import PromptLoader
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    PatientInfo,
    SafetyExplanation,
    SafetyIssue,
    SafetyRuleResult,
    XuanhuState,
)
from app.schemas.types import SafetyIssueType, Severity, Stage


class FakeGateway:
    """可控 fake gateway。"""

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_prompt_files(tmp_path: Path, *, manifest_extra: str = "") -> Path:
    """写临时 prompt 文件，返回 manifest 路径。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    manifest_content = (
        "test_agent: test_agent_v1.jinja2\n"
        "inquiry: inquiry_v1.jinja2\n"
        "sufficiency: sufficiency_v1.jinja2\n"
        "syndrome: syndrome_v1.jinja2\n"
        "prescription: prescription_v1.jinja2\n"
        "modification: modification_v1.jinja2\n"
        "safety: safety_v1.jinja2\n"
        + manifest_extra
    )
    (prompt_dir / "manifest.yaml").write_text(manifest_content, encoding="utf-8")
    (prompt_dir / "test_agent_v1.jinja2").write_text("TEST_PROMPT", encoding="utf-8")
    (prompt_dir / "inquiry_v1.jinja2").write_text("inquiry", encoding="utf-8")
    (prompt_dir / "sufficiency_v1.jinja2").write_text("sufficiency", encoding="utf-8")
    (prompt_dir / "syndrome_v1.jinja2").write_text("syndrome", encoding="utf-8")
    (prompt_dir / "prescription_v1.jinja2").write_text("prescription", encoding="utf-8")
    (prompt_dir / "modification_v1.jinja2").write_text("modification", encoding="utf-8")
    (prompt_dir / "safety_v1.jinja2").write_text(
        "You are a TCM safety explanation assistant.\n"
        "passed={{ passed }}\n"
        "issues={{ issues_text }}\n"
        "warnings={{ warnings_text }}\n"
        "formula_name={{ formula_name }}\n"
        "composition={{ composition_text }}\n"
        "formula_rationale={{ formula_rationale }}\n"
        "patient_gender={{ patient_gender }}\n"
        "patient_age={{ patient_age }}\n"
        "allergies={{ allergies_text }}\n"
        "pregnancy_status={{ pregnancy_status }}\n"
        "rule_version={{ rule_version }}\n"
        "execution_order={{ execution_order }}\n"
        "issue_count={{ issue_count }}\n"
        "warning_count={{ warning_count }}\n",
        encoding="utf-8",
    )
    return prompt_dir / "manifest.yaml"


def _safe_state(
    session_id: str | None = None,
    passed: bool = False,
    issues: list[SafetyIssue] | None = None,
) -> XuanhuState:
    """构造含 safety_rule_result 的 XuanhuState。"""
    if issues is None:
        issues = [
            SafetyIssue(
                type=SafetyIssueType.DOSE_LIMIT,
                severity=Severity.HIGH,
                herbs=["党参"],
                rule_source="中国药典",
                suggestion="党参剂量 100.0g 超过上限 30.0g（一般超量）。请调整剂量。",
            )
        ]

    formula = FormulaResult(
        name="四君子汤",
        composition=[
            HerbDose(herb="党参", dose=12, unit="g"),
            HerbDose(herb="白术", dose=10, unit="g"),
        ],
        rationale="健脾益气",
    )

    return XuanhuState(
        session_id=session_id or str(uuid.uuid4()),
        patient_info=PatientInfo(
            name="测试患者",
            gender="female",
            age=30,
            allergies=["阿司匹林"],
            pregnancy_status="no",
        ),
        safety_rule_result=SafetyRuleResult(
            passed=passed,
            issues=issues,
            normalized_formula=formula,
            warnings=[],
            rule_version="v1.0.0",
            execution_order=["normalize", "convert_dose", "dose_limit"],
        ),
    )


def _explanation(
    summary: str = "审核通过",
    issue_explanations: list[str] | None = None,
    recommendations: str | None = None,
) -> SafetyExplanation:
    return SafetyExplanation(
        summary=summary,
        issue_explanations=issue_explanations or [],
        recommendations=recommendations,
    )


# ===========================================================================
# SafetyExplanation Schema 测试
# ===========================================================================


def test_safety_explanation_minimal_valid() -> None:
    """最少合法 SafetyExplanation 可独立校验。"""
    exp = SafetyExplanation.model_validate({"summary": "审核通过"})
    assert exp.summary == "审核通过"
    assert exp.issue_explanations == []
    assert exp.recommendations is None


def test_safety_explanation_full() -> None:
    """完整 SafetyExplanation 包含所有字段。"""
    exp = SafetyExplanation.model_validate({
        "summary": "审核发现问题",
        "issue_explanations": ["党参剂量超限"],
        "recommendations": "建议减量至 30g",
    })
    assert len(exp.issue_explanations) == 1
    assert exp.recommendations == "建议减量至 30g"


def test_safety_explanation_summary_required() -> None:
    """summary 必填。"""
    with pytest.raises(ValidationError):
        SafetyExplanation.model_validate({"issue_explanations": []})


def test_safety_review_accepts_explanation_fields() -> None:
    """SafetyReview 兼容 P6-4 新增的 explanation 字段。"""
    from app.schemas.agent import SafetyReview
    from app.schemas.types import RollbackTarget

    review = SafetyReview(
        passed=True,
        issues=[],
        rollback_target=RollbackTarget.NONE,
        summary="安全规则审核通过",
        explanation="经安全规则审核，该处方未发现安全问题，可进入医师复核。",
        explanation_issues=[],
        safety_agent_run_id="run-123",
        safety_agent_model="fake-model",
    )
    assert review.explanation is not None
    assert review.explanation_issues == []
    assert review.safety_agent_run_id == "run-123"
    assert review.safety_agent_model == "fake-model"


def test_safety_review_defaults_explanation_none() -> None:
    """SafetyReview 向后兼容：不传 explanation 字段时默认为 None。"""
    from app.schemas.agent import SafetyReview
    from app.schemas.types import RollbackTarget

    review = SafetyReview(
        passed=True,
        issues=[],
        rollback_target=RollbackTarget.NONE,
        summary="安全规则审核通过",
    )
    assert review.explanation is None
    assert review.explanation_issues is None
    assert review.safety_agent_run_id is None
    assert review.safety_agent_model is None


# ===========================================================================
# SafetyAgent 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_safety_agent_passed_produces_positive_summary(tmp_path: Path) -> None:
    """通过处方：SafetyAgent 输出肯定性总结。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([
        _explanation(
            summary="经安全规则审核，该处方未发现配伍禁忌、剂量超限、妊娠禁忌或过敏风险，可安全进入医师复核阶段。",
        )
    ])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _safe_state(passed=True, issues=[])
    result = await agent.run(state, "trace-passed")

    output = result.output
    assert isinstance(output, SafetyExplanation)
    assert "安全" in output.summary or "通过" in output.summary
    assert output.issue_explanations == []
    # 不输出路由决策字段
    output_dict = output.model_dump()
    assert "passed" not in output_dict
    assert "issues" not in output_dict
    assert "rollback_target" not in output_dict


@pytest.mark.asyncio
async def test_safety_agent_failed_produces_explanations(tmp_path: Path) -> None:
    """未通过处方：SafetyAgent 为每个 issue 生成独立解释。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([
        _explanation(
            summary="安全审核发现问题：党参剂量超限，需调整后重新审核。",
            issue_explanations=[
                "党参剂量 100.0g 超过药典上限 30.0g，属于一般超量，建议调整至 30g 以内。"
            ],
            recommendations="建议将党参剂量从 100g 调整至 30g 以内。",
        )
    ])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _safe_state(passed=False)
    result = await agent.run(state, "trace-failed")

    output = result.output
    assert isinstance(output, SafetyExplanation)
    assert "超限" in output.summary or "超量" in output.summary
    assert len(output.issue_explanations) == 1
    assert "党参" in output.issue_explanations[0]
    assert output.recommendations is not None


@pytest.mark.asyncio
async def test_safety_agent_output_has_no_routing_fields(tmp_path: Path) -> None:
    """SafetyExplanation 不包含 passed/issues/severity/rollback_target。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_explanation()])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _safe_state(passed=True, issues=[])
    result = await agent.run(state, "trace-no-routing")

    output_dict = result.output.model_dump()
    assert "passed" not in output_dict
    assert "issues" not in output_dict
    assert "severity" not in output_dict
    assert "rollback_target" not in output_dict


@pytest.mark.asyncio
async def test_safety_agent_missing_rule_result(tmp_path: Path) -> None:
    """safety_rule_result 为 None 时抛出 ValueError。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_explanation()])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = XuanhuState(
        session_id=str(uuid.uuid4()),
        patient_info=PatientInfo(name="测试", gender="female", age=30),
        safety_rule_result=None,
    )

    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-missing-result")
    # ValueError 被 BaseAgentImpl 归一化为 AGENT_FAILED
    assert exc_info.value.code == "AGENT_FAILED"


@pytest.mark.asyncio
async def test_safety_agent_bad_schema(tmp_path: Path) -> None:
    """fake gateway 返回坏 schema 时 AGENT_SCHEMA_INVALID。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([
        {"bad": "missing summary field"},
        {"also": "bad second attempt"},
    ])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=1,
        model_name="fake-model",
    )

    state = _safe_state(passed=True, issues=[])
    with pytest.raises(AgentRunError) as exc_info:
        await agent.run(state, "trace-bad-schema")

    error = exc_info.value
    assert error.code == "AGENT_SCHEMA_INVALID"
    assert error.retryable is False
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_safety_agent_prompt_includes_rule_context(tmp_path: Path) -> None:
    """构造的 prompt 包含规则问题、处方信息和患者上下文。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_explanation()])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _safe_state(passed=False)
    await agent.run(state, "trace-prompt-check")

    system_msg = gateway.calls[0]["messages"][0]["content"]
    assert "党参" in system_msg
    assert "四君子汤" in system_msg
    assert "测试患者" in system_msg or "30" in system_msg
    assert "阿司匹林" in system_msg


@pytest.mark.asyncio
async def test_safety_agent_no_rag(tmp_path: Path) -> None:
    """SafetyAgent 不调用 RAG。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_explanation()])

    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    state = _safe_state(passed=True, issues=[])
    result = await agent.run(state, "trace-no-rag")

    assert len(result.evidences) == 0


@pytest.mark.asyncio
async def test_safety_agent_stage_and_next_stage() -> None:
    """SafetyAgent.stage 为 SAFETY，next_stage 为 None。"""
    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(max_retries=0)
    assert agent.stage == Stage.SAFETY
    assert agent.next_stage is None
    assert agent.output_schema == SafetyExplanation
    assert agent.name == "safety"


@pytest.mark.asyncio
async def test_safety_agent_via_base_agent_impl(tmp_path: Path) -> None:
    """SafetyAgent 走 BaseAgentImpl 和模型网关抽象。"""
    manifest = _write_prompt_files(tmp_path)
    gateway = FakeGateway([_explanation()])

    from app.agents.base import BaseAgentImpl
    from app.agents.safety import SafetyAgent

    agent = SafetyAgent(
        gateway=gateway,
        prompt_loader=PromptLoader(manifest),
        max_retries=0,
        model_name="fake-model",
    )

    assert isinstance(agent, BaseAgentImpl)
    state = _safe_state(passed=True, issues=[])
    result = await agent.run(state, "trace-base-impl")

    assert isinstance(result.output, SafetyExplanation)
    assert result.prompt_version == "safety_v1.jinja2"
    assert gateway.calls[0]["agent_name"] == "safety"
