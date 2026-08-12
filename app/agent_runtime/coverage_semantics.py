"""R9 semantic sufficiency validation: a real quote is not the same as an
answered aspect.

Grounding proves the evidence text exists in the answer; this layer checks
whether the text actually addresses the aspect's criterion.  The check is a
deliberately small, configurable wordlist: when no hint covers the criterion,
or the evidence hits a required term, the model's judgment stands; only a
confident miss downgrades ``addressed``/``not_applicable`` to ``unclear`` so
the aspect stays in the residual follow-up instead of silently satisfying the
contract (降级不误伤、不 reject、不扩拒绝面).

Every ten-question dimension ships hints.  Routing is first-match over the
ordered ``_SEMANTIC_HINTS`` tuple: within a dimension the more specific
conditional-aspect hints come BEFORE the base hint (their route terms are
distinctive long strings, so no cross-dimension hijack).  required_terms are
negation-robust (a normal negative answer still hits a term — "不出汗" contains
汗, "不口干" contains 口干); contradicts_terms stay empty except where a
legitimate N/A answer could not contain the term (the sputum case).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.question_contract import CoverageStatus, QuestionAspect

COVERAGE_SEMANTICS_SCHEMA_VERSION: str = "coverage-semantics.v1"


class _SemanticHint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Terms whose presence in the evidence supports ``addressed``.
    required_terms: tuple[str, ...] = ()
    # Terms whose presence contradicts ``not_applicable`` (e.g. "有痰" for a
    # sputum-color aspect marked N/A).
    contradicts_terms: tuple[str, ...] = ()


# Routing: any match term appearing in the aspect criterion selects the hint.
# Multiple aliases cover the composer's real criteria, which vary from the
# canonical keys.  The tuple order is load-bearing: conditional-aspect hints
# precede the base hint of the same dimension so a criterion like
# "说明怕冷与发热是否同时出现…" routes to the 并见 hint, not the base 寒热 hint.
_SEMANTIC_HINTS: tuple[tuple[tuple[str, ...], _SemanticHint], ...] = (
    # ---- respiratory (validated in production) -----------------------------
    (
        ("痰的颜色", "痰液颜色", "痰色", "痰是什么颜色"),
        _SemanticHint(
            required_terms=("白", "黄", "绿", "红", "褐", "透明", "咖啡", "脓", "血", "带血"),
            contradicts_terms=("有痰", "咳痰"),
        ),
    ),
    (
        ("痰的量", "痰量", "痰多", "痰多不多"),
        _SemanticHint(
            required_terms=("多", "少", "大量", "少量", "中等", "毫升", "一口", "两口", "不多"),
            contradicts_terms=("有痰", "咳痰"),
        ),
    ),
    (
        ("咳嗽性质", "咳嗽是否有痰", "是否有痰", "干咳还是有痰"),
        _SemanticHint(
            required_terms=("干咳", "有痰", "咳痰", "无痰", "没痰", "没有痰"),
        ),
    ),
    # ---- cold_heat 寒热 ------------------------------------------------------
    (
        ("同时出现", "并见", "但热不寒", "但寒不热", "往来寒热", "寒热往来", "恶寒发热", "发热恶寒", "先冷后热", "先热后冷", "交替"),
        _SemanticHint(
            required_terms=("并见", "同时", "同时出现", "一起", "同现", "但热不寒", "但寒不热", "往来寒热", "寒热往来", "先冷后热", "先热后冷", "交替"),
        ),
    ),
    (
        ("发热的程度", "发热程度", "低热", "高热", "低烧", "高烧", "多少度", "几度", "烧到"),
        _SemanticHint(
            required_terms=("低热", "高热", "低烧", "高烧", "度", "烧", "热", "正常"),
        ),
    ),
    (
        ("怕冷", "恶寒", "畏寒", "寒战", "发热", "发烧", "寒热", "发冷", "体温"),
        _SemanticHint(
            required_terms=("怕冷", "恶寒", "畏寒", "寒战", "发热", "发烧", "低热", "高热", "不冷", "不热", "正常", "冷", "热"),
        ),
    ),
    # ---- sweat 汗出 ---------------------------------------------------------
    (
        ("出汗的时间", "出汗时间", "汗出时间", "出汗的诱因", "出汗诱因", "白天自汗", "夜间盗汗", "自汗还是盗汗"),
        _SemanticHint(
            required_terms=("自汗", "盗汗", "白天", "夜间", "晚上", "夜里", "睡", "活动", "运动"),
        ),
    ),
    (
        ("出汗的部位", "出汗部位", "汗出部位", "哪里出汗", "哪个部位出汗"),
        _SemanticHint(
            required_terms=("头", "面", "手", "脚", "足", "全身", "背", "胸", "脸", "腋", "身"),
        ),
    ),
    (
        ("出汗", "汗出", "汗多", "汗少", "无汗"),
        _SemanticHint(
            required_terms=("出汗", "汗出", "盗汗", "自汗", "虚汗", "冷汗", "少汗", "无汗", "汗"),
        ),
    ),
    # ---- head_body 头身 -----------------------------------------------------
    (
        ("头痛或头晕的部位", "头痛或头晕的性质", "头痛部位", "头晕部位"),
        _SemanticHint(
            required_terms=("部位", "左边", "右边", "前额", "后脑", "两侧", "刺痛", "胀痛", "隐痛", "绞痛", "痛", "头", "晕"),
        ),
    ),
    (
        ("头痛", "头晕", "头重", "头胀", "头部", "头不"),
        _SemanticHint(
            required_terms=("头痛", "头晕", "头重", "头胀", "眩晕", "头部", "头不痛", "头不晕", "正常"),
        ),
    ),
    (
        ("身痛", "身重", "乏力", "酸痛", "身体", "身"),
        _SemanticHint(
            required_terms=("身痛", "身重", "乏力", "酸痛", "肌肉", "累", "困", "没力气", "正常", "身"),
        ),
    ),
    # ---- stool_urine 二便 ---------------------------------------------------
    (
        ("稀溏", "干结", "不成形", "大便异常"),
        _SemanticHint(
            required_terms=("稀", "溏", "干", "结", "血", "不成形", "次数", "次"),
        ),
    ),
    (
        ("大便", "排便"),
        _SemanticHint(
            required_terms=("大便", "排便", "成形", "腹泻", "便秘", "便血", "便稀", "溏", "干结", "不成形", "正常", "拉", "泄", "血"),
        ),
    ),
    (
        ("小便", "尿"),
        _SemanticHint(
            required_terms=("小便", "尿", "夜尿", "尿频", "尿痛", "尿急", "尿色", "排尿", "正常"),
        ),
    ),
    # ---- diet 饮食 -----------------------------------------------------------
    (
        ("口味", "口苦", "口淡", "口腻", "口甜", "口黏", "味觉"),
        _SemanticHint(
            required_terms=("口味", "口苦", "口淡", "口腻", "口甜", "口黏", "味", "苦", "甜", "正常"),
        ),
    ),
    (
        ("食欲", "食量", "胃口", "饮食", "吃饭", "进食", "纳差"),
        _SemanticHint(
            required_terms=("食欲", "胃口", "食量", "能吃", "吃得下", "吃不下", "饭量", "纳差", "饭", "正常", "吃", "饿"),
        ),
    ),
    # ---- chest_abdomen 胸腹 -------------------------------------------------
    (
        ("胸腹不适的部位", "胸腹不适的性质", "胸部不适的部位", "腹部不适的部位"),
        _SemanticHint(
            required_terms=("部位", "左边", "右边", "上腹", "下腹", "刺痛", "胀痛", "隐痛", "绞痛", "痛", "胸", "腹"),
        ),
    ),
    (
        ("胸闷", "胸痛", "心悸", "胸部", "胸"),
        _SemanticHint(
            required_terms=("胸闷", "胸痛", "心悸", "心慌", "胸部", "胸", "正常", "痛"),
        ),
    ),
    (
        ("腹胀", "腹痛", "胃胀", "胃痛", "腹部", "腹", "肚"),
        _SemanticHint(
            required_terms=("腹胀", "腹痛", "胃胀", "胃痛", "肚子", "腹部", "胃", "正常", "痛"),
        ),
    ),
    # ---- thirst 口渴 --------------------------------------------------------
    (
        ("冷饮", "热饮", "温水", "凉水", "冷热"),
        _SemanticHint(
            required_terms=("冷饮", "热饮", "温水", "凉水", "冰水", "热水", "喝冷的", "喝热的"),
        ),
    ),
    (
        ("口渴", "口干", "喜饮", "饮水", "喝水", "渴"),
        _SemanticHint(
            required_terms=("口渴", "口干", "喝水", "饮水", "喜饮", "不渴", "口不干", "正常", "渴"),
        ),
    ),
    # ---- sleep 睡眠 ---------------------------------------------------------
    (
        ("入睡", "睡不着", "难入睡", "不易入睡", "难以入睡"),
        _SemanticHint(
            required_terms=("入睡", "睡不着", "难入睡", "不易入睡", "难以入睡", "睡不好", "失眠"),
        ),
    ),
    (
        ("多梦", "易醒", "梦", "夜醒"),
        _SemanticHint(
            required_terms=("多梦", "易醒", "梦", "醒", "夜醒", "浅睡", "一觉到天亮"),
        ),
    ),
    (
        ("睡眠", "睡", "失眠"),
        _SemanticHint(
            required_terms=("睡眠", "睡", "入睡", "失眠", "多梦", "易醒", "夜醒", "梦", "醒", "睡得好", "睡得沉", "一觉到天亮", "正常"),
        ),
    ),
    # ---- pain 疼痛 ----------------------------------------------------------
    (
        ("疼痛的部位", "疼痛部位", "部位", "哪里", "位置", "什么地方"),
        _SemanticHint(
            required_terms=("痛", "疼", "部位", "位置", "左边", "右边", "上腹", "下腹", "头部", "腰", "关节", "背"),
        ),
    ),
    (
        ("疼痛的性质", "性质", "刺痛", "胀痛", "隐痛", "绞痛", "酸痛", "类型"),
        _SemanticHint(
            required_terms=("刺痛", "胀痛", "隐痛", "绞痛", "酸痛", "钝痛", "灼痛", "撕裂", "针扎", "牵拉", "隐隐", "痛"),
        ),
    ),
    (
        ("加重", "缓解", "诱因", "程度", "减轻"),
        _SemanticHint(
            required_terms=("加重", "缓解", "减轻", "按压", "活动", "休息", "程度", "轻", "重", "厉害", "明显", "很痛", "剧痛"),
        ),
    ),
    # ---- menses_leukorrhea 月经带下 -----------------------------------------
    (
        ("痛经", "血块", "乳胀", "伴随"),
        _SemanticHint(
            required_terms=("痛经", "血块", "乳胀", "腰酸", "伴随", "胀", "痛", "酸"),
        ),
    ),
    (
        ("带下", "白带", "分泌物"),
        _SemanticHint(
            required_terms=("带下", "白带", "分泌物", "异味", "瘙痒", "正常", "异常", "多", "少", "痒"),
        ),
    ),
    (
        ("月经", "经期", "经量", "经色", "月事", "例假", "行经"),
        _SemanticHint(
            required_terms=("月经", "经期", "经量", "经色", "月事", "例假", "周期", "推迟", "提前", "血块", "痛经", "正常", "规律", "异常", "绝经", "停经", "行经", "多", "少"),
        ),
    ),
)


def _hint_for_criterion(criterion: str) -> _SemanticHint | None:
    for terms, hint in _SEMANTIC_HINTS:
        if any(term in criterion for term in terms):
            return hint
    return None


def validate_aspect_semantics(
    aspect: QuestionAspect,
    evidence_text: str,
    current_status: CoverageStatus,
) -> CoverageStatus | None:
    """Return a corrected status when confident, else ``None`` (keep judgment).

    - ``addressed`` without a required term → ``unclear`` (residual re-ask);
    - ``not_applicable`` with a contradicting term → ``unclear``;
    - any other case (no hint / term hit / other statuses) → ``None``.
    """

    if current_status is not CoverageStatus.ADDRESSED and current_status is not CoverageStatus.NOT_APPLICABLE:
        return None
    hint = _hint_for_criterion(aspect.criterion)
    if hint is None:
        return None
    if current_status is CoverageStatus.ADDRESSED:
        if not hint.required_terms:
            return None
        if any(term in evidence_text for term in hint.required_terms):
            return None
        return CoverageStatus.UNCLEAR
    if not hint.contradicts_terms:
        return None
    if any(term in evidence_text for term in hint.contradicts_terms):
        return CoverageStatus.UNCLEAR
    return None


__all__ = [
    "COVERAGE_SEMANTICS_SCHEMA_VERSION",
    "validate_aspect_semantics",
]
