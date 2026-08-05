"""病历叙述与完整病历结构单元测试。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.schemas.domain import ObservationSchema, ObservationStatus
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    SafetyIssue,
    SafetyIssueType,
    SafetyRuleResult,
    Severity,
)
from app.services.langgraph_record import _clinical_record_fields, _render_record_text


class _Herb:
    def __init__(self, herb: str, dose: int, unit: str) -> None:
        self.herb = herb
        self.dose = dose
        self.unit = unit


class _Formula:
    formula = type(
        "F",
        (),
        {
            "name": "麻黄汤",
            "composition": (_Herb("麻黄", 9, "g"), _Herb("生石膏", 30, "g")),
        },
    )()


def _observations() -> tuple[ObservationSchema, ...]:
    sid = uuid.uuid4()
    return (
        ObservationSchema(
            observation_id=uuid.uuid4(), session_id=sid, fact_key="chief_complaint.symptom",
            value="头痛发热", status=ObservationStatus.ACTIVE, confidence=0.9,
            source_message_id=uuid.uuid4(), created_at=datetime.now(UTC),
        ),
        ObservationSchema(
            observation_id=uuid.uuid4(), session_id=sid, fact_key="chief_complaint.course",
            value="三天", status=ObservationStatus.ACTIVE, confidence=0.9,
            source_message_id=uuid.uuid4(), created_at=datetime.now(UTC),
        ),
        ObservationSchema(
            observation_id=uuid.uuid4(), session_id=sid, fact_key="ten_questions.cold_heat",
            value="恶寒发热", status=ObservationStatus.ACTIVE, confidence=0.9,
            source_message_id=uuid.uuid4(), created_at=datetime.now(UTC),
        ),
    )


def _observation(session_id: uuid.UUID, fact_key: str, value: str) -> ObservationSchema:
    return ObservationSchema(
        observation_id=uuid.uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value=value,
        status=ObservationStatus.ACTIVE,
        confidence=0.9,
        source_message_id=uuid.uuid4(),
        created_at=datetime.now(UTC),
    )


def _safety() -> SafetyRuleResult:
    return SafetyRuleResult(
        rule_version="safety-rule-engine.product.v1",
        passed=True,
        issues=[
            SafetyIssue(
                type=SafetyIssueType.CAUTION, severity=Severity.HIGH,
                herbs=["生石膏"],
                rule_source="知识库",
                suggestion="「生石膏」需核实",
            )
        ],
        normalized_formula=FormulaResult(
            name="麻黄汤",
            composition=[HerbDose(herb="麻黄", dose=9, unit="g")],
            rationale="解表散寒",
        ),
    )


class _Profile:
    pregnancy_collection_status = "explicitly_none"


def test_clinical_record_fields_are_deterministic() -> None:
    clinical = _clinical_record_fields(
        observations=_observations(),
        syndrome={
            "syndrome": "风寒表实证",
            "treatment_principle": "解表散寒",
            "syndrome_basis": ({"claim": "恶寒发热无汗，脉浮紧"},),
        },
        formula=_Formula(),
        safety_result=_safety(),
        safety_profile=_Profile(),
    )
    assert clinical["chief_complaint"] == "头痛发热三天"
    assert "恶寒发热" in clinical["present_illness"]
    assert clinical["diagnosis"] == "风寒表实证"
    assert clinical["syndrome_process"] == "恶寒发热无汗，脉浮紧"
    assert "麻黄9g" in clinical["formula_composition"]
    assert "生石膏30g" in clinical["formula_composition"]
    assert "需核实" in clinical["precautions"]


def test_present_illness_is_grouped_into_clinical_narrative() -> None:
    session_id = uuid.uuid4()
    observations = (
        _observation(session_id, "chief_complaint.symptom", "感冒一周"),
        _observation(session_id, "chief_complaint.course", "一周"),
        _observation(session_id, "present_illness.change", "逐渐加重"),
        _observation(session_id, "ten_questions.cold_heat", "怕冷、发热37.8度"),
        _observation(session_id, "ten_questions.respiratory", "咳嗽，痰白浓稠"),
        _observation(session_id, "ten_questions.head_body", "头昏沉"),
        _observation(session_id, "ten_questions.diet", "食欲一般，口淡"),
        _observation(session_id, "ten_questions.thirst", "想喝温水"),
        _observation(session_id, "ten_questions.sweat", "夜晚盗汗"),
        _observation(session_id, "ten_questions.stool_urine", "大便不成形，小便微黄发烫"),
        _observation(session_id, "ten_questions.sleep", "咳嗽导致失眠，早醒"),
        _observation(session_id, "past_history", "没有"),
    )

    clinical = _clinical_record_fields(
        observations=observations,
        syndrome=None,
        formula=_Formula(),
        safety_result=_safety(),
        safety_profile=_Profile(),
    )

    assert clinical["chief_complaint"] == "感冒一周"
    assert clinical["present_illness"] == (
        "患者因“感冒一周”就诊。病程中症状逐渐加重。"
        "伴怕冷、发热37.8度；咳嗽，痰白浓稠；头昏沉。"
        "饮食方面，食欲一般，口淡。口渴情况，想喝温水。"
        "汗出情况，夜晚盗汗。二便方面，大便不成形，小便微黄发烫。"
        "睡眠方面，咳嗽导致失眠，早醒。"
    )
    assert clinical["past_history"] == ""
    assert "没有" not in clinical["present_illness"]


def test_render_record_text_contains_full_sections() -> None:
    clinical = {
        "chief_complaint": "头痛发热 三天",
        "present_illness": "恶寒发热",
        "syndrome_process": "恶寒发热无汗",
        "diagnosis": "风寒表实证",
        "treatment_principle": "解表散寒",
        "formula_name": "麻黄汤",
        "formula_composition": "麻黄9g、生石膏30g",
        "precautions": "无",
    }
    text = _render_record_text(
        formula=_Formula(),
        clinical=clinical,
        narrative={"diet_advice": "宜清淡", "prognosis": "及时复诊"},
    )
    for section in (
        "主诉：",
        "现病史：",
        "辨证过程：",
        "中医诊断：",
        "治则治法：",
        "处方：",
        "组成：",
        "注意事项：",
        "饮食建议：",
        "预后情况：",
        "免责声明：",
    ):
        assert section in text
    assert "安全审核：" not in text
    assert "医师复核：" not in text
