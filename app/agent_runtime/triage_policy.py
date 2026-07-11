"""Pure deterministic L3-2 triage policy.

The policy consumes only verified L3-1 red-flag candidates and produces an
authoritative GateResult.  It never calls a model, graph, database, repository,
or external service.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.domain import GateDecision, GateResultSchema
from app.schemas.intake import RedFlagCategory
from app.schemas.triage import (
    TRIAGE_GATE_NAME,
    TRIAGE_POLICY_VERSION,
    TriageCategoryCount,
    TriageDisposition,
    TriageGateDetails,
    TriageGateResult,
    TriagePolicyInput,
    TriagePolicyResult,
    TriageRuleOutcome,
)


class TriagePolicyFailureCode(StrEnum):
    INPUT_SCHEMA_INVALID = "TRIAGE_INPUT_SCHEMA_INVALID"
    INPUT_AUTHORITY_FIELD_FORBIDDEN = "TRIAGE_INPUT_AUTHORITY_FIELD_FORBIDDEN"


class TriagePolicyInputError(ValueError):
    """Fixed-code rejection from triage input canonical reconstruction."""

    def __init__(self, code: TriagePolicyFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class TriageRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=64)
    category: RedFlagCategory
    disposition: TriageDisposition


class FrozenTriageRuleRegistry(Mapping[RedFlagCategory, TriageRule]):
    """Immutable rule registry backed only by a tuple of frozen rules."""

    __slots__ = ("_rules",)
    _rules: tuple[TriageRule, ...]

    def __init__(self, rules: tuple[TriageRule, ...]) -> None:
        categories = tuple(rule.category for rule in rules)
        if len(categories) != len(frozenset(categories)):
            raise ValueError("triage red-flag rule categories must be unique")
        object.__setattr__(self, "_rules", rules)

    def __getitem__(self, category: RedFlagCategory) -> TriageRule:
        for rule in self._rules:
            if rule.category is category:
                return rule
        raise KeyError(category)

    def __iter__(self) -> Iterator[RedFlagCategory]:
        return (rule.category for rule in self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("triage rule registry is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("triage rule registry is immutable")


TRIAGE_RED_FLAG_RULES: Mapping[RedFlagCategory, TriageRule] = FrozenTriageRuleRegistry(
    (
        TriageRule(
            rule_id="red_flag.severe_pain.emergency_referral.v1",
            category=RedFlagCategory.SEVERE_PAIN,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.breathing_difficulty.emergency_referral.v1",
            category=RedFlagCategory.BREATHING_DIFFICULTY,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.altered_consciousness.emergency_referral.v1",
            category=RedFlagCategory.ALTERED_CONSCIOUSNESS,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.severe_bleeding.emergency_referral.v1",
            category=RedFlagCategory.SEVERE_BLEEDING,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.neurologic_deficit.emergency_referral.v1",
            category=RedFlagCategory.NEUROLOGIC_DEFICIT,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.high_fever.emergency_referral.v1",
            category=RedFlagCategory.HIGH_FEVER,
            disposition=TriageDisposition.EMERGENCY_REFERRAL,
        ),
        TriageRule(
            rule_id="red_flag.other.manual_review.v1",
            category=RedFlagCategory.OTHER,
            disposition=TriageDisposition.MANUAL_REVIEW,
        ),
    )
)

if frozenset(TRIAGE_RED_FLAG_RULES) != frozenset(RedFlagCategory):
    raise RuntimeError("triage red-flag rule table must cover every RedFlagCategory")


def canonicalize_triage_input(input_payload: object) -> TriagePolicyInput:
    """Rebuild the exact base DTO and reject hidden or constructed fields."""

    try:
        candidate = TriagePolicyInput.model_validate(input_payload)
        canonical_json = TriagePolicyInput.__pydantic_serializer__.to_json(candidate, warnings=False)
        canonical = TriagePolicyInput.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        raise TriagePolicyInputError(TriagePolicyFailureCode.INPUT_SCHEMA_INVALID) from exc
    if _has_undeclared_fields(input_payload, canonical):
        raise TriagePolicyInputError(TriagePolicyFailureCode.INPUT_AUTHORITY_FIELD_FORBIDDEN)
    return canonical


def evaluate_triage_policy(input_payload: object) -> TriagePolicyResult:
    """Return a deterministic authoritative triage GateResult."""

    triage_input = canonicalize_triage_input(input_payload)
    outcomes = _rule_outcomes(triage_input)
    disposition = _overall_disposition(outcomes)
    decision = GateDecision.PASSED if disposition is TriageDisposition.CONTINUE else GateDecision.BLOCKED
    details = _gate_details(disposition, outcomes)
    gate_result = TriageGateResult(
        gate_name=TRIAGE_GATE_NAME,
        policy_version=TRIAGE_POLICY_VERSION,
        input_state_version=triage_input.input_state_version,
        decision=decision,
        details=details,
    )
    return TriagePolicyResult(
        disposition=disposition,
        input_state_version=triage_input.input_state_version,
        gate_result=gate_result,
        rule_outcomes=outcomes,
    )


def triage_gate_result(input_payload: object) -> TriageGateResult:
    """Stable L3-5 integration point for graph adapters."""

    return evaluate_triage_policy(input_payload).gate_result


def to_gate_result_schema(result: TriagePolicyResult | TriageGateResult) -> GateResultSchema:
    """Create an explicit mutable compatibility DTO from immutable authority."""

    gate = result.gate_result if isinstance(result, TriagePolicyResult) else result
    details = gate.details
    return GateResultSchema(
        gate_name=gate.gate_name,
        policy_version=gate.policy_version,
        input_state_version=gate.input_state_version,
        decision=gate.decision,
        details={
            "disposition": details.disposition.value,
            "candidate_count": details.candidate_count,
            "category_counts": {
                item.category: item.candidate_count for item in details.category_counts
            },
            "rule_ids": list(details.rule_ids),
            "rules": [item.model_dump(mode="json") for item in details.rules],
            "source_message_ids": list(details.source_message_ids),
        },
    )


def _rule_outcomes(triage_input: TriagePolicyInput) -> tuple[TriageRuleOutcome, ...]:
    source_ids_by_category: dict[RedFlagCategory, set[str]] = {}
    for candidate in triage_input.red_flag_candidates:
        source_ids_by_category.setdefault(candidate.category, set()).add(str(candidate.source_message_id))

    outcomes: list[TriageRuleOutcome] = []
    for category in sorted(source_ids_by_category, key=lambda item: item.value):
        rule = TRIAGE_RED_FLAG_RULES[category]
        source_ids = tuple(sorted(source_ids_by_category[category]))
        outcomes.append(
            TriageRuleOutcome(
                rule_id=rule.rule_id,
                category=category.value,
                disposition=rule.disposition,
                candidate_count=len(source_ids),
                source_message_ids=source_ids,
            )
        )
    return tuple(outcomes)


def _overall_disposition(outcomes: tuple[TriageRuleOutcome, ...]) -> TriageDisposition:
    if not outcomes:
        return TriageDisposition.CONTINUE
    if any(outcome.disposition is TriageDisposition.EMERGENCY_REFERRAL for outcome in outcomes):
        return TriageDisposition.EMERGENCY_REFERRAL
    return TriageDisposition.MANUAL_REVIEW


def _gate_details(
    disposition: TriageDisposition,
    outcomes: tuple[TriageRuleOutcome, ...],
) -> TriageGateDetails:
    category_counts = tuple(
        TriageCategoryCount(category=outcome.category, candidate_count=outcome.candidate_count)
        for outcome in outcomes
    )
    source_message_ids = tuple(
        sorted({source for outcome in outcomes for source in outcome.source_message_ids})
    )
    return TriageGateDetails(
        disposition=disposition,
        candidate_count=sum(outcome.candidate_count for outcome in outcomes),
        category_counts=category_counts,
        rule_ids=tuple(outcome.rule_id for outcome in outcomes),
        rules=outcomes,
        source_message_ids=source_message_ids,
    )


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
