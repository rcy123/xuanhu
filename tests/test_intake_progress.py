from __future__ import annotations

import uuid
from typing import Literal

import pytest

from app.schemas.intake import (
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessage,
    IntakeMessageRole,
    IntakeReplyContext,
)
from app.services.langgraph_intake import (
    _INTAKE_RETRYABLE_MODEL_CODES,
    _INTAKE_SILENT_DEGRADE_CODES,
    _bound_required_reply_fallback_output,
    _bound_social_reply_output,
    _gateway_bound_reply_fallback_output,
    _is_social_acknowledgement,
    _reply_binding_extraction_metadata,
)


def _bound_input(
    content: str,
    *,
    dimension: str = "present_illness.change",
    selection_kind: Literal["required", "conflict"] = "required",
) -> IntakeExtractionInput:
    return IntakeExtractionInput(
        current_messages=(
            IntakeMessage(
                message_id=uuid.uuid4(),
                role=IntakeMessageRole.PATIENT,
                content=content,
            ),
        ),
        reply_context=IntakeReplyContext(
            question_message_id=uuid.uuid4(),
            selected_dimension=dimension,
            selection_kind=selection_kind,
        ),
    )


@pytest.mark.parametrize(
    "content",
    ("你好", "您好！", "hi", "Hello?", "谢谢"),
)
def test_greeting_only_turn_is_not_clinical_stagnation(content: str) -> None:
    assert _is_social_acknowledgement(content) is True


@pytest.mark.parametrize(
    "content",
    ("你好，我咳嗽三天", "没有", "不清楚", "hello，发烧了"),
)
def test_clinical_or_ambiguous_turn_still_counts_for_progress(content: str) -> None:
    assert _is_social_acknowledgement(content) is False


def test_bound_required_reply_gateway_fallback_keeps_dimension_incomplete() -> None:
    output = _gateway_bound_reply_fallback_output(
        _bound_input("咳嗽、发烧"),
        "MODEL_GATEWAY_TIMEOUT",
    )

    assert output is not None
    assert output.decision is IntakeExtractionDecision.NEEDS_CLARIFICATION
    assert output.observations == ()


@pytest.mark.parametrize(
    "failure_code",
    ("STRUCTURED_OUTPUT_INVALID", "INTAKE_IDENTITY_FACT_FORBIDDEN"),
)
def test_model_quality_extraction_failure_degrades_to_abstain(failure_code: str) -> None:
    """真实后端复盘: grounding/结构化输出失败若整轮 503,安全项采集会被模型随机性卡死;
    改为退 ABSTAINED 追问,不再硬失败。"""
    unbound = IntakeExtractionInput(
        current_messages=(
            IntakeMessage(
                message_id=uuid.uuid4(),
                role=IntakeMessageRole.PATIENT,
                content="胸口有点闷，疼得厉害",
            ),
        ),
    )
    output = _gateway_bound_reply_fallback_output(
        unbound,
        failure_code,
    )
    assert output is not None
    assert output.decision is IntakeExtractionDecision.ABSTAINED
    assert output.observations == ()


def test_identity_authority_model_failures_are_retryable_and_degradable() -> None:
    """33377ef6 复盘: 模型幻觉身份字段属可重试软失败,重试仍失败时降级而非 503。"""
    for code in ("INTAKE_IDENTITY_FACT_FORBIDDEN", "INTAKE_AUTHORITY_FIELD_FORBIDDEN"):
        assert code in _INTAKE_RETRYABLE_MODEL_CODES
        assert code in _INTAKE_SILENT_DEGRADE_CODES


def test_model_quality_failure_bound_reply_keeps_focused_followup() -> None:
    output = _gateway_bound_reply_fallback_output(
        _bound_input("咳嗽、发烧"),
        "INTAKE_GROUNDING_VALUE_MISMATCH",
    )
    assert output is not None
    assert output.decision is IntakeExtractionDecision.NEEDS_CLARIFICATION
    assert output.observations == ()


def test_degraded_unbound_fallback_metadata_does_not_require_reply_binding() -> None:
    """真实后端 recover 重放复盘: 未绑定 reply_context 的 ABSTAINED 降级输出
    必须能安全写留痕元数据,不能因断言 reply binding 而整轮崩溃。"""
    unbound = IntakeExtractionInput(
        current_messages=(
            IntakeMessage(
                message_id=uuid.uuid4(),
                role=IntakeMessageRole.PATIENT,
                content="胸口有点闷，疼得厉害",
            ),
        ),
    )
    output = IntakeExtractionOutput(decision=IntakeExtractionDecision.ABSTAINED)
    metadata = _reply_binding_extraction_metadata(
        output,
        3,
        unbound,
        fallback_error_code="INTAKE_GROUNDING_VALUE_MISMATCH",
    )
    assert metadata["source"] == "degraded_fallback"
    assert metadata["degraded"] is True
    assert metadata["last_failure_code"] == "INTAKE_GROUNDING_VALUE_MISMATCH"


def test_uninformative_bound_reply_remains_a_followup_gap() -> None:
    output = _bound_required_reply_fallback_output(_bound_input("不清楚"))

    assert output is not None
    assert output.decision is IntakeExtractionDecision.NEEDS_CLARIFICATION
    assert output.observations == ()


def test_safety_or_conflict_reply_never_uses_raw_observation_fallback() -> None:
    assert (
        _bound_required_reply_fallback_output(
            _bound_input("青霉素", dimension="safety.allergy_status")
        )
        is None
    )
    assert (
        _bound_required_reply_fallback_output(_bound_input("以三天为准", selection_kind="conflict"))
        is None
    )


def test_bound_greeting_abstains_without_model_extraction() -> None:
    output = _bound_social_reply_output(_bound_input("你好"))

    assert output is not None
    assert output.decision is IntakeExtractionDecision.ABSTAINED
