"""Public contracts for the high-risk safety-fact confirmation boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAFETY_ASSERTION_SCHEMA_VERSION = "safety-fact-assertion.v1"


class SafetyAssertionStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class SafetyFactField(StrEnum):
    ALLERGY = "allergy"
    PREGNANCY = "pregnancy"
    LACTATION = "lactation"
    MEDICATIONS = "medications"
    MAJOR_CONDITIONS = "major_conditions"
    CONTRAINDICATIONS = "contraindications"
    RED_FLAG = "red_flag"


class SafetyEvidenceRef(BaseModel):
    """Verifiable evidence coordinates without copying raw clinical text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_message_id: UUID
    start_char: int = Field(ge=0, le=4_000)
    end_char: int = Field(gt=0, le=4_000)
    quote_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reply_to_question_message_id: UUID | None = None
    reply_dimension: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )

    @model_validator(mode="after")
    def reply_binding_is_complete(self) -> SafetyEvidenceRef:
        if (self.reply_to_question_message_id is None) != (self.reply_dimension is None):
            raise ValueError("reply binding requires both question id and dimension")
        return self


class SafetyFactAssertionRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SAFETY_ASSERTION_SCHEMA_VERSION
    assertion_id: UUID
    session_id: UUID
    field_name: SafetyFactField
    value: dict[str, Any]
    value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: SafetyAssertionStatus
    source_kind: str
    source_message_id: UUID
    extraction_run_id: UUID | None = None
    template_version: str
    evidence_spans: tuple[SafetyEvidenceRef, ...]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_at: datetime
    confirmed_at: datetime | None = None
    rejected_at: datetime | None = None
    retracted_at: datetime | None = None
    superseded_at: datetime | None = None
    supersedes_assertion_id: UUID | None = None


class SafetyFactAssertionList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[SafetyFactAssertionRead, ...]


class SafetyAssertionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")


__all__ = [
    "SAFETY_ASSERTION_SCHEMA_VERSION",
    "SafetyAssertionDecisionRequest",
    "SafetyAssertionStatus",
    "SafetyEvidenceRef",
    "SafetyFactAssertionList",
    "SafetyFactAssertionRead",
    "SafetyFactField",
]
