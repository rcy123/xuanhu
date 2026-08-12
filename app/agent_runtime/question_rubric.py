"""R9 planning layer: per-dimension Rubrics decide what a question must cover.

The Rubric is versioned Python configuration — not a database field, not a
per-dimension code branch.  When a dimension has a Rubric, the durable contract
aspects are planned deterministically (Rubric criteria first, model aspects
merged in); without one, the model's aspects stand unchanged (legacy behavior).

Conditional criteria are evaluated against the active fact ledger: once a fact
such as ``present_illness.sputum=有痰`` exists, the sputum follow-up criteria
become mandatory so a coarse "有痰" can never close the respiratory dimension.
Every ten-question dimension ships a Rubric; the conditional criteria freeze
only on POSITIVE facts (a 无痰/dry-cough fact must NOT pull the sputum
conditionals, symmetric for the other dimensions).
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
    conditional: tuple[str, ...] = Field(default=(), max_length=8)


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

COLD_HEAT_RUBRIC_V1 = DimensionRubric(
    version="cold_heat.v1",
    dimension=InquiryDimension.TEN_COLD_HEAT,
    aspects=(
        RubricAspect(criterion="说明是否怕冷或发热（恶寒/畏寒/发热）", required=True),
        RubricAspect(
            criterion="说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）",
            conditional=(
                "ten_questions.cold_heat=热",
                "ten_questions.cold_heat=恶寒",
                "ten_questions.cold_heat=畏寒",
                "present_illness.fever=热",
                "present_illness.chills=恶寒",
                "present_illness.chills=怕冷",
                "present_illness.chills=寒战",
                "present_illness.fever=发烧",
            ),
        ),
        RubricAspect(
            criterion="说明发热的程度（低热/高热）",
            conditional=(
                "ten_questions.cold_heat=低热",
                "ten_questions.cold_heat=高热",
                "present_illness.fever=低热",
                "present_illness.fever=高热",
            ),
        ),
    ),
)

SWEAT_RUBRIC_V1 = DimensionRubric(
    version="sweat.v1",
    dimension=InquiryDimension.TEN_SWEAT,
    aspects=(
        RubricAspect(criterion="说明是否出汗（自汗/盗汗/无汗）", required=True),
        RubricAspect(
            criterion="说明出汗的时间或诱因（白天自汗/夜间盗汗）",
            conditional=(
                "ten_questions.sweat=自汗",
                "ten_questions.sweat=盗汗",
                "present_illness.sweat=自汗",
                "present_illness.sweat=盗汗",
            ),
        ),
        RubricAspect(
            criterion="说明出汗的部位（头面/手足心/全身）",
            conditional=(
                "ten_questions.sweat=自汗",
                "ten_questions.sweat=盗汗",
                "present_illness.sweat=自汗",
                "present_illness.sweat=盗汗",
            ),
        ),
    ),
)

HEAD_BODY_RUBRIC_V1 = DimensionRubric(
    version="head_body.v1",
    dimension=InquiryDimension.TEN_HEAD_BODY,
    aspects=(
        RubricAspect(criterion="说明头部有无不适（头痛/头晕/头重）", required=True),
        RubricAspect(criterion="说明身体有无酸痛或不适（身痛/身重/乏力）", required=True),
        RubricAspect(
            criterion="说明头痛或头晕的部位与性质",
            conditional=(
                "ten_questions.head_body=头痛",
                "ten_questions.head_body=头晕",
                "ten_questions.head_body=头重",
                "present_illness.head_body=头痛",
                "present_illness.head_body=头晕",
            ),
        ),
    ),
)

STOOL_URINE_RUBRIC_V1 = DimensionRubric(
    version="stool_urine.v1",
    dimension=InquiryDimension.TEN_STOOL_URINE,
    aspects=(
        RubricAspect(criterion="说明大便情况（次数/性状/是否带血）", required=True),
        RubricAspect(criterion="说明小便情况（频次/颜色/涩痛）", required=True),
        RubricAspect(
            criterion="说明大便异常的具体表现（稀溏/干结/带血）",
            conditional=(
                "ten_questions.stool_urine=腹泻",
                "ten_questions.stool_urine=便秘",
                "ten_questions.stool_urine=便血",
                "ten_questions.stool_urine=稀",
                "present_illness.stool=腹泻",
                "present_illness.stool=便秘",
                "present_illness.stool=便血",
            ),
        ),
    ),
)

DIET_RUBRIC_V1 = DimensionRubric(
    version="diet.v1",
    dimension=InquiryDimension.TEN_DIET,
    aspects=(
        RubricAspect(criterion="说明食欲与食量（正常/减退/亢进）", required=True),
        RubricAspect(
            criterion="说明口味有无异常（口苦/口淡/口腻/口甜）",
            conditional=(
                "ten_questions.diet=减退",
                "ten_questions.diet=纳差",
                "ten_questions.diet=不振",
                "present_illness.appetite=减退",
            ),
        ),
    ),
)

CHEST_ABDOMEN_RUBRIC_V1 = DimensionRubric(
    version="chest_abdomen.v1",
    dimension=InquiryDimension.TEN_CHEST_ABDOMEN,
    aspects=(
        RubricAspect(criterion="说明胸部有无不适（胸闷/胸痛/心悸）", required=True),
        RubricAspect(criterion="说明腹部有无不适（腹胀/腹痛/胃部不适）", required=True),
        RubricAspect(
            criterion="说明胸腹不适的部位与性质",
            conditional=(
                "ten_questions.chest_abdomen=胸闷",
                "ten_questions.chest_abdomen=胸痛",
                "ten_questions.chest_abdomen=腹胀",
                "present_illness.abdomen=腹痛",
            ),
        ),
    ),
)

THIRST_RUBRIC_V1 = DimensionRubric(
    version="thirst.v1",
    dimension=InquiryDimension.TEN_THIRST,
    aspects=(
        RubricAspect(criterion="说明是否口渴（口干/喜饮水）", required=True),
        RubricAspect(
            criterion="说明口渴时喜冷饮还是热饮",
            conditional=(
                "ten_questions.thirst=口渴",
                "ten_questions.thirst=口干",
                "present_illness.thirst=口渴",
                "present_illness.thirst=口干",
            ),
        ),
    ),
)

SLEEP_RUBRIC_V1 = DimensionRubric(
    version="sleep.v1",
    dimension=InquiryDimension.TEN_SLEEP,
    aspects=(
        RubricAspect(criterion="说明睡眠情况（时长/质量）", required=True),
        RubricAspect(
            criterion="说明是否多梦或易醒",
            conditional=(
                "ten_questions.sleep=失眠",
                "ten_questions.sleep=多梦",
                "ten_questions.sleep=易醒",
                "ten_questions.sleep=差",
                "present_illness.sleep=失眠",
                "present_illness.insomnia=失眠",
                "present_illness.insomnia=难",
            ),
        ),
        RubricAspect(
            criterion="说明入睡是否困难",
            conditional=(
                "ten_questions.sleep=失眠",
                "ten_questions.sleep=难",
                "ten_questions.sleep=睡不着",
                "present_illness.insomnia=失眠",
                "present_illness.insomnia=难",
            ),
        ),
    ),
)

PAIN_RUBRIC_V1 = DimensionRubric(
    version="pain.v1",
    dimension=InquiryDimension.TEN_PAIN,
    aspects=(
        RubricAspect(criterion="说明疼痛的部位", required=True),
        RubricAspect(criterion="说明疼痛的性质（刺痛/胀痛/隐痛/绞痛等）", required=True),
        RubricAspect(
            criterion="说明疼痛的程度或加重/缓解因素",
            conditional=(
                "ten_questions.pain=胀痛",
                "ten_questions.pain=酸痛",
                "ten_questions.pain=隐痛",
                "ten_questions.pain=刺痛",
                "present_illness.pain=胀痛",
                "present_illness.pain=酸痛",
                "present_illness.body_ache=酸痛",
            ),
        ),
    ),
)

MENSES_LEUKORRHEA_RUBRIC_V1 = DimensionRubric(
    version="menses_leukorrhea.v1",
    dimension=InquiryDimension.TEN_MENSES_LEUKORRHEA,
    aspects=(
        RubricAspect(criterion="说明月经情况（周期/经量/经色）", required=True),
        RubricAspect(criterion="说明带下情况（量/色/味）", required=True),
        RubricAspect(
            criterion="说明月经伴随症状（痛经/血块/经前乳胀）",
            conditional=(
                "ten_questions.menses_leukorrhea=量多",
                "ten_questions.menses_leukorrhea=量少",
                "ten_questions.menses_leukorrhea=淋漓",
                "present_illness.pain=痛经",
            ),
        ),
    ),
)

QUESTION_RUBRICS: dict[InquiryDimension, DimensionRubric] = {
    InquiryDimension.TEN_RESPIRATORY: RESPIRATORY_RUBRIC_V1,
    InquiryDimension.TEN_COLD_HEAT: COLD_HEAT_RUBRIC_V1,
    InquiryDimension.TEN_SWEAT: SWEAT_RUBRIC_V1,
    InquiryDimension.TEN_HEAD_BODY: HEAD_BODY_RUBRIC_V1,
    InquiryDimension.TEN_STOOL_URINE: STOOL_URINE_RUBRIC_V1,
    InquiryDimension.TEN_DIET: DIET_RUBRIC_V1,
    InquiryDimension.TEN_CHEST_ABDOMEN: CHEST_ABDOMEN_RUBRIC_V1,
    InquiryDimension.TEN_THIRST: THIRST_RUBRIC_V1,
    InquiryDimension.TEN_SLEEP: SLEEP_RUBRIC_V1,
    InquiryDimension.TEN_PAIN: PAIN_RUBRIC_V1,
    InquiryDimension.TEN_MENSES_LEUKORRHEA: MENSES_LEUKORRHEA_RUBRIC_V1,
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


def plan_contract_aspects(
    dimension: InquiryDimension,
    model_aspects: tuple[QuestionAspectDraft, ...],
    current_facts: Sequence[ActiveObservationContext],
) -> tuple[str, ...]:
    """Deterministically plan the durable contract criteria for one question.

    Rubric criteria come first (in rubric order: required aspects, then
    conditional aspects whose conditions hold), then the model's aspects
    (deduplicated).  Conditional criteria are frozen by the active fact
    ledger — e.g. a sputum fact pulls the sputum follow-up criteria into the
    mandatory set so the contract can never regress to "有痰只问一次".
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
    return tuple(planned[:_RUBRIC_MAX_ASPECTS])


__all__ = [
    "QUESTION_RUBRIC_SCHEMA_VERSION",
    "QUESTION_RUBRICS",
    "RESPIRATORY_RUBRIC_V1",
    "COLD_HEAT_RUBRIC_V1",
    "SWEAT_RUBRIC_V1",
    "HEAD_BODY_RUBRIC_V1",
    "STOOL_URINE_RUBRIC_V1",
    "DIET_RUBRIC_V1",
    "CHEST_ABDOMEN_RUBRIC_V1",
    "THIRST_RUBRIC_V1",
    "SLEEP_RUBRIC_V1",
    "PAIN_RUBRIC_V1",
    "MENSES_LEUKORRHEA_RUBRIC_V1",
    "DimensionRubric",
    "RubricAspect",
    "plan_contract_aspects",
]
