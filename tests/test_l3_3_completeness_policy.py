"""L3-3 CompletenessPolicy: deterministic completeness and stagnation tests."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ConfigDict, ValidationError

import app.agent_runtime.completeness_policy as completeness_policy
from app.agent_runtime.completeness_policy import (
    COMPLETENESS_DIMENSION_RULES,
    COMPLETENESS_POLICY_CONFIG,
    CompletenessPolicyFailureCode,
    CompletenessPolicyInputError,
    completeness_to_gate_result_schema,
    evaluate_completeness_policy,
)
from app.agent_runtime.triage_policy import evaluate_triage_policy
from app.schemas.completeness import (
    COMPLETENESS_GATE_NAME,
    COMPLETENESS_INPUT_SCHEMA_VERSION,
    COMPLETENESS_POLICY_VERSION,
    COMPLETENESS_RESULT_SCHEMA_VERSION,
    ApplicabilityStatus,
    CompletenessDisposition,
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
    InquiryDimension,
    StagnationReasonCode,
)
from app.schemas.domain import CollectionStatus, GateDecision, ObservationStatus
from app.schemas.intake import CandidateSeverity, RedFlagCandidate, RedFlagCategory
from app.schemas.triage import (
    TriageCategoryCount,
    TriageDisposition,
    TriageGateDetails,
    TriageGateResult,
    TriagePolicyInput,
    TriageRuleOutcome,
)


def passed_triage(state_version: int = 7) -> TriageGateResult:
    return evaluate_triage_policy(TriagePolicyInput(input_state_version=state_version)).gate_result


def blocked_triage(state_version: int = 7) -> TriageGateResult:
    return evaluate_triage_policy(
        TriagePolicyInput(
            input_state_version=state_version,
            red_flag_candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.BREATHING_DIFFICULTY,
                    source_message_id=uuid4(),
                    severity=CandidateSeverity.LOW,
                    evidence="sanitized evidence not retained by completeness",
                    confidence=0,
                ),
            ),
        )
    ).gate_result


def fact(
    key: str,
    code: str,
    *,
    observation_id: UUID | None = None,
    session_id: UUID | None = None,
    status: ObservationStatus = ObservationStatus.ACTIVE,
    supersedes: UUID | None = None,
) -> CompletenessObservationFact:
    return CompletenessObservationFact(
        observation_id=observation_id or uuid4(),
        session_id=session_id or SESSION_ID,
        fact_key=key,
        value_fingerprint=f"fp:{code}",
        normalized_code=code,
        status=status,
        supersedes_observation_id=supersedes,
    )


def safety(
    *,
    allergy: CollectionStatus = CollectionStatus.EXPLICITLY_NONE,
    medications: CollectionStatus = CollectionStatus.EXPLICITLY_NONE,
    major_conditions: CollectionStatus = CollectionStatus.EXPLICITLY_NONE,
    pregnancy: CollectionStatus = CollectionStatus.UNKNOWN,
    lactation: CollectionStatus = CollectionStatus.UNKNOWN,
) -> CompletenessSafetyProfile:
    return CompletenessSafetyProfile(
        session_id=SESSION_ID,
        allergy_collection_status=allergy,
        allergen_count=1 if allergy is CollectionStatus.COLLECTED else 0,
        medications_collection_status=medications,
        medication_count=1 if medications is CollectionStatus.COLLECTED else 0,
        major_conditions_collection_status=major_conditions,
        major_condition_count=1 if major_conditions is CollectionStatus.COLLECTED else 0,
        pregnancy_collection_status=pregnancy,
        lactation_collection_status=lactation,
    )


def snapshot(
    *facts: CompletenessObservationFact,
    state_version: int = 7,
    safety_profile: CompletenessSafetyProfile | None = None,
) -> CompletenessDomainSnapshot:
    return CompletenessDomainSnapshot(
        session_id=SESSION_ID,
        state_version=state_version,
        observations=facts,
        safety_profile=safety_profile,
    )


def policy_input(
    *facts: CompletenessObservationFact,
    state_version: int = 7,
    triage: TriageGateResult | None = None,
    safety_profile: CompletenessSafetyProfile | None = None,
    progress: CompletenessProgress | None = None,
) -> CompletenessPolicyInput:
    return CompletenessPolicyInput(
        input_state_version=state_version,
        domain_snapshot=snapshot(
            *facts,
            state_version=state_version,
            safety_profile=safety_profile if safety_profile is not None else safety(),
        ),
        triage_gate=triage or passed_triage(state_version),
        progress=progress or CompletenessProgress(),
    )


def complete_general_facts() -> tuple[CompletenessObservationFact, ...]:
    return (
        fact("chief_complaint.category", "general"),
        fact("chief_complaint.symptom", "headache"),
        fact("chief_complaint.course", "two_days"),
        fact("present_illness.change", "stable"),
        fact("ten_questions.cold_heat", "none"),
        fact("ten_questions.stool_urine", "normal"),
        fact("ten_questions.diet", "normal"),
        fact("ten_questions.sleep", "normal"),
        fact("patient.sex", "male"),
    )


SESSION_ID = uuid4()


def test_contract_is_versioned_strict_serializable_and_output_uses_authoritative_gate() -> None:
    payload = policy_input(*complete_general_facts())
    result = evaluate_completeness_policy(payload)

    assert payload.schema_version == COMPLETENESS_INPUT_SCHEMA_VERSION
    assert result.schema_version == COMPLETENESS_RESULT_SCHEMA_VERSION
    assert result.policy_version == COMPLETENESS_POLICY_VERSION
    assert result.gate_result.gate_name == COMPLETENESS_GATE_NAME
    assert result.gate_result.policy_version == COMPLETENESS_POLICY_VERSION
    assert result.gate_result.input_state_version == payload.input_state_version
    assert result.gate_result.details.rule_ids == tuple(item.rule_id for item in result.rule_outcomes)
    assert CompletenessPolicyInput.model_validate_json(payload.model_dump_json()) == payload
    with pytest.raises(ValidationError):
        CompletenessPolicyInput.model_validate(
            {
                **payload.model_dump(mode="json"),
                "ready": True,
            }
        )


def test_no_chief_complaint_is_incomplete_and_failed() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "chief_complaint.symptom")

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.INCOMPLETE
    assert result.gate_result.decision is GateDecision.FAILED
    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM in result.missing_required


def test_chief_complaint_category_alone_does_not_cover_real_symptom() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "chief_complaint.symptom")

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.INCOMPLETE
    assert result.gate_result.decision is GateDecision.FAILED
    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM in result.missing_required
    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM not in result.covered_dimensions


def test_chief_complaint_category_and_symptom_are_complementary_not_conflicting() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts()))

    assert result.disposition is CompletenessDisposition.READY
    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM in result.covered_dimensions
    assert all(
        item.dimension is not InquiryDimension.CHIEF_COMPLAINT_SYMPTOM
        for item in result.conflicting_dimensions
    )


def test_conflicting_current_chief_complaint_categories_are_conflict_and_failed() -> None:
    facts = complete_general_facts() + (fact("chief_complaint.category", "pain"),)

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.CONFLICT
    assert result.gate_result.decision is GateDecision.FAILED
    assert result.conflicting_dimensions == (
        result.gate_result.details.conflicting_dimensions[0],
    )
    assert result.conflicting_dimensions[0].dimension is InquiryDimension.CHIEF_COMPLAINT_CATEGORY
    assert result.conflicting_dimensions[0].rule_id == "completeness.conflict.chief_complaint.category.v1"
    assert result.conflicting_dimensions[0].current_value_count == 2


def test_conflicting_chief_complaint_categories_are_order_independent() -> None:
    general = fact("chief_complaint.category", "general")
    pain = fact("chief_complaint.category", "pain")
    rest = tuple(item for item in complete_general_facts() if item.fact_key != "chief_complaint.category")

    first = evaluate_completeness_policy(policy_input(general, pain, *rest))
    second = evaluate_completeness_policy(policy_input(pain, general, *rest))

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.disposition is CompletenessDisposition.CONFLICT


def test_duplicate_same_chief_complaint_category_value_does_not_conflict() -> None:
    result = evaluate_completeness_policy(
        policy_input(*complete_general_facts(), fact("chief_complaint.category", "general"))
    )

    assert result.disposition is CompletenessDisposition.READY
    assert all(
        item.dimension is not InquiryDimension.CHIEF_COMPLAINT_CATEGORY
        for item in result.conflicting_dimensions
    )


def test_complete_information_with_conflicting_category_still_cannot_be_ready() -> None:
    result = evaluate_completeness_policy(
        policy_input(*complete_general_facts(), fact("chief_complaint.category", "pain"))
    )

    assert result.disposition is CompletenessDisposition.CONFLICT
    assert result.gate_result.decision is GateDecision.FAILED
    assert result.missing_required == ()


def test_category_conflict_output_keeps_only_dimension_rule_and_count() -> None:
    result = evaluate_completeness_policy(
        policy_input(*complete_general_facts(), fact("chief_complaint.category", "pain"))
    )

    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    assert "general" not in encoded
    assert "pain" not in encoded
    assert "fp:general" not in encoded
    assert "fp:pain" not in encoded
    assert result.conflicting_dimensions[0].dimension is InquiryDimension.CHIEF_COMPLAINT_CATEGORY
    assert result.conflicting_dimensions[0].current_value_count == 2


def test_symptom_without_basic_course_is_still_incomplete() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "chief_complaint.course")

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.INCOMPLETE
    assert InquiryDimension.BASIC_COURSE in result.missing_required


def test_required_dimensions_complete_is_ready_and_passed() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts()))

    assert result.disposition is CompletenessDisposition.READY
    assert result.gate_result.decision is GateDecision.PASSED
    assert result.missing_required == ()
    assert result.gate_result.details.covered_dimensions == result.covered_dimensions


def test_optional_dimensions_missing_do_not_block_ready() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts()))

    assert result.disposition is CompletenessDisposition.READY
    assert result.missing_optional == (InquiryDimension.FOUR_DIAGNOSIS, InquiryDimension.PAST_HISTORY)


def test_single_snapshot_with_multiple_structured_facts_covers_multiple_dimensions() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts()))

    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM in result.covered_dimensions
    assert InquiryDimension.BASIC_COURSE in result.covered_dimensions
    assert InquiryDimension.PRESENT_ILLNESS_CHANGE in result.covered_dimensions
    assert InquiryDimension.TEN_STOOL_URINE in result.covered_dimensions


def test_duplicate_and_reordered_facts_are_stable() -> None:
    base = complete_general_facts()
    duplicate = fact("ten_questions.sleep", "normal")
    first = evaluate_completeness_policy(policy_input(*base, duplicate))
    second = evaluate_completeness_policy(policy_input(*(tuple(reversed(base)) + (duplicate,))))

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_corrected_retracted_and_superseded_old_facts_do_not_count_as_current_coverage() -> None:
    old_course = fact("chief_complaint.course", "one_day", observation_id=uuid4())
    correction = fact(
        "chief_complaint.course",
        "two_days",
        status=ObservationStatus.CORRECTED,
        supersedes=old_course.observation_id,
    )
    retracted_sleep = fact("ten_questions.sleep", "poor", observation_id=uuid4())
    retract = fact(
        "ten_questions.sleep",
        "none",
        status=ObservationStatus.RETRACTED,
        supersedes=retracted_sleep.observation_id,
    )
    facts = tuple(
        item
        for item in complete_general_facts()
        if item.fact_key not in {"chief_complaint.course", "ten_questions.sleep"}
    )

    result = evaluate_completeness_policy(policy_input(*facts, old_course, correction, retracted_sleep, retract))

    assert InquiryDimension.BASIC_COURSE in result.covered_dimensions
    assert InquiryDimension.TEN_SLEEP in result.missing_required


def test_conflicting_current_facts_are_conflict_and_failed() -> None:
    facts = complete_general_facts() + (fact("ten_questions.sleep", "poor"),)

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.CONFLICT
    assert result.gate_result.decision is GateDecision.FAILED
    assert result.conflicting_dimensions[0].dimension is InquiryDimension.TEN_SLEEP
    assert result.conflicting_dimensions[0].current_value_count == 2


def test_complementary_ten_question_subfields_do_not_conflict() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "ten_questions.stool_urine") + (
        fact("ten_questions.stool", "normal"),
        fact("ten_questions.urine", "normal"),
    )

    result = evaluate_completeness_policy(policy_input(*facts))

    assert result.disposition is CompletenessDisposition.READY
    assert InquiryDimension.TEN_STOOL_URINE in result.covered_dimensions
    assert all(item.dimension is not InquiryDimension.TEN_STOOL_URINE for item in result.conflicting_dimensions)


def test_unknown_allergy_is_not_complete_but_explicitly_none_is_complete() -> None:
    unknown = evaluate_completeness_policy(
        policy_input(*complete_general_facts(), safety_profile=safety(allergy=CollectionStatus.UNKNOWN))
    )
    none = evaluate_completeness_policy(policy_input(*complete_general_facts(), safety_profile=safety()))

    assert InquiryDimension.ALLERGY_STATUS in unknown.missing_required
    assert none.disposition is CompletenessDisposition.READY
    assert InquiryDimension.ALLERGY_STATUS in none.covered_dimensions


def test_unknown_current_medications_and_major_conditions_are_not_complete() -> None:
    result = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            safety_profile=safety(
                medications=CollectionStatus.UNKNOWN,
                major_conditions=CollectionStatus.UNKNOWN,
            ),
        )
    )

    assert InquiryDimension.MEDICATION_STATUS in result.missing_required
    assert InquiryDimension.MAJOR_CONDITION_STATUS in result.missing_required


def test_explicit_no_medications_and_no_major_conditions_are_complete() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts(), safety_profile=safety()))

    assert InquiryDimension.MEDICATION_STATUS in result.covered_dimensions
    assert InquiryDimension.MAJOR_CONDITION_STATUS in result.covered_dimensions
    assert result.disposition is CompletenessDisposition.READY


def test_female_valid_age_missing_menopause_makes_applicability_unknown() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "patient.sex") + (
        fact("patient.sex", "female"),
        fact("patient.age", "30"),
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert result.gate_result.details.applicability.pregnancy is ApplicabilityStatus.UNKNOWN
    assert result.gate_result.details.applicability.lactation is ApplicabilityStatus.UNKNOWN
    assert InquiryDimension.PREGNANCY_STATUS in result.missing_required
    assert InquiryDimension.LACTATION_STATUS in result.missing_required


def test_female_valid_age_explicitly_not_menopausal_is_applicable() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "patient.sex") + (
        fact("patient.sex", "female"),
        fact("patient.age", "30"),
        fact("patient.menopause", "false"),
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert result.gate_result.details.applicability.pregnancy is ApplicabilityStatus.APPLICABLE
    assert result.gate_result.details.applicability.lactation is ApplicabilityStatus.APPLICABLE
    assert InquiryDimension.PREGNANCY_STATUS in result.missing_required
    assert InquiryDimension.LACTATION_STATUS in result.missing_required


def test_female_explicitly_menopausal_is_not_applicable() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "patient.sex") + (
        fact("patient.sex", "female"),
        fact("patient.age", "50"),
        fact("patient.menopause", "true"),
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert result.gate_result.details.applicability.pregnancy is ApplicabilityStatus.NOT_APPLICABLE
    assert result.gate_result.details.applicability.lactation is ApplicabilityStatus.NOT_APPLICABLE
    assert InquiryDimension.PREGNANCY_STATUS not in result.missing_required
    assert InquiryDimension.LACTATION_STATUS not in result.missing_required


def test_explicitly_not_applicable_patient_does_not_require_pregnancy_or_lactation() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts(), safety_profile=safety()))

    assert result.gate_result.details.applicability.pregnancy is ApplicabilityStatus.NOT_APPLICABLE
    assert InquiryDimension.PREGNANCY_STATUS not in result.missing_required
    assert InquiryDimension.LACTATION_STATUS not in result.missing_required


def test_unknown_applicability_does_not_auto_pass() -> None:
    facts = tuple(item for item in complete_general_facts() if item.fact_key != "patient.sex")

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert result.gate_result.details.applicability.pregnancy is ApplicabilityStatus.UNKNOWN
    assert InquiryDimension.PREGNANCY_STATUS in result.missing_required
    assert InquiryDimension.LACTATION_STATUS in result.missing_required


def test_complaint_category_selects_dynamic_ten_question_thresholds() -> None:
    facts = (
        fact("chief_complaint.category", "respiratory"),
        fact("chief_complaint.symptom", "cough"),
        fact("chief_complaint.course", "two_days"),
        fact("present_illness.change", "cough"),
        fact("ten_questions.cold_heat", "none"),
        fact("ten_questions.sleep", "normal"),
        fact("patient.sex", "male"),
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert InquiryDimension.TEN_RESPIRATORY in result.missing_required
    assert InquiryDimension.TEN_DIET not in result.missing_required


def test_triage_blocked_never_becomes_ready() -> None:
    result = evaluate_completeness_policy(
        policy_input(*complete_general_facts(), triage=blocked_triage(), safety_profile=safety())
    )

    assert result.disposition is CompletenessDisposition.TRIAGE_BLOCKED
    assert result.gate_result.decision is GateDecision.BLOCKED


def test_triage_input_state_version_mismatch_is_fixed_rejection() -> None:
    payload = policy_input(*complete_general_facts(), state_version=7, triage=passed_triage(8))

    with pytest.raises(CompletenessPolicyInputError) as exc_info:
        evaluate_completeness_policy(payload)

    assert exc_info.value.code is CompletenessPolicyFailureCode.TRIAGE_GATE_MISMATCH
    assert str(exc_info.value) == "COMPLETENESS_TRIAGE_GATE_MISMATCH"


def test_triage_continue_passed_with_nonzero_candidate_count_is_fixed_rejection() -> None:
    forged = TriageGateResult(
        input_state_version=7,
        decision=GateDecision.PASSED,
        details=TriageGateDetails(
            disposition=TriageDisposition.CONTINUE,
            candidate_count=1,
        ),
    )

    with pytest.raises(CompletenessPolicyInputError) as exc_info:
        evaluate_completeness_policy(policy_input(*complete_general_facts(), triage=forged))

    assert exc_info.value.code is CompletenessPolicyFailureCode.TRIAGE_GATE_MISMATCH


@pytest.mark.parametrize(
    "details",
    [
        TriageGateDetails(
            disposition=TriageDisposition.CONTINUE,
            candidate_count=0,
            category_counts=(TriageCategoryCount(category="other", candidate_count=1),),
        ),
        TriageGateDetails(
            disposition=TriageDisposition.CONTINUE,
            candidate_count=0,
            rule_ids=("red_flag.other.manual_review.v1",),
        ),
        TriageGateDetails(
            disposition=TriageDisposition.CONTINUE,
            candidate_count=0,
            rules=(
                TriageRuleOutcome(
                    rule_id="red_flag.other.manual_review.v1",
                    category="other",
                    disposition=TriageDisposition.MANUAL_REVIEW,
                    candidate_count=1,
                    source_message_ids=(str(uuid4()),),
                ),
            ),
        ),
        TriageGateDetails(
            disposition=TriageDisposition.CONTINUE,
            candidate_count=0,
            source_message_ids=(str(uuid4()),),
        ),
    ],
)
def test_triage_continue_passed_with_any_candidate_reference_is_fixed_rejection(
    details: TriageGateDetails,
) -> None:
    forged = TriageGateResult(input_state_version=7, decision=GateDecision.PASSED, details=details)

    with pytest.raises(CompletenessPolicyInputError) as exc_info:
        evaluate_completeness_policy(policy_input(*complete_general_facts(), triage=forged))

    assert exc_info.value.code is CompletenessPolicyFailureCode.TRIAGE_GATE_MISMATCH


def test_normal_triage_continue_passed_gate_from_policy_still_enters_completeness() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts(), triage=passed_triage()))

    assert result.disposition is CompletenessDisposition.READY
    assert result.gate_result.decision is GateDecision.PASSED


def test_no_new_facts_before_threshold_does_not_trigger_handoff() -> None:
    result = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            progress=CompletenessProgress(
                no_new_facts_rounds=COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold - 1,
            ),
        )
    )

    assert result.disposition is CompletenessDisposition.READY
    assert not result.stagnation.manual_handoff_required


def test_no_new_facts_at_threshold_is_stagnated_and_blocked() -> None:
    result = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            progress=CompletenessProgress(
                no_new_facts_rounds=COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold,
            ),
        )
    )

    assert result.disposition is CompletenessDisposition.STAGNATED
    assert result.gate_result.decision is GateDecision.BLOCKED
    assert result.stagnation.reason_codes == (StagnationReasonCode.NO_NEW_FACTS_THRESHOLD,)


def test_max_followup_rounds_boundary_behavior() -> None:
    before = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            progress=CompletenessProgress(followup_rounds=COMPLETENESS_POLICY_CONFIG.max_followup_rounds - 1),
        )
    )
    at_threshold = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            progress=CompletenessProgress(followup_rounds=COMPLETENESS_POLICY_CONFIG.max_followup_rounds),
        )
    )

    assert before.disposition is CompletenessDisposition.READY
    assert at_threshold.disposition is CompletenessDisposition.STAGNATED
    assert at_threshold.stagnation.reason_codes == (StagnationReasonCode.MAX_FOLLOWUP_ROUNDS,)


def test_any_stagnation_condition_is_deterministic_and_auditable() -> None:
    payload = policy_input(
        *complete_general_facts(),
        progress=CompletenessProgress(
            no_new_facts_rounds=COMPLETENESS_POLICY_CONFIG.no_new_facts_round_threshold,
            followup_rounds=COMPLETENESS_POLICY_CONFIG.max_followup_rounds,
        ),
    )
    first = evaluate_completeness_policy(payload)
    second = evaluate_completeness_policy(payload)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.stagnation.reason_codes == (
        StagnationReasonCode.NO_NEW_FACTS_THRESHOLD,
        StagnationReasonCode.MAX_FOLLOWUP_ROUNDS,
    )


def test_constructed_copy_subclass_and_hidden_authority_fields_are_rejected() -> None:
    constructed = CompletenessPolicyInput.model_construct(
        schema_version="wrong",
        input_state_version=7,
        domain_snapshot=snapshot(state_version=7, safety_profile=safety()),
        triage_gate=passed_triage(),
        progress=CompletenessProgress(),
    )
    copied = policy_input(*complete_general_facts()).model_copy(update={"force": True})

    class ForgedCompletenessInput(CompletenessPolicyInput):
        model_config = ConfigDict(frozen=True, extra="allow")

    hidden = ForgedCompletenessInput.model_validate(
        {**policy_input(*complete_general_facts()).model_dump(mode="json"), "route": "reasoning"}
    )

    with pytest.raises(CompletenessPolicyInputError) as constructed_exc:
        evaluate_completeness_policy(constructed)
    assert constructed_exc.value.code is CompletenessPolicyFailureCode.INPUT_SCHEMA_INVALID

    for payload in (copied, hidden):
        with pytest.raises(CompletenessPolicyInputError) as exc_info:
            evaluate_completeness_policy(payload)
        assert exc_info.value.code is CompletenessPolicyFailureCode.INPUT_AUTHORITY_FIELD_FORBIDDEN


def test_authoritative_result_is_deeply_immutable_and_re_evaluation_is_unchanged() -> None:
    payload = policy_input(*complete_general_facts())
    result = evaluate_completeness_policy(payload)

    with pytest.raises(ValidationError):
        result.gate_result.decision = GateDecision.FAILED
    with pytest.raises(ValidationError):
        result.disposition = CompletenessDisposition.INCOMPLETE
    with pytest.raises(ValidationError):
        result.gate_result.details.missing_required += (InquiryDimension.BASIC_COURSE,)
    with pytest.raises(ValidationError):
        result.gate_result.details.rule_ids += ("forged.rule",)
    with pytest.raises(ValidationError):
        result.gate_result.details.stagnation.reason_codes += (
            StagnationReasonCode.MAX_FOLLOWUP_ROUNDS,
        )

    fresh = evaluate_completeness_policy(payload)
    assert fresh.model_dump(mode="json") == result.model_dump(mode="json")


def test_rule_registry_is_structurally_immutable_and_has_no_mutable_backing_store() -> None:
    payload = policy_input(*complete_general_facts())
    original = evaluate_completeness_policy(payload)
    replacement = COMPLETENESS_DIMENSION_RULES[InquiryDimension.BASIC_COURSE].model_copy(
        update={"required_by_default": False}
    )
    mutable_rule_table_names: list[str] = []

    with pytest.raises(TypeError):
        COMPLETENESS_DIMENSION_RULES[InquiryDimension.BASIC_COURSE] = replacement
    with pytest.raises(TypeError):
        del COMPLETENESS_DIMENSION_RULES[InquiryDimension.BASIC_COURSE]
    registry_attr = "_rules"
    with pytest.raises(TypeError):
        setattr(COMPLETENESS_DIMENSION_RULES, registry_attr, ())
    with pytest.raises(ValidationError):
        COMPLETENESS_DIMENSION_RULES[InquiryDimension.BASIC_COURSE].required_by_default = False

    for name, value in vars(completeness_policy).items():
        if isinstance(value, dict) and any(
            getattr(item, "__class__", None).__name__ == "CompletenessDimensionRule"
            for item in value.values()
        ):
            mutable_rule_table_names.append(name)

    assert mutable_rule_table_names == []
    fresh = evaluate_completeness_policy(payload)
    assert fresh.model_dump(mode="json") == original.model_dump(mode="json")


def test_mutable_gate_result_schema_adapter_does_not_mutate_authority() -> None:
    authority = evaluate_completeness_policy(policy_input(*complete_general_facts()))
    compatible = completeness_to_gate_result_schema(authority)

    compatible.decision = GateDecision.FAILED
    assert compatible.details is not None
    compatible.details["disposition"] = "incomplete"
    compatible.details["missing_required"].append("forged")

    assert authority.disposition is CompletenessDisposition.READY
    assert authority.gate_result.decision is GateDecision.PASSED
    assert authority.missing_required == ()


def test_output_does_not_contain_clinical_text_identity_prompt_or_raw_model_output() -> None:
    result = evaluate_completeness_policy(policy_input(*complete_general_facts()))

    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    assert "Alice" not in encoded
    assert "13800138000" not in encoded
    assert "ignore rules" not in encoded
    assert "raw_model_output" not in encoded
    assert "头痛" not in encoded
    assert "prompt" not in encoded
