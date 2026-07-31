"""Pure deterministic L3-4 gap selection from authoritative Completeness results."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.schemas.completeness import (
    COMPLETENESS_GATE_NAME,
    COMPLETENESS_POLICY_VERSION,
    COMPLETENESS_RESULT_SCHEMA_VERSION,
    CompletenessDisposition,
    CompletenessPolicyResult,
    InquiryDimension,
)
from app.schemas.domain import GateDecision
from app.schemas.question import (
    GapPriorityRule,
    GapSelectionDisposition,
    GapSelectionKind,
    GapSelectionResult,
)


class GapSelectionFailureCode(StrEnum):
    INPUT_SCHEMA_INVALID = "GAP_SELECTION_INPUT_SCHEMA_INVALID"
    AUTHORITY_FIELD_FORBIDDEN = "GAP_SELECTION_AUTHORITY_FIELD_FORBIDDEN"
    COMPLETENESS_RESULT_MISMATCH = "GAP_SELECTION_COMPLETENESS_RESULT_MISMATCH"
    UNREGISTERED_DIMENSION = "GAP_SELECTION_UNREGISTERED_DIMENSION"


class GapSelectionInputError(ValueError):
    """Fixed-code rejection from gap selection input canonical reconstruction."""

    def __init__(self, code: GapSelectionFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class FrozenGapPriorityRegistry(Mapping[InquiryDimension, GapPriorityRule]):
    """Immutable priority registry backed only by a tuple of frozen rules."""

    __slots__ = ("_rules",)
    _rules: tuple[GapPriorityRule, ...]

    def __init__(self, rules: tuple[GapPriorityRule, ...]) -> None:
        dimensions = tuple(rule.dimension for rule in rules)
        if len(dimensions) != len(frozenset(dimensions)):
            raise ValueError("gap priority dimensions must be unique")
        object.__setattr__(self, "_rules", rules)

    def __getitem__(self, dimension: InquiryDimension) -> GapPriorityRule:
        for rule in self._rules:
            if rule.dimension is dimension:
                return rule
        raise KeyError(dimension)

    def __iter__(self) -> Iterator[InquiryDimension]:
        return (rule.dimension for rule in self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("gap priority registry is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("gap priority registry is immutable")


_GAP_PRIORITY_RULES_AUTHORITY: Mapping[InquiryDimension, GapPriorityRule] = FrozenGapPriorityRegistry(
    (
        GapPriorityRule(
            rule_id="gap.priority.chief_complaint.category.v1",
            dimension=InquiryDimension.CHIEF_COMPLAINT_CATEGORY,
            conflict_priority=80,
        ),
        GapPriorityRule(
            rule_id="gap.priority.safety.allergy_status.v1",
            dimension=InquiryDimension.ALLERGY_STATUS,
            required_priority=500,
            conflict_priority=90,
        ),
        GapPriorityRule(
            rule_id="gap.priority.safety.pregnancy_status.v1",
            dimension=InquiryDimension.PREGNANCY_STATUS,
            required_priority=530,
            conflict_priority=91,
        ),
        GapPriorityRule(
            rule_id="gap.priority.safety.lactation_status.v1",
            dimension=InquiryDimension.LACTATION_STATUS,
            required_priority=540,
            conflict_priority=92,
        ),
        GapPriorityRule(
            rule_id="gap.priority.safety.medication_status.v1",
            dimension=InquiryDimension.MEDICATION_STATUS,
            required_priority=510,
            conflict_priority=93,
        ),
        GapPriorityRule(
            rule_id="gap.priority.safety.major_condition_status.v1",
            dimension=InquiryDimension.MAJOR_CONDITION_STATUS,
            required_priority=520,
            conflict_priority=94,
        ),
        GapPriorityRule(
            rule_id="gap.priority.chief_complaint.symptom.v1",
            dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
            required_priority=100,
            conflict_priority=100,
        ),
        GapPriorityRule(
            rule_id="gap.priority.chief_complaint.course.v1",
            dimension=InquiryDimension.BASIC_COURSE,
            required_priority=110,
            conflict_priority=110,
        ),
        GapPriorityRule(
            rule_id="gap.priority.present_illness.change.v1",
            dimension=InquiryDimension.PRESENT_ILLNESS_CHANGE,
            required_priority=120,
            conflict_priority=120,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.cold_heat.v1",
            dimension=InquiryDimension.TEN_COLD_HEAT,
            required_priority=200,
            conflict_priority=200,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.stool_urine.v1",
            dimension=InquiryDimension.TEN_STOOL_URINE,
            required_priority=210,
            conflict_priority=210,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.diet.v1",
            dimension=InquiryDimension.TEN_DIET,
            required_priority=220,
            conflict_priority=220,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.sleep.v1",
            dimension=InquiryDimension.TEN_SLEEP,
            required_priority=230,
            conflict_priority=230,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.respiratory.v1",
            dimension=InquiryDimension.TEN_RESPIRATORY,
            required_priority=240,
            conflict_priority=240,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.pain.v1",
            dimension=InquiryDimension.TEN_PAIN,
            required_priority=250,
            conflict_priority=250,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.menses_leukorrhea.v1",
            dimension=InquiryDimension.TEN_MENSES_LEUKORRHEA,
            required_priority=260,
            conflict_priority=260,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.sweat.v1",
            dimension=InquiryDimension.TEN_SWEAT,
            required_priority=270,
            conflict_priority=270,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.head_body.v1",
            dimension=InquiryDimension.TEN_HEAD_BODY,
            required_priority=280,
            conflict_priority=280,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.chest_abdomen.v1",
            dimension=InquiryDimension.TEN_CHEST_ABDOMEN,
            required_priority=290,
            conflict_priority=290,
        ),
        GapPriorityRule(
            rule_id="gap.priority.ten_questions.thirst.v1",
            dimension=InquiryDimension.TEN_THIRST,
            required_priority=300,
            conflict_priority=300,
        ),
        GapPriorityRule(
            rule_id="gap.priority.past_history.v1",
            dimension=InquiryDimension.PAST_HISTORY,
            conflict_priority=700,
        ),
        GapPriorityRule(
            rule_id="gap.priority.four_diagnosis.v1",
            dimension=InquiryDimension.FOUR_DIAGNOSIS,
            conflict_priority=710,
        ),
        GapPriorityRule(
            rule_id="gap.priority.patient.sex.v1",
            dimension=InquiryDimension.PATIENT_SEX,
            conflict_priority=800,
        ),
        GapPriorityRule(
            rule_id="gap.priority.patient.age.v1",
            dimension=InquiryDimension.PATIENT_AGE,
            conflict_priority=810,
        ),
        GapPriorityRule(
            rule_id="gap.priority.patient.menopause_status.v1",
            dimension=InquiryDimension.MENOPAUSE_STATUS,
            conflict_priority=820,
        ),
        GapPriorityRule(
            rule_id="gap.priority.patient.pregnancy_applicability.v1",
            dimension=InquiryDimension.PREGNANCY_APPLICABILITY_FLAG,
            conflict_priority=830,
        ),
        GapPriorityRule(
            rule_id="gap.priority.patient.lactation_applicability.v1",
            dimension=InquiryDimension.LACTATION_APPLICABILITY_FLAG,
            conflict_priority=840,
        ),
    )
)
GAP_PRIORITY_RULES = _GAP_PRIORITY_RULES_AUTHORITY

_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {
        "next_gap",
        "selected_gap",
        "selected_dimension",
        "route",
        "stage",
        "ready",
        "force",
        "manual_override",
    }
)


def canonicalize_gap_selection_input(input_payload: object) -> CompletenessPolicyResult:
    """Rebuild Completeness authority and reject hidden selection/route fields."""

    try:
        candidate = CompletenessPolicyResult.model_validate(input_payload)
        canonical_json = CompletenessPolicyResult.__pydantic_serializer__.to_json(candidate, warnings=False)
        canonical = CompletenessPolicyResult.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        if _looks_like_completeness_authority_mismatch(input_payload):
            raise GapSelectionInputError(GapSelectionFailureCode.COMPLETENESS_RESULT_MISMATCH) from exc
        raise GapSelectionInputError(GapSelectionFailureCode.INPUT_SCHEMA_INVALID) from exc
    if _has_undeclared_fields(input_payload, canonical) or _has_forbidden_authority_field(input_payload):
        raise GapSelectionInputError(GapSelectionFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    if not _completeness_result_is_internally_consistent(canonical):
        raise GapSelectionInputError(GapSelectionFailureCode.COMPLETENESS_RESULT_MISMATCH)
    return canonical


def select_gap(
    completeness_result: object,
    *,
    pending_safety_dimensions: tuple[InquiryDimension, ...] = (),
) -> GapSelectionResult:
    """Select at most one deterministic required/conflict gap."""

    return _select_gap_with_priority_registry(
        completeness_result,
        _GAP_PRIORITY_RULES_AUTHORITY,
        pending_safety_dimensions=pending_safety_dimensions,
    )


def _select_gap_with_priority_registry(
    completeness_result: object,
    priority_registry: Mapping[InquiryDimension, GapPriorityRule],
    *,
    pending_safety_dimensions: tuple[InquiryDimension, ...] = (),
) -> GapSelectionResult:
    """Private test seam. Production callers cannot choose priority rules."""

    completeness = canonicalize_gap_selection_input(completeness_result)
    deferred = _canonical_pending_safety_dimensions(pending_safety_dimensions)
    if completeness.disposition is CompletenessDisposition.INCOMPLETE:
        dimensions = tuple(
            item for item in completeness.missing_required if item not in deferred
        )
        if not dimensions:
            return GapSelectionResult(
                input_state_version=completeness.input_state_version,
                disposition=GapSelectionDisposition.NO_SELECTION,
                selection_kind=GapSelectionKind.NONE,
                source_completeness_disposition=completeness.disposition.value,
                deferred_dimensions=deferred,
            )
        return _select_from_dimensions(
            completeness=completion_result_to_selection_source(completeness),
            dimensions=dimensions,
            kind=GapSelectionKind.REQUIRED,
            priority_registry=priority_registry,
            deferred_dimensions=deferred,
        )
    if completeness.disposition is CompletenessDisposition.CONFLICT:
        return _select_from_dimensions(
            completeness=completion_result_to_selection_source(completeness),
            dimensions=tuple(item.dimension for item in completeness.conflicting_dimensions),
            kind=GapSelectionKind.CONFLICT,
            priority_registry=priority_registry,
            deferred_dimensions=deferred,
        )
    return GapSelectionResult(
        input_state_version=completeness.input_state_version,
        disposition=GapSelectionDisposition.NO_SELECTION,
        selection_kind=GapSelectionKind.NONE,
        source_completeness_disposition=completeness.disposition.value,
        deferred_dimensions=deferred,
    )


def completion_result_to_selection_source(completeness: CompletenessPolicyResult) -> CompletenessPolicyResult:
    """Typed identity helper keeps selection construction explicit."""

    return completeness


def _select_from_dimensions(
    *,
    completeness: CompletenessPolicyResult,
    dimensions: tuple[InquiryDimension, ...],
    kind: GapSelectionKind,
    priority_registry: Mapping[InquiryDimension, GapPriorityRule],
    deferred_dimensions: tuple[InquiryDimension, ...] = (),
) -> GapSelectionResult:
    unique_dimensions = tuple(sorted(frozenset(dimensions), key=lambda item: item.value))
    if not unique_dimensions:
        raise GapSelectionInputError(GapSelectionFailureCode.COMPLETENESS_RESULT_MISMATCH)
    ranked: list[tuple[int, InquiryDimension, GapPriorityRule]] = []
    for dimension in unique_dimensions:
        try:
            rule = priority_registry[dimension]
        except KeyError as exc:
            raise GapSelectionInputError(GapSelectionFailureCode.UNREGISTERED_DIMENSION) from exc
        priority = rule.required_priority if kind is GapSelectionKind.REQUIRED else rule.conflict_priority
        if priority is None:
            raise GapSelectionInputError(GapSelectionFailureCode.UNREGISTERED_DIMENSION)
        ranked.append((priority, dimension, rule))
    priority, selected_dimension, selected_rule = min(ranked, key=lambda item: (item[0], item[1].value))
    del priority
    return GapSelectionResult(
        input_state_version=completeness.input_state_version,
        disposition=GapSelectionDisposition.SELECTED,
        selected_dimension=selected_dimension,
        selection_kind=kind,
        priority_rule_id=selected_rule.rule_id,
        source_completeness_disposition=completeness.disposition.value,
        deferred_dimensions=deferred_dimensions,
    )


def _canonical_pending_safety_dimensions(
    dimensions: tuple[InquiryDimension, ...],
) -> tuple[InquiryDimension, ...]:
    safety_dimensions = {
        InquiryDimension.ALLERGY_STATUS,
        InquiryDimension.MEDICATION_STATUS,
        InquiryDimension.MAJOR_CONDITION_STATUS,
        InquiryDimension.PREGNANCY_STATUS,
        InquiryDimension.LACTATION_STATUS,
    }
    try:
        canonical = tuple(
            sorted(
                {InquiryDimension(item) for item in dimensions},
                key=lambda item: item.value,
            )
        )
    except (TypeError, ValueError) as exc:
        raise GapSelectionInputError(GapSelectionFailureCode.INPUT_SCHEMA_INVALID) from exc
    if any(item not in safety_dimensions for item in canonical):
        raise GapSelectionInputError(GapSelectionFailureCode.INPUT_SCHEMA_INVALID)
    return canonical


def _completeness_result_is_internally_consistent(result: CompletenessPolicyResult) -> bool:
    if (
        result.schema_version != COMPLETENESS_RESULT_SCHEMA_VERSION
        or result.policy_version != COMPLETENESS_POLICY_VERSION
        or result.gate_result.gate_name != COMPLETENESS_GATE_NAME
        or result.gate_result.policy_version != result.policy_version
        or result.gate_result.input_state_version != result.input_state_version
    ):
        return False
    details = result.gate_result.details
    if (
        details.disposition is not result.disposition
        or details.covered_dimensions != result.covered_dimensions
        or details.missing_required != result.missing_required
        or details.missing_optional != result.missing_optional
        or details.conflicting_dimensions != result.conflicting_dimensions
        or details.stagnation != result.stagnation
        or details.rule_outcomes != result.rule_outcomes
    ):
        return False
    decision_by_disposition = (
        (CompletenessDisposition.READY, GateDecision.PASSED),
        (CompletenessDisposition.INCOMPLETE, GateDecision.FAILED),
        (CompletenessDisposition.CONFLICT, GateDecision.FAILED),
        (CompletenessDisposition.STAGNATED, GateDecision.BLOCKED),
        (CompletenessDisposition.TRIAGE_BLOCKED, GateDecision.BLOCKED),
        # 2d(决策 11): PARTIAL 与 READY 同权(落库推进不阻断)。
        (CompletenessDisposition.PARTIAL, GateDecision.PASSED),
    )
    return any(
        result.disposition is disposition and result.gate_result.decision is decision
        for disposition, decision in decision_by_disposition
    )


def _looks_like_completeness_authority_mismatch(raw: Any) -> bool:
    if not isinstance(raw, BaseModel):
        return False
    if (
        getattr(raw, "schema_version", None) != COMPLETENESS_RESULT_SCHEMA_VERSION
        or getattr(raw, "policy_version", None) != COMPLETENESS_POLICY_VERSION
    ):
        return False
    gate = getattr(raw, "gate_result", None)
    if gate is None:
        return False
    return (
        getattr(gate, "gate_name", None) == COMPLETENESS_GATE_NAME
        and getattr(gate, "policy_version", None) == COMPLETENESS_POLICY_VERSION
    )


def _has_forbidden_authority_field(raw: Any) -> bool:
    if isinstance(raw, BaseModel):
        keys = set(raw.__dict__)
        extra = getattr(raw, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            keys.update(extra)
        if keys & _FORBIDDEN_AUTHORITY_FIELDS:
            return True
        return any(_has_forbidden_authority_field(value) for value in raw.__dict__.values())
    if isinstance(raw, dict):
        if set(raw) & _FORBIDDEN_AUTHORITY_FIELDS:
            return True
        return any(_has_forbidden_authority_field(value) for value in raw.values())
    if isinstance(raw, (list, tuple)):
        return any(_has_forbidden_authority_field(value) for value in raw)
    return False


def _has_undeclared_fields(raw: Any, canonical: Any) -> bool:
    if isinstance(canonical, BaseModel):
        allowed = set(type(canonical).model_fields)
        if isinstance(raw, BaseModel):
            raw_keys = set(raw.__dict__)
            extra = getattr(raw, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                raw_keys.update(extra)
            if raw_keys - allowed:
                return True
            return any(
                _has_undeclared_fields(getattr(raw, name, None), getattr(canonical, name))
                for name in allowed
            )
        if isinstance(raw, dict):
            if set(raw) - allowed:
                return True
            return any(
                _has_undeclared_fields(raw.get(name), getattr(canonical, name))
                for name in allowed
            )
        return True
    if isinstance(canonical, (list, tuple)):
        if not isinstance(raw, (list, tuple)) or len(raw) != len(canonical):
            return True
        return any(_has_undeclared_fields(raw_item, item) for raw_item, item in zip(raw, canonical, strict=True))
    if isinstance(canonical, dict):
        return not isinstance(raw, dict)
    return isinstance(raw, (BaseModel, dict, list, tuple))
