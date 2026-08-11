"""P4-1 核心 Agent schema 测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rag.schemas import Evidence as RAGEvidence
from app.schemas.agent import (
    Evidence,
    FormulaResult,
    HerbDose,
    MedicalRecord,
    MenstruationInfo,
    ModificationItem,
    ModifiedFormulaResult,
    PatientInfo,
    SafetyIssue,
    SafetyReview,
    SafetyRuleResult,
    SufficiencyReport,
    SyndromeResult,
    TenQuestions,
    XuanhuState,
    is_pregnancy_risk_status,
)
from app.schemas.review import ReviewRequest
from app.schemas.types import PregnancyStatus, Severity


def _formula() -> FormulaResult:
    return FormulaResult(
        name="桂枝汤",
        composition=[
            HerbDose(herb="桂枝", dose=9),
            HerbDose(herb="白芍", dose=9),
        ],
        source="伤寒论",
        rationale="调和营卫",
        citations=["formula:guizhi_tang"],
    )


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="ev-001",
        source_type="formula",
        source_id="550e8400-e29b-41d4-a716-446655440000",
        chunk_id="660e8400-e29b-41d4-a716-446655440000",
        title="桂枝汤",
        content_snippet="太阳中风，桂枝汤主之。",
        score=0.91,
        rank=1,
        metadata={"vector_score": 0.9},
    )


def test_patient_info_defaults_remain_session_api_compatible() -> None:
    """PatientInfo 默认值保持 P3 会话 API 兼容。"""
    patient = PatientInfo()

    assert patient.gender == "unknown"
    assert patient.age is None
    assert patient.allergies == []
    assert patient.pregnancy_status == "unknown"
    assert patient.menstruation_summary is None
    assert patient.special_conditions == []


def test_pregnancy_status_possible_supported_and_strict() -> None:
    """possible 必须按妊娠同等严格处理，供 Safety 后续硬规则使用。"""
    patient = PatientInfo(gender="female", pregnancy_status="possible")

    assert patient.pregnancy_status == PregnancyStatus.POSSIBLE
    assert is_pregnancy_risk_status(patient.pregnancy_status)
    assert is_pregnancy_risk_status(PregnancyStatus.PREGNANT)
    assert not is_pregnancy_risk_status("no")


def test_patient_age_boundaries() -> None:
    """年龄边界与 P3 会话 API 保持一致：0-130。"""
    assert PatientInfo(age=0).age == 0
    assert PatientInfo(age=130).age == 130

    with pytest.raises(ValidationError):
        PatientInfo(age=-1)
    with pytest.raises(ValidationError):
        PatientInfo(age=131)


def test_evidence_reuses_rag_schema() -> None:
    """Agent 层 Evidence 直接复用 app.rag.schemas.Evidence。"""
    evidence = _evidence()

    assert Evidence is RAGEvidence
    assert evidence.content_snippet == "太阳中风，桂枝汤主之。"
    assert evidence.model_dump()["metadata"]["vector_score"] == 0.9


def test_formula_and_modified_formula_validation() -> None:
    """基础方和加减方可独立校验，非法剂量/空 composition 会失败。"""
    formula = _formula()
    modified = ModifiedFormulaResult(
        formula=formula,
        modifications=[
            ModificationItem(action="adjust", herb="桂枝", dose=6, reason="患者汗出较多，减量")
        ],
    )

    assert modified.formula.name == "桂枝汤"
    assert modified.modifications[0].action == "adjust"

    with pytest.raises(ValidationError):
        HerbDose(herb="桂枝", dose=-1)
    with pytest.raises(ValidationError):
        FormulaResult(name="空方", composition=[], rationale="不应为空")


def test_safety_issue_and_review_enums() -> None:
    """SafetyIssue / SafetyReview 严格约束枚举值。"""
    issue = SafetyIssue(
        type="pregnancy",
        severity=Severity.BLOCKER,
        herbs=["半夏"],
        rule_source="PatientInfo.pregnancy_status",
        suggestion="possible 按妊娠处理，需医师确认或替换",
    )
    review = SafetyReview(
        passed=False,
        issues=[issue],
        rollback_target="modification",
        summary="存在妊娠相关阻断项",
    )

    assert issue.severity == "blocker"
    assert review.rollback_target == "modification"

    with pytest.raises(ValidationError):
        SafetyIssue(
            type="pregnancy",
            severity="fatal",
            herbs=[],
            rule_source="test",
            suggestion="bad",
        )


def test_xuanhu_state_minimal_and_partial_update() -> None:
    """XuanhuState 最小状态可创建，并适合 Supervisor 局部更新。"""
    state = XuanhuState(session_id="sid-001")
    updated = state.model_copy(update={"current_stage": "syndrome", "state_version": 2})

    assert state.current_stage == "inquiry"
    assert state.recovery_status == "normal"
    assert state.state_version == 1
    assert updated.current_stage == "syndrome"
    assert updated.state_version == 2


def test_xuanhu_state_model_copy_update_validates_data() -> None:
    """XuanhuState 的局部更新不得绕过 current_stage(3d 后为 str 口径)和 state_version 校验。"""
    state = XuanhuState(session_id="sid-validated")

    with pytest.raises(ValidationError):
        state.model_copy(update={"current_stage": ""})

    with pytest.raises(ValidationError):
        state.model_copy(update={"state_version": 0})


def test_xuanhu_state_complete_shape() -> None:
    """XuanhuState 完整状态可容纳后续 Agent 输出和 RAG Evidence。"""
    formula = _formula()
    issue = SafetyIssue(
        type="caution",
        severity="warning",
        herbs=[],
        rule_source="manual",
        suggestion="医师复核",
    )
    state = XuanhuState(
        session_id="sid-002",
        patient_info=PatientInfo(patient_ref="P4-001", pregnancy_status="possible"),
        chief_complaint="恶寒发热",
        present_illness="三日",
        past_history="无特殊",
        ten_questions=TenQuestions(cold_heat="恶寒发热"),
        evidences=[_evidence()],
        sufficiency_report=SufficiencyReport(
            covered=["主诉", "寒热"],
            missing=[],
            sufficient=True,
            suggestions=[],
        ),
        syndrome_result=SyndromeResult(
            syndrome="太阳中风",
            syndrome_basis=["恶风", "汗出"],
            treatment_principle="解肌发表，调和营卫",
            citations=["ev-001"],
            confidence=0.82,
        ),
        base_formula=formula,
        modified_formula=ModifiedFormulaResult(formula=formula),
        safety_rule_result=SafetyRuleResult(passed=False, issues=[issue], normalized_formula=formula),
        safety_review=SafetyReview(passed=False, issues=[issue], rollback_target="modification", summary="需调整"),
        medical_record=MedicalRecord(text="病历文本", json={"chief_complaint": "恶寒发热"}, disclaimer="仅供医师参考"),
        current_stage="review",
        pending_review=True,
        rollback_counts={"modification": 1},
        state_version=3,
        trace_id="trace-001",
    )

    assert state.patient_info.pregnancy_status == "possible"
    assert state.evidences[0].evidence_id == "ev-001"
    assert state.medical_record is not None
    assert state.medical_record.record_json["chief_complaint"] == "恶寒发热"


def test_agent_output_schemas_validate_independently() -> None:
    """核心 Agent 输出结构可脱离业务流程独立校验。"""
    formula = _formula()

    SufficiencyReport(covered=["主诉"], missing=["二便"], sufficient=False, suggestions=["补问二便"])
    SyndromeResult(
        syndrome="风寒表证",
        syndrome_basis=["恶寒", "无汗"],
        treatment_principle="辛温解表",
        citations=["ev-001"],
        confidence=0.7,
    )
    SafetyRuleResult(passed=True, issues=[], normalized_formula=formula)
    record = MedicalRecord(
        text="主诉：恶寒发热。",
        json={"diagnosis": "风寒表证"},
        disclaimer="需医师确认。",
        doctor_review={"action": "confirm"},
    )

    assert record.model_dump(by_alias=True)["json"]["diagnosis"] == "风寒表证"


def test_inquiry_nested_defaults() -> None:
    """问诊嵌套结构默认值稳定。"""
    ten = TenQuestions(menstruation_detail=MenstruationInfo())

    assert ten.menstruation_detail is not None
    assert ten.menstruation_detail.menopause_status == "unknown"


@pytest.mark.parametrize("action", ["reject", "request_more_info"])
def test_review_return_actions_require_feedback(action: str) -> None:
    with pytest.raises(ValidationError, match="必须填写 feedback"):
        ReviewRequest(action=action)


def test_review_confirm_does_not_require_feedback() -> None:
    assert ReviewRequest(action="confirm").feedback is None
