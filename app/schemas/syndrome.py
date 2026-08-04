"""Strict L4-1 Syndrome Draft contracts.

These DTOs describe a read-only model draft and its authoritative input
projection.  They do not authorize routing, formula generation, safety
decisions, doctor review, or persistence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.reducer import DomainState
from app.schemas.completeness import InquiryDimension
from app.schemas.domain import GateResultSchema, ObservationStatus

SYNDROME_DRAFT_SCHEMA_VERSION: Literal["syndrome-draft.v1"] = "syndrome-draft.v1"
SYNDROME_INPUT_SCHEMA_VERSION: Literal["syndrome-draft-input.v1"] = "syndrome-draft-input.v1"
SYNDROME_EVIDENCE_MODE: Literal["model_knowledge_only"] = "model_knowledge_only"
SYNDROME_RAG_EVIDENCE_MODE: Literal["rag_retrieved"] = "rag_retrieved"
SYNDROME_EVIDENCE_MODE_T = Literal["model_knowledge_only", "rag_retrieved"]
SYNDROME_POLICY_VERSION: Literal["syndrome-draft-policy.no-rag.v1"] = "syndrome-draft-policy.no-rag.v1"
SYNDROME_RAG_POLICY_VERSION: Literal["syndrome-draft-policy.rag.v1"] = "syndrome-draft-policy.rag.v1"
SYNDROME_POLICY_VERSION_T = Literal["syndrome-draft-policy.no-rag.v1", "syndrome-draft-policy.rag.v1"]
SYNDROME_READY_STAGE: Literal["READY_FOR_REASONING"] = "READY_FOR_REASONING"
SYNDROME_NO_RAG_CONFIDENCE_MAX: float = 0.65
# RAG 模式置信度上限：有检索证据时允许的最高自评置信度。
SYNDROME_RAG_CONFIDENCE_MAX: float = 0.9
# RAG 模式但检索结果为空（降级）时的置信度上限——缺证提示下不允许过度自信。
SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX: float = 0.5


class SyndromeDraftDecision(StrEnum):
    COMPLETED = "completed"
    NEEDS_MORE_INFO = "needs_more_info"
    ABSTAINED = "abstained"


class SyndromeFactClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=500)
    fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_fact_ids(self) -> SyndromeFactClaim:
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("fact_ids must be unique")
        return self


class SyndromeClaimEvidenceLink(BaseModel):
    """Evidence-link shape.

    no-RAG 契约要求集合为空；RAG 契约（syndrome-draft-policy.rag.v1）允许
    非空，且每条 ``evidence_id`` 必须命中本次检索证据集合（verifier 校验，
    防幻觉引用）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=128)


class SyndromeDraft(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": SYNDROME_DRAFT_SCHEMA_VERSION},
    )

    schema_version: Literal["syndrome-draft.v1"] = SYNDROME_DRAFT_SCHEMA_VERSION
    decision: SyndromeDraftDecision
    syndrome: str | None = Field(default=None, min_length=1, max_length=200)
    syndrome_basis: tuple[SyndromeFactClaim, ...] = Field(default=(), max_length=16)
    differential: tuple[SyndromeFactClaim, ...] = Field(default=(), max_length=16)
    treatment_principle: str | None = Field(default=None, min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    evidence_mode: SYNDROME_EVIDENCE_MODE_T = SYNDROME_EVIDENCE_MODE
    claim_evidence_links: tuple[SyndromeClaimEvidenceLink, ...] = Field(default=(), max_length=16)
    missing_inputs: tuple[InquiryDimension, ...] = Field(default=(), max_length=16)
    review_required: bool = True

    @model_validator(mode="after")
    def unique_missing_inputs(self) -> SyndromeDraft:
        if len(self.missing_inputs) != len(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be unique")
        return self


class SyndromeObservationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: UUID
    session_id: UUID
    state_version: int = Field(ge=1)
    fact_key: str = Field(min_length=1, max_length=128)
    value: Any | None = None
    normalized_value: Any | None = None
    status: Literal[ObservationStatus.ACTIVE] = ObservationStatus.ACTIVE


class SyndromeDraftInput(BaseModel):
    """Authoritative pre-model input projection for L4-1."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": SYNDROME_INPUT_SCHEMA_VERSION},
    )

    schema_version: Literal["syndrome-draft-input.v1"] = SYNDROME_INPUT_SCHEMA_VERSION
    session_id: UUID
    state_version: int = Field(ge=1)
    current_stage: Literal["READY_FOR_REASONING"] = SYNDROME_READY_STAGE
    policy_version: SYNDROME_POLICY_VERSION_T = SYNDROME_POLICY_VERSION
    domain_state: DomainState
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema
    context_observations: tuple[SyndromeObservationContext, ...] = Field(default=(), max_length=512)
    # 医师否决反馈（reject 后重新辨证时注入）：None=首次开方
    review_feedback: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def input_consistency(self) -> SyndromeDraftInput:
        if self.domain_state.session_id != self.session_id or self.domain_state.state_version != self.state_version:
            raise ValueError("domain_state must match input session and state_version")
        if any(item.session_id != self.session_id or item.state_version != self.state_version for item in self.context_observations):
            raise ValueError("context observations must match input session and state_version")
        ids = [item.observation_id for item in self.context_observations]
        if len(ids) != len(set(ids)):
            raise ValueError("context observation ids must be unique")
        return self
