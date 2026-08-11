"""R9 semantic sufficiency validation: a real quote is not the same as an
answered aspect.

Grounding proves the evidence text exists in the answer; this layer checks
whether the text actually addresses the aspect's criterion.  The check is a
deliberately small, configurable wordlist: when no hint covers the criterion,
or the evidence hits a required term, the model's judgment stands; only a
confident miss downgrades ``addressed``/``not_applicable`` to ``unclear`` so
the aspect stays in the residual follow-up instead of silently satisfying the
contract (降级不误伤、不 reject、不扩拒绝面).
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
# Multiple aliases cover the composer's real criteria (痰液颜色 / 痰量 / 咳嗽
# 是否有痰...), which vary from the canonical keys.
_SEMANTIC_HINTS: tuple[tuple[tuple[str, ...], _SemanticHint], ...] = (
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
