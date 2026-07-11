"""Strict L3-3 completeness policy contracts.

These DTOs describe a deterministic projection of Domain State plus the
authoritative triage gate.  They do not authorize state writes, graph
transitions, repository commits, question generation, or model calls.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.domain import CollectionStatus, GateDecision, ObservationStatus
from app.schemas.triage import TriageGateResult

COMPLETENESS_INPUT_SCHEMA_VERSION: Literal["completeness-input.v1"] = "completeness-input.v1"
COMPLETENESS_RESULT_SCHEMA_VERSION: Literal["completeness-result.v1"] = "completeness-result.v1"
COMPLETENESS_POLICY_VERSION: Literal["completeness-policy.v1"] = "completeness-policy.v1"
COMPLETENESS_GATE_NAME: Literal["completeness"] = "completeness"


class _CompletenessModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": COMPLETENESS_RESULT_SCHEMA_VERSION},
    )


class InquiryDimension(StrEnum):
    CHIEF_COMPLAINT_SYMPTOM = "chief_complaint.symptom"
    CHIEF_COMPLAINT_CATEGORY = "chief_complaint.category"
    BASIC_COURSE = "chief_complaint.course"
    PRESENT_ILLNESS_CHANGE = "present_illness.change"
    TEN_COLD_HEAT = "ten_questions.cold_heat"
    TEN_SWEAT = "ten_questions.sweat"
    TEN_HEAD_BODY = "ten_questions.head_body"
    TEN_STOOL_URINE = "ten_questions.stool_urine"
    TEN_DIET = "ten_questions.diet"
    TEN_CHEST_ABDOMEN = "ten_questions.chest_abdomen"
    TEN_THIRST = "ten_questions.thirst"
    TEN_SLEEP = "ten_questions.sleep"
    TEN_MENSES_LEUKORRHEA = "ten_questions.menses_leukorrhea"
    TEN_PAIN = "ten_questions.pain"
    TEN_RESPIRATORY = "ten_questions.respiratory"
    ALLERGY_STATUS = "safety.allergy_status"
    MEDICATION_STATUS = "safety.medication_status"
    MAJOR_CONDITION_STATUS = "safety.major_condition_status"
    PREGNANCY_STATUS = "safety.pregnancy_status"
    LACTATION_STATUS = "safety.lactation_status"
    PAST_HISTORY = "past_history"
    FOUR_DIAGNOSIS = "four_diagnosis"
    PATIENT_SEX = "patient.sex"
    PATIENT_AGE = "patient.age"
    MENOPAUSE_STATUS = "patient.menopause_status"
    PREGNANCY_APPLICABILITY_FLAG = "patient.pregnancy_applicability"
    LACTATION_APPLICABILITY_FLAG = "patient.lactation_applicability"


class CompletenessDisposition(StrEnum):
    READY = "ready"
    INCOMPLETE = "incomplete"
    CONFLICT = "conflict"
    STAGNATED = "stagnated"
    TRIAGE_BLOCKED = "triage_blocked"


class ApplicabilityStatus(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class StagnationReasonCode(StrEnum):
    NO_NEW_FACTS_THRESHOLD = "no_new_facts_threshold"
    MAX_FOLLOWUP_ROUNDS = "max_followup_rounds"


class CompletenessObservationFact(_CompletenessModel):
    """Non-text projection of a Domain State observation."""

    observation_id: UUID
    session_id: UUID
    fact_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    value_fingerprint: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    normalized_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_.:-]+$",
    )
    status: ObservationStatus = ObservationStatus.ACTIVE
    supersedes_observation_id: UUID | None = None

    @model_validator(mode="after")
    def validate_status_relation(self) -> CompletenessObservationFact:
        has_relation = self.supersedes_observation_id is not None
        if (self.status is ObservationStatus.ACTIVE and has_relation) or (
            self.status is not ObservationStatus.ACTIVE and not has_relation
        ):
            raise ValueError("active observations have no relation; updates require one")
        if self.supersedes_observation_id == self.observation_id:
            raise ValueError("an observation cannot supersede itself")
        return self


class CompletenessSafetyProfile(_CompletenessModel):
    """Safety collection-state projection without clinical text values."""

    session_id: UUID
    allergy_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    allergen_count: int = Field(default=0, ge=0, le=64)
    pregnancy_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    lactation_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    medications_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    medication_count: int = Field(default=0, ge=0, le=64)
    major_conditions_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    major_condition_count: int = Field(default=0, ge=0, le=64)
    contraindications_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    contraindication_count: int = Field(default=0, ge=0, le=64)

    @model_validator(mode="after")
    def validate_collection_counts(self) -> CompletenessSafetyProfile:
        for status, count in (
            (self.allergy_collection_status, self.allergen_count),
            (self.medications_collection_status, self.medication_count),
            (self.major_conditions_collection_status, self.major_condition_count),
            (self.contraindications_collection_status, self.contraindication_count),
        ):
            if status is CollectionStatus.COLLECTED and count < 1:
                raise ValueError("collected list safety fields require a positive count")
            if status is not CollectionStatus.COLLECTED and count != 0:
                raise ValueError("non-collected list safety fields must not carry counts")
        return self


class CompletenessDomainSnapshot(_CompletenessModel):
    """Strict input projection built from the current Domain State version."""

    session_id: UUID
    state_version: int = Field(ge=1)
    observations: tuple[CompletenessObservationFact, ...] = Field(default=(), max_length=512)
    safety_profile: CompletenessSafetyProfile | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> CompletenessDomainSnapshot:
        if any(item.session_id != self.session_id for item in self.observations):
            raise ValueError("observation session mismatch")
        if self.safety_profile is not None and self.safety_profile.session_id != self.session_id:
            raise ValueError("safety profile session mismatch")
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("duplicate observation id")
        return self


class CompletenessProgress(_CompletenessModel):
    """Explicit progress counters supplied by L3-5, not persistence authority."""

    no_new_facts_rounds: int = Field(default=0, ge=0, le=100)
    followup_rounds: int = Field(default=0, ge=0, le=100)


class CompletenessPolicyInput(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": COMPLETENESS_INPUT_SCHEMA_VERSION},
    )

    schema_version: Literal["completeness-input.v1"] = COMPLETENESS_INPUT_SCHEMA_VERSION
    input_state_version: int = Field(ge=1)
    domain_snapshot: CompletenessDomainSnapshot
    triage_gate: TriageGateResult
    progress: CompletenessProgress = Field(default_factory=CompletenessProgress)

    @model_validator(mode="after")
    def state_version_matches_snapshot(self) -> CompletenessPolicyInput:
        if self.input_state_version != self.domain_snapshot.state_version:
            raise ValueError("input_state_version must match domain snapshot")
        return self


class CompletenessDimensionRule(_CompletenessModel):
    rule_id: str = Field(min_length=1, max_length=96)
    dimension: InquiryDimension
    fact_keys: tuple[str, ...] = Field(default=())
    required_by_default: bool = False
    optional_report: bool = False


class CompletenessRuleOutcome(_CompletenessModel):
    rule_id: str = Field(min_length=1, max_length=96)
    dimension: InquiryDimension
    required: bool
    covered: bool
    conflicting_value_count: int = Field(ge=0)


class CompletenessConflict(_CompletenessModel):
    dimension: InquiryDimension
    rule_id: str = Field(min_length=1, max_length=96)
    current_value_count: int = Field(ge=2)


class CompletenessStagnationResult(_CompletenessModel):
    stagnated: bool
    manual_handoff_required: bool
    no_new_facts_rounds: int = Field(ge=0)
    followup_rounds: int = Field(ge=0)
    reason_codes: tuple[StagnationReasonCode, ...] = Field(default=())


class CompletenessApplicabilityResult(_CompletenessModel):
    pregnancy: ApplicabilityStatus
    lactation: ApplicabilityStatus


class CompletenessGateDetails(_CompletenessModel):
    disposition: CompletenessDisposition
    covered_dimensions: tuple[InquiryDimension, ...] = Field(default=())
    missing_required: tuple[InquiryDimension, ...] = Field(default=())
    missing_optional: tuple[InquiryDimension, ...] = Field(default=())
    conflicting_dimensions: tuple[CompletenessConflict, ...] = Field(default=())
    rule_ids: tuple[str, ...] = Field(default=())
    rule_outcomes: tuple[CompletenessRuleOutcome, ...] = Field(default=())
    stagnation: CompletenessStagnationResult
    applicability: CompletenessApplicabilityResult
    triage_disposition: str | None = Field(default=None, max_length=64)


class CompletenessGateResult(_CompletenessModel):
    gate_name: Literal["completeness"] = COMPLETENESS_GATE_NAME
    policy_version: Literal["completeness-policy.v1"] = COMPLETENESS_POLICY_VERSION
    input_state_version: int = Field(ge=1)
    decision: GateDecision
    details: CompletenessGateDetails


class CompletenessPolicyResult(_CompletenessModel):
    schema_version: Literal["completeness-result.v1"] = COMPLETENESS_RESULT_SCHEMA_VERSION
    disposition: CompletenessDisposition
    policy_version: Literal["completeness-policy.v1"] = COMPLETENESS_POLICY_VERSION
    input_state_version: int = Field(ge=1)
    covered_dimensions: tuple[InquiryDimension, ...] = Field(default=())
    missing_required: tuple[InquiryDimension, ...] = Field(default=())
    missing_optional: tuple[InquiryDimension, ...] = Field(default=())
    conflicting_dimensions: tuple[CompletenessConflict, ...] = Field(default=())
    stagnation: CompletenessStagnationResult
    gate_result: CompletenessGateResult
    rule_outcomes: tuple[CompletenessRuleOutcome, ...] = Field(default=())

    @model_validator(mode="after")
    def gate_matches_result(self) -> CompletenessPolicyResult:
        if (
            self.gate_result.gate_name != COMPLETENESS_GATE_NAME
            or self.gate_result.policy_version != self.policy_version
            or self.gate_result.input_state_version != self.input_state_version
        ):
            raise ValueError("completeness gate metadata must match result metadata")
        if self.gate_result.details.disposition is not self.disposition:
            raise ValueError("completeness gate details must carry the result disposition")
        decision_by_disposition = {
            CompletenessDisposition.READY: GateDecision.PASSED,
            CompletenessDisposition.INCOMPLETE: GateDecision.FAILED,
            CompletenessDisposition.CONFLICT: GateDecision.FAILED,
            CompletenessDisposition.STAGNATED: GateDecision.BLOCKED,
            CompletenessDisposition.TRIAGE_BLOCKED: GateDecision.BLOCKED,
        }
        if self.gate_result.decision is not decision_by_disposition[self.disposition]:
            raise ValueError("completeness gate decision does not match disposition")
        return self
