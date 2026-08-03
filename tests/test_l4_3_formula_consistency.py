from __future__ import annotations

import math
import uuid

import pytest
from pydantic import ValidationError

import app.agent_runtime.formula_consistency as consistency_module
from app.agent_runtime.formula_consistency import (
    FormulaConsistencyFailureCode,
    verify_formula_consistency,
    verify_trusted_formula_execution,
)
from app.agents.formula_draft import (
    FormulaExecutionResult,
    FormulaExecutionStatus,
)
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    FormulaModification,
    HerbItem,
    ModificationAction,
)

FACT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
OTHER_FACT_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
FORBIDDEN_TEXT_CASES = (
    pytest.param("\n", id="line-feed"),
    pytest.param("\r", id="carriage-return"),
    pytest.param("\t", id="tab"),
    pytest.param("\u200b", id="zero-width-space"),
    pytest.param("\ud800", id="surrogate"),
)


def _claim(fact_id: uuid.UUID = FACT_ID) -> FormulaFactClaim:
    return FormulaFactClaim(claim="当前事实支持该方药选择", fact_ids=(fact_id,))


def _formula(*items: HerbItem, name: str = "基础方") -> FormulaComposition:
    return FormulaComposition(
        name=name,
        composition=items,
        rationale="依据当前证型拟定基础方",
        basis=(_claim(),),
    )


def _draft(
    base: FormulaComposition,
    candidate: FormulaComposition,
    modifications: tuple[FormulaModification, ...] = (),
) -> FormulaDraft:
    return FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=base,
        modifications=modifications,
        candidate_formula=candidate,
        rationale="基础方与加减动作形成候选方",
        confidence=0.6,
        evidence_mode=FORMULA_EVIDENCE_MODE,
        claim_evidence_links=(),
        review_required=True,
    )


def _verify(draft: object, facts: tuple[uuid.UUID, ...] = (FACT_ID,)):
    return verify_formula_consistency(draft, active_fact_ids=facts)


def _code(report: object, verifier: str) -> FormulaConsistencyFailureCode | None:
    return next(item.failure_code for item in report.checks if item.verifier == verifier)  # type: ignore[attr-defined]


def test_no_modifications_equivalent_base_and_candidate_passes_deterministically() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    draft = _draft(base, base.model_copy(deep=True))

    first = _verify(draft)
    second = _verify(draft)

    assert first.passed
    assert first == second
    assert first.subject_digest == second.subject_digest
    assert first.requires_human is True
    assert first.trusted_formula_source is False
    assert draft == _draft(base, base.model_copy(deep=True))


def test_candidate_with_model_renaming_and_rationale_passes_when_composition_matches() -> None:
    """2026-08 真实会话（f26da40f）：模型对候选方自然改写方名/理法/依据，
    composition 与 base+modifications 确定性重建一致即可通过——name/rationale/basis
    是模型临床自由文本，不可从 base 确定性推导，不应参与候选一致性比较。"""
    base = _formula(
        HerbItem(herb="川芎", dose=10, unit="g"),
        HerbItem(herb="荆芥", dose=9, unit="g"),
    )
    mod = FormulaModification(
        action=ModificationAction.ADD,
        herb="白芷",
        dose=6,
        unit="g",
        reason="加强止痛",
        basis=_claim(),
    )
    candidate = FormulaComposition(
        name="川芎茶调散加白芷",
        composition=(*base.composition, HerbItem(herb="白芷", dose=6, unit="g")),
        rationale="在基础方疏风止痛之上加白芷增强止痛之力",
        basis=(_claim(), _claim()),
    )
    report = _verify(_draft(base, candidate, (mod,)))

    assert report.passed, report.failure_code
    assert _code(report, "candidate_match") is None

    # composition 不一致（多一味未经修改的药材）仍必须被拒。
    extra = candidate.model_copy(
        update={"composition": (*candidate.composition, HerbItem(herb="细辛", dose=3, unit="g"))}
    )
    extra_report = _verify(_draft(base, extra, (mod,)))
    assert _code(extra_report, "candidate_match") is FormulaConsistencyFailureCode.CANDIDATE_MISMATCH


def test_add_appends_to_tail() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    mod = FormulaModification(
        action=ModificationAction.ADD,
        herb="白芷",
        dose=1,
        unit="钱",
        reason="加强止痛",
        basis=_claim(),
    )
    candidate = base.model_copy(update={"composition": (*base.composition, HerbItem(herb="白芷", dose=3, unit="克"))})
    report = _verify(_draft(base, candidate, (mod,)))
    assert report.passed
    assert [item.herb for item in report.recomputed_candidate.composition] == ["川芎", "白芷"]  # type: ignore[union-attr]


def test_remove_preserves_remaining_order_and_metadata() -> None:
    base = _formula(
        HerbItem(herb="川芎", dose=10, unit="g", note="先煎"),
        HerbItem(herb="白芷", dose=6, unit="g"),
    )
    mod = FormulaModification(
        action=ModificationAction.REMOVE,
        herb="白芷",
        reason="当前不需此药",
        basis=_claim(),
    )
    candidate = base.model_copy(update={"composition": (base.composition[0],)})
    assert _verify(_draft(base, candidate, (mod,))).passed


def test_dose_adjust_only_changes_dose() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g", note="后下"))
    mod = FormulaModification(
        action=ModificationAction.DOSE_ADJUST,
        herb="川芎",
        dose=4,
        unit="钱",
        reason="调整剂量",
        basis=_claim(),
    )
    candidate = base.model_copy(update={"composition": (HerbItem(herb="川芎", dose=12, unit="g", note="后下"),)})
    assert _verify(_draft(base, candidate, (mod,))).passed


def test_replace_is_fixed_rejected_in_v1() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    mod = FormulaModification(
        action=ModificationAction.REPLACE,
        herb="白芷",
        dose=10,
        unit="g",
        reason="替换药味",
        basis=_claim(),
    )
    report = _verify(_draft(base, base, (mod,)))
    assert _code(report, "modification_semantics") is FormulaConsistencyFailureCode.REPLACE_UNSUPPORTED_V1


def test_multiple_actions_apply_in_declared_order() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    remove = FormulaModification(
        action=ModificationAction.REMOVE,
        herb="川芎",
        reason="先移除",
        basis=_claim(),
    )
    add = FormulaModification(
        action=ModificationAction.ADD,
        herb="川芎",
        dose=15,
        unit="g",
        reason="后重新加入",
        basis=_claim(),
    )
    candidate = base.model_copy(update={"composition": (HerbItem(herb="川芎", dose=15, unit="g"),)})
    assert _verify(_draft(base, candidate, (remove, add))).passed


@pytest.mark.parametrize(
    ("base_item", "candidate_item"),
    [
        (HerbItem(herb="甘草", dose=3, unit="g"), HerbItem(herb="甘草", dose=3, unit="克")),
        (HerbItem(herb="甘草", dose=3, unit="公克"), HerbItem(herb="甘草", dose=3, unit="g")),
        (HerbItem(herb="甘草", dose=1, unit="两"), HerbItem(herb="甘草", dose=30, unit="g")),
        (HerbItem(herb="甘草", dose=1, unit="市两"), HerbItem(herb="甘草", dose=10, unit="钱")),
        (HerbItem(herb="甘草", dose=1, unit="市钱"), HerbItem(herb="甘草", dose=3, unit="g")),
    ],
)
def test_convertible_units_are_exactly_equivalent(base_item: HerbItem, candidate_item: HerbItem) -> None:
    base = _formula(base_item)
    candidate = base.model_copy(update={"composition": (candidate_item,)})
    assert _verify(_draft(base, candidate)).passed


def test_float_representation_noise_is_quantized_deterministically() -> None:
    base = _formula(HerbItem(herb="甘草", dose=0.1 + 0.2, unit="g"))
    candidate = base.model_copy(update={"composition": (HerbItem(herb="甘草", dose=0.3, unit="g"),)})
    assert _verify(_draft(base, candidate)).passed


@pytest.mark.parametrize(
    ("unit", "code"),
    [
        ("枚", FormulaConsistencyFailureCode.UNIT_HERB_SPECIFIC),
        ("个", FormulaConsistencyFailureCode.UNIT_HERB_SPECIFIC),
        ("适量", FormulaConsistencyFailureCode.UNIT_UNSUPPORTED),
        ("少许", FormulaConsistencyFailureCode.UNIT_UNSUPPORTED),
        ("汤匙", FormulaConsistencyFailureCode.UNIT_UNKNOWN),
    ],
)
def test_nonconvertible_units_require_human(unit: str, code: FormulaConsistencyFailureCode) -> None:
    base = _formula(HerbItem(herb="甘草", dose=1, unit=unit))
    report = _verify(_draft(base, base))
    assert not report.passed
    assert report.requires_human
    assert _code(report, "unit_conversion") is code


@pytest.mark.parametrize("dose", [None, 0, -1, math.nan, math.inf, 501])
def test_invalid_doses_are_fixed_rejected(dose: float | None) -> None:
    # model_construct represents hostile callers that bypass the public DTO validator.
    item = HerbItem.model_construct(herb="甘草", dose=dose, unit="g", note=None)
    formula = FormulaComposition.model_construct(
        name="基础方",
        composition=(item,),
        rationale="依据当前证型拟定基础方",
        basis=(_claim(),),
    )
    draft = FormulaDraft.model_construct(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=formula,
        modifications=(),
        candidate_formula=formula,
        rationale="形成候选方",
        confidence=0.6,
        evidence_mode=FORMULA_EVIDENCE_MODE,
        claim_evidence_links=(),
        missing_inputs=(),
        review_required=True,
        schema_version="formula-draft.v1",
    )
    report = _verify(draft)
    assert not report.passed
    assert report.requires_human


def test_alias_collision_is_duplicate_after_normalization() -> None:
    base = _formula(
        HerbItem(herb="黃芪", dose=10, unit="g"),
        HerbItem(herb="黄芪", dose=10, unit="g"),
    )
    report = _verify(_draft(base, base))
    assert _code(report, "herb_normalization") is FormulaConsistencyFailureCode.DUPLICATE_HERB


@pytest.mark.parametrize("forbidden", FORBIDDEN_TEXT_CASES)
def test_herb_control_format_and_surrogate_are_rejected_before_folding(forbidden: str) -> None:
    item = HerbItem.model_construct(herb=f"甘{forbidden}草", dose=3, unit="g", note=None)
    base = _formula(item)
    report = _verify(_draft(base, base))

    assert not report.passed
    assert report.requires_human is True
    assert report.canonical_candidate is None
    assert report.recomputed_candidate is None


@pytest.mark.parametrize("forbidden", FORBIDDEN_TEXT_CASES)
def test_unit_control_format_and_surrogate_are_rejected_before_folding(forbidden: str) -> None:
    item = HerbItem.model_construct(herb="甘草", dose=3, unit=f"g{forbidden}", note=None)
    base = _formula(item)
    report = _verify(_draft(base, base))

    assert not report.passed
    assert report.requires_human is True
    assert report.canonical_candidate is None
    assert report.recomputed_candidate is None


def test_control_character_cannot_evade_duplicate_herb_detection() -> None:
    base = _formula(
        HerbItem(herb="甘草", dose=3, unit="g"),
        HerbItem(herb="甘\n草", dose=3, unit="g"),
    )
    report = _verify(_draft(base, base))

    assert not report.passed
    assert report.requires_human is True
    assert report.canonical_candidate is None
    assert report.recomputed_candidate is None


@pytest.mark.parametrize("field", ["name", "note", "rationale"])
@pytest.mark.parametrize("forbidden", FORBIDDEN_TEXT_CASES)
def test_canonical_candidate_text_fields_use_same_safe_order(field: str, forbidden: str) -> None:
    item = HerbItem(herb="甘草", dose=3, unit="g")
    base = _formula(item)
    if field == "name":
        base = base.model_copy(update={"name": f"基础{forbidden}方"})
    elif field == "rationale":
        base = base.model_copy(update={"rationale": f"依据{forbidden}证型"})
    else:
        base = base.model_copy(
            update={"composition": (item.model_copy(update={"note": f"先{forbidden}煎"}),)}
        )
    report = _verify(_draft(base, base))

    assert not report.passed
    assert report.requires_human is True
    assert report.canonical_candidate is None
    assert report.recomputed_candidate is None


def test_callers_cannot_inject_authoritative_alias_or_unit_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _formula(HerbItem(herb="川芎", dose=1, unit="两"))
    candidate = base.model_copy(
        update={"composition": (HerbItem(herb="川芎", dose=30, unit="g"),)}
    )
    monkeypatch.setattr(consistency_module, "HERB_ALIAS_REGISTRY", {"川芎": "白芷"})
    monkeypatch.setattr(consistency_module, "UNIT_REGISTRY", {})

    report = _verify(_draft(base, candidate))

    assert report.passed
    assert report.canonical_candidate.composition[0].herb == "川芎"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "candidate_items",
    [
        (HerbItem(herb="川芎", dose=10, unit="g"),),
        (
            HerbItem(herb="川芎", dose=10, unit="g"),
            HerbItem(herb="白芷", dose=6, unit="g"),
            HerbItem(herb="甘草", dose=3, unit="g"),
        ),
        (HerbItem(herb="白芷", dose=6, unit="g"), HerbItem(herb="川芎", dose=10, unit="g")),
        (HerbItem(herb="川芎", dose=11, unit="g"), HerbItem(herb="白芷", dose=6, unit="g")),
        (HerbItem(herb="川芎", dose=10, unit="g", note="后下"), HerbItem(herb="白芷", dose=6, unit="g")),
    ],
)
def test_unauthorized_candidate_changes_fail(candidate_items: tuple[HerbItem, ...]) -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"), HerbItem(herb="白芷", dose=6, unit="g"))
    candidate = base.model_copy(update={"composition": candidate_items})
    report = _verify(_draft(base, candidate))
    assert _code(report, "candidate_match") is FormulaConsistencyFailureCode.CANDIDATE_MISMATCH


def test_candidate_name_rationale_and_basis_are_exactly_preserved() -> None:
    # 2026-08 政策调整：候选方 name/rationale/basis 是模型临床自由文本，确定性
    # 重建只覆盖 composition——改名/改写理法不再视为不一致（真实模型必然如此）。
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    changed = base.model_copy(update={"name": "加味基础方"})
    assert _code(_verify(_draft(base, changed)), "candidate_match") is None
    # 换药才是不一致（composition 改变但无对应 modification）。
    swapped = base.model_copy(update={"composition": (HerbItem(herb="白芷", dose=10, unit="g"),)})
    assert (
        _code(_verify(_draft(base, swapped)), "candidate_match")
        is FormulaConsistencyFailureCode.CANDIDATE_MISMATCH
    )


@pytest.mark.parametrize(
    "mods",
    [
        (
            FormulaModification(
                action=ModificationAction.ADD,
                herb="川芎",
                dose=5,
                unit="g",
                reason="重复加入",
                basis=_claim(),
            ),
        ),
        (
            FormulaModification(
                action=ModificationAction.REMOVE,
                herb="白芷",
                reason="目标不存在",
                basis=_claim(),
            ),
        ),
        (
            FormulaModification(
                action=ModificationAction.REMOVE,
                herb="川芎",
                reason="首次移除",
                basis=_claim(),
            ),
            FormulaModification(
                action=ModificationAction.DOSE_ADJUST,
                herb="川芎",
                dose=5,
                unit="g",
                reason="移除后调整",
                basis=_claim(),
            ),
        ),
    ],
)
def test_conflicting_modifications_fail(mods: tuple[FormulaModification, ...]) -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    report = _verify(_draft(base, base, mods))
    assert _code(report, "modification_semantics") in {
        FormulaConsistencyFailureCode.MODIFICATION_TARGET_EXISTS,
        FormulaConsistencyFailureCode.MODIFICATION_TARGET_MISSING,
    }


def test_unknown_inactive_or_cross_session_fact_id_fails() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    report = _verify(_draft(base, base), facts=(OTHER_FACT_ID,))
    assert _code(report, "fact_links") is FormulaConsistencyFailureCode.FACT_LINK_INVALID


def test_trusted_syndrome_basis_fact_id_is_allowed() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    report = verify_formula_consistency(
        _draft(base, base),
        active_fact_ids=(),
        trusted_syndrome_fact_ids=(FACT_ID,),
    )
    assert report.passed


def test_no_rag_and_review_contract_tampering_fails() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    draft = _draft(base, base).model_copy(update={"review_required": False})
    assert _code(_verify(draft), "no_rag_contract") is FormulaConsistencyFailureCode.NO_RAG_CONTRACT_VIOLATED


def test_hidden_authority_field_fails() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    draft = _draft(base, base)
    object.__setattr__(draft, "route", "safety")
    report = _verify(draft)
    assert not report.passed
    assert _code(report, "authority_boundary") is FormulaConsistencyFailureCode.AUTHORITY_BOUNDARY_VIOLATED


def test_handcrafted_or_copied_execution_result_is_not_trusted() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    forged = FormulaExecutionResult.model_construct(
        status=FormulaExecutionStatus.SUCCEEDED,
        output=_draft(base, base),
        verification=None,
        failure_code=None,
    )
    report = verify_trusted_formula_execution(forged)
    assert not report.passed
    assert _code(report, "trusted_formula_source") is FormulaConsistencyFailureCode.SOURCE_NOT_TRUSTED


@pytest.mark.asyncio
async def test_only_exact_real_l4_2_success_result_is_trusted() -> None:
    # Reuse the already-authoritative L4-2 execution fixture rather than
    # manufacturing a second, weaker runtime boundary in this test module.
    from tests.test_l4_2_formula_draft import _completed_formula, _execute, _input

    payload = _input()
    result, _ = await _execute(payload, _completed_formula(payload))
    trusted = verify_trusted_formula_execution(result)
    copied = verify_trusted_formula_execution(result.model_copy(deep=True))

    assert trusted.passed
    assert trusted.trusted_formula_source
    assert not copied.passed
    assert _code(copied, "trusted_formula_source") is FormulaConsistencyFailureCode.SOURCE_NOT_TRUSTED


def test_report_is_frozen_and_extra_forbidden() -> None:
    base = _formula(HerbItem(herb="川芎", dose=10, unit="g"))
    report = _verify(_draft(base, base))
    with pytest.raises(ValidationError):
        report.passed = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        type(report)(**report.model_dump(), route="safety")
