"""Strict, versioned candidate contracts for L3-1 intake extraction.

These DTOs are model products, not authoritative domain state.  They contain no
route, stage transition, readiness, safety approval, database key, or timestamp.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.domain import CollectionStatus, LactationValue, PregnancyValue

INTAKE_SCHEMA_VERSION = "intake-extraction.v1"


class _IntakeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": INTAKE_SCHEMA_VERSION},
    )
    schema_version: ClassVar[str] = INTAKE_SCHEMA_VERSION


class IntakeMessageRole(StrEnum):
    PATIENT = "patient"
    ASSISTANT = "assistant"


class IntakeMessage(_IntakeModel):
    message_id: UUID
    role: IntakeMessageRole
    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content must not be blank")
        return value


class ActiveObservationContext(_IntakeModel):
    """Minimal active-fact context; it may only support dedup/correction."""

    observation_id: UUID
    fact_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: Any | None = None
    normalized_value: Any | None = None

    @model_validator(mode="after")
    def has_value(self) -> ActiveObservationContext:
        if self.value is None and self.normalized_value is None:
            raise ValueError("an active fact requires a value")
        _ensure_json_safe(self.value)
        _ensure_json_safe(self.normalized_value)
        return self


class IntakeExtractionInput(_IntakeModel):
    current_messages: tuple[IntakeMessage, ...] = Field(min_length=1, max_length=8)
    historical_active_facts: tuple[ActiveObservationContext, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def current_patient_messages_only(self) -> IntakeExtractionInput:
        if any(message.role is not IntakeMessageRole.PATIENT for message in self.current_messages):
            raise ValueError("only current patient messages are allowed")
        message_ids = [message.message_id for message in self.current_messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("current message ids must be unique")
        observation_ids = [fact.observation_id for fact in self.historical_active_facts]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("historical observation ids must be unique")
        return self


class IntakeExtractionDecision(StrEnum):
    EXTRACTED = "extracted"
    NEEDS_CLARIFICATION = "needs_clarification"
    ABSTAINED = "abstained"


class ObservationOperation(StrEnum):
    ADD = "add"
    CORRECT = "correct"
    RETRACT = "retract"


class ObservationDelta(_IntakeModel):
    fact_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: Any | None = None
    normalized_value: Any | None = None
    source_message_id: UUID
    confidence: float = Field(ge=0, le=1)
    operation: ObservationOperation = ObservationOperation.ADD
    target_observation_id: UUID | None = None

    @model_validator(mode="after")
    def valid_operation(self) -> ObservationDelta:
        if not math.isfinite(self.confidence):
            raise ValueError("confidence must be finite")
        _ensure_json_safe(self.value)
        _ensure_json_safe(self.normalized_value)
        if self.operation is ObservationOperation.ADD:
            if self.target_observation_id is not None:
                raise ValueError("add cannot target a historical observation")
            if self.value is None and self.normalized_value is None:
                raise ValueError("add requires a value")
        elif self.operation is ObservationOperation.CORRECT:
            if self.target_observation_id is None:
                raise ValueError("correction requires a historical target")
            if self.value is None and self.normalized_value is None:
                raise ValueError("correction requires a value")
        else:
            if self.target_observation_id is None:
                raise ValueError("retraction requires a historical target")
            if self.value is not None or self.normalized_value is not None:
                raise ValueError("retraction cannot carry a value")
        return self


class SafetyListDelta(_IntakeModel):
    status: CollectionStatus = CollectionStatus.UNKNOWN
    values: tuple[str, ...] | None = Field(default=None, max_length=32)
    source_message_id: UUID | None = None

    @field_validator("values")
    @classmethod
    def clean_values(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(item.strip() for item in value)
        if any(not item or len(item) > 200 for item in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("safety values must be unique, non-blank, and bounded")
        return cleaned

    @model_validator(mode="after")
    def status_matches_values(self) -> SafetyListDelta:
        if self.status is CollectionStatus.COLLECTED:
            if not self.values or self.source_message_id is None:
                raise ValueError("collected requires values and provenance")
        elif self.status is CollectionStatus.EXPLICITLY_NONE:
            if self.values is not None or self.source_message_id is None:
                raise ValueError("explicitly_none requires provenance and no values")
        elif self.values is not None or self.source_message_id is not None:
            raise ValueError("unknown cannot carry values or provenance")
        return self


class PregnancyDelta(_IntakeModel):
    status: CollectionStatus = CollectionStatus.UNKNOWN
    value: PregnancyValue | None = None
    source_message_id: UUID | None = None

    @model_validator(mode="after")
    def status_matches_value(self) -> PregnancyDelta:
        _validate_scalar_safety(self.status, self.value, self.source_message_id)
        return self


class LactationDelta(_IntakeModel):
    status: CollectionStatus = CollectionStatus.UNKNOWN
    value: LactationValue | None = None
    source_message_id: UUID | None = None

    @model_validator(mode="after")
    def status_matches_value(self) -> LactationDelta:
        _validate_scalar_safety(self.status, self.value, self.source_message_id)
        return self


class PatientSafetyDelta(_IntakeModel):
    allergy: SafetyListDelta = Field(default_factory=SafetyListDelta)
    pregnancy: PregnancyDelta = Field(default_factory=PregnancyDelta)
    lactation: LactationDelta = Field(default_factory=LactationDelta)
    medications: SafetyListDelta = Field(default_factory=SafetyListDelta)
    major_conditions: SafetyListDelta = Field(default_factory=SafetyListDelta)
    contraindications: SafetyListDelta = Field(default_factory=SafetyListDelta)

    def has_candidate(self) -> bool:
        return any(
            field.status is not CollectionStatus.UNKNOWN
            for field in (
                self.allergy,
                self.pregnancy,
                self.lactation,
                self.medications,
                self.major_conditions,
                self.contraindications,
            )
        )


class RedFlagCategory(StrEnum):
    SEVERE_PAIN = "severe_pain"
    BREATHING_DIFFICULTY = "breathing_difficulty"
    ALTERED_CONSCIOUSNESS = "altered_consciousness"
    SEVERE_BLEEDING = "severe_bleeding"
    NEUROLOGIC_DEFICIT = "neurologic_deficit"
    HIGH_FEVER = "high_fever"
    OTHER = "other"


class CandidateSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RedFlagCandidate(_IntakeModel):
    category: RedFlagCategory
    source_message_id: UUID
    severity: CandidateSeverity
    evidence: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def finite_confidence(self) -> RedFlagCandidate:
        if not math.isfinite(self.confidence) or not self.evidence.strip():
            raise ValueError("red flag candidate values are invalid")
        return self


class AmbiguityCode(StrEnum):
    UNCLEAR_VALUE = "unclear_value"
    CONFLICTING_STATEMENT = "conflicting_statement"
    UNCLEAR_REFERENCE = "unclear_reference"
    UNCERTAIN_NEGATION = "uncertain_negation"
    OTHER = "other"


class Ambiguity(_IntakeModel):
    code: AmbiguityCode
    source_message_id: UUID
    fact_key: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=1, max_length=240)

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ambiguity description must not be blank")
        return value


class IntakeExtractionOutput(_IntakeModel):
    """Candidate-only output.  The five fields are the entire authority surface."""

    decision: IntakeExtractionDecision
    observations: tuple[ObservationDelta, ...] = Field(default=(), max_length=64)
    patient_safety_delta: PatientSafetyDelta = Field(default_factory=PatientSafetyDelta)
    red_flag_candidates: tuple[RedFlagCandidate, ...] = Field(default=(), max_length=16)
    ambiguities: tuple[Ambiguity, ...] = Field(default=(), max_length=16)


def _validate_scalar_safety(status: CollectionStatus, value: object | None, source: UUID | None) -> None:
    if status is CollectionStatus.COLLECTED:
        if value is None or source is None:
            raise ValueError("collected requires a value and provenance")
    elif status is CollectionStatus.EXPLICITLY_NONE:
        if value is not None or source is None:
            raise ValueError("explicitly_none requires provenance and no value")
    elif value is not None or source is not None:
        raise ValueError("unknown cannot carry a value or provenance")


def _ensure_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON value")
        return
    if isinstance(value, list):
        for item in value:
            _ensure_json_safe(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _ensure_json_safe(item)
        return
    raise ValueError("value must be JSON-safe")
