"""Focused R9-C tests for semantic sufficiency validation.

``validate_aspect_semantics`` only intervenes when confident: an addressed
aspect whose evidence lacks any required term becomes ``unclear`` (residual
re-ask), a not_applicable aspect with contradicting evidence becomes
``unclear``, and everything else keeps the model's judgment.
"""

from __future__ import annotations

from uuid import uuid4

from app.agent_runtime.coverage_semantics import validate_aspect_semantics
from app.schemas.question_contract import (
    CoverageStatus,
    QuestionAspect,
    build_question_contract,
)


def _aspect(criterion: str) -> QuestionAspect:
    contract = build_question_contract(
        session_id=uuid4(),
        question_message_id=uuid4(),
        question_text="痰是什么颜色，量多不多？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=(criterion,),
    )
    return contract.aspects[0]


def test_color_addressed_with_color_word_keeps_status() -> None:
    aspect = _aspect("说明痰的颜色")
    assert (
        validate_aspect_semantics(aspect, "痰是黄色的", CoverageStatus.ADDRESSED)
        is None
    )


def test_color_addressed_without_color_word_downgrades_to_unclear() -> None:
    aspect = _aspect("说明痰的颜色")
    assert (
        validate_aspect_semantics(aspect, "有痰", CoverageStatus.ADDRESSED)
        is CoverageStatus.UNCLEAR
    )


def test_amount_addressed_with_quantity_word_keeps_status() -> None:
    aspect = _aspect("说明痰的量")
    assert (
        validate_aspect_semantics(aspect, "痰量不多", CoverageStatus.ADDRESSED)
        is None
    )


def test_amount_addressed_without_quantity_downgrades() -> None:
    aspect = _aspect("说明痰的量")
    assert (
        validate_aspect_semantics(aspect, "有痰，痰是白色的", CoverageStatus.ADDRESSED)
        is CoverageStatus.UNCLEAR
    )


def test_not_applicable_with_contradicting_sputum_downgrades() -> None:
    """A sputum-color aspect marked N/A while the evidence says 有痰 is a
    contradiction → unclear (stays in the residual instead of satisfying)."""
    aspect = _aspect("说明痰的颜色")
    assert (
        validate_aspect_semantics(aspect, "有痰", CoverageStatus.NOT_APPLICABLE)
        is CoverageStatus.UNCLEAR
    )


def test_not_applicable_with_dry_cough_keeps_status() -> None:
    aspect = _aspect("说明痰的颜色")
    assert (
        validate_aspect_semantics(aspect, "干咳", CoverageStatus.NOT_APPLICABLE)
        is None
    )


def test_cough_nature_addressed_with_dry_cough_keeps_status() -> None:
    aspect = _aspect("说明咳嗽是否有痰（干咳/有痰）")
    assert (
        validate_aspect_semantics(aspect, "干咳", CoverageStatus.ADDRESSED)
        is None
    )


def test_unanswered_status_never_intervened() -> None:
    aspect = _aspect("说明痰的颜色")
    for status in (
        CoverageStatus.UNANSWERED,
        CoverageStatus.UNCLEAR,
        CoverageStatus.UNAVAILABLE,
    ):
        assert validate_aspect_semantics(aspect, "有痰", status) is None


def test_uncovered_criterion_never_intervened() -> None:
    aspect = _aspect("说明是否有头部沉重不适")
    assert (
        validate_aspect_semantics(aspect, "头不晕", CoverageStatus.ADDRESSED)
        is None
    )
