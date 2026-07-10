"""Serializable L2 domain-state contracts.

They describe facts and metadata only; safety decisions remain owned by the
deterministic SafetyRuleEngine, not by model-produced data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservationStatus(StrEnum):
    ACTIVE = "active"
    CORRECTED = "corrected"
    RETRACTED = "retracted"


class CollectionStatus(StrEnum):
    UNKNOWN = "unknown"
    EXPLICITLY_NONE = "explicitly_none"
    COLLECTED = "collected"


class ArtifactStatus(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    STALE = "stale"


class GateDecision(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class PregnancyValue(StrEnum):
    PREGNANT = "pregnant"
    NOT_PREGNANT = "not_pregnant"
    POSSIBLE = "possible"


class LactationValue(StrEnum):
    LACTATING = "lactating"
    NOT_LACTATING = "not_lactating"


class ObservationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: UUID
    session_id: UUID
    fact_key: str = Field(min_length=1, max_length=128)
    value: Any | None = None
    normalized_value: Any | None = None
    source_message_id: UUID
    status: ObservationStatus = ObservationStatus.ACTIVE
    confidence: float | None = Field(default=None, ge=0, le=1)
    supersedes_observation_id: UUID | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_status_relation(self) -> ObservationSchema:
        has_relation = self.supersedes_observation_id is not None
        if (self.status is ObservationStatus.ACTIVE and has_relation) or (
            self.status is not ObservationStatus.ACTIVE and not has_relation
        ):
            raise ValueError("active observations have no relation; corrected/retracted observations require one")
        if self.supersedes_observation_id == self.observation_id:
            raise ValueError("an observation cannot supersede itself")
        return self


class SafetyProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    allergy_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    allergens: list[str] | None = None
    pregnancy_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    pregnancy_value: PregnancyValue | None = None
    lactation_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    lactation_value: LactationValue | None = None
    medications_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    medications: list[str] | None = None
    major_conditions_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    major_conditions: list[str] | None = None
    contraindications_collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    contraindications: list[str] | None = None

    @model_validator(mode="after")
    def validate_collection_values(self) -> SafetyProfileSchema:
        for status_name, value_name in (
            ("allergy_collection_status", "allergens"),
            ("medications_collection_status", "medications"),
            ("major_conditions_collection_status", "major_conditions"),
            ("contraindications_collection_status", "contraindications"),
        ):
            status = getattr(self, status_name)
            value = getattr(self, value_name)
            if status is CollectionStatus.COLLECTED and not value:
                raise ValueError(f"{value_name} must be non-empty when collected")
            if status is not CollectionStatus.COLLECTED and value is not None:
                raise ValueError(f"{value_name} must be null unless collected")
        for status_name, value_name in (
            ("pregnancy_collection_status", "pregnancy_value"),
            ("lactation_collection_status", "lactation_value"),
        ):
            status = getattr(self, status_name)
            value = getattr(self, value_name)
            if (status is CollectionStatus.COLLECTED) != (value is not None):
                raise ValueError(f"{value_name} is required exactly when collected")
        return self


class ArtifactRevisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    artifact_type: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    session_id: UUID
    input_state_version: int = Field(ge=1)
    status: ArtifactStatus
    produced_by_run_id: UUID
    parent_revision_id: UUID | None = None
    parent_revision: int | None = Field(default=None, ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_parent_relation(self) -> ArtifactRevisionSchema:
        if self.revision == 1 and (self.parent_revision_id is not None or self.parent_revision is not None):
            raise ValueError("revision 1 has no parent")
        if self.revision > 1 and (self.parent_revision_id is None or self.parent_revision != self.revision - 1):
            raise ValueError("later revisions require their immediately preceding parent revision")
        return self


class GateResultSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_name: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    input_state_version: int = Field(ge=1)
    decision: GateDecision
    details: dict[str, Any] | None = None


class GraphRunMetadataSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    session_id: UUID
    graph_version: str = Field(min_length=1, max_length=64)
    command_id: str = Field(min_length=1, max_length=128)
    input_state_version: int = Field(ge=1)
