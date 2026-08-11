"""R9 planning layer: per-dimension Rubrics decide what a question must cover.

The Rubric is versioned Python configuration — not a database field, not a
per-dimension code branch.  When a dimension has a Rubric, the durable contract
aspects are planned deterministically (Rubric criteria first, model aspects
merged in); without one, the model's aspects stand unchanged (legacy behavior).

Conditional criteria are evaluated against the active fact ledger: once a fact
such as ``present_illness.sputum=有痰`` exists, the sputum follow-up criteria
become mandatory so a coarse "有痰" can never close the respiratory dimension.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.completeness import InquiryDimension
from app.schemas.intake import ActiveObservationContext
from app.schemas.question import QuestionAspectDraft

QUESTION_RUBRIC_SCHEMA_VERSION: str = "question-rubric.v1"

_RUBRIC_MAX_ASPECTS = 4


class RubricAspect(BaseModel):
    """One criterion a question for this dimension should cover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str = Field(min_length=1, max_length=240)
    required: bool = False
    # Conditions of the form ``fact_key=value``; the criterion becomes mandatory
    # when any condition matches an active fact (value compared by containment).
    conditional: tuple[str, ...] = Field(default=(), max_length=4)


class DimensionRubric(BaseModel):
    """Versioned per-dimension planning configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = QUESTION_RUBRIC_SCHEMA_VERSION
    version: str = Field(min_length=1, max_length=64)
    dimension: InquiryDimension
    aspects: tuple[RubricAspect, ...] = Field(min_length=1, max_length=4)


RESPIRATORY_RUBRIC_V1 = DimensionRubric(
    version="respiratory.v1",
    dimension=InquiryDimension.TEN_RESPIRATORY,
    aspects=(
        RubricAspect(criterion="说明咳嗽是否有痰（干咳/有痰）", required=True),
        RubricAspect(criterion="说明痰的颜色", conditional=("present_illness.sputum=有痰",)),
        RubricAspect(criterion="说明痰的量", conditional=("present_illness.sputum=有痰",)),
        RubricAspect(criterion="说明痰的质地（稀/黏/泡沫等）", conditional=("present_illness.sputum=有痰",)),
    ),
)

QUESTION_RUBRICS: dict[InquiryDimension, DimensionRubric] = {
    InquiryDimension.TEN_RESPIRATORY: RESPIRATORY_RUBRIC_V1,
    # 其余维度按需补齐；未配置的维度走模型自由 aspects（现状行为）。
}


def _condition_holds(
    conditions: tuple[str, ...],
    current_facts: Sequence[ActiveObservationContext],
) -> bool:
    for condition in conditions:
        fact_key, _, expected = condition.partition("=")
        if not fact_key:
            continue
        for fact in current_facts:
            if fact.fact_key != fact_key:
                continue
            for value in (fact.normalized_value, fact.value):
                if value is None:
                    continue
                try:
                    if expected in str(value):
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def _has_fact(
    condition: str,
    current_facts: Sequence[ActiveObservationContext],
) -> bool:
    return _condition_holds((condition,), current_facts)


def plan_contract_aspects(
    dimension: InquiryDimension,
    model_aspects: tuple[QuestionAspectDraft, ...],
    current_facts: Sequence[ActiveObservationContext],
) -> tuple[str, ...]:
    """Deterministically plan the durable contract criteria for one question.

    Rubric criteria come first (in rubric order), then the model's aspects
    (deduplicated).  A sputum fact pulls the conditional sputum criteria into
    the mandatory set so the contract can never regress to "有痰只问一次"。
    """

    rubric = QUESTION_RUBRICS.get(dimension)
    if rubric is None:
        criteria = tuple(item.criterion for item in model_aspects)
        return criteria or (f"补全 {dimension.value} 当前问题所需信息",)

    planned: list[str] = []
    for aspect in rubric.aspects:
        if (aspect.required or _condition_holds(aspect.conditional, current_facts)) and aspect.criterion not in planned:
            planned.append(aspect.criterion)
    for model in model_aspects:
        if model.criterion not in planned:
            planned.append(model.criterion)
    if _has_fact("present_illness.sputum=有痰", current_facts):
        for aspect in rubric.aspects:
            if aspect.conditional and aspect.criterion not in planned:
                planned.append(aspect.criterion)
    return tuple(planned[:_RUBRIC_MAX_ASPECTS])


__all__ = [
    "QUESTION_RUBRIC_SCHEMA_VERSION",
    "QUESTION_RUBRICS",
    "RESPIRATORY_RUBRIC_V1",
    "DimensionRubric",
    "RubricAspect",
    "plan_contract_aspects",
]
