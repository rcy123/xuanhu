"""Focused R9-C tests for the planning-layer dimension Rubric.

``plan_contract_aspects`` deterministically merges Rubric criteria with the
model's aspects: Rubric first, conditionals frozen once a sputum fact exists,
and un-configured dimensions keep the legacy model-only behavior.
"""

from __future__ import annotations

from uuid import uuid4

from app.agent_runtime.question_rubric import (
    QUESTION_RUBRICS,
    RESPIRATORY_RUBRIC_V1,
    plan_contract_aspects,
)
from app.schemas.completeness import InquiryDimension
from app.schemas.intake import ActiveObservationContext
from app.schemas.question import QuestionAspectDraft


def _draft(*criteria: str) -> tuple[QuestionAspectDraft, ...]:
    return tuple(QuestionAspectDraft(criterion=criterion) for criterion in criteria)


def _fact(*, fact_key: str, value: str, normalized: str | None = None) -> ActiveObservationContext:
    return ActiveObservationContext(
        observation_id=uuid4(),
        fact_key=fact_key,
        value=value,
        normalized_value=normalized,
    )


def test_rubric_registry_has_respiratory_v1() -> None:
    rubric = QUESTION_RUBRICS[InquiryDimension.TEN_RESPIRATORY]
    assert rubric.version == "respiratory.v1"
    assert rubric.dimension is InquiryDimension.TEN_RESPIRATORY
    assert tuple(item.criterion for item in rubric.aspects) == (
        "说明咳嗽是否有痰（干咳/有痰）",
        "说明痰的颜色",
        "说明痰的量",
        "说明痰的质地（稀/黏/泡沫等）",
    )
    assert RESPIRATORY_RUBRIC_V1 is rubric


def test_first_ask_freezes_required_and_model_aspects() -> None:
    """On the first ask (no sputum fact yet) the contract freezes the required
    criterion plus the model's own criteria; conditionals wait for the fact."""
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        _draft("说明是否有咳嗽"),
        (),
    )
    assert planned[0] == "说明咳嗽是否有痰（干咳/有痰）"
    assert "说明是否有咳嗽" in planned
    assert "说明痰的颜色" not in planned


def test_sputum_fact_freezes_conditional_aspects() -> None:
    """Once the ledger shows 有痰, the sputum conditionals become mandatory so
    a coarse fact can never close the respiratory dimension."""
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        _draft("说明咳嗽是否有痰"),
        (_fact(fact_key="present_illness.sputum", value="有痰，痰是白色的"),),
    )
    assert planned == (
        "说明咳嗽是否有痰（干咳/有痰）",
        "说明痰的颜色",
        "说明痰的量",
        "说明痰的质地（稀/黏/泡沫等）",
    )


def test_sputum_condition_matches_normalized_value() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        (),
        (_fact(fact_key="present_illness.sputum", value="有痰", normalized="有痰"),),
    )
    assert "说明痰的颜色" in planned


def test_plan_dedupes_model_criteria() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        _draft("说明咳嗽是否有痰（干咳/有痰）", "补充说明痰的量"),
        (),
    )
    assert planned[0] == "说明咳嗽是否有痰（干咳/有痰）"
    assert planned.count("说明咳嗽是否有痰（干咳/有痰）") == 1
    assert "补充说明痰的量" in planned


def test_plan_caps_at_four_aspects() -> None:
    model_aspects = _draft("模型额外项一", "模型额外项二", "模型额外项三", "模型额外项四")
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        model_aspects,
        (_fact(fact_key="present_illness.sputum", value="有痰"),),
    )
    assert len(planned) <= 4
    # The Rubric's mandatory set wins over extra model criteria under the cap.
    assert planned == (
        "说明咳嗽是否有痰（干咳/有痰）",
        "说明痰的颜色",
        "说明痰的量",
        "说明痰的质地（稀/黏/泡沫等）",
    )


def test_unconfigured_dimension_keeps_legacy_model_aspects() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        _draft("说明睡眠质量"),
        (),
    )
    assert planned == ("说明睡眠质量",)


def test_unconfigured_dimension_without_model_aspects_uses_fallback() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        (),
        (),
    )
    assert planned[0].startswith("补全 ten_questions.sleep")


def test_sputum_condition_does_not_match_dry_cough_fact() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        _draft("说明咳嗽是否有痰"),
        (_fact(fact_key="present_illness.sputum", value="无痰，是干咳"),),
    )
    assert "说明痰的颜色" not in planned
