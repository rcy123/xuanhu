"""R9-C semantic wordlist tests across all ten-question dimensions.

For every dimension: an addressed aspect whose evidence actually contains the
required terms keeps the model judgment; a vague answer ("还好"/"正常" where
the aspect needs specifics) downgrades to ``unclear`` and stays in the residual
follow-up.  Negation-robustness is asserted explicitly (negative answers must
still hit a required term so they are not wrongly downgraded).
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
        question_text="请补充说明。",
        dimension="ten_questions.diet",
        selection_kind="required",
        aspect_criteria=(criterion,),
    )
    return contract.aspects[0]


def _addressed(criterion: str, evidence: str) -> CoverageStatus | None:
    return validate_aspect_semantics(_aspect(criterion), evidence, CoverageStatus.ADDRESSED)


# ---- cold_heat -------------------------------------------------------------


def test_cold_heat_addressed_keeps_with_cold_word() -> None:
    assert _addressed("说明是否怕冷或发热（恶寒/畏寒/发热）", "有点怕冷") is None


def test_cold_heat_addressed_keeps_with_fever_word() -> None:
    assert _addressed("说明是否怕冷或发热（恶寒/畏寒/发热）", "发烧到38度") is None


def test_cold_heat_negation_keeps_status() -> None:
    assert _addressed("说明是否怕冷或发热（恶寒/畏寒/发热）", "不冷不热，体温正常") is None


def test_cold_heat_vague_downgrades() -> None:
    assert _addressed("说明是否怕冷或发热（恶寒/畏寒/发热）", "还好") is CoverageStatus.UNCLEAR


def test_cold_heat_conjunction_downgrades_without_relation() -> None:
    assert (
        _addressed("说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）", "有发烧")
        is CoverageStatus.UNCLEAR
    )
    assert (
        _addressed("说明怕冷与发热是否同时出现（寒热并见/但热不寒/往来寒热）", "又冷又热，一起出现")
        is None
    )


# ---- sweat -----------------------------------------------------------------


def test_sweat_addressed_keeps_with_sweat_word() -> None:
    assert _addressed("说明是否出汗（自汗/盗汗/无汗）", "白天稍微一动就出汗") is None


def test_sweat_negation_keeps_status() -> None:
    assert _addressed("说明是否出汗（自汗/盗汗/无汗）", "不出汗") is None


def test_sweat_vague_downgrades() -> None:
    assert _addressed("说明是否出汗（自汗/盗汗/无汗）", "没感觉") is CoverageStatus.UNCLEAR


def test_sweat_time_downgrades_without_time() -> None:
    assert _addressed("说明出汗的时间或诱因（白天自汗/夜间盗汗）", "出汗多") is CoverageStatus.UNCLEAR
    assert _addressed("说明出汗的时间或诱因（白天自汗/夜间盗汗）", "晚上睡觉盗汗") is None


# ---- head_body -------------------------------------------------------------


def test_head_addressed_keeps() -> None:
    assert _addressed("说明头部有无不适（头痛/头晕/头重）", "头痛，前额胀") is None
    assert _addressed("说明头部有无不适（头痛/头晕/头重）", "头不痛不晕") is None


def test_head_vague_downgrades() -> None:
    assert _addressed("说明头部有无不适（头痛/头晕/头重）", "没别的") is CoverageStatus.UNCLEAR


def test_body_addressed_keeps() -> None:
    assert _addressed("说明身体有无酸痛或不适（身痛/身重/乏力）", "浑身酸痛没力气") is None
    assert _addressed("说明身体有无酸痛或不适（身痛/身重/乏力）", "身上不酸不痛") is None


# ---- stool_urine -----------------------------------------------------------


def test_stool_addressed_keeps() -> None:
    assert _addressed("说明大便情况（次数/性状/是否带血）", "大便正常，一天一次") is None
    assert _addressed("说明大便情况（次数/性状/是否带血）", "有点拉肚子，一天三次") is None


def test_stool_vague_downgrades() -> None:
    assert _addressed("说明大便情况（次数/性状/是否带血）", "挺好的") is CoverageStatus.UNCLEAR


def test_urine_addressed_keeps() -> None:
    assert _addressed("说明小便情况（频次/颜色/涩痛）", "小便正常，颜色清亮") is None
    assert _addressed("说明小便情况（频次/颜色/涩痛）", "晚上起夜两次，尿频") is None


def test_stool_abnormal_downgrades_without_detail() -> None:
    assert _addressed("说明大便异常的具体表现（稀溏/干结/带血）", "大便不正常") is CoverageStatus.UNCLEAR
    assert _addressed("说明大便异常的具体表现（稀溏/干结/带血）", "大便稀溏不成形") is None


# ---- diet ------------------------------------------------------------------


def test_appetite_addressed_keeps() -> None:
    assert _addressed("说明食欲与食量（正常/减退/亢进）", "胃口还可以，饭量正常") is None
    assert _addressed("说明食欲与食量（正常/减退/亢进）", "吃不下，没胃口") is None


def test_appetite_vague_downgrades() -> None:
    assert _addressed("说明食欲与食量（正常/减退/亢进）", "还行吧") is CoverageStatus.UNCLEAR


def test_taste_addressed_keeps() -> None:
    assert _addressed("说明口味有无异常（口苦/口淡/口腻/口甜）", "嘴里发苦") is None
    assert _addressed("说明口味有无异常（口苦/口淡/口腻/口甜）", "口味正常") is None


# ---- chest_abdomen ---------------------------------------------------------


def test_chest_addressed_keeps() -> None:
    assert _addressed("说明胸部有无不适（胸闷/胸痛/心悸）", "胸闷，气短") is None
    assert _addressed("说明胸部有无不适（胸闷/胸痛/心悸）", "胸口不闷不痛") is None


def test_abdomen_addressed_keeps() -> None:
    assert _addressed("说明腹部有无不适（腹胀/腹痛/胃部不适）", "肚子胀，有点痛") is None


def test_chest_abdomen_vague_downgrades() -> None:
    assert _addressed("说明胸部有无不适（胸闷/胸痛/心悸）", "还行") is CoverageStatus.UNCLEAR


# ---- thirst ----------------------------------------------------------------


def test_thirst_addressed_keeps() -> None:
    assert _addressed("说明是否口渴（口干/喜饮水）", "口干，喝很多水") is None
    assert _addressed("说明是否口渴（口干/喜饮水）", "不渴，不怎么喝水") is None


def test_thirst_preference_addressed_keeps() -> None:
    assert _addressed("说明口渴时喜冷饮还是热饮", "喜欢喝凉的，冰水") is None


def test_thirst_preference_downgrades_without_preference() -> None:
    assert _addressed("说明口渴时喜冷饮还是热饮", "就是口渴") is CoverageStatus.UNCLEAR


# ---- sleep -----------------------------------------------------------------


def test_sleep_addressed_keeps() -> None:
    assert _addressed("说明睡眠情况（时长/质量）", "睡得还行，一觉到天亮") is None
    assert _addressed("说明睡眠情况（时长/质量）", "睡得不好，半夜总醒") is None


def test_sleep_difficulty_addressed_keeps() -> None:
    assert _addressed("说明入睡是否困难", "入睡困难，要躺一小时") is None


def test_sleep_difficulty_downgrades_without_detail() -> None:
    assert _addressed("说明入睡是否困难", "睡眠还行") is CoverageStatus.UNCLEAR


def test_sleep_dreams_addressed_keeps() -> None:
    assert _addressed("说明是否多梦或易醒", "多梦，一晚上醒三四次") is None


# ---- pain ------------------------------------------------------------------


def test_pain_location_addressed_keeps() -> None:
    assert _addressed("说明疼痛的部位", "右边下腹疼") is None


def test_pain_nature_addressed_keeps() -> None:
    assert _addressed("说明疼痛的性质（刺痛/胀痛/隐痛/绞痛等）", "是胀痛，不是刺痛") is None


def test_pain_nature_vague_downgrades() -> None:
    assert _addressed("说明疼痛的性质（刺痛/胀痛/隐痛/绞痛等）", "就是疼") is CoverageStatus.UNCLEAR


def test_pain_severity_addressed_keeps() -> None:
    assert _addressed("说明疼痛的程度或加重/缓解因素", "疼得厉害，按压更重") is None


# ---- menses_leukorrhea -----------------------------------------------------


def test_menses_addressed_keeps() -> None:
    assert _addressed("说明月经情况（周期/经量/经色）", "月经规律，量正常") is None
    assert _addressed("说明月经情况（周期/经量/经色）", "经量偏少，颜色暗") is None


def test_leukorrhea_addressed_keeps() -> None:
    assert _addressed("说明带下情况（量/色/味）", "白带有点多，无异味") is None


def test_menses_concomitants_addressed_keeps() -> None:
    assert _addressed("说明月经伴随症状（痛经/血块/经前乳胀）", "有痛经，还带血块") is None


def test_menses_vague_downgrades() -> None:
    assert _addressed("说明月经情况（周期/经量/经色）", "还行") is CoverageStatus.UNCLEAR


# ---- cross-dimension routing -----------------------------------------------


def test_shared_location_word_routes_to_correct_dimension_hint() -> None:
    """部位/性质 appear in several dimensions; a location answer must not be
    wrongly downgraded no matter which 部位 hint it routes to."""
    for criterion, evidence in (
        ("说明头痛或头晕的部位与性质", "前额隐痛"),
        ("说明胸腹不适的部位与性质", "右上腹胀痛"),
        ("说明疼痛的部位", "腰背部隐痛"),
    ):
        assert _addressed(criterion, evidence) is None


def test_stool_and_urine_criteria_route_to_their_own_hints() -> None:
    """The 小便 criterion must never downgrade a urine answer via the stool
    hint and vice versa."""
    assert _addressed("说明小便情况（频次/颜色/涩痛）", "尿频，颜色黄") is None
    assert _addressed("说明大便情况（次数/性状/是否带血）", "大便正常") is None


def test_uncovered_criterion_still_never_intervened() -> None:
    assert _addressed("说明痰的质地（稀/黏/泡沫等）", "是白色的泡沫痰", ) is None


def test_not_applicable_keeps_status_when_no_contradiction() -> None:
    """contradicts_terms stay empty for the new dimensions, so a legitimate
    N/A answer ("没有怕冷发热") keeps the model's not_applicable."""
    assert (
        validate_aspect_semantics(
            _aspect("说明是否怕冷或发热（恶寒/畏寒/发热）"),
            "没有怕冷发热",
            CoverageStatus.NOT_APPLICABLE,
        )
        is None
    )
