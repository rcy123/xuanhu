"""L3-4 GapSelector and Question Composer tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import app.agent_runtime.gap_selector as gap_selector
import app.agents.question_composer as question_composer
from app.agent_runtime.completeness_policy import evaluate_completeness_policy
from app.agent_runtime.gap_selector import (
    GAP_PRIORITY_RULES,
    FrozenGapPriorityRegistry,
    GapSelectionFailureCode,
    GapSelectionInputError,
    _select_gap_with_priority_registry,
    select_gap,
)
from app.agent_runtime.runtime import AgentRuntime, RuntimeErrorBase
from app.agent_runtime.specs import Capability, FailurePolicy, RunSpec, RuntimeErrorCode
from app.agent_runtime.triage_policy import evaluate_triage_policy
from app.agents.question_composer import (
    QUESTION_COMPOSER_POLICY_VERSION,
    QUESTION_TEMPLATES,
    FrozenQuestionTemplateRegistry,
    QuestionTemplate,
    build_question_composer_agent_spec,
    compose_question,
    validate_single_question_text,
)
from app.schemas.completeness import (
    COMPLETENESS_POLICY_VERSION,
    COMPLETENESS_RESULT_SCHEMA_VERSION,
    CompletenessDisposition,
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
    InquiryDimension,
)
from app.schemas.domain import CollectionStatus, GateDecision, ObservationStatus
from app.schemas.intake import CandidateSeverity, EvidenceSpan, RedFlagCandidate, RedFlagCategory
from app.schemas.question import (
    GAP_SELECTION_RESULT_SCHEMA_VERSION,
    GAP_SELECTOR_POLICY_VERSION,
    QUESTION_COMPOSER_AGENT_NAME,
    QUESTION_COMPOSER_AGENT_VERSION,
    QUESTION_COMPOSER_PROMPT_VERSION,
    QUESTION_MODEL_OUTPUT_SCHEMA_VERSION,
    QUESTION_RESULT_SCHEMA_VERSION,
    GapSelectionDisposition,
    GapSelectionKind,
    GapSelectionResult,
    QuestionComposerClinicalFact,
    QuestionComposerFailureCode,
    QuestionComposerModelInput,
    QuestionComposerModelOutput,
    QuestionCompositionStatus,
    QuestionSource,
)
from app.schemas.triage import TriageGateResult, TriagePolicyInput

SESSION_ID = uuid4()


class FakeGateway:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.actual_request_count = 0
        self.entered = asyncio.Event()

    async def chat_structured(
        self, messages: list[dict[str, Any]], output_schema: type[BaseModel], **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": messages, "output_schema": output_schema, **kwargs})
        self.actual_request_count += kwargs.get("max_requests", 1)
        self.entered.set()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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
                    evidence="sanitized evidence not retained by question composer",
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


def missing_symptom_facts() -> tuple[CompletenessObservationFact, ...]:
    return tuple(item for item in complete_general_facts() if item.fact_key != "chief_complaint.symptom")


def policy_input(
    *facts: CompletenessObservationFact,
    state_version: int = 7,
    triage: TriageGateResult | None = None,
    safety_profile: CompletenessSafetyProfile | None = None,
    progress: CompletenessProgress | None = None,
) -> CompletenessPolicyInput:
    return CompletenessPolicyInput(
        input_state_version=state_version,
        domain_snapshot=CompletenessDomainSnapshot(
            session_id=SESSION_ID,
            state_version=state_version,
            observations=facts,
            safety_profile=safety_profile if safety_profile is not None else safety(),
        ),
        triage_gate=triage or passed_triage(state_version),
        progress=progress or CompletenessProgress(),
    )


def build_run_spec(selection: GapSelectionResult, **overrides: Any) -> RunSpec:
    data: dict[str, Any] = {
        "run_id": uuid4(),
        "session_id": uuid4(),
        "state_version": selection.input_state_version,
        "stage": "intake_question",
        "agent_spec_version": QUESTION_COMPOSER_AGENT_VERSION,
        "prompt_version": QUESTION_COMPOSER_PROMPT_VERSION,
        "policy_version": QUESTION_COMPOSER_POLICY_VERSION,
        "deadline_at": datetime.now(UTC) + timedelta(seconds=5),
        "total_attempt_budget": 1,
        "idempotency_key": f"test:{selection.input_state_version}",
        "trace_id": f"trace-{uuid4()}",
    }
    data.update(overrides)
    return RunSpec(**data)


def fallback_gateway(question: str = "请问您这次主要不舒服是什么？") -> FakeGateway:
    return FakeGateway([{"schema_version": QUESTION_MODEL_OUTPUT_SCHEMA_VERSION, "question": question}])


def test_gap_schema_versions_and_serialization_contract() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    selection = select_gap(completeness)

    assert completeness.schema_version == COMPLETENESS_RESULT_SCHEMA_VERSION
    assert completeness.policy_version == COMPLETENESS_POLICY_VERSION
    assert selection.schema_version == GAP_SELECTION_RESULT_SCHEMA_VERSION
    assert selection.policy_version == GAP_SELECTOR_POLICY_VERSION
    assert selection.input_state_version == completeness.input_state_version
    assert GapSelectionResult.model_validate_json(selection.model_dump_json()) == selection


def test_incomplete_collects_chief_complaint_before_routine_safety_fields() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(*missing_symptom_facts(), safety_profile=safety(allergy=CollectionStatus.UNKNOWN))
    )

    selection = select_gap(completeness)

    assert completeness.disposition is CompletenessDisposition.INCOMPLETE
    assert selection.disposition is GapSelectionDisposition.SELECTED
    assert selection.selection_kind is GapSelectionKind.REQUIRED
    assert selection.selected_dimension is InquiryDimension.CHIEF_COMPLAINT_SYMPTOM
    assert selection.priority_rule_id == "gap.priority.chief_complaint.symptom.v1"


def test_course_and_present_change_are_collected_before_routine_safety_fields() -> None:
    missing_course = tuple(
        item
        for item in complete_general_facts()
        if item.fact_key not in {"chief_complaint.course", "present_illness.change"}
    )
    completeness = evaluate_completeness_policy(
        policy_input(
            *missing_course,
            safety_profile=safety(allergy=CollectionStatus.UNKNOWN),
        )
    )

    first = select_gap(completeness)

    assert first.selected_dimension is InquiryDimension.BASIC_COURSE
    assert (
        GAP_PRIORITY_RULES[InquiryDimension.PRESENT_ILLNESS_CHANGE].required_priority
        < GAP_PRIORITY_RULES[InquiryDimension.ALLERGY_STATUS].required_priority
    )


def test_pending_safety_dimension_is_deferred_without_changing_completeness() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            safety_profile=safety(
                allergy=CollectionStatus.UNKNOWN,
                medications=CollectionStatus.UNKNOWN,
            ),
        )
    )

    selection = select_gap(
        completeness,
        pending_safety_dimensions=(InquiryDimension.ALLERGY_STATUS,),
    )

    assert InquiryDimension.ALLERGY_STATUS in completeness.missing_required
    assert selection.selected_dimension is InquiryDimension.MEDICATION_STATUS
    assert selection.deferred_dimensions == (InquiryDimension.ALLERGY_STATUS,)


@pytest.mark.asyncio
async def test_only_pending_safety_gap_returns_no_question_but_gate_stays_failed() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            safety_profile=safety(allergy=CollectionStatus.UNKNOWN),
        )
    )

    selection = select_gap(
        completeness,
        pending_safety_dimensions=(InquiryDimension.ALLERGY_STATUS,),
    )
    outcome = await compose_question(
        completeness_result=completeness,
        pending_safety_dimensions=(InquiryDimension.ALLERGY_STATUS,),
    )

    assert completeness.disposition is CompletenessDisposition.INCOMPLETE
    assert completeness.gate_result.decision is GateDecision.FAILED
    assert completeness.missing_required == (InquiryDimension.ALLERGY_STATUS,)
    assert selection.disposition is GapSelectionDisposition.NO_SELECTION
    assert selection.deferred_dimensions == (InquiryDimension.ALLERGY_STATUS,)
    assert outcome.status is QuestionCompositionStatus.NO_QUESTION


def test_chief_complaint_priority_beats_ten_question_required_gap() -> None:
    facts = (
        fact("chief_complaint.category", "respiratory"),
        fact("present_illness.change", "stable"),
        fact("ten_questions.cold_heat", "none"),
        fact("ten_questions.sleep", "normal"),
        fact("patient.sex", "male"),
    )
    completeness = evaluate_completeness_policy(policy_input(*facts))

    selection = select_gap(completeness)

    assert InquiryDimension.CHIEF_COMPLAINT_SYMPTOM in completeness.missing_required
    assert InquiryDimension.TEN_RESPIRATORY in completeness.missing_required
    assert selection.selected_dimension is InquiryDimension.CHIEF_COMPLAINT_SYMPTOM


def test_input_order_and_duplicate_dimensions_do_not_change_selection() -> None:
    base = complete_general_facts()
    duplicate = fact("ten_questions.sleep", "normal")
    missing_course = tuple(item for item in base if item.fact_key != "chief_complaint.course")
    first = select_gap(evaluate_completeness_policy(policy_input(*missing_course, duplicate)))
    second = select_gap(evaluate_completeness_policy(policy_input(*(tuple(reversed(missing_course)) + (duplicate,)))))

    assert first.selected_dimension is InquiryDimension.BASIC_COURSE
    assert second.selected_dimension is InquiryDimension.BASIC_COURSE
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_conflict_path_selects_single_conflicting_dimension_by_priority() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(
            *complete_general_facts(),
            fact("chief_complaint.category", "pain"),
            fact("ten_questions.sleep", "poor"),
        )
    )

    selection = select_gap(completeness)

    assert completeness.disposition is CompletenessDisposition.CONFLICT
    assert {item.dimension for item in completeness.conflicting_dimensions} == {
        InquiryDimension.CHIEF_COMPLAINT_CATEGORY,
        InquiryDimension.TEN_SLEEP,
    }
    assert selection.selection_kind is GapSelectionKind.CONFLICT
    assert selection.selected_dimension is InquiryDimension.CHIEF_COMPLAINT_CATEGORY


def test_no_selection_paths_never_choose_optional_or_blocked_questions() -> None:
    ready = select_gap(evaluate_completeness_policy(policy_input(*complete_general_facts())))
    stagnated = select_gap(
        evaluate_completeness_policy(
            policy_input(
                *complete_general_facts(),
                progress=CompletenessProgress(no_new_facts_rounds=2),
            )
        )
    )
    triage_blocked = select_gap(
        evaluate_completeness_policy(policy_input(*complete_general_facts(), triage=blocked_triage()))
    )

    for selection in (ready, stagnated, triage_blocked):
        assert selection.disposition is GapSelectionDisposition.NO_SELECTION
        assert selection.selection_kind is GapSelectionKind.NONE
        assert selection.selected_dimension is None
    assert ready.source_completeness_disposition == "ready"
    assert stagnated.source_completeness_disposition == "stagnated"
    assert triage_blocked.source_completeness_disposition == "triage_blocked"


def test_completeness_mismatch_and_state_version_mismatch_are_fixed_rejections() -> None:
    ready = evaluate_completeness_policy(policy_input(*complete_general_facts()))
    mismatched_state = ready.model_copy(update={"input_state_version": 99})
    mismatched_details = ready.model_copy(
        update={"covered_dimensions": ready.covered_dimensions + (InquiryDimension.PAST_HISTORY,)}
    )

    for payload in (mismatched_state, mismatched_details):
        with pytest.raises(GapSelectionInputError) as exc_info:
            select_gap(payload)
        assert exc_info.value.code is GapSelectionFailureCode.COMPLETENESS_RESULT_MISMATCH
        assert str(exc_info.value) == "GAP_SELECTION_COMPLETENESS_RESULT_MISMATCH"


def test_constructed_copy_subclass_and_hidden_authority_fields_are_rejected() -> None:
    ready = evaluate_completeness_policy(policy_input(*complete_general_facts()))
    constructed = ready.model_construct(schema_version="wrong", **ready.model_dump(exclude={"schema_version"}))
    copied = ready.model_copy(update={"next_gap": "safety.allergy_status"})

    class ForgedCompletenessResult(type(ready)):
        model_config = ConfigDict(frozen=True, extra="allow")

    hidden = ForgedCompletenessResult.model_validate({**ready.model_dump(mode="json"), "route": "reasoning"})

    with pytest.raises(GapSelectionInputError) as constructed_exc:
        select_gap(constructed)
    assert constructed_exc.value.code is GapSelectionFailureCode.INPUT_SCHEMA_INVALID

    for payload in (copied, hidden):
        with pytest.raises(GapSelectionInputError) as exc_info:
            select_gap(payload)
        assert exc_info.value.code is GapSelectionFailureCode.AUTHORITY_FIELD_FORBIDDEN


def test_unregistered_selectable_dimension_private_seam_is_fixed_rejection() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    registry_without_symptom = FrozenGapPriorityRegistry(
        tuple(
            rule
            for _, rule in GAP_PRIORITY_RULES.items()
            if rule.dimension is not InquiryDimension.CHIEF_COMPLAINT_SYMPTOM
        )
    )

    with pytest.raises(GapSelectionInputError) as exc_info:
        _select_gap_with_priority_registry(completeness, registry_without_symptom)

    assert exc_info.value.code is GapSelectionFailureCode.UNREGISTERED_DIMENSION


def test_public_select_gap_does_not_accept_replacement_priority_registry() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    registry = FrozenGapPriorityRegistry(tuple(GAP_PRIORITY_RULES.values()))

    with pytest.raises(TypeError):
        select_gap(completeness, priority_registry=registry)  # type: ignore[call-arg]


def test_public_select_gap_uses_private_authority_even_if_export_is_rebound() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(*missing_symptom_facts(), safety_profile=safety(allergy=CollectionStatus.UNKNOWN))
    )
    original_export = gap_selector.GAP_PRIORITY_RULES
    try:
        gap_selector.GAP_PRIORITY_RULES = FrozenGapPriorityRegistry(
            (
                *tuple(
                    rule
                    for _, rule in original_export.items()
                    if rule.dimension is not InquiryDimension.CHIEF_COMPLAINT_SYMPTOM
                ),
                original_export[InquiryDimension.CHIEF_COMPLAINT_SYMPTOM].model_copy(update={"required_priority": 1}),
            )
        )
        selection = select_gap(completeness)
    finally:
        gap_selector.GAP_PRIORITY_RULES = original_export

    assert selection.selected_dimension is InquiryDimension.CHIEF_COMPLAINT_SYMPTOM


@pytest.mark.asyncio
async def test_forged_selected_source_ready_stagnated_or_triage_blocked_cannot_generate_question() -> None:
    ready = evaluate_completeness_policy(policy_input(*complete_general_facts()))
    forged = GapSelectionResult(
        input_state_version=ready.input_state_version,
        disposition=GapSelectionDisposition.SELECTED,
        selected_dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        selection_kind=GapSelectionKind.REQUIRED,
        priority_rule_id="gap.priority.chief_complaint.symptom.v1",
        source_completeness_disposition="ready",
    )

    outcome = await compose_question(completeness_result=ready, selection=forged)

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.SELECTION_AUTHORITY_MISMATCH

    for completeness in (
        evaluate_completeness_policy(
            policy_input(*complete_general_facts(), progress=CompletenessProgress(no_new_facts_rounds=2))
        ),
        evaluate_completeness_policy(policy_input(*complete_general_facts(), triage=blocked_triage())),
    ):
        forged_blocked = forged.model_copy(update={"source_completeness_disposition": completeness.disposition.value})
        blocked_outcome = await compose_question(completeness_result=completeness, selection=forged_blocked)
        assert blocked_outcome.status is QuestionCompositionStatus.FAILED
        assert blocked_outcome.failure_code is QuestionComposerFailureCode.SELECTION_AUTHORITY_MISMATCH


@pytest.mark.asyncio
async def test_forged_selected_dimension_or_priority_rule_is_rejected() -> None:
    completeness = evaluate_completeness_policy(
        policy_input(*missing_symptom_facts(), safety_profile=safety(allergy=CollectionStatus.UNKNOWN))
    )
    authority = select_gap(completeness)
    forged_dimension = authority.model_copy(
        update={
            "selected_dimension": InquiryDimension.ALLERGY_STATUS,
            "priority_rule_id": "gap.priority.safety.allergy_status.v1",
        }
    )
    forged_rule = authority.model_copy(update={"priority_rule_id": "gap.priority.safety.allergy_status.v1"})

    for forged in (forged_dimension, forged_rule):
        outcome = await compose_question(completeness_result=completeness, selection=forged)
        assert outcome.status is QuestionCompositionStatus.FAILED
        assert outcome.failure_code is QuestionComposerFailureCode.SELECTION_AUTHORITY_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden_update",
    [
        {"route": "ready"},
        {"force": True},
        {"next_gap": "ten_questions.sleep"},
        {"route": "ready", "force": True, "next_gap": "ten_questions.sleep"},
        {"metadata": {"route": "ready"}},
        {"metadata": [{"force": True}]},
        {"metadata": ({"next_gap": "ten_questions.sleep"},)},
    ],
)
async def test_supplied_selection_hidden_authority_fields_are_rejected_before_question(
    hidden_update: dict[str, Any],
) -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    authoritative = select_gap(completeness)
    forged = authoritative.model_copy(update=hidden_update)
    gateway = fallback_gateway()

    outcome = await compose_question(
        completeness_result=completeness,
        selection=forged,
        runtime=AgentRuntime(gateway, recorder=None),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.SELECTION_AUTHORITY_FIELD_FORBIDDEN
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_supplied_selection_subclass_with_authority_field_is_rejected() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    authoritative = select_gap(completeness)

    class ForgedGapSelection(GapSelectionResult):
        model_config = ConfigDict(frozen=True, extra="allow")

    forged = ForgedGapSelection.model_validate({**authoritative.model_dump(mode="json"), "route": "ready"})
    gateway = fallback_gateway()

    outcome = await compose_question(
        completeness_result=completeness,
        selection=forged,
        runtime=AgentRuntime(gateway, recorder=None),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.SELECTION_AUTHORITY_FIELD_FORBIDDEN
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_supplied_constructed_selection_invalid_combination_is_rejected() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    forged = GapSelectionResult.model_construct(
        input_state_version=7,
        disposition=GapSelectionDisposition.NO_SELECTION,
        selected_dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        selection_kind=GapSelectionKind.REQUIRED,
        priority_rule_id="gap.priority.chief_complaint.symptom.v1",
        source_completeness_disposition="incomplete",
    )
    gateway = fallback_gateway()

    outcome = await compose_question(
        completeness_result=completeness,
        selection=forged,
        runtime=AgentRuntime(gateway, recorder=None),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.SELECTION_INPUT_INVALID
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_normal_supplied_selection_and_omitted_selection_continue_to_work() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    authoritative = select_gap(completeness)

    supplied = await compose_question(completeness_result=completeness, selection=authoritative)
    omitted = await compose_question(completeness_result=completeness)

    assert supplied.status is QuestionCompositionStatus.SUCCEEDED
    assert omitted.status is QuestionCompositionStatus.SUCCEEDED
    assert supplied.result == omitted.result


@pytest.mark.asyncio
async def test_template_hit_generates_one_question_and_zero_model_requests() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    gateway = fallback_gateway("unused？")

    outcome = await compose_question(
        completeness_result=completeness,
        runtime=AgentRuntime(gateway, recorder=None),
    )

    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.schema_version == QUESTION_RESULT_SCHEMA_VERSION
    assert outcome.result.selected_dimension is InquiryDimension.CHIEF_COMPLAINT_SYMPTOM
    assert outcome.result.selection_kind is GapSelectionKind.REQUIRED
    assert outcome.result.source is QuestionSource.TEMPLATE
    assert outcome.result.template_version == "question-template-registry.v1"
    assert outcome.result.question.count("？") == 1
    assert gateway.actual_request_count == 0
    # 纯模板命中（不调模型）不是退化
    assert outcome.degraded is False
    assert outcome.last_failure_code is None
    assert outcome.failure_code is None


@pytest.mark.asyncio
async def test_no_selection_does_not_generate_question_or_call_model() -> None:
    completeness = evaluate_completeness_policy(policy_input(*complete_general_facts()))
    gateway = fallback_gateway("unused？")

    outcome = await compose_question(
        completeness_result=completeness,
        runtime=AgentRuntime(gateway, recorder=None),
    )

    assert outcome.status is QuestionCompositionStatus.NO_QUESTION
    assert outcome.result is None
    assert gateway.actual_request_count == 0


def test_template_registry_covers_current_required_and_conflict_dimensions() -> None:
    required_dimensions = {
        rule.dimension for _, rule in GAP_PRIORITY_RULES.items() if rule.required_priority is not None
    }
    conflict_dimensions = {
        rule.dimension for _, rule in GAP_PRIORITY_RULES.items() if rule.conflict_priority is not None
    }

    assert all((dimension, GapSelectionKind.REQUIRED) in QUESTION_TEMPLATES for dimension in required_dimensions)
    assert all((dimension, GapSelectionKind.CONFLICT) in QUESTION_TEMPLATES for dimension in conflict_dimensions)


@pytest.mark.asyncio
async def test_public_composer_does_not_accept_replacement_template_registry_or_force_fallback() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    selection = select_gap(completeness)
    gateway = fallback_gateway()

    with pytest.raises(TypeError):
        await compose_question(  # type: ignore[call-arg]
            completeness_result=completeness,
            runtime=AgentRuntime(gateway, recorder=None),
            run_spec=build_run_spec(selection),
            template_registry=FrozenQuestionTemplateRegistry(()),
        )
    outcome = await compose_question(
        completeness_result=completeness,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
    )
    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.source is QuestionSource.MODEL
    assert gateway.actual_request_count == 1
    # 模型直接成功不是退化
    assert outcome.degraded is False
    assert outcome.last_failure_code is None


@pytest.mark.asyncio
async def test_invalid_model_wording_falls_back_to_validated_template() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    selection = select_gap(completeness)
    gateway = fallback_gateway("这不是一个合规问题。")

    outcome = await compose_question(
        completeness_result=completeness,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
    )

    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.source is QuestionSource.TEMPLATE
    assert gateway.actual_request_count == 1
    # 模型软失败回模板：携带退化信号 + last_failure_code（越界文案 → SINGLE_QUESTION_INVALID）
    assert outcome.degraded is True
    assert outcome.last_failure_code is QuestionComposerFailureCode.SINGLE_QUESTION_INVALID
    assert outcome.failure_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code, expected_failure_code",
    [
        (RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID, QuestionComposerFailureCode.MODEL_OUTPUT_INVALID),
        (RuntimeErrorCode.OUTPUT_SCHEMA_INVALID, QuestionComposerFailureCode.MODEL_OUTPUT_INVALID),
        (RuntimeErrorCode.AGENT_SPEC_VERSION_MISMATCH, QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH),
        (RuntimeErrorCode.INPUT_SCHEMA_INVALID, QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH),
        (RuntimeErrorCode.MODEL_INPUT_PRIVACY_VIOLATION, QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH),
        (RuntimeErrorCode.ATTEMPT_BUDGET_EXHAUSTED, QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH),
    ],
)
async def test_model_result_attributes_runtime_errors_by_code(
    error_code: RuntimeErrorCode,
    expected_failure_code: QuestionComposerFailureCode,
) -> None:
    """0a-1 归因 bug 回归：RuntimeErrorBase 必须按 exc.code 分桶，不再一律 MODEL_OUTPUT_INVALID。

    这些 error_code 均不在软失败回模板白名单内（仅 MODEL_UNAVAILABLE / OUTPUT_INVALID /
    SINGLE_QUESTION_INVALID 回模板），故 outcome 为 FAILED，failure_code 是精确归因结果。
    用空 registry 确保即便误判也不会回模板，outcome 归因直接暴露。
    """
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    gateway = FakeGateway([RuntimeErrorBase(error_code, "simulated runtime failure")])

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=FrozenQuestionTemplateRegistry(()),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is expected_failure_code
    assert outcome.degraded is False
    assert outcome.last_failure_code is None


@pytest.mark.asyncio
async def test_model_unavailable_falls_back_to_template_with_degraded_signal() -> None:
    """网关类 RuntimeErrorBase → MODEL_UNAVAILABLE → 软失败回模板，退化留痕 last_failure_code。"""
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    gateway = FakeGateway(
        [RuntimeErrorBase(RuntimeErrorCode.MODEL_GATEWAY_UNAVAILABLE, "gateway unavailable")]
    )

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=QUESTION_TEMPLATES,
    )

    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.source is QuestionSource.TEMPLATE
    assert outcome.degraded is True
    assert outcome.last_failure_code is QuestionComposerFailureCode.MODEL_UNAVAILABLE


@pytest.mark.asyncio
async def test_structured_output_invalid_falls_back_to_template_with_output_invalid_signal() -> None:
    """STRUCTURED_OUTPUT_INVALID → MODEL_OUTPUT_INVALID → 软失败回模板。"""
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    gateway = FakeGateway(
        [RuntimeErrorBase(RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID, "parse failed")]
    )

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=QUESTION_TEMPLATES,
    )

    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.source is QuestionSource.TEMPLATE
    assert outcome.degraded is True
    assert outcome.last_failure_code is QuestionComposerFailureCode.MODEL_OUTPUT_INVALID


def test_question_context_rejects_identity_dimensions() -> None:
    with pytest.raises(ValidationError):
        QuestionComposerClinicalFact(fact_key="patient.age", value="42")


@pytest.mark.asyncio
async def test_template_key_dimension_or_kind_mismatch_is_fixed_failure_without_model_call() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    selection = select_gap(completeness)
    gateway = fallback_gateway()
    wrong_dimension = QuestionTemplate(
        template_id="question.template.required.forged.v1",
        dimension=InquiryDimension.TEN_SLEEP,
        selection_kind=GapSelectionKind.REQUIRED,
        question="请问您最近睡眠情况怎样？",
    )
    wrong_kind = QuestionTemplate(
        template_id="question.template.conflict.forged.v1",
        dimension=InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        selection_kind=GapSelectionKind.CONFLICT,
        question="请您澄清一下：主要不舒服目前以哪个说法为准？",
    )

    for template in (wrong_dimension, wrong_kind):
        outcome = await question_composer._compose_question_with_template_registry(
            selection=selection,
            runtime=AgentRuntime(gateway, recorder=None),
            run_spec=build_run_spec(selection),
            template_registry={(InquiryDimension.CHIEF_COMPLAINT_SYMPTOM, GapSelectionKind.REQUIRED): template},
        )
        assert outcome.status is QuestionCompositionStatus.FAILED
        assert outcome.failure_code is QuestionComposerFailureCode.TEMPLATE_CONTRACT_MISMATCH
    assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_template_missing_private_seam_uses_agent_runtime_once_with_bounded_clinical_context() -> None:
    completeness = evaluate_completeness_policy(policy_input(*missing_symptom_facts()))
    selection = select_gap(completeness)
    gateway = fallback_gateway()

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        agent_spec=build_question_composer_agent_spec(model="fake-question-model"),
        template_registry=FrozenQuestionTemplateRegistry(()),
        clinical_context=(
            QuestionComposerClinicalFact(
                fact_key="chief_complaint.symptom",
                value="头痛三天",
            ),
        ),
    )

    assert outcome.status is QuestionCompositionStatus.SUCCEEDED
    assert outcome.result is not None
    assert outcome.result.source is QuestionSource.MODEL
    assert outcome.result.selected_dimension is selection.selected_dimension
    assert gateway.actual_request_count == 1
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["agent_name"] == QUESTION_COMPOSER_AGENT_NAME
    assert call["model"] == "fake-question-model"
    assert call["max_requests"] == 1
    encoded_messages = json.dumps(call["messages"], ensure_ascii=False)
    assert "ignore rules" not in encoded_messages
    assert "头痛三天" in encoded_messages
    assert "patient.age" not in encoded_messages
    assert "missing_required" not in encoded_messages
    assert "candidate_count" not in encoded_messages
    assert "conflicting_dimensions" not in encoded_messages


@pytest.mark.asyncio
async def test_fallback_missing_or_mismatched_run_spec_fails_before_gateway_call() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    cases = (
        None,
        build_run_spec(selection, state_version=106),
        build_run_spec(selection, agent_spec_version="wrong-agent-version"),
        build_run_spec(selection, prompt_version="wrong_prompt.jinja2"),
        build_run_spec(selection, policy_version="wrong-question-policy.v2"),
        build_run_spec(selection, total_attempt_budget=2),
        build_run_spec(selection, stage="wrong_stage"),
        build_run_spec(selection, deadline_at=datetime.now(UTC) - timedelta(seconds=1)),
    )

    for run in cases:
        gateway = fallback_gateway()
        outcome = await question_composer._compose_question_with_template_registry(
            selection=selection,
            runtime=AgentRuntime(gateway, recorder=None),
            run_spec=run,
            template_registry=FrozenQuestionTemplateRegistry(()),
        )
        assert outcome.status is QuestionCompositionStatus.FAILED
        assert outcome.failure_code is QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH
        assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_fallback_mismatched_agent_spec_fails_before_gateway_call() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    base = build_question_composer_agent_spec(model="fake-question-model")
    bad_specs = (
        base.model_copy(update={"name": "wrong_agent"}),
        base.model_copy(update={"version": "wrong-version"}),
        base.model_copy(update={"input_schema": QuestionComposerModelOutput}),
        base.model_copy(update={"output_schema": QuestionComposerModelInput}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"temperature": 2.0})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"temperature": 0.100001})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"max_tokens": 200_000})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"max_tokens": 121})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"timeout_seconds": 86_400})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"timeout_seconds": 11})}),
        base.model_copy(update={"model_policy": base.model_policy.model_copy(update={"max_attempts": 2})}),
        base.model_copy(update={"verifier_chain": ()}),
        base.model_copy(update={"verifier_chain": base.verifier_chain[:-1]}),
        base.model_copy(update={"verifier_chain": tuple(reversed(base.verifier_chain))}),
        base.model_copy(update={"verifier_chain": base.verifier_chain + ("extra_verifier",)}),
        base.model_copy(
            update={
                "failure_policy": FailurePolicy(retryable_codes=frozenset({RuntimeErrorCode.MODEL_GATEWAY_TIMEOUT}))
            }
        ),
        base.model_copy(update={"tool_permissions": frozenset({Capability.READ_STATE, Capability.READ_EVIDENCE})}),
        base.model_copy(update={"tool_permissions": frozenset()}),
        base.model_construct(
            name=QUESTION_COMPOSER_AGENT_NAME,
            version=QUESTION_COMPOSER_AGENT_VERSION,
            input_schema=QuestionComposerModelInput,
            output_schema=QuestionComposerModelOutput,
            model_policy=base.model_policy,
            tool_permissions=frozenset({Capability.READ_STATE, Capability.WRITE_STATE}),
            verifier_chain=base.verifier_chain,
            failure_policy=FailurePolicy(),
        ),
    )

    for spec in bad_specs:
        gateway = fallback_gateway()
        outcome = await question_composer._compose_question_with_template_registry(
            selection=selection,
            runtime=AgentRuntime(gateway, recorder=None),
            run_spec=build_run_spec(selection, agent_spec_version=spec.version),
            agent_spec=spec,
            template_registry=FrozenQuestionTemplateRegistry(()),
        )
        assert outcome.status is QuestionCompositionStatus.FAILED
        assert outcome.failure_code is QuestionComposerFailureCode.RUNTIME_CONTRACT_MISMATCH
        assert gateway.actual_request_count == 0


@pytest.mark.asyncio
async def test_model_cannot_override_selected_dimension_or_emit_authority_fields() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    gateway = FakeGateway(
        [
            {
                "schema_version": QUESTION_MODEL_OUTPUT_SCHEMA_VERSION,
                "question": "请问您这次主要不舒服是什么？",
                "selected_dimension": "ten_questions.sleep",
                "route": "ready",
                "next_gap": "none",
            }
        ]
    )

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=FrozenQuestionTemplateRegistry(()),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.MODEL_OUTPUT_INVALID
    assert gateway.actual_request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "请问您这次主要不舒服是什么？另外睡眠怎样？",
        "请问您的姓名是什么？",
        "请问您是否可以进入下一阶段？",
        "请问您这次主要不舒服是什么？还有多久了？",
        "What is your phone number?",
        "What is your full name?",
        "Please provide your ID number?",
        "What is your outpatient number?",
        "What is your medical record number?",
        "What is your home address?",
        "What is your phone_number?",
        "What is your full-name?",
        "Please provide your identity.card?",
        "What is your mobile   number?",
        "请问您的名字是什么？",
        "请问您的联系方式是什么？",
        "请问您的住院号是什么？",
    ],
)
async def test_model_multi_question_identity_connector_and_authority_text_are_rejected(question: str) -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    gateway = fallback_gateway(question)

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=FrozenQuestionTemplateRegistry(()),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.SINGLE_QUESTION_INVALID


def test_one_natural_question_may_use_a_clinical_connector() -> None:
    assert validate_single_question_text("请补充患者咳嗽和发热目前分别是什么情况？") is None


def test_normal_chinese_templates_continue_to_pass_single_question_validation() -> None:
    for template in QUESTION_TEMPLATES.values():
        assert validate_single_question_text(template.question) is None


def test_question_templates_address_the_doctor_about_the_patient() -> None:
    for template in QUESTION_TEMPLATES.values():
        assert "患者" in template.question
        assert "请问您" not in template.question


@pytest.mark.asyncio
async def test_constructed_model_output_with_hidden_fields_is_rejected() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    hidden_output = QuestionComposerModelOutput(
        schema_version=QUESTION_MODEL_OUTPUT_SCHEMA_VERSION,
        question="请问您这次主要不舒服是什么？",
    ).model_copy(update={"ready": True})
    gateway = FakeGateway([hidden_output])

    outcome = await question_composer._compose_question_with_template_registry(
        selection=selection,
        runtime=AgentRuntime(gateway, recorder=None),
        run_spec=build_run_spec(selection),
        template_registry=FrozenQuestionTemplateRegistry(()),
    )

    assert outcome.status is QuestionCompositionStatus.FAILED
    assert outcome.failure_code is QuestionComposerFailureCode.MODEL_OUTPUT_INVALID


def test_authority_selection_template_registry_and_result_are_deeply_immutable() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    template = QUESTION_TEMPLATES[(InquiryDimension.CHIEF_COMPLAINT_SYMPTOM, GapSelectionKind.REQUIRED)]

    with pytest.raises(ValidationError):
        selection.selected_dimension = InquiryDimension.TEN_SLEEP
    with pytest.raises(TypeError):
        QUESTION_TEMPLATES[(InquiryDimension.CHIEF_COMPLAINT_SYMPTOM, GapSelectionKind.REQUIRED)] = template
    with pytest.raises(TypeError):
        QUESTION_TEMPLATES._templates = ()
    with pytest.raises(ValidationError):
        template.question = "请问睡眠怎样？"


def test_no_module_level_mutable_priority_or_template_backing_dicts() -> None:
    gap_dicts = [
        name for name, value in vars(gap_selector).items() if not name.startswith("__") and isinstance(value, dict)
    ]
    question_dicts = [
        name for name, value in vars(question_composer).items() if not name.startswith("__") and isinstance(value, dict)
    ]

    assert gap_dicts == []
    assert question_dicts == []


def test_output_does_not_contain_clinical_text_identity_prompt_or_raw_model_output() -> None:
    selection = select_gap(evaluate_completeness_policy(policy_input(*missing_symptom_facts())))
    template = QUESTION_TEMPLATES[(selection.selected_dimension, selection.selection_kind)]

    encoded = json.dumps(
        {
            "selection": selection.model_dump(mode="json"),
            "question": template.question,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "Alice" not in encoded
    assert "13800138000" not in encoded
    assert "ignore rules" not in encoded
    assert "raw_model_output" not in encoded
    assert "头痛" not in encoded
    assert "prompt" not in encoded.lower()
