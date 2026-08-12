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
    """Non-ten-question dimensions (chief-complaint layer) have no Rubric and
    keep the legacy model-only behavior."""
    planned = plan_contract_aspects(
        InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        _draft("说明本次主要不适"),
        (),
    )
    assert planned == ("说明本次主要不适",)


def test_unconfigured_dimension_without_model_aspects_uses_fallback() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        (),
        (),
    )
    assert planned[0].startswith("补全 chief_complaint.symptom")


def test_sputum_condition_does_not_match_dry_cough_fact() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_RESPIRATORY,
        _draft("说明咳嗽是否有痰"),
        (_fact(fact_key="present_illness.sputum", value="无痰，是干咳"),),
    )
    assert "说明痰的颜色" not in planned


# ---- ten-question registry completeness -----------------------------------


def test_registry_covers_all_ten_question_dimensions() -> None:
    ten_questions = (
        InquiryDimension.TEN_RESPIRATORY,
        InquiryDimension.TEN_COLD_HEAT,
        InquiryDimension.TEN_SWEAT,
        InquiryDimension.TEN_HEAD_BODY,
        InquiryDimension.TEN_STOOL_URINE,
        InquiryDimension.TEN_DIET,
        InquiryDimension.TEN_CHEST_ABDOMEN,
        InquiryDimension.TEN_THIRST,
        InquiryDimension.TEN_SLEEP,
        InquiryDimension.TEN_PAIN,
        InquiryDimension.TEN_MENSES_LEUKORRHEA,
    )
    for dimension in ten_questions:
        rubric = QUESTION_RUBRICS[dimension]
        assert rubric.dimension is dimension
        assert 1 <= len(rubric.aspects) <= 4
        assert any(aspect.required for aspect in rubric.aspects)
        assert len({aspect.criterion for aspect in rubric.aspects}) == len(rubric.aspects)


def test_every_conditional_uses_contract_fact_keys() -> None:
    """Conditions reference only the intake contract's fact keys."""
    valid_keys = {"chief_complaint.symptom", "present_illness.change"} | {
        "ten_questions." + d
        for d in (
            "cold_heat", "sweat", "head_body", "stool_urine", "diet",
            "chest_abdomen", "thirst", "sleep", "menses_leukorrhea", "pain", "respiratory",
        )
    } | {
        "present_illness." + k
        for k in (
            "chills", "fever", "aversion_cold", "sweat", "head_body", "body_ache",
            "chest", "abdomen", "distension", "thirst", "appetite", "diet", "sleep",
            "insomnia", "pain", "stool", "urine", "sputum",
        )
    }
    for rubric in QUESTION_RUBRICS.values():
        for aspect in rubric.aspects:
            for condition in aspect.conditional:
                fact_key = condition.partition("=")[0]
                assert fact_key in valid_keys, f"{condition} uses unknown key {fact_key}"


# ---- per-dimension conditional freeze -------------------------------------


def test_cold_heat_base_and_positive_fact_freezes_conjunction() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_COLD_HEAT,
        _draft("说明是否发热"),
        (_fact(fact_key="present_illness.fever", value="低热"),),
    )
    assert planned[0] == "说明是否怕冷或发热（恶寒/畏寒/发热）"
    assert "说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）" in planned
    assert "说明发热的程度（低热/高热）" in planned


def test_cold_heat_normal_fact_does_not_freeze() -> None:
    """A normal 寒热 fact (no positive token) must not freeze the conjunction.
    Negated-word values like "不发热" may mildly over-freeze (one extra
    question the patient can answer) — the conservative direction."""
    planned = plan_contract_aspects(
        InquiryDimension.TEN_COLD_HEAT,
        _draft("说明是否有发热"),
        (_fact(fact_key="ten_questions.cold_heat", value="正常"),),
    )
    assert "说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）" not in planned


def test_sweat_fact_freezes_time_and_location() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SWEAT,
        _draft("说明是否出汗"),
        (_fact(fact_key="present_illness.sweat", value="盗汗"),),
    )
    assert planned[0] == "说明是否出汗（自汗/盗汗/无汗）"
    assert "说明出汗的时间或诱因（白天自汗/夜间盗汗）" in planned
    assert "说明出汗的部位（头面/手足心/全身）" in planned


def test_sweat_no_sweat_fact_keeps_only_required() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SWEAT,
        _draft("说明是否出汗"),
        (_fact(fact_key="ten_questions.sweat", value="无汗"),),
    )
    assert "说明出汗的时间或诱因（白天自汗/夜间盗汗）" not in planned


def test_sleep_disturbance_freezes_more_and_difficulty() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        _draft("说明睡眠质量"),
        (_fact(fact_key="present_illness.insomnia", value="入睡困难"),),
    )
    assert planned[0] == "说明睡眠情况（时长/质量）"
    assert "说明是否多梦或易醒" in planned
    assert "说明入睡是否困难" in planned


def test_sleep_normal_fact_keeps_only_required() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        _draft("说明睡眠质量"),
        (_fact(fact_key="ten_questions.sleep", value="睡眠正常"),),
    )
    assert "说明是否多梦或易醒" not in planned
    assert "说明入睡是否困难" not in planned


def test_stool_abnormal_fact_freezes_details() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_STOOL_URINE,
        _draft("说明大便情况"),
        (_fact(fact_key="present_illness.stool", value="腹泻"),),
    )
    assert planned[0] == "说明大便情况（次数/性状/是否带血）"
    assert "说明大便异常的具体表现（稀溏/干结/带血）" in planned


def test_pain_fact_freezes_severity() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_PAIN,
        _draft("说明疼痛位置"),
        (_fact(fact_key="present_illness.pain", value="胀痛"),),
    )
    assert planned[0] == "说明疼痛的部位"
    assert "说明疼痛的性质（刺痛/胀痛/隐痛/绞痛等）" in planned
    assert "说明疼痛的程度或加重/缓解因素" in planned


def test_menses_abnormal_fact_freezes_concomitants() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_MENSES_LEUKORRHEA,
        _draft("说明月经情况"),
        (_fact(fact_key="ten_questions.menses_leukorrhea", value="量多"),),
    )
    assert "说明月经伴随症状（痛经/血块/经前乳胀）" in planned


def test_thirst_fact_freezes_preference() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_THIRST,
        _draft("说明是否口渴"),
        (_fact(fact_key="ten_questions.thirst", value="口渴"),),
    )
    assert planned[0] == "说明是否口渴（口干/喜饮水）"
    assert "说明口渴时喜冷饮还是热饮" in planned


def test_head_body_and_chest_abdomen_have_two_required_slots() -> None:
    for dimension, first, second in (
        (InquiryDimension.TEN_HEAD_BODY, "说明头部有无不适（头痛/头晕/头重）", "说明身体有无酸痛或不适（身痛/身重/乏力）"),
        (InquiryDimension.TEN_CHEST_ABDOMEN, "说明胸部有无不适（胸闷/胸痛/心悸）", "说明腹部有无不适（腹胀/腹痛/胃部不适）"),
    ):
        planned = plan_contract_aspects(dimension, (), ())
        assert planned[:2] == (first, second)


def test_rubric_criteria_are_short_verifiable_statements() -> None:
    for rubric in QUESTION_RUBRICS.values():
        for aspect in rubric.aspects:
            assert aspect.criterion.startswith("说明")
            assert "？" not in aspect.criterion
            assert len(aspect.criterion) <= 240


def test_sleep_complaint_fact_freezes_conditionals() -> None:
    """A chief complaint carrying 失眠 must freeze the sleep conditionals —
    the complaint is the strongest signal and lands on chief_complaint.symptom."""
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        _draft("说明睡眠质量"),
        (_fact(fact_key="chief_complaint.symptom", value="失眠一周了"),),
    )
    assert "说明是否多梦或易醒" in planned
    assert "说明入睡是否困难" in planned


def test_cold_heat_complaint_fact_freezes_conjunction() -> None:
    planned = plan_contract_aspects(
        InquiryDimension.TEN_COLD_HEAT,
        _draft("说明是否有发热"),
        (_fact(fact_key="chief_complaint.symptom", value="发烧两天"),),
    )
    assert "说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）" in planned


def test_change_fact_carries_sleep_disturbance() -> None:
    """Sleep disturbance mentioned incidentally in a change answer freezes the
    conditionals (present_illness.change is where incidental symptoms land)."""
    planned = plan_contract_aspects(
        InquiryDimension.TEN_SLEEP,
        _draft("说明睡眠质量"),
        (_fact(fact_key="present_illness.change", value="睡眠不好，入睡困难"),),
    )
    assert "说明入睡是否困难" in planned
