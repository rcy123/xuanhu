"""2.8 简短回答归约（required 绑定）测试：确定性把“正常/没有”落为维度 observation。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.agent_runtime.reducer import DomainState
from app.schemas.domain import ObservationSchema, ObservationStatus
from app.schemas.intake import (
    IntakeExtractionInput,
    IntakeMessage,
    IntakeMessageRole,
    IntakeReplyContext,
    ObservationDelta,
    ObservationOperation,
)
from app.services import langgraph_intake as langgraph_intake
from app.services import langgraph_reasoning
from app.services.langgraph_intake import (
    _bound_explicit_none_output,
    _bound_required_reply_normal_output,
)


def _input(content: str, dimension: str, kind: str = "required") -> IntakeExtractionInput:
    return IntakeExtractionInput(
        current_messages=(
            IntakeMessage(message_id=uuid.uuid4(), role=IntakeMessageRole.PATIENT, content=content),
        ),
        reply_context=IntakeReplyContext(
            question_message_id=uuid.uuid4(),
            selected_dimension=dimension,
            selection_kind=kind,
        ),
    )


@pytest.mark.parametrize(
    ("content", "dimension"),
    [
        ("正常", "ten_questions.stool_urine"),
        ("还好", "ten_questions.sleep"),
        ("没问题", "ten_questions.diet"),
        ("都正常", "ten_questions.stool_urine"),
        ("没有", "ten_questions.sweat"),
        ("无", "ten_questions.chest_abdomen"),
        ("是的", "ten_questions.cold_heat"),
        ("可以", "ten_questions.thirst"),
        ("正常。", "ten_questions.stool_urine"),
    ],
)
def test_short_reply_bound_to_required_dimension_is_reduced(content: str, dimension: str) -> None:
    out = _bound_required_reply_normal_output(_input(content, dimension))
    assert out is not None
    assert out.decision.value == "extracted"
    assert len(out.observations) == 1
    obs = out.observations[0]
    assert obs.fact_key == dimension
    assert obs.value == content.strip()


@pytest.mark.parametrize(
    ("content", "dimension"),
    [
        ("有时会头痛，不严重", "ten_questions.head_body"),
        ("睡不好，半夜老醒", "ten_questions.sleep"),
        ("口干，想喝水", "ten_questions.thirst"),
        ("你好", "ten_questions.diet"),  # 社交问候走既有 abstain 分支
        ("没有", "safety.allergy_status"),  # 安全维度走 explicit_none
    ],
)
def test_non_short_or_safety_replies_are_not_reduced(content: str, dimension: str) -> None:
    assert _bound_required_reply_normal_output(_input(content, dimension)) is None


def test_conflict_binding_never_reduces() -> None:
    assert _bound_required_reply_normal_output(_input("正常", "ten_questions.diet", kind="conflict")) is None


def test_safety_negative_still_uses_explicit_none_path() -> None:
    out = _bound_explicit_none_output(_input("没有", "safety.allergy_status"))
    assert out is not None
    assert out.patient_safety_delta is not None


def test_bound_four_diagnosis_fallback_records_tongue_and_pulse() -> None:
    content = "舌质淡红，舌苔薄白且润泽；脉浮。"

    out = langgraph_intake._gateway_bound_reply_fallback_output(
        _input(content, "four_diagnosis"),
        "INTAKE_IDENTITY_FACT_FORBIDDEN",
    )

    assert out is not None
    assert out.decision.value == "extracted"
    assert [(item.fact_key, item.value) for item in out.observations] == [
        ("four_diagnosis.inspection", content),
        ("four_diagnosis.palpation", content),
    ]


def test_bound_four_diagnosis_explicit_reply_skips_model_and_records_slots() -> None:
    content = "舌淡红，苔薄白，脉弦。"

    out = _bound_required_reply_normal_output(_input(content, "four_diagnosis"))

    assert out is not None
    assert out.decision.value == "extracted"
    assert [(item.fact_key, item.value) for item in out.observations] == [
        ("four_diagnosis.inspection", content),
        ("four_diagnosis.palpation", content),
    ]


def test_bound_four_diagnosis_confirmation_is_not_used_as_clinical_fact() -> None:
    assert _bound_required_reply_normal_output(_input("是的，基本完整。", "four_diagnosis")) is None


def test_bound_four_diagnosis_fallback_never_infers_an_unmentioned_component() -> None:
    content = "舌质淡红，舌苔薄白。"

    out = langgraph_intake._gateway_bound_reply_fallback_output(
        _input(content, "four_diagnosis"),
        "INTAKE_IDENTITY_FACT_FORBIDDEN",
    )

    assert out is not None
    assert [item.fact_key for item in out.observations] == ["four_diagnosis.inspection"]


# ---------------------------------------------------------------------------
# 2.8 冲突过滤：模型重复提取同键不同值 → 丢弃留痕，不整轮失败
# ---------------------------------------------------------------------------


def _obs_state(*facts: tuple[str, object]) -> DomainState:
    sid = uuid.uuid4()
    existing = tuple(
        ObservationSchema(
            observation_id=uuid.uuid4(),
            session_id=sid,
            fact_key=k,
            value=v,
            normalized_value=None,
            status=ObservationStatus.ACTIVE,
            confidence=0.5,
            supersedes_observation_id=None,
            source_message_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
        )
        for k, v in facts
    )
    return DomainState(session_id=sid, state_version=1, observations=existing)


def _add(k: str, v: object) -> ObservationDelta:
    return ObservationDelta(
        fact_key=k,
        value=v,
        source_message_id=uuid.uuid4(),
        confidence=0.5,
        operation=ObservationOperation.ADD,
    )


def test_conflicting_add_is_dropped_and_recorded() -> None:
    state = _obs_state(("ten_questions.sleep", "睡眠质量一般，受感冒影响"))
    rejected: list[object] = []
    out = langgraph_intake._drop_value_conflicting_adds(
        (
            _add("present_illness.cough", "咳嗽"),
            _add("ten_questions.sleep", "睡眠一般"),
            _add("ten_questions.sleep", "睡眠质量一般，受感冒影响"),
            _add("ten_questions.diet", "食欲正常"),
        ),
        state=state,
        rejected_observations=rejected,
    )
    keys = [(o.fact_key, o.value) for o in out]
    assert ("present_illness.cough", "咳嗽") in keys
    assert ("ten_questions.sleep", "睡眠质量一般，受感冒影响") in keys  # 同值保留
    assert ("ten_questions.diet", "食欲正常") in keys
    assert ("ten_questions.sleep", "睡眠一般") not in keys  # 冲突值丢弃
    assert len(rejected) == 1
    assert rejected[0].fact_key == "ten_questions.sleep"
    assert rejected[0].reason == "value_conflicts_active_fact"


def test_no_active_fact_key_keeps_all_adds() -> None:
    state = _obs_state(("ten_questions.sleep", "睡眠质量一般，受感冒影响"))
    out = langgraph_intake._drop_value_conflicting_adds(
        (_add("present_illness.cough", "咳嗽"), _add("present_illness.sputum", "嗓子有痰")),
        state=state,
        rejected_observations=None,
    )
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 2.8 舌脉采集：reasoning 回退时确定性缺失判定 + 舌脉引导问句
# ---------------------------------------------------------------------------


def _reasoning_state(*facts: tuple[str, object]) -> DomainState:
    sid = uuid.uuid4()
    return DomainState(
        session_id=sid,
        state_version=1,
        observations=tuple(
            ObservationSchema(
                observation_id=uuid.uuid4(),
                session_id=sid,
                fact_key=k,
                value=v,
                normalized_value=None,
                status=ObservationStatus.ACTIVE,
                confidence=0.5,
                supersedes_observation_id=None,
                source_message_id=uuid.uuid4(),
                created_at=datetime.now(UTC),
            )
            for k, v in facts
        ),
    )


def test_missing_dimensions_filters_model_false_positives() -> None:
    # 模型误报 cold_heat/stool_urine（实际存在）→ 过滤；four_diagnosis 真缺 → 保留
    state = _reasoning_state(
        ("ten_questions.cold_heat", "夜晚怕冷，微微发热"),
        ("ten_questions.stool_urine", "大小便正常"),
        ("ten_questions.sleep", "睡眠一般"),
    )
    missing = langgraph_reasoning._true_missing_dimensions(state, "ten_questions.cold_heat")
    assert missing == ["four_diagnosis"]


def test_missing_dimensions_keeps_true_four_diagnosis_gap() -> None:
    assert langgraph_reasoning._true_missing_dimensions(_reasoning_state(), "four_diagnosis") == [
        "four_diagnosis"
    ]


def test_missing_dimensions_empty_when_four_diagnosis_present() -> None:
    state = _reasoning_state(
        ("four_diagnosis.inspection", "舌淡胖苔白腻"),
        ("four_diagnosis.palpation", "脉沉细"),
    )
    assert langgraph_reasoning._true_missing_dimensions(state, "four_diagnosis") == []


def test_four_diagnosis_question_guides_direct_input() -> None:
    from app.schemas.completeness import InquiryDimension

    question = langgraph_reasoning._question_for_dimension(InquiryDimension.FOUR_DIAGNOSIS)
    assert "舌" in question and "脉" in question


# ---------------------------------------------------------------------------
# 2.8 formula 一致性失败可重试（模型随机质量问题，同输入重放自愈）
# ---------------------------------------------------------------------------


def test_consistency_failure_codes_are_retryable() -> None:
    from app.agent_runtime.formula_consistency import FormulaConsistencyFailureCode

    for code in FormulaConsistencyFailureCode:
        assert langgraph_reasoning._reasoning_failure_is_retryable(code.value), code.value
    # 具体触发码（REAL-SESSION b801423b）
    assert (
        langgraph_reasoning._reasoning_failure_is_retryable(
            "FORMULA_CONSISTENCY_MODIFICATION_TARGET_MISSING"
        )
        is True
    )
