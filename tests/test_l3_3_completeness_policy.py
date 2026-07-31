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
from app.schemas.intake import CandidateSeverity, EvidenceSpan, RedFlagCandidate, RedFlagCategory
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
    source_message_id = uuid4()
    return evaluate_triage_policy(
        TriagePolicyInput(
            input_state_version=state_version,
            red_flag_candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.BREATHING_DIFFICULTY,
                    source_message_id=source_message_id,
                    span=EvidenceSpan(
                        source_message_id=source_message_id,
                        start_char=0,
                        end_char=4,
                        quote="呼吸困难",
                    ),
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

    assert result.disposition is CompletenessDisposition.PARTIAL
    assert result.gate_result.decision is GateDecision.PASSED
    assert result.stagnation.partial_required is True
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
    # 2d(决策 11): cap 到且缺非安全维度 → PARTIAL(落库推进),不再一律 STAGNATED。
    assert at_threshold.disposition is CompletenessDisposition.PARTIAL
    assert at_threshold.stagnation.partial_required is True
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


# ---------------------------------------------------------------------------
# D1: 派生覆盖映射——抽取层把寒热落到 present_illness.{chills,fever} 时仍视为
# 十问寒热维度已覆盖（解 trigger session 63e78741 死循环根因）。
# ---------------------------------------------------------------------------


def _ten_question_cold_heat_facts_only_chills_fever() -> tuple[CompletenessObservationFact, ...]:
    """复现 trigger session 的真实事实面：只在 present_illness.* 上有寒热，无 ten_questions.*。"""
    return (
        fact("chief_complaint.category", "general"),
        fact("chief_complaint.symptom", "headache"),
        fact("chief_complaint.course", "two_days"),
        fact("present_illness.change", "stable"),
        fact("present_illness.chills", "mild"),
        fact("present_illness.fever", "mild"),
        fact("ten_questions.stool_urine", "normal"),
        fact("ten_questions.sleep", "normal"),
        fact("patient.sex", "male"),
    )


def test_present_illness_chills_fever_derives_cold_heat_coverage() -> None:
    """D1：present_illness.{chills,fever} 有值 → cold_heat 判 covered、移出 missing_required。"""

    result = evaluate_completeness_policy(
        policy_input(
            *_ten_question_cold_heat_facts_only_chills_fever(),
            safety_profile=safety(),
        )
    )

    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions
    assert InquiryDimension.TEN_COLD_HEAT not in result.missing_required


def test_present_illness_chills_alone_derives_cold_heat_coverage() -> None:
    """D1：派生键集任一命中即覆盖——单条 present_illness.chills 也足够。"""

    facts = tuple(
        item for item in _ten_question_cold_heat_facts_only_chills_fever()
        if item.fact_key != "present_illness.fever"
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions
    assert InquiryDimension.TEN_COLD_HEAT not in result.missing_required


def test_no_derivation_without_present_illness_temperature_facts() -> None:
    """D1：无派生键命中时 cold_heat 仍判缺失（覆盖派生不凭空加分）。"""

    facts = tuple(
        item
        for item in _ten_question_cold_heat_facts_only_chills_fever()
        if item.fact_key not in {"present_illness.chills", "present_illness.fever"}
    )

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    assert InquiryDimension.TEN_COLD_HEAT not in result.covered_dimensions
    assert InquiryDimension.TEN_COLD_HEAT in result.missing_required


def test_canonical_ten_questions_cold_heat_still_covers() -> None:
    """D1：canonical 路径不变——ten_questions.cold_heat 有值仍覆盖（回归保护）。"""

    facts = tuple(
        item for item in complete_general_facts() if item.fact_key != "ten_questions.cold_heat"
    ) + (fact("ten_questions.cold_heat", "none"),)

    result = evaluate_completeness_policy(policy_input(*facts))

    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions
    assert InquiryDimension.TEN_COLD_HEAT not in result.missing_required


def test_derivation_does_not_back_propagate_to_present_illness_dimension() -> None:
    """D1：单向覆盖——派生只把 present→ten 兜上来，不会把 ten_questions.* 派生到
    PRESENT_ILLNESS_CHANGE，也不会删掉 canonical 现性维度事实。"""

    facts = _ten_question_cold_heat_facts_only_chills_fever()

    result = evaluate_completeness_policy(policy_input(*facts, safety_profile=safety()))

    # canonical PRESENT_ILLNESS_CHANGE 仍由 present_illness.change 命中——保留原有覆盖，
    # 派生没有把它"偷放"或"没收"。
    assert InquiryDimension.PRESENT_ILLNESS_CHANGE in result.covered_dimensions
    # 派生键 present_illness.chills/fever 本身不是任何 canonical 规则的 fact_keys，因此不
    # 会额外产生 PRESENT_ILLNESS_CHANGE 之外的 canonical 维度重复计数或冲突。
    assert all(
        item.dimension is not InquiryDimension.PRESENT_ILLNESS_CHANGE
        for item in result.conflicting_dimensions
    )


# ---------------------------------------------------------------------------
# D1 厚真源：present_illness.cough / symptom.* 等具体现病症状键现在也覆盖
# PRESENT_ILLNESS_CHANGE（解 trigger session d8ba36ae 现病变化维度永远 missing 的死循环）。
# 覆盖 ≠ 成熟——成熟度（KEY_THRESHOLDS / 趋势键）在 D2 闸门再判，此处只测覆盖层。
# ---------------------------------------------------------------------------


def _d8ba36ae_face_seed_plus_cough_fever() -> tuple[CompletenessObservationFact, ...]:
    """复现 trigger session d8ba36ae 第 2 轮后的事实面：seed + 抽取产出 cough/fever，
    **无** present_illness.change / associated_symptom。值用 ascii 占位码（schema 要求
    normalized_code 仅含 ascii）。"""
    return (
        fact("chief_complaint.category", "respiratory"),
        fact("chief_complaint.symptom", "cold_one_week"),
        fact("chief_complaint.course", "one_week"),
        fact("present_illness.cough", "cough"),
        fact("present_illness.fever", "low_fever"),
        fact("patient.sex", "male"),
    )


def test_present_illness_cough_derives_change_coverage_breaks_d8ba36ae_loop() -> None:
    """D1 厚：present_illness.cough 覆盖 PRESENT_ILLNESS_CHANGE → change 移出 missing_required
    → gap_selector 选下一维度而非锁死 change 命中同一写死模板。"""

    result = evaluate_completeness_policy(
        policy_input(*_d8ba36ae_face_seed_plus_cough_fever(), safety_profile=safety())
    )

    assert InquiryDimension.PRESENT_ILLNESS_CHANGE in result.covered_dimensions
    assert InquiryDimension.PRESENT_ILLNESS_CHANGE not in result.missing_required


def test_present_illness_symptom_subprefix_derives_coverage() -> None:
    """D1 厚：抽取常漂移到 present_illness.symptom.<sub> 子前缀（67e04fc4 实测），子前缀键
    也覆盖对应维度。"""

    result = evaluate_completeness_policy(
        policy_input(
            fact("chief_complaint.category", "respiratory"),
            fact("chief_complaint.symptom", "cold_one_week"),
            fact("chief_complaint.course", "one_week"),
            fact("present_illness.symptom.cough", "cough_worsening"),
            fact("present_illness.symptom.fever", "low_grade_fever"),
            fact("patient.sex", "male"),
            safety_profile=safety(),
        )
    )

    # 子前缀 cough/fever 既覆盖现病变化，又覆盖寒热、呼吸。
    assert InquiryDimension.PRESENT_ILLNESS_CHANGE in result.covered_dimensions
    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions
    assert InquiryDimension.TEN_RESPIRATORY in result.covered_dimensions


def test_ten_stool_urine_subprefix_keys_cover_boundary_dimension() -> None:
    """D1 厚：抽取漂移到 ten_questions.stool_urine.stool / .urine 子前缀键（1b89a179 实测
    canonical ten_questions.stool_urine 的别名键）也覆盖 TEN_STOOL_URINE。"""

    result = evaluate_completeness_policy(
        policy_input(
            fact("chief_complaint.category", "digestive"),
            fact("chief_complaint.symptom", "abdominal_pain"),
            fact("chief_complaint.course", "three_days"),
            fact("present_illness.change", "stable"),
            fact("ten_questions.stool_urine.stool", "soft_stool"),
            fact("ten_questions.stool_urine.urine", "yellow_urine"),
            fact("ten_questions.cold_heat", "none"),
            fact("ten_questions.sleep", "normal"),
            fact("patient.sex", "male"),
            safety_profile=safety(),
        )
    )

    assert InquiryDimension.TEN_STOOL_URINE in result.covered_dimensions
    assert InquiryDimension.TEN_STOOL_URINE not in result.missing_required


def test_coverage_bool_breaks_d8ba36ae_loop() -> None:
    """端到端：d8ba36ae 事实面经厚 D1 后，gap_selector 不再选 PRESENT_ILLNESS_CHANGE
    （priority 120），改选下一缺失维度——解死循环。"""

    from app.agent_runtime.gap_selector import select_gap

    result = evaluate_completeness_policy(
        policy_input(*_d8ba36ae_face_seed_plus_cough_fever(), safety_profile=safety())
    )
    gap = select_gap(result)

    assert gap.selected_dimension is not InquiryDimension.PRESENT_ILLNESS_CHANGE
    assert gap.selection_kind.value == "required"


# ---------------------------------------------------------------------------
# D1 厚真源：键桥真源方法 + 成熟度计数/趋势访问器（D2 的契约预留，单元固化）。
# ---------------------------------------------------------------------------


def test_dimension_keysets_are_exhaustive_and_cover_all_dimensions() -> None:
    """D1 厚：DIMENSION_KEYSETS 覆盖所有完整体维度（缺一项 D2 maturity 计数会 KeyError）。"""

    from app.agent_runtime.intake_dimension_mapping import DIMENSION_KEYSETS

    expected = set(InquiryDimension)
    assert set(DIMENSION_KEYSETS) == expected


def test_safety_dimensions_have_empty_keysets_never_derived() -> None:
    """D1 厚：安全维度 keyset 留空占位——派生永不碰安全维度（仍由 safety_profile 决定）。"""

    from app.agent_runtime.intake_dimension_mapping import derived_coverage_for_fact_keys

    safety_dims = (
        InquiryDimension.ALLERGY_STATUS,
        InquiryDimension.MEDICATION_STATUS,
        InquiryDimension.MAJOR_CONDITION_STATUS,
        InquiryDimension.PREGNANCY_STATUS,
        InquiryDimension.LACTATION_STATUS,
    )
    # 即使把安全维度的 canonical 键喂进 active，派生也不返回它。
    derived = derived_coverage_for_fact_keys(
        frozenset(
            key
            for d in safety_dims
            for key in (d.value,)
        )
    )
    assert all(d not in derived for d in safety_dims)


def test_dimension_acquired_key_count_treats_canonical_and_derived_equally() -> None:
    """D1 厚：成熟度计数对 canonical 与派生键等权——doctor 答到的都算"采到关键键"。"""

    from app.agent_runtime.intake_dimension_mapping import dimension_acquired_key_count

    # 只派生键（无 canonical）→ acquired 2。
    assert (
        dimension_acquired_key_count(
            InquiryDimension.TEN_COLD_HEAT,
            frozenset({"present_illness.chills", "present_illness.fever"}),
        )
        == 2
    )
    # canonical + 派生混采 → 全计。
    assert (
        dimension_acquired_key_count(
            InquiryDimension.TEN_COLD_HEAT,
            frozenset({"ten_questions.cold_heat", "present_illness.chills"}),
        )
        == 2
    )
    # 无命中 → 0。
    assert (
        dimension_acquired_key_count(
            InquiryDimension.TEN_COLD_HEAT, frozenset({"patient.age"})
        )
        == 0
    )


def test_dimension_acquired_key_count_filters_to_active_only() -> None:
    """D1 厚：corrected/retracted 的事实不应计入 acquired——调用方传 active_keys 已过滤，
    此处固化契约：函数本身只看传入集合，重复键不双计。"""

    from app.agent_runtime.intake_dimension_mapping import dimension_acquired_key_count

    # 同一 keyset 键即使集合里出现一次也只计一。
    assert (
        dimension_acquired_key_count(
            InquiryDimension.TEN_COLD_HEAT,
            frozenset({"present_illness.chills"}),
        )
        == 1
    )


def test_dimension_has_trend_key_for_change_requires_trend_semantics() -> None:
    """D1 厚（D2 预留）：PRESENT_ILLNESS_CHANGE 仅有具体症状键（cough）时无趋势 → trend=False，
    D2 据此追问"加重/减轻/稳定"；有 canonical 趋势键时 trend=True。"""

    from app.agent_runtime.intake_dimension_mapping import (
        MATURITY_TREND_KEYS,
        dimension_has_trend_key,
    )

    assert InquiryDimension.PRESENT_ILLNESS_CHANGE in MATURITY_TREND_KEYS

    # 只有具体症状键 → 无趋势。
    assert (
        dimension_has_trend_key(
            InquiryDimension.PRESENT_ILLNESS_CHANGE,
            frozenset({"present_illness.cough", "present_illness.fever"}),
        )
        is False
    )
    # 含 canonical 趋势键 → 有趋势。
    assert (
        dimension_has_trend_key(
            InquiryDimension.PRESENT_ILLNESS_CHANGE,
            frozenset({"present_illness.change"}),
        )
        is True
    )
    # 无趋势要求的维度（如十问寒热）恒 True。
    assert (
        dimension_has_trend_key(
            InquiryDimension.TEN_COLD_HEAT, frozenset({"present_illness.chills"})
        )
        is True
    )
