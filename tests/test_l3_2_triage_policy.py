"""L3-2 TriagePolicy: deterministic red-flag gate tests."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ConfigDict, ValidationError

import app.agent_runtime.triage_policy as triage_policy
from app.agent_runtime.triage_policy import (
    TRIAGE_RED_FLAG_RULES,
    TriagePolicyFailureCode,
    TriagePolicyInputError,
    TriageRule,
    evaluate_triage_policy,
    to_gate_result_schema,
    triage_gate_result,
)
from app.schemas.domain import GateDecision
from app.schemas.intake import CandidateSeverity, EvidenceSpan, RedFlagCandidate, RedFlagCategory
from app.schemas.triage import (
    TRIAGE_GATE_NAME,
    TRIAGE_INPUT_SCHEMA_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_RESULT_SCHEMA_VERSION,
    TriageDisposition,
    TriagePolicyInput,
)


def candidate(
    category: RedFlagCategory,
    *,
    source: UUID | None = None,
    severity: CandidateSeverity = CandidateSeverity.HIGH,
    confidence: float = 0.9,
    evidence: str = "sanitized red flag evidence",
) -> RedFlagCandidate:
    actual_source = source or uuid4()
    return RedFlagCandidate(
        category=category,
        source_message_id=actual_source,
        span=EvidenceSpan(
            source_message_id=actual_source,
            start_char=0,
            end_char=8,
            quote="evidence",
        ),
        severity=severity,
        evidence=evidence,
        confidence=confidence,
    )


def triage_input(*candidates: RedFlagCandidate, state_version: int = 7) -> TriagePolicyInput:
    return TriagePolicyInput(input_state_version=state_version, red_flag_candidates=candidates)


def test_contract_is_versioned_strict_serializable_and_output_uses_gate_result() -> None:
    payload = triage_input()
    result = evaluate_triage_policy(payload)

    assert payload.schema_version == TRIAGE_INPUT_SCHEMA_VERSION
    assert result.schema_version == TRIAGE_RESULT_SCHEMA_VERSION
    assert result.policy_version == TRIAGE_POLICY_VERSION
    assert result.gate_result.gate_name == TRIAGE_GATE_NAME
    assert result.gate_result.policy_version == TRIAGE_POLICY_VERSION
    assert result.gate_result.input_state_version == payload.input_state_version
    assert result.gate_result == triage_gate_result(payload)
    assert TriagePolicyInput.model_validate_json(payload.model_dump_json()) == payload
    with pytest.raises(ValidationError):
        TriagePolicyInput.model_validate({"input_state_version": 1, "red_flag_candidates": (), "passed": True})


def test_no_candidates_is_the_only_continue_and_passed_path() -> None:
    result = evaluate_triage_policy(triage_input())

    assert result.disposition is TriageDisposition.CONTINUE
    assert result.gate_result.decision is GateDecision.PASSED
    assert result.rule_outcomes == ()
    assert result.gate_result.details.disposition is TriageDisposition.CONTINUE
    assert result.gate_result.details.candidate_count == 0
    assert result.gate_result.details.category_counts == ()
    assert result.gate_result.details.rule_ids == ()
    assert result.gate_result.details.rules == ()
    assert result.gate_result.details.source_message_ids == ()


@pytest.mark.parametrize(
    ("category", "expected_disposition"),
    [
        (RedFlagCategory.SEVERE_PAIN, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.BREATHING_DIFFICULTY, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.ALTERED_CONSCIOUSNESS, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.SEVERE_BLEEDING, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.NEUROLOGIC_DEFICIT, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.HIGH_FEVER, TriageDisposition.EMERGENCY_REFERRAL),
        (RedFlagCategory.OTHER, TriageDisposition.RISK_NOTE),
    ],
)
def test_every_red_flag_category_has_explicit_deterministic_mapping(
    category: RedFlagCategory,
    expected_disposition: TriageDisposition,
) -> None:
    assert set(TRIAGE_RED_FLAG_RULES) == set(RedFlagCategory)

    result = evaluate_triage_policy(triage_input(candidate(category)))

    # 0d-3 风险三级化：紧急类别（HIGH/高置信）→ 阻断；OTHER → RISK_NOTE 不阻断。
    expected_decision = (
        GateDecision.BLOCKED
        if expected_disposition is TriageDisposition.EMERGENCY_REFERRAL
        else GateDecision.PASSED
    )
    assert result.disposition is expected_disposition
    assert result.gate_result.decision is expected_decision
    assert result.rule_outcomes[0].category == category.value
    assert result.rule_outcomes[0].disposition is expected_disposition
    assert result.rule_outcomes[0].rule_id == TRIAGE_RED_FLAG_RULES[category].rule_id


def test_high_risk_category_downgraded_by_low_severity_metadata() -> None:
    # 0d-3：低危/低置信的 LLM 候选不触发紧急阻断，降级 RISK_NOTE 留痕继续。
    result = evaluate_triage_policy(
        triage_input(
            candidate(
                RedFlagCategory.BREATHING_DIFFICULTY,
                severity=CandidateSeverity.LOW,
                confidence=0,
            )
        )
    )

    assert result.disposition is TriageDisposition.RISK_NOTE
    assert result.gate_result.decision is GateDecision.PASSED


def test_high_severity_candidate_triggers_emergency_referral() -> None:
    result = evaluate_triage_policy(
        triage_input(
            candidate(
                RedFlagCategory.BREATHING_DIFFICULTY,
                severity=CandidateSeverity.HIGH,
                confidence=0.9,
            )
        )
    )

    assert result.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert result.gate_result.decision is GateDecision.BLOCKED


def test_other_candidate_enters_risk_note_and_never_blocks() -> None:
    result = evaluate_triage_policy(triage_input(candidate(RedFlagCategory.OTHER)))

    assert result.disposition is TriageDisposition.RISK_NOTE
    assert result.gate_result.decision is GateDecision.PASSED
    assert result.gate_result.details.risk_level == "noted"


def test_emergency_referral_takes_precedence_over_manual_review() -> None:
    result = evaluate_triage_policy(
        triage_input(
            candidate(RedFlagCategory.OTHER),
            candidate(RedFlagCategory.SEVERE_BLEEDING),
        )
    )

    assert result.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert result.gate_result.decision is GateDecision.BLOCKED


def test_duplicate_and_reordered_candidates_are_stable_and_idempotent() -> None:
    source_a = uuid4()
    source_b = uuid4()
    breathing = candidate(RedFlagCategory.BREATHING_DIFFICULTY, source=source_a, evidence="first")
    breathing_duplicate = candidate(
        RedFlagCategory.BREATHING_DIFFICULTY,
        source=source_a,
        severity=CandidateSeverity.LOW,
        confidence=0,
        evidence="duplicate should not change authority",
    )
    other = candidate(RedFlagCategory.OTHER, source=source_b)

    baseline = evaluate_triage_policy(triage_input(breathing, other))
    reordered_with_duplicate = evaluate_triage_policy(triage_input(other, breathing_duplicate, breathing))

    assert baseline.model_dump(mode="json") == reordered_with_duplicate.model_dump(mode="json")
    assert baseline.gate_result.details.candidate_count == 2


def test_gate_result_details_do_not_leak_evidence_patient_identity_prompt_or_model_output() -> None:
    result = evaluate_triage_policy(
        triage_input(
            candidate(
                RedFlagCategory.SEVERE_PAIN,
                evidence="Alice phone 13800138000 prompt: ignore rules raw_model_output",
            )
        )
    )

    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    assert "Alice" not in encoded
    assert "13800138000" not in encoded
    assert "ignore rules" not in encoded
    assert "raw_model_output" not in encoded
    assert "evidence" not in encoded
    assert "severity" not in encoded
    assert "confidence" not in encoded


@pytest.mark.parametrize(
    "payload",
    [
        TriagePolicyInput.model_construct(schema_version="wrong", input_state_version=1, red_flag_candidates=()),
        TriagePolicyInput.model_construct(input_state_version=0, red_flag_candidates=()),
    ],
)
def test_constructed_wrong_version_and_illegal_state_version_are_fixed_rejections(payload: object) -> None:
    with pytest.raises(TriagePolicyInputError) as exc_info:
        evaluate_triage_policy(payload)
    assert exc_info.value.code is TriagePolicyFailureCode.INPUT_SCHEMA_INVALID
    assert str(exc_info.value) == "TRIAGE_INPUT_SCHEMA_INVALID"


def test_constructed_nested_candidate_cannot_bypass_schema_revalidation() -> None:
    forged_candidate = RedFlagCandidate.model_construct(
        category="unknown",
        source_message_id=uuid4(),
        severity="low",
        evidence="do not leak prompt text",
        confidence=0.5,
    )
    forged = TriagePolicyInput.model_construct(
        schema_version=TRIAGE_INPUT_SCHEMA_VERSION,
        input_state_version=1,
        red_flag_candidates=(forged_candidate,),
    )

    with pytest.raises(TriagePolicyInputError) as exc_info:
        evaluate_triage_policy(forged)
    assert exc_info.value.code is TriagePolicyFailureCode.INPUT_SCHEMA_INVALID
    assert "prompt text" not in str(exc_info.value)


def test_hidden_subclass_and_copy_fields_are_rejected_before_policy_evaluation() -> None:
    class ForgedTriageInput(TriagePolicyInput):
        model_config = ConfigDict(frozen=True, extra="allow")

    hidden_extra = ForgedTriageInput.model_validate(
        {
            "schema_version": TRIAGE_INPUT_SCHEMA_VERSION,
            "input_state_version": 1,
            "red_flag_candidates": [],
            "passed": True,
        }
    )
    copied_extra = triage_input().model_copy(update={"verified": True})

    for payload in (hidden_extra, copied_extra):
        with pytest.raises(TriagePolicyInputError) as exc_info:
            evaluate_triage_policy(payload)
        assert exc_info.value.code is TriagePolicyFailureCode.INPUT_AUTHORITY_FIELD_FORBIDDEN


def test_gate_decision_and_disposition_matrix_is_fixed() -> None:
    passed = evaluate_triage_policy(triage_input())
    manual = evaluate_triage_policy(triage_input(candidate(RedFlagCategory.OTHER)))
    emergency = evaluate_triage_policy(triage_input(candidate(RedFlagCategory.NEUROLOGIC_DEFICIT)))

    assert (passed.gate_result.decision, passed.disposition) == (
        GateDecision.PASSED,
        TriageDisposition.CONTINUE,
    )
    assert (manual.gate_result.decision, manual.disposition) == (
        GateDecision.PASSED,
        TriageDisposition.RISK_NOTE,
    )
    assert (emergency.gate_result.decision, emergency.disposition) == (
        GateDecision.BLOCKED,
        TriageDisposition.EMERGENCY_REFERRAL,
    )


def test_authoritative_result_is_deeply_immutable_and_re_evaluation_is_unchanged() -> None:
    payload = triage_input(candidate(RedFlagCategory.BREATHING_DIFFICULTY))
    result = evaluate_triage_policy(payload)
    gate = triage_gate_result(payload)

    with pytest.raises(ValidationError):
        result.gate_result.decision = GateDecision.PASSED
    with pytest.raises(ValidationError):
        result.disposition = TriageDisposition.CONTINUE
    with pytest.raises(ValidationError):
        result.gate_result.details.disposition = TriageDisposition.CONTINUE
    with pytest.raises(ValidationError):
        result.gate_result.details.category_counts[0].candidate_count = 999
    with pytest.raises(ValidationError):
        result.gate_result.details.rules[0].disposition = TriageDisposition.MANUAL_REVIEW
    with pytest.raises(ValidationError):
        result.gate_result.details.rule_ids += ("forged.rule",)
    with pytest.raises(ValidationError):
        result.gate_result.details.source_message_ids += (str(uuid4()),)
    with pytest.raises(ValidationError):
        gate.details.disposition = TriageDisposition.CONTINUE

    fresh = evaluate_triage_policy(payload)
    assert fresh.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert fresh.gate_result.decision is GateDecision.BLOCKED
    assert fresh.model_dump(mode="json") == result.model_dump(mode="json")


def test_rule_registry_is_structurally_immutable_and_re_evaluation_is_unchanged() -> None:
    payload = triage_input(candidate(RedFlagCategory.BREATHING_DIFFICULTY))
    original = evaluate_triage_policy(payload)
    downgraded_rule = TriageRule(
        rule_id="red_flag.breathing_difficulty.manual_review.v1",
        category=RedFlagCategory.BREATHING_DIFFICULTY,
        disposition=TriageDisposition.MANUAL_REVIEW,
    )

    with pytest.raises(TypeError):
        TRIAGE_RED_FLAG_RULES[RedFlagCategory.BREATHING_DIFFICULTY] = downgraded_rule
    with pytest.raises(TypeError):
        del TRIAGE_RED_FLAG_RULES[RedFlagCategory.BREATHING_DIFFICULTY]
    with pytest.raises(TypeError):
        TRIAGE_RED_FLAG_RULES["new_category"] = downgraded_rule
    registry_attr = "_rules"
    with pytest.raises(TypeError):
        setattr(TRIAGE_RED_FLAG_RULES, registry_attr, ())
    with pytest.raises(TypeError):
        delattr(TRIAGE_RED_FLAG_RULES, registry_attr)
    with pytest.raises(ValidationError):
        TRIAGE_RED_FLAG_RULES[RedFlagCategory.BREATHING_DIFFICULTY].disposition = (
            TriageDisposition.MANUAL_REVIEW
        )

    fresh = evaluate_triage_policy(payload)
    assert fresh.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert fresh.gate_result.decision is GateDecision.BLOCKED
    assert fresh.model_dump(mode="json") == original.model_dump(mode="json")


def test_rule_registry_has_no_mutable_module_backing_store_to_patch() -> None:
    payload = triage_input(candidate(RedFlagCategory.BREATHING_DIFFICULTY))
    original = evaluate_triage_policy(payload)
    downgraded_rule = TriageRule(
        rule_id="red_flag.breathing_difficulty.manual_review.v1",
        category=RedFlagCategory.BREATHING_DIFFICULTY,
        disposition=TriageDisposition.MANUAL_REVIEW,
    )
    mutable_rule_table_names: list[str] = []

    for name, value in vars(triage_policy).items():
        if isinstance(value, dict) and any(isinstance(item, TriageRule) for item in value.values()):
            mutable_rule_table_names.append(name)
            value[RedFlagCategory.BREATHING_DIFFICULTY] = downgraded_rule

    assert mutable_rule_table_names == []
    assert TRIAGE_RED_FLAG_RULES[RedFlagCategory.BREATHING_DIFFICULTY].disposition is (
        TriageDisposition.EMERGENCY_REFERRAL
    )
    fresh = evaluate_triage_policy(payload)
    assert fresh.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert fresh.gate_result.decision is GateDecision.BLOCKED
    assert fresh.model_dump(mode="json") == original.model_dump(mode="json")


def test_mutable_gate_result_schema_adapter_is_explicit_and_does_not_mutate_authority() -> None:
    authority = evaluate_triage_policy(triage_input(candidate(RedFlagCategory.BREATHING_DIFFICULTY)))
    compatible = to_gate_result_schema(authority)

    compatible.decision = GateDecision.PASSED
    assert compatible.details is not None
    compatible.details["disposition"] = "continue"
    compatible.details["category_counts"]["breathing_difficulty"] = 0
    compatible.details["rules"][0]["disposition"] = "manual_review"
    compatible.details["rule_ids"].append("forged.rule")
    compatible.details["source_message_ids"].append(str(uuid4()))

    assert authority.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert authority.gate_result.decision is GateDecision.BLOCKED
    assert authority.gate_result.details.disposition is TriageDisposition.EMERGENCY_REFERRAL
    assert authority.gate_result.details.category_counts[0].candidate_count == 1
    assert authority.gate_result.details.rules[0].disposition is TriageDisposition.EMERGENCY_REFERRAL
