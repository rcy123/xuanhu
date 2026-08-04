"""Strict L4-2 Formula Draft contracts.

These DTOs describe a read-only model draft that merges the legacy
PrescriptionAgent and ModificationAgent into a single model call producing
base_formula, modifications and candidate_formula at once.

They do not authorize routing, safety decisions, doctor review, or
persistence, and they carry no RAG/citation/source semantics.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_runtime.reducer import DomainState
from app.schemas.domain import GateResultSchema
from app.schemas.syndrome import SyndromeDraft, SyndromeObservationContext

FORMULA_DRAFT_SCHEMA_VERSION: Literal["formula-draft.v1"] = "formula-draft.v1"
FORMULA_INPUT_SCHEMA_VERSION: Literal["formula-draft-input.v1"] = "formula-draft-input.v1"
FORMULA_EVIDENCE_MODE: Literal["model_knowledge_only"] = "model_knowledge_only"
FORMULA_RAG_EVIDENCE_MODE: Literal["rag_retrieved"] = "rag_retrieved"
FORMULA_EVIDENCE_MODE_T = Literal["model_knowledge_only", "rag_retrieved"]
FORMULA_POLICY_VERSION: Literal["formula-draft-policy.no-rag.v1"] = "formula-draft-policy.no-rag.v1"
FORMULA_RAG_POLICY_VERSION: Literal["formula-draft-policy.rag.v1"] = "formula-draft-policy.rag.v1"
FORMULA_POLICY_VERSION_T = Literal[
    "formula-draft-policy.no-rag.v1",
    "formula-draft-policy.rag.v1",
    "base-formula-draft-policy.no-rag.v1",
    "base-formula-draft-policy.rag.v1",
    "modification-draft-policy.no-rag.v1",
    "modification-draft-policy.rag.v1",
]
FORMULA_READY_STAGE: Literal["READY_FOR_FORMULA"] = "READY_FOR_FORMULA"
FORMULA_NO_RAG_CONFIDENCE_MAX: float = 0.65
# RAG 模式置信度上限：有检索证据时允许的最高自评置信度。
FORMULA_RAG_CONFIDENCE_MAX: float = 0.9
# RAG 模式但检索结果为空（降级）时的置信度上限——缺证提示下不允许过度自信。
FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX: float = 0.5


class FormulaDraftDecision(StrEnum):
    COMPLETED = "completed"
    NEEDS_MORE_INFO = "needs_more_info"
    ABSTAINED = "abstained"


class ModificationAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    DOSE_ADJUST = "dose_adjust"


class HerbItem(BaseModel):
    """A single ordered herb in a formula composition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    herb: str = Field(min_length=1, max_length=64)
    dose: float | None = Field(default=None, gt=0, le=500)
    unit: str = Field(default="g", min_length=1, max_length=8)
    note: str | None = Field(default=None, max_length=200)


class FormulaFactClaim(BaseModel):
    """A fact-linked basis claim supporting a formula choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=500)
    fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_fact_ids(self) -> FormulaFactClaim:
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("fact_ids must be unique")
        return self


class FormulaComposition(BaseModel):
    """A base or candidate formula with ordered composition and basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    composition: tuple[HerbItem, ...] = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1, max_length=1000)
    basis: tuple[FormulaFactClaim, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_herbs(self) -> FormulaComposition:
        names = [item.herb for item in self.composition]
        if len(names) != len(set(names)):
            raise ValueError("composition herbs must be unique")
        return self


class FormulaModification(BaseModel):
    """A single modification action with mandatory fact/syndrome basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ModificationAction
    herb: str = Field(min_length=1, max_length=64)
    dose: float | None = Field(default=None, gt=0, le=500)
    unit: str = Field(default="g", min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=500)
    basis: FormulaFactClaim


class FormulaClaimEvidenceLink(BaseModel):
    """Evidence-link shape.

    no-RAG 契约要求集合为空；RAG 契约（formula-draft-policy.rag.v1）允许
    非空，且每条 ``evidence_id`` 必须命中本次检索证据集合（verifier 校验，
    防幻觉引用）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=128)


class FormulaDraft(BaseModel):
    """Strong-typed formula draft output from a single model call."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": FORMULA_DRAFT_SCHEMA_VERSION},
    )

    schema_version: Literal["formula-draft.v1"] = FORMULA_DRAFT_SCHEMA_VERSION
    decision: FormulaDraftDecision
    base_formula: FormulaComposition | None = None
    modifications: tuple[FormulaModification, ...] = Field(default=(), max_length=32)
    candidate_formula: FormulaComposition | None = None
    rationale: str | None = Field(default=None, min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_mode: FORMULA_EVIDENCE_MODE_T = FORMULA_EVIDENCE_MODE
    claim_evidence_links: tuple[FormulaClaimEvidenceLink, ...] = Field(default=(), max_length=16)
    missing_inputs: tuple[str, ...] = Field(default=(), max_length=16)
    review_required: bool = True

    @model_validator(mode="after")
    def unique_missing_inputs(self) -> FormulaDraft:
        if len(self.missing_inputs) != len(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be unique")
        return self


class FormulaDraftInput(BaseModel):
    """Authoritative pre-model input projection for L4-2.

    Internal runtime projection carrying the sealed upstream SyndromeDraft
    together with the same authority bundle used by L4-1.  The public Formula
    entry replaces all clinical and authority fields before model execution.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": FORMULA_INPUT_SCHEMA_VERSION},
    )

    schema_version: Literal["formula-draft-input.v1"] = FORMULA_INPUT_SCHEMA_VERSION
    session_id: UUID
    state_version: int = Field(ge=1)
    current_stage: Literal["READY_FOR_FORMULA"] = FORMULA_READY_STAGE
    policy_version: FORMULA_POLICY_VERSION_T = FORMULA_POLICY_VERSION
    domain_state: DomainState
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema
    context_observations: tuple[SyndromeObservationContext, ...] = Field(default=(), max_length=512)
    syndrome_draft: SyndromeDraft
    # 医师否决反馈（reject 后重新开方时注入）：None=首次开方
    review_feedback: str | None = Field(default=None, max_length=2_000)
    # 知识库修正提示（开方预检发现未收录药时注入重试输入）：None=无
    knowledge_correction: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def input_consistency(self) -> FormulaDraftInput:
        if self.domain_state.session_id != self.session_id or self.domain_state.state_version != self.state_version:
            raise ValueError("domain_state must match input session and state_version")
        if any(
            item.session_id != self.session_id or item.state_version != self.state_version
            for item in self.context_observations
        ):
            raise ValueError("context observations must match input session and state_version")
        ids = [item.observation_id for item in self.context_observations]
        if len(ids) != len(set(ids)):
            raise ValueError("context observation ids must be unique")
        # Note: syndrome_draft decision and treatment_principle are validated
        # by the FormulaVerifier preflight (_verify_upstream_syndrome), not
        # by the input schema.  This ensures a forged non-completed draft is
        # rejected with SYNDROME_DRAFT_INVALID / TREATMENT_PRINCIPLE_MISSING
        # rather than INPUT_SCHEMA_INVALID.
        return self


# ---------------------------------------------------------------------------
# 2.8 两阶段开方（B 方案）：base_formula_draft → modification_draft
# ---------------------------------------------------------------------------

BASE_FORMULA_POLICY_VERSION: Literal["base-formula-draft-policy.no-rag.v1"] = "base-formula-draft-policy.no-rag.v1"
BASE_FORMULA_RAG_POLICY_VERSION: Literal["base-formula-draft-policy.rag.v1"] = "base-formula-draft-policy.rag.v1"
BASE_FORMULA_PROMPT_VERSION = "base_formula_draft_v1.jinja2"
BASE_FORMULA_RAG_PROMPT_VERSION = "base_formula_draft_rag_v1.jinja2"
MODIFICATION_DRAFT_POLICY_VERSION: Literal["modification-draft-policy.no-rag.v1"] = "modification-draft-policy.no-rag.v1"
MODIFICATION_DRAFT_RAG_POLICY_VERSION: Literal["modification-draft-policy.rag.v1"] = "modification-draft-policy.rag.v1"
MODIFICATION_DRAFT_PROMPT_VERSION = "modification_draft_v1.jinja2"
MODIFICATION_DRAFT_RAG_PROMPT_VERSION = "modification_draft_rag_v1.jinja2"
BASE_FORMULA_AGENT_NAME = "base_formula_draft"
BASE_FORMULA_RAG_AGENT_NAME = "base_formula_draft_rag"
BASE_FORMULA_AGENT_VERSION = "base-formula-draft-agent.v1"
MODIFICATION_DRAFT_AGENT_NAME = "modification_draft"
MODIFICATION_DRAFT_RAG_AGENT_NAME = "modification_draft_rag"
MODIFICATION_DRAFT_AGENT_VERSION = "modification-draft-agent.v1"
BASE_FORMULA_DRAFT_SCHEMA_VERSION: Literal["base-formula-draft.v1"] = "base-formula-draft.v1"
MODIFICATION_DRAFT_SCHEMA_VERSION: Literal["modification-draft.v1"] = "modification-draft.v1"
MODIFICATION_DRAFT_INPUT_SCHEMA_VERSION: Literal["modification-draft-input.v1"] = "modification-draft-input.v1"


class BaseFormulaDraft(BaseModel):
    """阶段 1 输出：仅生成基础方（选方），不做加减。

    与单步 FormulaDraft 的 base_formula 部分同构；decision 语义：
    - COMPLETED：base_formula 必填（basis 含事实/证候依据）
    - NEEDS_MORE_INFO：missing_inputs 说明还缺什么
    - ABSTAINED：放弃（不应发生，保守校验）
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": BASE_FORMULA_DRAFT_SCHEMA_VERSION},
    )

    schema_version: Literal["base-formula-draft.v1"] = BASE_FORMULA_DRAFT_SCHEMA_VERSION
    decision: FormulaDraftDecision
    base_formula: FormulaComposition | None = None
    rationale: str | None = Field(default=None, min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_mode: FORMULA_EVIDENCE_MODE_T = FORMULA_EVIDENCE_MODE
    claim_evidence_links: tuple[FormulaClaimEvidenceLink, ...] = Field(default=(), max_length=16)
    missing_inputs: tuple[str, ...] = Field(default=(), max_length=16)
    review_required: bool = True

    @model_validator(mode="after")
    def completed_requires_base(self) -> BaseFormulaDraft:
        if self.decision is FormulaDraftDecision.COMPLETED and self.base_formula is None:
            raise ValueError("completed base formula draft requires base_formula")
        if len(self.missing_inputs) != len(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be unique")
        return self


class ModificationDraft(BaseModel):
    """阶段 2 输出：基于权威基础方做加减（输入必含 base_formula 全文）。

    校验器以传入的 base_formula composition 为唯一真源检查
    REMOVE/DOSE_ADJUST 的 target 存在性 —— 结构性杜绝
    MODIFICATION_TARGET_MISSING 类自相矛盾输出。
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": MODIFICATION_DRAFT_SCHEMA_VERSION},
    )

    schema_version: Literal["modification-draft.v1"] = MODIFICATION_DRAFT_SCHEMA_VERSION
    decision: FormulaDraftDecision
    modifications: tuple[FormulaModification, ...] = Field(default=(), max_length=32)
    rationale: str | None = Field(default=None, min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_mode: FORMULA_EVIDENCE_MODE_T = FORMULA_EVIDENCE_MODE
    claim_evidence_links: tuple[FormulaClaimEvidenceLink, ...] = Field(default=(), max_length=16)
    missing_inputs: tuple[str, ...] = Field(default=(), max_length=16)
    review_required: bool = True

    @model_validator(mode="after")
    def unique_missing_inputs(self) -> ModificationDraft:
        if len(self.missing_inputs) != len(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be unique")
        return self


class ModificationDraftInput(BaseModel):
    """阶段 2 输入：FormulaDraftInput 全部字段 + 权威基础方全文。

    ``base_formula`` 来自阶段 1 已提交的权威产物（非模型自述），
    加减模型必须对照该 composition 逐味操作。
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={"x-schema-version": MODIFICATION_DRAFT_INPUT_SCHEMA_VERSION},
    )

    schema_version: Literal["modification-draft-input.v1"] = MODIFICATION_DRAFT_INPUT_SCHEMA_VERSION
    session_id: UUID
    state_version: int = Field(ge=1)
    current_stage: Literal["READY_FOR_FORMULA"] = FORMULA_READY_STAGE
    policy_version: FORMULA_POLICY_VERSION_T = FORMULA_POLICY_VERSION
    domain_state: DomainState
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema
    context_observations: tuple[SyndromeObservationContext, ...] = Field(default=(), max_length=512)
    syndrome_draft: SyndromeDraft
    base_formula: FormulaComposition
    base_formula_rationale: str | None = Field(default=None, max_length=1000)
    # 医师否决反馈（reject 后重新开方时注入）：None=首次开方
    review_feedback: str | None = Field(default=None, max_length=2_000)
    # 知识库修正提示（开方预检发现未收录药时注入重试输入）：None=无
    knowledge_correction: str | None = Field(default=None, max_length=500)
    base_confidence: float = Field(ge=0, le=1)
