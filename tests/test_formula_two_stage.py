"""2.8 两阶段开方（B 方案）测试：装配、合成、结构性拦截。"""

from __future__ import annotations

import uuid

import pytest

from app.agent_runtime.formula_consistency import apply_modifications_to_base
from app.agents.formula_draft import (
    assemble_base_formula_output,
    assemble_modification_output,
)
from app.schemas.formula import (
    BaseFormulaDraft,
    FormulaComposition,
    FormulaDraftDecision,
    FormulaFactClaim,
    FormulaModification,
    HerbItem,
    ModificationAction,
    ModificationDraft,
)


def _basis() -> FormulaFactClaim:
    return FormulaFactClaim(claim="证候依据", fact_ids=(uuid.uuid4(),))


def _base() -> FormulaComposition:
    basis = _basis()
    return FormulaComposition(
        name="麻黄汤",
        composition=(
            HerbItem(herb="麻黄", dose=9),
            HerbItem(herb="桂枝", dose=6),
            HerbItem(herb="杏仁", dose=9),
            HerbItem(herb="甘草", dose=3),
        ),
        rationale="解表散寒",
        basis=(basis,),
    )


def test_base_formula_draft_assembly() -> None:
    draft = assemble_base_formula_output(
        BaseFormulaDraft(
            decision=FormulaDraftDecision.COMPLETED,
            base_formula=_base(),
            rationale="解表散寒",
            confidence=0.8,
        )
    )
    assert draft.candidate_formula == draft.base_formula
    assert draft.modifications == ()
    assert draft.decision is FormulaDraftDecision.COMPLETED


def test_modification_draft_assembly_applies_changes() -> None:
    basis = _basis()
    base = _base()
    out = assemble_modification_output(
        ModificationDraft(
            decision=FormulaDraftDecision.COMPLETED,
            modifications=(
                FormulaModification(action=ModificationAction.REMOVE, herb="桂枝", reason="去桂", basis=basis),
                FormulaModification(action=ModificationAction.ADD, herb="生姜", dose=9, reason="加姜", basis=basis),
            ),
            confidence=0.8,
        ),
        base,
    )
    herbs = [item.herb for item in out.candidate_formula.composition]
    assert herbs == ["麻黄", "杏仁", "甘草", "生姜"]
    assert out.base_formula == base  # 权威基础方保持不变


def test_modification_remove_missing_herb_is_structurally_rejected() -> None:
    """结构性杜绝 MODIFICATION_TARGET_MISSING：REMOVE 不存在的药在合成时即失败。"""
    basis = _basis()
    with pytest.raises(Exception) as exc_info:
        assemble_modification_output(
            ModificationDraft(
                decision=FormulaDraftDecision.COMPLETED,
                modifications=(
                    FormulaModification(action=ModificationAction.REMOVE, herb="附子", reason="不存在", basis=basis),
                ),
                confidence=0.8,
            ),
            _base(),
        )
    assert "MODIFICATION_TARGET_MISSING" in str(exc_info.value)


def test_modification_dose_adjust_missing_herb_is_rejected() -> None:
    basis = _basis()
    with pytest.raises(Exception) as exc_info:
        apply_modifications_to_base(
            _base(),
            (
                FormulaModification(action=ModificationAction.DOSE_ADJUST, herb="附子", dose=10, reason="不存在", basis=basis),
            ),
        )
    assert "MODIFICATION_TARGET_MISSING" in str(exc_info.value)


def test_empty_modifications_keep_base_unchanged() -> None:
    out = assemble_modification_output(
        ModificationDraft(decision=FormulaDraftDecision.COMPLETED, modifications=(), confidence=0.8),
        _base(),
    )
    assert [item.herb for item in out.candidate_formula.composition] == ["麻黄", "桂枝", "杏仁", "甘草"]


# ---------------------------------------------------------------------------
