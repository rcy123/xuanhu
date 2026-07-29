from __future__ import annotations

import uuid
from typing import Literal

import pytest

from app.schemas.intake import (
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeMessage,
    IntakeMessageRole,
    IntakeReplyContext,
)
from app.services.langgraph_intake import (
    _bound_required_reply_fallback_output,
    _bound_social_reply_output,
    _gateway_bound_reply_fallback_output,
    _is_social_acknowledgement,
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


def test_non_gateway_extraction_failure_is_not_masked() -> None:
    assert (
        _gateway_bound_reply_fallback_output(
            _bound_input("咳嗽、发烧"),
            "STRUCTURED_OUTPUT_INVALID",
        )
        is None
    )


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
