"""Deterministic L4-3 formula consistency verification.

This module owns its normalization policy and performs no model, retrieval,
Safety, persistence, repository, or graph operations.  A bare ``FormulaDraft``
may be structurally audited, but only the separate execution-boundary entry
can attest that it came from the real L4-2 success path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas.formula import (
    BASE_FORMULA_RAG_POLICY_VERSION,
    FORMULA_DRAFT_SCHEMA_VERSION,
    FORMULA_EVIDENCE_MODE,
    FORMULA_NO_RAG_CONFIDENCE_MAX,
    FORMULA_RAG_CONFIDENCE_MAX,
    FORMULA_RAG_EVIDENCE_MODE,
    FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX,
    FORMULA_RAG_POLICY_VERSION,
    MODIFICATION_DRAFT_RAG_POLICY_VERSION,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    FormulaModification,
    HerbItem,
    ModificationAction,
)

if TYPE_CHECKING:
    from app.agents.formula_draft import FormulaExecutionResult

FORMULA_CONSISTENCY_POLICY_VERSION: Literal["formula-consistency-policy.v1"] = "formula-consistency-policy.v1"
FORMULA_CONSISTENCY_REPORT_VERSION: Literal["formula-consistency-report.v1"] = "formula-consistency-report.v1"
FORMULA_HERB_NORMALIZER_VERSION: Literal["formula-herb-normalizer.v1"] = "formula-herb-normalizer.v1"
FORMULA_UNIT_REGISTRY_VERSION: Literal["formula-unit-registry.v1"] = "formula-unit-registry.v1"

_DOSE_QUANTUM = Decimal("0.000001")
_MAX_DOSE_G = Decimal("500")
_FORBIDDEN_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_PLACEHOLDER_TEXT = frozenset({"无", "暂无", "待补充", "待确认", "unknown", "n/a", "na", "none", "-"})
_SPACE_RE = re.compile(r"\s+")

# Code-owned immutable aliases.  Unknown valid names are preserved after
# Unicode/whitespace normalization; they are never guessed or remapped.
_HERB_ALIAS_ITEMS = (
    ("黃芪", "黄芪"),
    ("黄耆", "黄芪"),
    ("白朮", "白术"),
    ("蒼朮", "苍术"),
    ("澤瀉", "泽泻"),
    ("川貝母", "川贝母"),
    ("浙貝母", "浙贝母"),
    ("當歸", "当归"),
    ("陳皮", "陈皮"),
    ("黃連", "黄连"),
    ("黃芩", "黄芩"),
    ("黃柏", "黄柏"),
    ("麥冬", "麦冬"),
    ("車前子", "车前子"),
    ("鉤藤", "钩藤"),
    ("荊芥", "荆芥"),
    ("薄荷葉", "薄荷"),
)
HERB_ALIAS_REGISTRY: Mapping[str, str] = MappingProxyType(dict(_HERB_ALIAS_ITEMS))


class UnitConversionKind(StrEnum):
    CONVERTIBLE = "convertible"
    HERB_SPECIFIC = "herb_specific"
    UNSUPPORTED = "unsupported"


class _UnitRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical: str
    factor_to_grams: Decimal | None
    kind: UnitConversionKind


_UNIT_RULE_ITEMS = (
    ("g", _UnitRule(canonical="g", factor_to_grams=Decimal("1"), kind=UnitConversionKind.CONVERTIBLE)),
    ("克", _UnitRule(canonical="g", factor_to_grams=Decimal("1"), kind=UnitConversionKind.CONVERTIBLE)),
    ("公克", _UnitRule(canonical="g", factor_to_grams=Decimal("1"), kind=UnitConversionKind.CONVERTIBLE)),
    ("两", _UnitRule(canonical="g", factor_to_grams=Decimal("30"), kind=UnitConversionKind.CONVERTIBLE)),
    ("市两", _UnitRule(canonical="g", factor_to_grams=Decimal("30"), kind=UnitConversionKind.CONVERTIBLE)),
    ("钱", _UnitRule(canonical="g", factor_to_grams=Decimal("3"), kind=UnitConversionKind.CONVERTIBLE)),
    ("市钱", _UnitRule(canonical="g", factor_to_grams=Decimal("3"), kind=UnitConversionKind.CONVERTIBLE)),
    ("枚", _UnitRule(canonical="枚", factor_to_grams=None, kind=UnitConversionKind.HERB_SPECIFIC)),
    ("个", _UnitRule(canonical="枚", factor_to_grams=None, kind=UnitConversionKind.HERB_SPECIFIC)),
    ("適量", _UnitRule(canonical="适量", factor_to_grams=None, kind=UnitConversionKind.UNSUPPORTED)),
    ("适量", _UnitRule(canonical="适量", factor_to_grams=None, kind=UnitConversionKind.UNSUPPORTED)),
    ("少许", _UnitRule(canonical="适量", factor_to_grams=None, kind=UnitConversionKind.UNSUPPORTED)),
    ("少許", _UnitRule(canonical="适量", factor_to_grams=None, kind=UnitConversionKind.UNSUPPORTED)),
)
UNIT_REGISTRY: Mapping[str, _UnitRule] = MappingProxyType(dict(_UNIT_RULE_ITEMS))


class FormulaConsistencyFailureCode(StrEnum):
    SCHEMA_INVALID = "FORMULA_CONSISTENCY_SCHEMA_INVALID"
    SOURCE_NOT_TRUSTED = "FORMULA_CONSISTENCY_SOURCE_NOT_TRUSTED"
    NO_RAG_CONTRACT_VIOLATED = "FORMULA_CONSISTENCY_NO_RAG_CONTRACT_VIOLATED"
    CONFIDENCE_EXCEEDS_RAG_LIMIT = "FORMULA_CONSISTENCY_CONFIDENCE_EXCEEDS_RAG_LIMIT"
    EVIDENCE_LINK_FABRICATED = "FORMULA_CONSISTENCY_EVIDENCE_LINK_FABRICATED"
    EVIDENCE_MODE_POLICY_MISMATCH = "FORMULA_CONSISTENCY_EVIDENCE_MODE_POLICY_MISMATCH"
    HERB_NAME_INVALID = "FORMULA_CONSISTENCY_HERB_NAME_INVALID"
    DUPLICATE_HERB = "FORMULA_CONSISTENCY_DUPLICATE_HERB"
    DOSE_MISSING = "FORMULA_CONSISTENCY_DOSE_MISSING"
    DOSE_INVALID = "FORMULA_CONSISTENCY_DOSE_INVALID"
    DOSE_OUT_OF_RANGE = "FORMULA_CONSISTENCY_DOSE_OUT_OF_RANGE"
    UNIT_HERB_SPECIFIC = "FORMULA_CONSISTENCY_UNIT_HERB_SPECIFIC"
    UNIT_UNSUPPORTED = "FORMULA_CONSISTENCY_UNIT_UNSUPPORTED"
    UNIT_UNKNOWN = "FORMULA_CONSISTENCY_UNIT_UNKNOWN"
    MODIFICATION_REASON_INVALID = "FORMULA_CONSISTENCY_MODIFICATION_REASON_INVALID"
    MODIFICATION_BASIS_INVALID = "FORMULA_CONSISTENCY_MODIFICATION_BASIS_INVALID"
    MODIFICATION_TARGET_EXISTS = "FORMULA_CONSISTENCY_MODIFICATION_TARGET_EXISTS"
    MODIFICATION_TARGET_MISSING = "FORMULA_CONSISTENCY_MODIFICATION_TARGET_MISSING"
    MODIFICATION_DOSE_FORBIDDEN = "FORMULA_CONSISTENCY_MODIFICATION_DOSE_FORBIDDEN"
    REPLACE_UNSUPPORTED_V1 = "FORMULA_CONSISTENCY_REPLACE_UNSUPPORTED_V1"
    CANDIDATE_REBUILD_FAILED = "FORMULA_CONSISTENCY_CANDIDATE_REBUILD_FAILED"
    CANDIDATE_MISMATCH = "FORMULA_CONSISTENCY_CANDIDATE_MISMATCH"
    FACT_LINK_INVALID = "FORMULA_CONSISTENCY_FACT_LINK_INVALID"
    AUTHORITY_BOUNDARY_VIOLATED = "FORMULA_CONSISTENCY_AUTHORITY_BOUNDARY_VIOLATED"


class FormulaConsistencyCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class FormulaConsistencyCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: str = Field(min_length=1, max_length=64)
    status: FormulaConsistencyCheckStatus
    failure_code: FormulaConsistencyFailureCode | None = None

    @model_validator(mode="after")
    def status_matches_failure(self) -> FormulaConsistencyCheck:
        if (self.status is FormulaConsistencyCheckStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failed status must exactly match failure_code")
        return self


class CanonicalFactClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str
    fact_ids: tuple[UUID, ...]


class CanonicalHerbItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    herb: str
    dose: str
    unit: Literal["g"] = "g"
    note: str | None = None


class CanonicalFormula(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    composition: tuple[CanonicalHerbItem, ...]
    rationale: str
    basis: tuple[CanonicalFactClaim, ...]


class FormulaConsistencyReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: Literal["formula-consistency-report.v1"] = FORMULA_CONSISTENCY_REPORT_VERSION
    policy_version: Literal["formula-consistency-policy.v1"] = FORMULA_CONSISTENCY_POLICY_VERSION
    herb_normalizer_version: Literal["formula-herb-normalizer.v1"] = FORMULA_HERB_NORMALIZER_VERSION
    unit_registry_version: Literal["formula-unit-registry.v1"] = FORMULA_UNIT_REGISTRY_VERSION
    trusted_formula_source: bool
    checks: tuple[FormulaConsistencyCheck, ...] = Field(min_length=10)
    canonical_candidate: CanonicalFormula | None = None
    recomputed_candidate: CanonicalFormula | None = None
    passed: bool
    requires_human: bool
    failure_code: FormulaConsistencyFailureCode | None = None
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def outcome_is_derived(self) -> FormulaConsistencyReport:
        first = next((item.failure_code for item in self.checks if item.failure_code is not None), None)
        if self.passed != (first is None) or self.failure_code is not first:
            raise ValueError("report outcome must be derived from checks")
        # L4-3 never grants clinical/safety authority; doctor review remains mandatory.
        if self.requires_human is not True:
            raise ValueError("formula consistency never waives human review")
        return self


class _ConsistencyFailure(ValueError):
    def __init__(self, code: FormulaConsistencyFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def apply_modifications_to_base(
    base: FormulaComposition,
    modifications: tuple[FormulaModification, ...],
) -> FormulaComposition:
    """2.8 阶段 2 合成：把加减应用到权威基础方，产出候选方（CanonicalFormula 形态）。

    REMOVE / DOSE_ADJUST 的 target 以 ``base`` composition 为唯一真源检查 ——
    结构性杜绝 MODIFICATION_TARGET_MISSING 类自相矛盾输出。

    Raises:
        _ConsistencyFailure: 任一 modification 越界（target 不存在/剂量非法等）。
    """
    canonical_base = _canonicalize_draft(
        FormulaDraft(
            schema_version=FORMULA_DRAFT_SCHEMA_VERSION,
            decision=FormulaDraftDecision.COMPLETED,
            base_formula=base,
            modifications=(),
            candidate_formula=base,
            rationale="base",
            confidence=0.5,
        )
    ).base_formula
    if canonical_base is None:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.SCHEMA_INVALID)
    return _apply_modifications(canonical_base, modifications)


def verify_formula_consistency(
    draft: object,
    *,
    active_fact_ids: Iterable[UUID],
    trusted_syndrome_fact_ids: Iterable[UUID] = (),
) -> FormulaConsistencyReport:
    """Audit an untrusted draft without granting execution authority."""

    return _verify(
        draft,
        active_fact_ids=frozenset(active_fact_ids),
        trusted_syndrome_fact_ids=frozenset(trusted_syndrome_fact_ids),
        trusted_source=False,
        require_trusted_source=False,
    )


def verify_trusted_formula_execution(result: FormulaExecutionResult) -> FormulaConsistencyReport:
    """Verify only the exact result instance registered by real L4-2 success."""

    # Local import avoids coupling the pure verifier to the Agent/runtime path.
    from app.agents.formula_draft import _consume_trusted_formula_execution

    trusted = _consume_trusted_formula_execution(result)
    if trusted is None:
        candidate = getattr(result, "output", result)
        return _verify(
            candidate,
            active_fact_ids=frozenset(),
            trusted_syndrome_fact_ids=frozenset(),
            trusted_source=False,
            require_trusted_source=True,
        )
    active = frozenset(item.observation_id for item in trusted.input_payload.context_observations)
    syndrome = frozenset(
        fact_id
        for claim in (
            *trusted.input_payload.syndrome_draft.syndrome_basis,
            *trusted.input_payload.syndrome_draft.differential,
        )
        for fact_id in claim.fact_ids
    )
    return _verify(
        trusted.output,
        active_fact_ids=active,
        trusted_syndrome_fact_ids=syndrome,
        trusted_source=True,
        require_trusted_source=True,
        policy_version=trusted.run_spec.policy_version,
        evidence_ids=frozenset(evidence.evidence_id for evidence in trusted.retrieved_evidence),
    )


class FormulaConsistencyVerifier:
    """Stateless convenience facade for the two explicit trust modes."""

    @staticmethod
    def verify(
        draft: object,
        *,
        active_fact_ids: Iterable[UUID],
        trusted_syndrome_fact_ids: Iterable[UUID] = (),
    ) -> FormulaConsistencyReport:
        return verify_formula_consistency(
            draft,
            active_fact_ids=active_fact_ids,
            trusted_syndrome_fact_ids=trusted_syndrome_fact_ids,
        )

    @staticmethod
    def verify_execution(result: FormulaExecutionResult) -> FormulaConsistencyReport:
        return verify_trusted_formula_execution(result)


def _verify(
    draft: object,
    *,
    active_fact_ids: frozenset[UUID],
    trusted_syndrome_fact_ids: frozenset[UUID],
    trusted_source: bool,
    require_trusted_source: bool,
    policy_version: str | None = None,
    evidence_ids: frozenset[str] = frozenset(),
) -> FormulaConsistencyReport:
    checks: list[FormulaConsistencyCheck] = []
    canonical_base: CanonicalFormula | None = None
    canonical_candidate: CanonicalFormula | None = None
    recomputed: CanonicalFormula | None = None
    schema_code: FormulaConsistencyFailureCode | None = None
    try:
        output = _canonicalize_draft(draft)
    except (ValidationError, TypeError, ValueError, AttributeError):
        output = None
        schema_code = FormulaConsistencyFailureCode.SCHEMA_INVALID
    checks.append(_check("schema", schema_code))
    checks.append(
        _check(
            "trusted_formula_source",
            FormulaConsistencyFailureCode.SOURCE_NOT_TRUSTED if require_trusted_source and not trusted_source else None,
        )
    )

    no_rag_code = (
        _verify_evidence_contract(output, policy_version=policy_version, evidence_ids=evidence_ids)
        if output is not None
        else FormulaConsistencyFailureCode.SCHEMA_INVALID
    )
    checks.append(_check("no_rag_contract", no_rag_code))

    normalization_code: FormulaConsistencyFailureCode | None = None
    unit_code: FormulaConsistencyFailureCode | None = None
    if output is not None and output.decision is FormulaDraftDecision.COMPLETED:
        try:
            assert output.base_formula is not None and output.candidate_formula is not None
            canonical_base = _canonicalize_formula(output.base_formula)
            canonical_candidate = _canonicalize_formula(output.candidate_formula)
        except _ConsistencyFailure as exc:
            if exc.code in {
                FormulaConsistencyFailureCode.HERB_NAME_INVALID,
                FormulaConsistencyFailureCode.DUPLICATE_HERB,
            }:
                normalization_code = exc.code
            else:
                unit_code = exc.code
    elif output is not None:
        normalization_code = FormulaConsistencyFailureCode.CANDIDATE_REBUILD_FAILED
    else:
        normalization_code = FormulaConsistencyFailureCode.SCHEMA_INVALID
    checks.append(_check("herb_normalization", normalization_code))
    checks.append(_check("unit_conversion", unit_code))

    semantics_code: FormulaConsistencyFailureCode | None = None
    rebuild_code: FormulaConsistencyFailureCode | None = None
    if output is not None and canonical_base is not None and normalization_code is None and unit_code is None:
        try:
            recomputed = _apply_modifications(canonical_base, output.modifications)
        except _ConsistencyFailure as exc:
            semantics_code = exc.code
            rebuild_code = FormulaConsistencyFailureCode.CANDIDATE_REBUILD_FAILED
    else:
        rebuild_code = FormulaConsistencyFailureCode.CANDIDATE_REBUILD_FAILED
    checks.append(_check("modification_semantics", semantics_code))
    checks.append(_check("candidate_rebuild", rebuild_code))
    match_code = (
        None
        if (
            canonical_candidate is not None
            and recomputed is not None
            and canonical_candidate.composition == recomputed.composition
        )
        else FormulaConsistencyFailureCode.CANDIDATE_MISMATCH
    )
    checks.append(_check("candidate_match", match_code))

    allowed = active_fact_ids | trusted_syndrome_fact_ids
    fact_code = (
        _verify_fact_links(output, allowed) if output is not None else FormulaConsistencyFailureCode.FACT_LINK_INVALID
    )
    checks.append(_check("fact_links", fact_code))
    authority_code = _verify_authority(draft, output)
    checks.append(_check("authority_boundary", authority_code))

    subject = {
        "draft": _safe_subject(output, draft),
        "active_fact_ids": sorted(str(item) for item in active_fact_ids),
        "trusted_syndrome_fact_ids": sorted(str(item) for item in trusted_syndrome_fact_ids),
        "trusted_source": trusted_source,
        "require_trusted_source": require_trusted_source,
        "policy_version": FORMULA_CONSISTENCY_POLICY_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    first = next((item.failure_code for item in checks if item.failure_code is not None), None)
    return FormulaConsistencyReport(
        trusted_formula_source=trusted_source,
        checks=tuple(checks),
        canonical_candidate=canonical_candidate,
        recomputed_candidate=recomputed,
        passed=first is None,
        requires_human=True,
        failure_code=first,
        subject_digest=digest,
    )


def _canonicalize_draft(value: object) -> FormulaDraft:
    candidate = FormulaDraft.model_validate(value)
    raw = FormulaDraft.__pydantic_serializer__.to_json(candidate, warnings=False)
    canonical = FormulaDraft.model_validate_json(raw)
    if _has_hidden_or_extra_fields(value):
        raise ValueError("undeclared field")
    if canonical.schema_version != FORMULA_DRAFT_SCHEMA_VERSION:
        raise ValueError("schema version")
    return canonical


def _canonicalize_formula(formula: FormulaComposition) -> CanonicalFormula:
    name = _normalize_text(formula.name, allow_empty=False)
    rationale = _normalize_text(formula.rationale, allow_empty=False)
    basis = tuple(_canonical_claim(item) for item in formula.basis)
    composition = tuple(_canonicalize_item(item) for item in formula.composition)
    names = tuple(item.herb for item in composition)
    if len(names) != len(set(names)):
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.DUPLICATE_HERB)
    return CanonicalFormula(name=name, composition=composition, rationale=rationale, basis=basis)


def _canonicalize_item(item: HerbItem) -> CanonicalHerbItem:
    herb = _normalize_herb(item.herb)
    dose = _convert_dose(item.dose, item.unit)
    note = _normalize_text(item.note, allow_empty=True) if item.note is not None else None
    return CanonicalHerbItem(herb=herb, dose=_decimal_text(dose), note=note or None)


def _canonical_claim(claim: FormulaFactClaim) -> CanonicalFactClaim:
    return CanonicalFactClaim(
        claim=_normalize_text(claim.claim, allow_empty=False),
        fact_ids=tuple(claim.fact_ids),
    )


def _apply_modifications(
    base: CanonicalFormula,
    modifications: tuple[FormulaModification, ...],
) -> CanonicalFormula:
    current = list(base.composition)
    for modification in modifications:
        herb = _normalize_herb(modification.herb)
        indexes = [index for index, item in enumerate(current) if item.herb == herb]
        if not _valid_reason(modification.reason):
            raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_REASON_INVALID)
        if not modification.basis.fact_ids or not _valid_reason(modification.basis.claim):
            raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_BASIS_INVALID)
        if modification.action is ModificationAction.REPLACE:
            raise _ConsistencyFailure(FormulaConsistencyFailureCode.REPLACE_UNSUPPORTED_V1)
        if modification.action is ModificationAction.ADD:
            if indexes:
                raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_TARGET_EXISTS)
            dose = _convert_dose(modification.dose, modification.unit)
            current.append(CanonicalHerbItem(herb=herb, dose=_decimal_text(dose), note=None))
        elif modification.action is ModificationAction.REMOVE:
            if not indexes:
                raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_TARGET_MISSING)
            if modification.dose is not None:
                raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_DOSE_FORBIDDEN)
            current.pop(indexes[0])
        elif modification.action is ModificationAction.DOSE_ADJUST:
            if not indexes:
                raise _ConsistencyFailure(FormulaConsistencyFailureCode.MODIFICATION_TARGET_MISSING)
            dose = _convert_dose(modification.dose, modification.unit)
            old = current[indexes[0]]
            current[indexes[0]] = old.model_copy(update={"dose": _decimal_text(dose)})
        else:
            raise _ConsistencyFailure(FormulaConsistencyFailureCode.CANDIDATE_REBUILD_FAILED)
    return base.model_copy(update={"composition": tuple(current)})


def _normalize_herb(
    value: str,
    _registry: Mapping[str, str] = HERB_ALIAS_REGISTRY,
) -> str:
    normalized = _normalize_text(value, allow_empty=False)
    if len(normalized) > 64:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.HERB_NAME_INVALID)
    return _registry.get(normalized, normalized)


def _normalize_text(value: str | None, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.HERB_NAME_INVALID)
    # Security order is intentional: controls must never disappear through
    # whitespace folding, and normalization itself must not introduce them.
    if _has_forbidden_unicode(value):
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.HERB_NAME_INVALID)
    nfkc = unicodedata.normalize("NFKC", value)
    if _has_forbidden_unicode(nfkc):
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.HERB_NAME_INVALID)
    normalized = _SPACE_RE.sub(" ", nfkc).strip()
    if not allow_empty and not normalized:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.HERB_NAME_INVALID)
    return normalized


def _has_forbidden_unicode(value: str) -> bool:
    return any(unicodedata.category(char) in _FORBIDDEN_UNICODE_CATEGORIES for char in value)


def _convert_dose(
    value: float | None,
    unit: str,
    _registry: Mapping[str, _UnitRule] = UNIT_REGISTRY,
) -> Decimal:
    if value is None:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.DOSE_MISSING)
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidOperation
        dose = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.DOSE_INVALID) from None
    if not dose.is_finite():
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.DOSE_INVALID)
    normalized_unit = _normalize_text(unit, allow_empty=False)
    rule = _registry.get(normalized_unit)
    if rule is None:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.UNIT_UNKNOWN)
    if rule.kind is UnitConversionKind.HERB_SPECIFIC:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.UNIT_HERB_SPECIFIC)
    if rule.kind is UnitConversionKind.UNSUPPORTED:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.UNIT_UNSUPPORTED)
    assert rule.factor_to_grams is not None
    converted = (dose * rule.factor_to_grams).quantize(_DOSE_QUANTUM, rounding=ROUND_HALF_EVEN)
    if converted <= 0 or converted > _MAX_DOSE_G:
        raise _ConsistencyFailure(FormulaConsistencyFailureCode.DOSE_OUT_OF_RANGE)
    return converted


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _verify_evidence_contract(
    output: FormulaDraft | None,
    *,
    policy_version: str | None,
    evidence_ids: frozenset[str],
) -> FormulaConsistencyFailureCode | None:
    """按 policy_version 分派的证据契约校验（与 L4-2 verifier 同口径）。

    policy_version 为 None（未声明来源的 draft 审计）时按 no-rag 契约兜底。
    """
    if policy_version in {
        FORMULA_RAG_POLICY_VERSION,
        BASE_FORMULA_RAG_POLICY_VERSION,
        MODIFICATION_DRAFT_RAG_POLICY_VERSION,
    }:
        if output.evidence_mode != FORMULA_RAG_EVIDENCE_MODE:
            return FormulaConsistencyFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
        return _verify_rag_contract(output, evidence_ids)
    if output.evidence_mode != FORMULA_EVIDENCE_MODE:
        return FormulaConsistencyFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
    return _verify_no_rag(output, evidence_ids)


def _verify_rag_contract(
    output: FormulaDraft,
    evidence_ids: frozenset[str],
) -> FormulaConsistencyFailureCode | None:
    if any(link.evidence_id not in evidence_ids for link in output.claim_evidence_links):
        return FormulaConsistencyFailureCode.EVIDENCE_LINK_FABRICATED
    if not evidence_ids:
        if output.confidence > FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX:
            return FormulaConsistencyFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    elif output.confidence > FORMULA_RAG_CONFIDENCE_MAX:
        return FormulaConsistencyFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    if output.review_required is not True:
        return FormulaConsistencyFailureCode.NO_RAG_CONTRACT_VIOLATED
    return None


def _verify_no_rag(
    output: FormulaDraft,
    evidence_ids: frozenset[str],
) -> FormulaConsistencyFailureCode | None:
    if evidence_ids:
        # no-rag 模式下不得携带任何检索证据 ID（防证据泄漏冒充）。
        return FormulaConsistencyFailureCode.NO_RAG_CONTRACT_VIOLATED
    if (
        output.evidence_mode != FORMULA_EVIDENCE_MODE
        or output.claim_evidence_links
        or output.review_required is not True
        or not math.isfinite(output.confidence)
        or output.confidence > FORMULA_NO_RAG_CONFIDENCE_MAX
    ):
        return FormulaConsistencyFailureCode.NO_RAG_CONTRACT_VIOLATED
    return None


def _verify_fact_links(
    output: FormulaDraft,
    allowed: frozenset[UUID],
) -> FormulaConsistencyFailureCode | None:
    claims: list[FormulaFactClaim] = []
    if output.base_formula is not None:
        claims.extend(output.base_formula.basis)
    if output.candidate_formula is not None:
        claims.extend(output.candidate_formula.basis)
    claims.extend(item.basis for item in output.modifications)
    if not claims or any(not claim.fact_ids or any(item not in allowed for item in claim.fact_ids) for claim in claims):
        return FormulaConsistencyFailureCode.FACT_LINK_INVALID
    for modification in output.modifications:
        if not _valid_reason(modification.reason) or not _valid_reason(modification.basis.claim):
            return FormulaConsistencyFailureCode.MODIFICATION_BASIS_INVALID
    return None


def _valid_reason(value: str | None) -> bool:
    try:
        normalized = _normalize_text(value, allow_empty=False)
    except _ConsistencyFailure:
        return False
    return normalized.casefold() not in _PLACEHOLDER_TEXT


_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "route",
        "stage",
        "next_stage",
        "approved",
        "safety_decision",
        "doctor_decision",
        "citation",
        "citations",
        "source",
        "sources",
        "literature",
        "retrieval",
    }
)


def _verify_authority(raw: object, output: FormulaDraft | None) -> FormulaConsistencyFailureCode | None:
    if output is None or _contains_forbidden_key(raw):
        return FormulaConsistencyFailureCode.AUTHORITY_BOUNDARY_VIOLATED
    return None


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, BaseModel):
        if any(str(key).casefold() in _FORBIDDEN_AUTHORITY_KEYS for key in vars(value)):
            return True
        return any(_contains_forbidden_key(item) for item in vars(value).values())
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _FORBIDDEN_AUTHORITY_KEYS or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _has_hidden_or_extra_fields(value: object) -> bool:
    if not isinstance(value, BaseModel):
        return False
    declared = set(type(value).model_fields)
    if any(key not in declared for key in vars(value)):
        return True
    return any(
        _has_hidden_or_extra_fields(item) for item in vars(value).values() if isinstance(item, BaseModel)
    ) or any(
        _has_hidden_or_extra_fields(item)
        for item in vars(value).values()
        if isinstance(item, (tuple, list))
        for item in item
        if isinstance(item, BaseModel)
    )


def _safe_subject(output: FormulaDraft | None, raw: object) -> object:
    if output is not None:
        return output.model_dump(mode="json")
    return {"input_type": f"{type(raw).__module__}.{type(raw).__qualname__}"}


def _check(
    name: str,
    code: FormulaConsistencyFailureCode | None,
) -> FormulaConsistencyCheck:
    return FormulaConsistencyCheck(
        verifier=name,
        status=(FormulaConsistencyCheckStatus.PASSED if code is None else FormulaConsistencyCheckStatus.FAILED),
        failure_code=code,
    )


__all__ = [
    "FORMULA_CONSISTENCY_POLICY_VERSION",
    "FORMULA_CONSISTENCY_REPORT_VERSION",
    "FORMULA_HERB_NORMALIZER_VERSION",
    "FORMULA_UNIT_REGISTRY_VERSION",
    "FormulaConsistencyCheck",
    "FormulaConsistencyFailureCode",
    "FormulaConsistencyReport",
    "FormulaConsistencyVerifier",
    "verify_formula_consistency",
    "verify_trusted_formula_execution",
]
