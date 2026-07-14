"""P7-2 RecordAgent 单元测试。

测试 RecordAgent 的各项辅助函数和 Agent 执行流程，
使用 fake gateway 不依赖真实模型调用。

覆盖：
- 摘要构造函数的输出格式
- 处方来源优先级（confirm vs modify）
- 安全审核摘要构造
- 医师确认摘要构造
- Agent 成功执行（fake gateway）
- Agent 失败重试
- 未确认处方时的处方摘要
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from app.agents.record_agent import (
    RecordAgent,
    _build_medical_record_json,
    _format_formula_override,
    _format_formula_result,
    build_doctor_review_summary_for_record,
    build_formula_summary_for_record,
    build_safety_summary_for_record,
    build_syndrome_summary_for_record,
)
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    MedicalRecord,
    ModifiedFormulaResult,
    SafetyIssue,
    SafetyReview,
    SafetyRuleResult,
    SyndromeResult,
    XuanhuState,
)
from app.schemas.types import (
    RollbackTarget,
    SafetyIssueType,
    Severity,
    Stage,
)

# ---------------------------------------------------------------------------
# Fake gateway
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


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------

def _minimal_state(**overrides: Any) -> XuanhuState:
    """构造最小 XuanhuState 用于测试。"""
    default: dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "current_stage": Stage.RECORD,
    }
    default.update(overrides)
    return XuanhuState.model_validate(default)


def test_build_syndrome_summary_has_syndrome() -> None:
    """有辨证结论时输出完整摘要。"""
    state = _minimal_state(
        syndrome_result=SyndromeResult(
            syndrome="肝郁脾虚",
            treatment_principle="疏肝健脾",
            syndrome_basis=["胁痛", "纳差"],
            differential=["与胃痛鉴别"],
            confidence=0.9,
        )
    )
    summary = build_syndrome_summary_for_record(state)
    assert "肝郁脾虚" in summary
    assert "疏肝健脾" in summary
    assert "胁痛" in summary
    assert "0.9" in summary


def test_build_syndrome_summary_none() -> None:
    """无辨证结论时输出占位文本。"""
    state = _minimal_state(syndrome_result=None)
    summary = build_syndrome_summary_for_record(state)
    assert "尚无辨证结论" in summary


def test_build_formula_summary_confirm_with_modified() -> None:
    """confirm 路径：有 modified_formula 时使用它。"""
    state = _minimal_state(
        doctor_review={"action": "confirm", "reviewed_by": "doctor-1", "reviewed_at": "2026-07-03T10:00:00"},
        modified_formula=ModifiedFormulaResult(
            formula=FormulaResult(
                name="参苓白术散加减",
                composition=[HerbDose(herb="党参", dose=12, unit="g")],
                rationale="健脾益气",
            ),
            modifications=[],
        ),
    )
    summary = build_formula_summary_for_record(state)
    assert "参苓白术散加减" in summary
    assert "党参" in summary


def test_build_formula_summary_confirm_fallback_base() -> None:
    """confirm 路径：无 modified_formula 时回退到 base_formula。"""
    state = _minimal_state(
        doctor_review={"action": "confirm", "reviewed_by": "doctor-1"},
        modified_formula=None,
        base_formula=FormulaResult(
            name="参苓白术散",
            composition=[HerbDose(herb="白术", dose=10, unit="g")],
            rationale="健脾益气",
        ),
    )
    summary = build_formula_summary_for_record(state)
    assert "参苓白术散" in summary
    assert "白术" in summary


def test_build_formula_summary_modify_with_override() -> None:
    """modify 路径：优先使用 formula_override。"""
    state = _minimal_state(
        doctor_review={
            "action": "modify",
            "reviewed_by": "doctor-1",
            "reviewed_at": "2026-07-03T10:00:00",
            "formula_override": {
                "name": "医师修改方",
                "composition": [
                    {"herb": "黄芪", "dose": 15, "unit": "g", "note": ""},
                ],
            },
        },
        modified_formula=ModifiedFormulaResult(
            formula=FormulaResult(
                name="原加减方",
                composition=[HerbDose(herb="党参", dose=12, unit="g")],
                rationale="原方义",
            ),
            modifications=[],
        ),
    )
    summary = build_formula_summary_for_record(state)
    assert "医师修改方" in summary
    assert "黄芪" in summary


def test_build_formula_summary_no_formula() -> None:
    """无处方信息时输出占位文本。"""
    state = _minimal_state(
        doctor_review=None,
        modified_formula=None,
        base_formula=None,
    )
    summary = build_formula_summary_for_record(state)
    assert "待医师补充" in summary


def test_build_safety_summary_passed() -> None:
    """安全审核通过时输出正确摘要。"""
    state = _minimal_state(
        safety_review=SafetyReview(
            passed=True,
            issues=[],
            rollback_target=RollbackTarget.NONE,
            summary="安全规则审核通过，无阻断性问题。",
        ),
        safety_rule_result=SafetyRuleResult(
            passed=True,
            issues=[],
            normalized_formula=FormulaResult(
                name="测试方",
                composition=[HerbDose(herb="党参", dose=12, unit="g")],
                rationale="测试",
            ),
            rule_version="v1.0.0",
        ),
    )
    summary = build_safety_summary_for_record(state)
    assert "通过" in summary
    assert "v1.0.0" in summary


def test_build_safety_summary_not_passed() -> None:
    """安全审核未通过时输出问题列表。"""
    state = _minimal_state(
        safety_review=SafetyReview(
            passed=False,
            issues=[
                SafetyIssue(
                    type=SafetyIssueType.DOSE_LIMIT,
                    severity=Severity.BLOCKER,
                    herbs=["党参"],
                    rule_source="《中国药典》",
                    suggestion="党参剂量 100.0g 超过上限 30.0g",
                ),
            ],
            rollback_target=RollbackTarget.MODIFICATION,
            summary="安全规则审核未通过，发现 1 个阻断性问题。",
        ),
    )
    summary = build_safety_summary_for_record(state)
    assert "审核通过：否" in summary
    assert "党参剂量" in summary


def test_build_safety_summary_none() -> None:
    """无安全审核记录时输出占位文本。"""
    state = _minimal_state(safety_review=None, safety_rule_result=None)
    summary = build_safety_summary_for_record(state)
    assert "无安全审核记录" in summary


def test_build_doctor_review_summary_confirm() -> None:
    """confirm 操作摘要正确。"""
    state = _minimal_state(
        doctor_review={
            "action": "confirm",
            "reviewed_by": "doctor-1",
            "reviewed_at": "2026-07-03T10:00:00",
        }
    )
    summary = build_doctor_review_summary_for_record(state)
    assert "确认处方" in summary
    assert "doctor-1" in summary


def test_build_doctor_review_summary_modify() -> None:
    """modify 操作摘要正确。"""
    state = _minimal_state(
        doctor_review={
            "action": "modify",
            "reviewed_by": "doctor-1",
            "reviewed_at": "2026-07-03T10:00:00",
            "feedback": "加黄芪",
            "formula_override": {"name": "医师修改方", "composition": []},
        }
    )
    summary = build_doctor_review_summary_for_record(state)
    assert "修改处方" in summary
    assert "加黄芪" in summary
    assert "医师修改" in summary


def test_build_doctor_review_summary_none() -> None:
    """无医师确认记录时输出占位文本。"""
    state = _minimal_state(doctor_review=None)
    summary = build_doctor_review_summary_for_record(state)
    assert "尚无医师确认记录" in summary


def test_format_formula_override_basic() -> None:
    """格式化 formula_override dict。"""
    override = {
        "name": "医师修改方",
        "composition": [
            {"herb": "黄芪", "dose": 15, "unit": "g", "note": ""},
        ],
    }
    result = _format_formula_override(override)
    assert "医师修改方" in result
    assert "黄芪" in result
    assert "15g" in result


def test_format_formula_result_basic() -> None:
    """格式化 FormulaResult。"""
    formula = FormulaResult(
        name="参苓白术散",
        source="《太平惠民和剂局方》",
        composition=[HerbDose(herb="党参", dose=12, unit="g")],
        rationale="健脾益气",
    )
    result = _format_formula_result(formula)
    assert "参苓白术散" in result
    assert "《太平惠民和剂局方》" in result
    assert "党参" in result


def test_build_medical_record_json_fallback() -> None:
    """非 LLM 兜底 record_json 构造正确。"""
    state = _minimal_state(
        chief_complaint="头痛",
        present_illness="近3日头痛",
        past_history=None,
        personal_family_history=None,
        syndrome_result=SyndromeResult(
            syndrome="风热头痛",
            treatment_principle="疏风清热",
            syndrome_basis=["头痛，发热"],
            confidence=0.8,
        ),
        base_formula=FormulaResult(
            name="川芎茶调散",
            composition=[HerbDose(herb="川芎", dose=10, unit="g")],
            rationale="疏风清热",
        ),
    )
    record_json = _build_medical_record_json(state)
    assert record_json["chief_complaint"] == "头痛"
    assert record_json["present_illness"] == "近3日头痛"
    assert record_json["past_history"] == "未采集"
    assert record_json["syndrome"] == "风热头痛"
    assert record_json["treatment_principle"] == "疏风清热"


# ---------------------------------------------------------------------------
# RecordAgent 执行测试
# ---------------------------------------------------------------------------

class TestRecordAgent:
    """RecordAgent 集成测试（fake gateway）。"""

    @pytest.mark.asyncio
    async def test_record_agent_success(self, tmp_path: Any) -> None:
        """RecordAgent 成功执行，fake gateway 返回合法 MedicalRecord。"""
        from app.agents.prompt_loader import PromptLoader

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "manifest.yaml").write_text(
            "record: record_v1.jinja2\n", encoding="utf-8"
        )
        (prompt_dir / "record_v1.jinja2").write_text(
            "# 病历生成\n\n{state_summary}\n\n{syndrome_summary}\n\n{formula_summary}\n\n{safety_summary}\n\n{doctor_review_summary}\n",
            encoding="utf-8",
        )

        fake_output = {
            "text": "完整病历文本",
            "json": {
                "patient_info": {"name": "患者甲"},
                "chief_complaint": "头痛3天",
                "present_illness": "近3日头痛",
                "past_history": "未采集",
                "personal_family_history": "未采集",
                "four_diagnosis": {
                    "inspection": "面色红",
                    "auscultation_olfaction": "未采集",
                    "inquiry": "头痛，发热",
                    "palpation": "未采集",
                },
                "syndrome_analysis": "风热外袭",
                "syndrome": "风热头痛",
                "treatment_principle": "疏风清热",
                "formula": {"name": "川芎茶调散"},
                "advice": ["避风寒", "清淡饮食"],
                "safety_review": {"passed": True},
                "doctor_review": {"action": "confirm"},
            },
            "disclaimer": "本记录由悬壶 AI 辅助生成，仅供执业中医师参考。",
            "doctor_review": {"action": "confirm", "reviewed_by": "doctor-1"},
        }

        gateway = FakeGateway([fake_output])
        agent = RecordAgent(
            gateway=gateway,
            prompt_loader=PromptLoader(str(prompt_dir / "manifest.yaml")),
            max_retries=0,
            model_name="fake-model",
        )

        state = _minimal_state(
            chief_complaint="头痛3天",
            present_illness="近3日头痛",
            doctor_review={
                "action": "confirm",
                "reviewed_by": "doctor-1",
                "reviewed_at": "2026-07-03T10:00:00",
            },
            modified_formula=ModifiedFormulaResult(
                formula=FormulaResult(
                    name="川芎茶调散",
                    composition=[HerbDose(herb="川芎", dose=10, unit="g")],
                    rationale="疏风清热",
                ),
                modifications=[],
            ),
            safety_review=SafetyReview(
                passed=True,
                issues=[],
                rollback_target=RollbackTarget.NONE,
                summary="通过",
            ),
        )

        result = await agent.run(state, "trace-record-1")
        assert isinstance(result.output, MedicalRecord)
        assert result.output.text == "完整病历文本"
        assert result.output.disclaimer == "本记录由悬壶 AI 辅助生成，仅供执业中医师参考。"
        assert result.output.record_json == fake_output["json"]
        assert result.next_stage == Stage.DONE
        assert result.prompt_version == "record_v1.jinja2"

    @pytest.mark.asyncio
    async def test_record_agent_prompt_includes_all_sections(self, tmp_path: Any) -> None:
        """RecordAgent 的 prompt 包含所有必要部分。"""
        from app.agents.prompt_loader import PromptLoader

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "manifest.yaml").write_text(
            "record: record_v1.jinja2\n", encoding="utf-8"
        )
        (prompt_dir / "record_v1.jinja2").write_text(
            "# 病历\n{syndrome_summary}\n{formula_summary}\n{safety_summary}\n{doctor_review_summary}",
            encoding="utf-8",
        )

        fake_output = {
            "text": "病历",
            "json": {},
            "disclaimer": "免责声明",
            "doctor_review": {},
        }

        gateway = FakeGateway([fake_output])
        agent = RecordAgent(
            gateway=gateway,
            prompt_loader=PromptLoader(str(prompt_dir / "manifest.yaml")),
            max_retries=0,
            model_name="fake-model",
        )

        state = _minimal_state(
            chief_complaint="头痛",
            syndrome_result=SyndromeResult(
                syndrome="风热头痛",
                treatment_principle="疏风清热",
                syndrome_basis=["头痛"],
                confidence=0.8,
            ),
            doctor_review={"action": "confirm", "reviewed_by": "doctor-1"},
            modified_formula=ModifiedFormulaResult(
                formula=FormulaResult(
                    name="川芎茶调散",
                    composition=[HerbDose(herb="川芎", dose=10, unit="g")],
                    rationale="疏风清热",
                ),
                modifications=[],
            ),
            safety_review=SafetyReview(
                passed=True,
                issues=[],
                rollback_target=RollbackTarget.NONE,
                summary="通过",
            ),
        )

        await agent.run(state, "trace-record-2")

        # 校验 prompt 包含所有必要部分
        system_content = gateway.calls[0]["messages"][0]["content"]
        assert "风热头痛" in system_content
        assert "川芎茶调散" in system_content
        assert "确认处方" in system_content or "confirm" in system_content

    @pytest.mark.asyncio
    async def test_record_agent_no_doctor_review_still_runs(self, tmp_path: Any) -> None:
        """无医师确认时 RecordAgent 仍可运行（但最终不应落库——由 Supervisor 控制）。"""
        from app.agents.prompt_loader import PromptLoader

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "manifest.yaml").write_text(
            "record: record_v1.jinja2\n", encoding="utf-8"
        )
        (prompt_dir / "record_v1.jinja2").write_text(
            "病历生成\n{state_summary}\n{syndrome_summary}\n{formula_summary}\n{safety_summary}\n{doctor_review_summary}",
            encoding="utf-8",
        )

        fake_output = {
            "text": "病历",
            "json": {},
            "disclaimer": "免责声明",
            "doctor_review": {},
        }

        gateway = FakeGateway([fake_output])
        agent = RecordAgent(
            gateway=gateway,
            prompt_loader=PromptLoader(str(prompt_dir / "manifest.yaml")),
            max_retries=0,
            model_name="fake-model",
        )

        state = _minimal_state(
            chief_complaint="头痛",
            doctor_review=None,
        )

        result = await agent.run(state, "trace-no-review")
        assert isinstance(result.output, MedicalRecord)
        system_content = gateway.calls[0]["messages"][0]["content"]
        assert "尚无医师确认记录" in system_content

    @pytest.mark.asyncio
    async def test_record_agent_schema_validation_rejects_invalid(self, tmp_path: Any) -> None:
        """RecordAgent 校验：无效输出被拒绝。"""
        from app.agents.errors import AgentRunError
        from app.agents.prompt_loader import PromptLoader

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "manifest.yaml").write_text(
            "record: record_v1.jinja2\n", encoding="utf-8"
        )
        (prompt_dir / "record_v1.jinja2").write_text("病历", encoding="utf-8")

        # 缺少 text 字段的无效输出
        invalid_output = {"json": {}, "disclaimer": "x", "doctor_review": {}}
        gateway = FakeGateway([invalid_output])
        agent = RecordAgent(
            gateway=gateway,
            prompt_loader=PromptLoader(str(prompt_dir / "manifest.yaml")),
            max_retries=0,
            model_name="fake-model",
        )

        state = _minimal_state()
        with pytest.raises(AgentRunError):
            await agent.run(state, "trace-invalid")
