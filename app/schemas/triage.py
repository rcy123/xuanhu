"""Strict L3-2 triage policy contracts.

These DTOs are deterministic policy inputs/outputs.  They do not authorize
state writes, graph transitions, repository commits, or model calls.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.domain import GateDecision
from app.schemas.intake import RedFlagCandidate

TRIAGE_INPUT_SCHEMA_VERSION: Literal["triage-input.v1"] = "triage-input.v1"
TRIAGE_RESULT_SCHEMA_VERSION: Literal["triage-result.v1"] = "triage-result.v1"
TRIAGE_POLICY_VERSION: Literal["triage-red-flag.v1"] = "triage-red-flag.v1"
TRIAGE_GATE_NAME: Literal["triage"] = "triage"


class _TriageModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": TRIAGE_RESULT_SCHEMA_VERSION},
    )


class TriageDisposition(StrEnum):
    CONTINUE = "continue"
    # 0d-3：有风险但不到紧急——不阻断（decision=PASSED）、留痕、继续问诊。
    RISK_NOTE = "risk_note"
    EMERGENCY_REFERRAL = "emergency_referral"
    MANUAL_REVIEW = "manual_review"


class TriagePolicyInput(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": TRIAGE_INPUT_SCHEMA_VERSION},
    )

    schema_version: Literal["triage-input.v1"] = TRIAGE_INPUT_SCHEMA_VERSION
    input_state_version: int = Field(ge=1)
    red_flag_candidates: tuple[RedFlagCandidate, ...] = Field(default=(), max_length=16)


class TriageRuleOutcome(_TriageModel):
    rule_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    disposition: TriageDisposition
    candidate_count: int = Field(ge=1)
    source_message_ids: tuple[str, ...] = Field(default=())


class TriageCategoryCount(_TriageModel):
    category: str = Field(min_length=1, max_length=64)
    candidate_count: int = Field(ge=1)


class TriageGateDetails(_TriageModel):
    disposition: TriageDisposition
    candidate_count: int = Field(ge=0)
    category_counts: tuple[TriageCategoryCount, ...] = Field(default=())
    rule_ids: tuple[str, ...] = Field(default=())
    rules: tuple[TriageRuleOutcome, ...] = Field(default=())
    source_message_ids: tuple[str, ...] = Field(default=())
    # 0d-3 风险三级化：none（无风险）/ noted（有风险·不阻断·留痕）/ emergency（重大风险·阻断）。
    risk_level: str = Field(default="none", pattern=r"^(none|noted|emergency)$")


class TriageGateResult(_TriageModel):
    gate_name: Literal["triage"] = TRIAGE_GATE_NAME
    policy_version: Literal["triage-red-flag.v1"] = TRIAGE_POLICY_VERSION
    input_state_version: int = Field(ge=1)
    decision: GateDecision
    details: TriageGateDetails


class TriagePolicyResult(_TriageModel):
    schema_version: Literal["triage-result.v1"] = TRIAGE_RESULT_SCHEMA_VERSION
    disposition: TriageDisposition
    policy_version: Literal["triage-red-flag.v1"] = TRIAGE_POLICY_VERSION
    input_state_version: int = Field(ge=1)
    gate_result: TriageGateResult
    rule_outcomes: tuple[TriageRuleOutcome, ...] = Field(default=())

    @model_validator(mode="after")
    def gate_matches_result(self) -> TriagePolicyResult:
        if (
            self.gate_result.gate_name != TRIAGE_GATE_NAME
            or self.gate_result.policy_version != self.policy_version
            or self.gate_result.input_state_version != self.input_state_version
        ):
            raise ValueError("triage gate metadata must match result metadata")
        if self.gate_result.details.disposition is not self.disposition:
            raise ValueError("triage gate details must carry the result disposition")
        return self
