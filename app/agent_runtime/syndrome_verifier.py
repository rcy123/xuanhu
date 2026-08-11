"""Deterministic L4-1 Syndrome Draft preflight and verifier."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.observation_projection import project_current_observations
from app.agent_runtime.reducer import DomainState
from app.agent_runtime.specs import AgentSpec, Capability, RunArtifact, RunSpec, run_artifact_subject_digest
from app.core.config import agent_model_timeout_seconds
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import GateDecision, GateResultSchema, ObservationSchema, ObservationStatus
from app.schemas.syndrome import (
    SYNDROME_DRAFT_SCHEMA_VERSION,
    SYNDROME_EVIDENCE_MODE,
    SYNDROME_INPUT_SCHEMA_VERSION,
    SYNDROME_NO_RAG_CONFIDENCE_MAX,
    SYNDROME_POLICY_VERSION,
    SYNDROME_RAG_CONFIDENCE_MAX,
    SYNDROME_RAG_EVIDENCE_MODE,
    SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX,
    SYNDROME_RAG_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
    SyndromeFactClaim,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION

SYNDROME_AGENT_NAME = "syndrome_draft"
SYNDROME_AGENT_VERSION = "syndrome-draft-agent.v1"
SYNDROME_PROMPT_VERSION = "syndrome_draft_v1.jinja2"
# RAG 模式的 manifest agent key。spec.name/version 保持 SYNDROME_AGENT_NAME/
# _VERSION 不变（_valid_agent_spec 只认固定 name/version），RAG 仅切换 prompt。
SYNDROME_RAG_AGENT_NAME = "syndrome_draft_rag"
SYNDROME_RAG_PROMPT_VERSION = "syndrome_draft_rag_v1.jinja2"
# 合法 policy_version / prompt_version 配对集合（D1：策略级决策，verifier 只认配对）。
SYNDROME_POLICY_PROMPT_PAIRS = frozenset(
    {
        (SYNDROME_POLICY_VERSION, SYNDROME_PROMPT_VERSION),
        (SYNDROME_RAG_POLICY_VERSION, SYNDROME_RAG_PROMPT_VERSION),
    }
)
# Syndrome 综合 AgentSpec 单次模型调用超时上限（s）。必须 > MODEL_GATEWAY_TIMEOUT_SECONDS
# （runtime 前置守卫强制），统一按网关超时 + 余量推导。
SYNDROME_MODEL_TIMEOUT_SECONDS = agent_model_timeout_seconds()
SYNDROME_VERIFIER_CHAIN = (
    "schema",
    "run_provenance",
    "preconditions",
    "fact_links",
    "decision_consistency",
    "no_rag_contract",
    "authority_boundary",
)


class SyndromeVerificationFailureCode(StrEnum):
    SCHEMA_INVALID = "SYNDROME_SCHEMA_INVALID"
    OUTPUT_TYPE_INVALID = "SYNDROME_OUTPUT_TYPE_INVALID"
    AGENT_SPEC_INVALID = "SYNDROME_AGENT_SPEC_INVALID"
    RUN_PROVENANCE_MISMATCH = "SYNDROME_RUN_PROVENANCE_MISMATCH"
    STAGE_NOT_READY = "SYNDROME_STAGE_NOT_READY"
    GATE_INVALID = "SYNDROME_GATE_INVALID"
    RED_FLAG_UNHANDLED = "SYNDROME_RED_FLAG_UNHANDLED"
    FACT_CONFLICT_BLOCKING = "SYNDROME_FACT_CONFLICT_BLOCKING"
    CONTEXT_NOT_ACTIVE = "SYNDROME_CONTEXT_NOT_ACTIVE"
    CONTEXT_PRIVACY_INVALID = "SYNDROME_CONTEXT_PRIVACY_INVALID"
    FACT_LINK_INVALID = "SYNDROME_FACT_LINK_INVALID"
    DECISION_CONTENT_INVALID = "SYNDROME_DECISION_CONTENT_INVALID"
    CONFIDENCE_EXCEEDS_NO_RAG_LIMIT = "SYNDROME_CONFIDENCE_EXCEEDS_NO_RAG_LIMIT"
    NO_RAG_CONTRACT_VIOLATED = "SYNDROME_NO_RAG_CONTRACT_VIOLATED"
    CONFIDENCE_EXCEEDS_RAG_LIMIT = "SYNDROME_CONFIDENCE_EXCEEDS_RAG_LIMIT"
    EVIDENCE_LINK_FABRICATED = "SYNDROME_EVIDENCE_LINK_FABRICATED"
    EVIDENCE_MODE_POLICY_MISMATCH = "SYNDROME_EVIDENCE_MODE_POLICY_MISMATCH"
    AUTHORITY_FIELD_FORBIDDEN = "SYNDROME_AUTHORITY_FIELD_FORBIDDEN"


class SyndromeCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class SyndromeCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: str = Field(min_length=1, max_length=64)
    status: SyndromeCheckStatus
    failure_code: SyndromeVerificationFailureCode | None = None

    @model_validator(mode="after")
    def status_matches_code(self) -> SyndromeCheckResult:
        if (self.status is SyndromeCheckStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failure code must exactly match failed status")
        return self


class SyndromeVerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checks: tuple[SyndromeCheckResult, ...] = Field(min_length=1)
    failure_code: SyndromeVerificationFailureCode | None = None
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deterministic_result(self) -> SyndromeVerificationReport:
        first = next((check.failure_code for check in self.checks if check.failure_code is not None), None)
        if self.passed != (first is None) or self.failure_code is not first:
            raise ValueError("report outcome must match checks")
        return self


class SyndromeGateAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema


class SyndromeOutputBoundaryError(ValueError):
    def __init__(self, code: SyndromeVerificationFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def canonicalize_syndrome_input(input_payload: object) -> SyndromeDraftInput:
    candidate = SyndromeDraftInput.model_validate(input_payload)
    canonical_json = SyndromeDraftInput.__pydantic_serializer__.to_json(candidate, warnings=False)
    canonical = SyndromeDraftInput.model_validate_json(canonical_json)
    if _has_undeclared_fields(input_payload, canonical):
        raise SyndromeOutputBoundaryError(SyndromeVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    return canonical


def canonicalize_syndrome_output(output: object) -> SyndromeDraft:
    try:
        candidate = SyndromeDraft.model_validate(output)
        canonical_json = SyndromeDraft.__pydantic_serializer__.to_json(candidate, warnings=False)
        canonical = SyndromeDraft.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        raise SyndromeOutputBoundaryError(SyndromeVerificationFailureCode.SCHEMA_INVALID) from exc
    if _has_undeclared_fields(output, canonical) or _contains_forbidden_authority_key(output):
        raise SyndromeOutputBoundaryError(SyndromeVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    return canonical


def validate_syndrome_preflight(
    agent_spec: AgentSpec,
    run_spec: RunSpec,
    input_payload: SyndromeDraftInput,
    gate_authority: SyndromeGateAuthority | None = None,
) -> SyndromeVerificationFailureCode | None:
    if not _valid_agent_spec(agent_spec):
        return SyndromeVerificationFailureCode.AGENT_SPEC_INVALID
    if (
        run_spec.agent_spec_version != agent_spec.version
        or (run_spec.policy_version, run_spec.prompt_version) not in SYNDROME_POLICY_PROMPT_PAIRS
        or run_spec.total_attempt_budget != 1
        or run_spec.session_id != input_payload.session_id
        or run_spec.state_version != input_payload.state_version
        or input_payload.policy_version != run_spec.policy_version
    ):
        return SyndromeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    stage_failure = _verify_stage_and_gates(run_spec, input_payload, gate_authority)
    if stage_failure is not None:
        return stage_failure
    context_failure = _verify_context(input_payload)
    if context_failure is not None:
        return context_failure
    if _has_active_conflicts(input_payload.domain_state.observations):
        return SyndromeVerificationFailureCode.FACT_CONFLICT_BLOCKING
    return None


def verify_syndrome_artifact(
    *,
    agent_spec: AgentSpec,
    run_spec: RunSpec,
    artifact: RunArtifact,
    input_payload: SyndromeDraftInput,
    gate_authority: SyndromeGateAuthority | None = None,
) -> SyndromeVerificationReport:
    checks: list[SyndromeCheckResult] = []
    try:
        output = canonicalize_syndrome_output(artifact.output)
    except SyndromeOutputBoundaryError as exc:
        checks.append(_check("schema", exc.code))
        return _report(checks, artifact)

    checks.append(_check("schema", _verify_schema(agent_spec, output)))
    checks.append(_check("run_provenance", _verify_run(agent_spec, run_spec, artifact)))
    checks.append(_check("preconditions", validate_syndrome_preflight(agent_spec, run_spec, input_payload, gate_authority)))
    checks.append(_check("fact_links", _verify_fact_links(output, input_payload)))
    checks.append(_check("decision_consistency", _verify_decision(output, input_payload)))
    checks.append(_check("no_rag_contract", _verify_evidence_contract(output, artifact.evidence_ids, run_spec.policy_version)))
    checks.append(_check("authority_boundary", _verify_authority(output)))
    return _report(checks, artifact)


def _valid_agent_spec(spec: AgentSpec) -> bool:
    policy = spec.model_policy
    return (
        spec.name == SYNDROME_AGENT_NAME
        and spec.version == SYNDROME_AGENT_VERSION
        and spec.input_schema is SyndromeDraftInput
        and spec.output_schema is SyndromeDraft
        and policy.temperature == 0.1
        and policy.max_tokens <= 1_500
        and policy.timeout_seconds <= SYNDROME_MODEL_TIMEOUT_SECONDS
        and policy.max_attempts == 1
        and spec.tool_permissions == frozenset({Capability.READ_STATE})
        and spec.verifier_chain == SYNDROME_VERIFIER_CHAIN
        and not spec.failure_policy.retryable_codes
    )


def _verify_schema(agent_spec: AgentSpec, output: SyndromeDraft) -> SyndromeVerificationFailureCode | None:
    if not _valid_agent_spec(agent_spec):
        return SyndromeVerificationFailureCode.AGENT_SPEC_INVALID
    if type(output) is not SyndromeDraft:
        return SyndromeVerificationFailureCode.OUTPUT_TYPE_INVALID
    if output.schema_version != SYNDROME_DRAFT_SCHEMA_VERSION:
        return SyndromeVerificationFailureCode.SCHEMA_INVALID
    return None


def _verify_run(agent_spec: AgentSpec, run_spec: RunSpec, artifact: RunArtifact) -> SyndromeVerificationFailureCode | None:
    if (
        artifact.run_id != run_spec.run_id
        or artifact.trace_id != run_spec.trace_id
        or artifact.agent_spec_version != agent_spec.version
        or artifact.prompt_version != run_spec.prompt_version
        or artifact.attempts != 1
    ):
        return SyndromeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    return None


def _verify_stage_and_gates(
    run_spec: RunSpec,
    input_payload: SyndromeDraftInput,
    gate_authority: SyndromeGateAuthority | None,
) -> SyndromeVerificationFailureCode | None:
    if input_payload.schema_version != SYNDROME_INPUT_SCHEMA_VERSION or input_payload.current_stage != SYNDROME_READY_STAGE:
        return SyndromeVerificationFailureCode.STAGE_NOT_READY
    if run_spec.stage != SYNDROME_READY_STAGE:
        return SyndromeVerificationFailureCode.STAGE_NOT_READY
    if gate_authority is None:
        return SyndromeVerificationFailureCode.GATE_INVALID
    authority = gate_authority
    if not _same_gate(input_payload.triage_gate, authority.triage_gate):
        return SyndromeVerificationFailureCode.RED_FLAG_UNHANDLED
    if not _same_gate(input_payload.completeness_gate, authority.completeness_gate):
        return SyndromeVerificationFailureCode.GATE_INVALID
    if not _triage_gate_allows_reasoning(authority.triage_gate):
        return SyndromeVerificationFailureCode.RED_FLAG_UNHANDLED
    if not _completeness_gate_allows_reasoning(authority.completeness_gate):
        return SyndromeVerificationFailureCode.GATE_INVALID
    if authority.triage_gate.input_state_version != authority.completeness_gate.input_state_version:
        return SyndromeVerificationFailureCode.GATE_INVALID
    return None


def _verify_context(input_payload: SyndromeDraftInput) -> SyndromeVerificationFailureCode | None:
    # 隐私闸门优先：泄漏身份数据的上下文无论是否匹配权威都要拦下（更严重）。
    if _contains_identity_key_or_value(input_payload.context_observations):
        return SyndromeVerificationFailureCode.CONTEXT_PRIVACY_INVALID
    if not _context_matches_authority(
        input_payload.context_observations,
        input_payload.domain_state,
        session_id=input_payload.session_id,
        state_version=input_payload.state_version,
    ):
        return SyndromeVerificationFailureCode.CONTEXT_NOT_ACTIVE
    return None


def prune_syndrome_fact_links(
    output: SyndromeDraft,
    allowed_ids: set[uuid.UUID],
) -> SyndromeDraft:
    """Deterministically drop fact_ids that are not in the authoritative context.

    真实会话（fc6b6a09 等）复盘：qwen 对长随机 uuid 偶发转写损坏（把
    head_body 槽位 id ``f07f339a-2195-…`` 输出成 ``f07f339a-212c-…``），
    重试不收敛 → 每次辨证都 FACT_LINK_INVALID → manual_required 死路。
    此处把证据引用修剪到权威上下文集合内：引用被剪光的 claim 整条丢弃，
    保留的 claim 至少携带一条有效引用。verifier 的 FACT_LINK_INVALID 兜底
    契约不削弱（剪枝后仍无有效引用的输出照样被拒）。
    """

    def _prune(claims: tuple[SyndromeFactClaim, ...]) -> tuple[SyndromeFactClaim, ...]:
        kept: list[SyndromeFactClaim] = []
        for claim in claims:
            valid = tuple(fact_id for fact_id in claim.fact_ids if fact_id in allowed_ids)
            if not valid:
                continue
            kept.append(claim.model_copy(update={"fact_ids": valid}))
        return tuple(kept)

    new_basis = _prune(output.syndrome_basis)
    new_differential = _prune(output.differential)
    if new_basis == output.syndrome_basis and new_differential == output.differential:
        return output
    return output.model_copy(update={"syndrome_basis": new_basis, "differential": new_differential})


def _context_signature(item: Any) -> tuple[Any, ...]:
    """JSON-safe comparable signature of one context row.

    Accepts either a context model (SyndromeObservationContext / similar) or a
    plain dict produced by ``derive_slot_context_rows``; both shapes appear on
    the two sides of the slot-projection comparison.
    """

    if isinstance(item, dict):
        observation_id = item.get("observation_id")
        session_id = item.get("session_id")
        state_version = item.get("state_version")
        fact_key = item.get("fact_key")
        value = item.get("value")
        status = item.get("status")
    else:
        observation_id = item.observation_id
        session_id = item.session_id
        state_version = item.state_version
        fact_key = item.fact_key
        value = item.value
        status = item.status
    return (
        str(observation_id),
        str(session_id),
        state_version,
        str(fact_key),
        _stable_json(value),
        str(getattr(status, "value", status)),
    )


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError, OverflowError):
        return "<unserializable>"


def _context_matches_authority(
    context_observations: tuple[Any, ...],
    domain_state: DomainState,
    *,
    session_id: uuid.UUID,
    state_version: int,
) -> bool:
    """True when the supplied context is a faithful projection of the domain state.

    Two authoritative derivations are accepted:
    1. plain: the exact active observations (real observation ids);
    2. slot projection (3a / 问题 22): one deterministic row per dimension with a
       stable synthetic uuid5 id — the intake slot path rewrites the context so
       downstream agents never see drifting raw fact keys.
    Both are pure functions of ``domain_state``, so accepting either keeps the
    verifier's security property: the model can never fabricate context facts.

    真实会话（4416ad28）：intake_slot_path_enabled 时 syndrome_draft 的
    ``_authoritative_input`` 用槽位投影重写 context（合成 observation_id），
    而旧 verifier 只认裸键集 → 每次草稿都 CONTEXT_NOT_ACTIVE → advance 后
    manual_required 死路。此处按投影确定性重算并接受。
    """

    active = tuple(_active_observations(domain_state.observations))
    active_by_id = {item.observation_id: item for item in active}
    context_by_id = {item.observation_id: item for item in context_observations}
    if set(active_by_id) == set(context_by_id):
        for item in context_observations:
            source = active_by_id.get(item.observation_id)
            # ``active_by_id`` is the projected current set, so the source is a
            # current semantic fact whether it is an ACTIVE root or a CORRECTED
            # successor.  The supplied context row must still carry the ACTIVE
            # semantic-view status, and the corrected successor's value is the
            # only value a faithful context may carry.
            if (
                source is None
                or source.session_id != session_id
                or item.status != ObservationStatus.ACTIVE
                or item.state_version != state_version
                or item.fact_key != source.fact_key
                or item.value != source.value
                or item.normalized_value != source.normalized_value
            ):
                return False
        return True
    try:
        from app.agent_runtime.completeness_policy import COMPLETENESS_DIMENSION_RULES
        from app.agent_runtime.intake_dimension_mapping import derive_slot_context_rows
    except ImportError:
        return False
    rows = derive_slot_context_rows(
        domain_state.observations,
        dimensions=frozenset(COMPLETENESS_DIMENSION_RULES),
        state_version=state_version,
        session_id=session_id,
    )
    expected = {_context_signature(row) for row in rows}
    actual = {_context_signature(item) for item in context_observations}
    return bool(expected) and actual == expected


def _verify_fact_links(output: SyndromeDraft, input_payload: SyndromeDraftInput) -> SyndromeVerificationFailureCode | None:
    # R2-B1: current fact identity comes from the shared projection (CORRECTED
    # heads are valid links; superseded roots and RETRACTED heads are not).
    current_ids = {item.observation_id for item in project_current_observations(input_payload.domain_state.observations)}
    # The slot-projected context (3a) carries synthetic deterministic ids that
    # are also authoritative derivations of the domain state.
    context_ids = {item.observation_id for item in input_payload.context_observations}
    active_ids = current_ids | context_ids
    all_claims = (*output.syndrome_basis, *output.differential)
    if any(not claim.fact_ids or any(fact_id not in active_ids for fact_id in claim.fact_ids) for claim in all_claims):
        return SyndromeVerificationFailureCode.FACT_LINK_INVALID
    return None


def _verify_decision(output: SyndromeDraft, input_payload: SyndromeDraftInput) -> SyndromeVerificationFailureCode | None:
    del input_payload
    if output.decision is SyndromeDraftDecision.COMPLETED:
        if (
            not _valid_clinical_text(output.syndrome)
            or not output.syndrome_basis
            or not _valid_clinical_text(output.treatment_principle)
            or output.missing_inputs
        ):
            return SyndromeVerificationFailureCode.DECISION_CONTENT_INVALID
    elif output.decision is SyndromeDraftDecision.NEEDS_MORE_INFO:
        if (
            output.syndrome is not None
            or output.treatment_principle is not None
            or output.syndrome_basis
            or output.differential
            or not output.missing_inputs
        ):
            return SyndromeVerificationFailureCode.DECISION_CONTENT_INVALID
    elif output.decision is SyndromeDraftDecision.ABSTAINED and (
        output.syndrome is not None or output.treatment_principle is not None or output.syndrome_basis or output.differential
    ):
        return SyndromeVerificationFailureCode.DECISION_CONTENT_INVALID
    return None


def _verify_evidence_contract(
    output: SyndromeDraft,
    evidence_ids: tuple[str, ...],
    policy_version: str,
) -> SyndromeVerificationFailureCode | None:
    """按 policy_version 分派的证据契约校验（D1/D2）。

    - no-rag 契约：evidence_ids 必须为空、evidence_mode=model_knowledge_only、
      links 空、confidence ≤ SYNDROME_NO_RAG_CONFIDENCE_MAX。
    - rag 契约：evidence_mode=rag_retrieved、每条 link 的 evidence_id 必须命中
      本次检索证据集合（防幻觉引用）、confidence 按证据空否分别封顶。
    """
    if policy_version == SYNDROME_RAG_POLICY_VERSION:
        if output.evidence_mode != SYNDROME_RAG_EVIDENCE_MODE:
            return SyndromeVerificationFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
        return _verify_rag_contract(output, evidence_ids)
    if output.evidence_mode != SYNDROME_EVIDENCE_MODE:
        return SyndromeVerificationFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
    return _verify_no_rag(output, evidence_ids)


def _verify_rag_contract(output: SyndromeDraft, evidence_ids: tuple[str, ...]) -> SyndromeVerificationFailureCode | None:
    allowed = frozenset(evidence_ids)
    if any(link.evidence_id not in allowed for link in output.claim_evidence_links):
        # 引用不存在的证据 ID = 模型编造引用，整份输出拒绝。
        return SyndromeVerificationFailureCode.EVIDENCE_LINK_FABRICATED
    if not allowed:
        # 空证据降级模式：不允许过度自信（RAG 无证据时模型只基于内知识）。
        if output.confidence > SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX:
            return SyndromeVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    elif output.confidence > SYNDROME_RAG_CONFIDENCE_MAX:
        return SyndromeVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    if output.review_required is not True:
        return SyndromeVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED
    return None


def _verify_no_rag(output: SyndromeDraft, evidence_ids: tuple[str, ...]) -> SyndromeVerificationFailureCode | None:
    if evidence_ids:
        # no-rag 模式下 artifact 不得携带任何检索证据 ID（防证据泄漏冒充）。
        return SyndromeVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED
    if output.confidence > SYNDROME_NO_RAG_CONFIDENCE_MAX:
        return SyndromeVerificationFailureCode.CONFIDENCE_EXCEEDS_NO_RAG_LIMIT
    if (
        output.evidence_mode != SYNDROME_EVIDENCE_MODE
        or output.claim_evidence_links
        or output.review_required is not True
    ):
        return SyndromeVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED
    return None


def _verify_authority(output: SyndromeDraft) -> SyndromeVerificationFailureCode | None:
    payload = output.model_dump(mode="python")
    if _contains_forbidden_authority_key(payload) or _contains_evidence_authority_key(payload):
        return SyndromeVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN
    return None


def _active_observations(observations: Iterable[ObservationSchema]) -> tuple[ObservationSchema, ...]:
    # R2-B1: current semantic chain heads (CORRECTED successors count, superseded
    # targets and RETRACTED heads do not) from the single shared projection.
    return tuple(project_current_observations(observations))


def _same_gate(left: GateResultSchema, right: GateResultSchema) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def _triage_gate_allows_reasoning(gate: GateResultSchema) -> bool:
    details = gate.details or {}
    return (
        gate.gate_name == TRIAGE_GATE_NAME
        and gate.policy_version == TRIAGE_POLICY_VERSION
        and gate.decision is GateDecision.PASSED
        and details.get("disposition") == "continue"
        and details.get("candidate_count") == 0
        and not details.get("rule_ids")
        and not details.get("rules")
    )


def _completeness_gate_allows_reasoning(gate: GateResultSchema) -> bool:
    details = gate.details or {}
    return (
        gate.gate_name == COMPLETENESS_GATE_NAME
        and gate.policy_version == COMPLETENESS_POLICY_VERSION
        and gate.decision is GateDecision.PASSED
        and details.get("disposition") == "ready"
    )


def _has_active_conflicts(observations: Iterable[ObservationSchema]) -> bool:
    by_key: dict[str, set[str]] = {}
    for item in _active_observations(observations):
        value = item.normalized_value if item.normalized_value is not None else item.value
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            encoded = repr(type(value))
        by_key.setdefault(item.fact_key, set()).add(encoded)
    return any(len(values) > 1 for values in by_key.values())


_PSEUDO_COMPLETED = frozenset(
    {
        "信息不足",
        "资料不足",
        "待补充",
        "待完善",
        "无法判断",
        "不能判断",
        "不详",
        "未知",
        "unknown",
        "n/a",
        "none",
    }
)
_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "route",
        "stage",
        "current_stage",
        "next_stage",
        "formula",
        "prescription",
        "safety_decision",
        "doctor_decision",
        "doctor_review",
        "approved",
        "transition",
        "ready",
    }
)
_EVIDENCE_KEYS = frozenset({"citation", "citations", "source", "sources", "source_title", "literature_title"})
_IDENTITY_FACT_PARTS = frozenset(
    {
        "name",
        "full_name",
        "patient_name",
        "phone",
        "phone_number",
        "mobile",
        "mobile_number",
        "telephone",
        "id_card",
        "identity_card",
        "national_id",
        "outpatient_no",
        "medical_record_no",
    }
)
_IDENTITY_FACT_FORMS = frozenset(alias.replace("_", "") for alias in _IDENTITY_FACT_PARTS)
_PII_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)"),
    re.compile(r"(?<!\d)\d{6}(?:[\s-]?\d){11}[\s-]?[\dXx](?!\d)"),
)
# 完整 UUID 字面量。UUID(observation_id / source_message_id / slot_id / session_id)
# 是系统生成的程序化标识符，不是患者身份数据。其十六进制数字 + 连字符布局会被
# _PII_PATTERNS 的身份证正则误判（见 _is_uuid_string 注释），必须先排除。
_UUID_HEX = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _is_uuid_string(value: str) -> bool:
    """Return True when the whole string is a canonical UUID literal.

    This is a pure shape test: whether the exemption actually applies is decided
    by :func:`_scan_identity`, which only exempts a UUID string when it sits under
    a machine-metadata id key (see :data:`_MACHINE_ID_KEYS`).  A UUID string in an
    arbitrary business value is still fed to the PII patterns.

    The ID-card pattern ``\\d{6}(?:[\\s-]?\\d){11}[\\s-]?[\\dXx]`` treats dash
    separators as legal within the "18-digit ID" and can therefore match the
    hex layout of a random UUID — e.g. ``00104524-0456-4789-99ff-…`` matches as
    ``00104524-0456-4789-99`` (6-digit prefix + 12 dash-separated digits).  Whether
    a given context collides depends on the random ``uuid4()`` embedded in the slot
    snapshot (``source_message_id``), so the privacy gate turned flaky.
    """

    return _UUID_HEX.fullmatch(value) is not None


def _valid_clinical_text(value: str | None) -> bool:
    if value is None:
        return False
    normalized = re.sub(r"\s+", "", value).lower()
    return bool(normalized) and normalized not in _PSEUDO_COMPLETED


# 3a 槽位投影的结构键（derive_dimension_slots 产出）：这些键名由程序定义
# （slot_name = 已验证的 canonical fact_key），不是患者身份数据，不应被
# _is_identity_key 误判（"slot_name" 含 "name" 后缀）。其 value 仍照常递归检查。
_SLOT_PROJECTION_SCHEMA_KEYS = frozenset({"slot_name", "dimension", "completeness", "missing_slots"})

# 机器元数据标识符键：这些键名由程序定义，其取值是系统生成的规范化 UUID
# （observation_id / session_id / source_message_id / slot_id），不是患者身份数据。
# 值模式豁免只允许发生在这些明确键下、且整串是规范 UUID 时。其他任意业务值 / note
# 里出现的 UUID 字符串必须照常过 PII 正则，不得被 UUID 规则自动豁免。
_MACHINE_ID_KEYS = frozenset({"observation_id", "session_id", "source_message_id", "slot_id"})


def _contains_identity_key_or_value(value: Any) -> bool:
    return _scan_identity(value, parent_key=None)


def _scan_identity(value: Any, parent_key: str | None) -> bool:
    """Recursively scan for identity keys / PII-bearing values, key-aware.

    ``parent_key`` is the dictionary key the current node was reached under.  It
    narrows the UUID exemption: only a canonical UUID string under a machine-id
    metadata key (:data:`_MACHINE_ID_KEYS`) is skipped.  Identity-name keys
    (``name`` / ``phone`` / ``id_card`` …) always reject; a UUID literal in an
    arbitrary business value/note is still matched against the PII patterns.
    """
    if isinstance(value, BaseModel):
        return _scan_identity(value.model_dump(mode="python"), parent_key)
    if isinstance(value, dict):
        if any(_is_identity_key(str(key)) and str(key) not in _SLOT_PROJECTION_SCHEMA_KEYS for key in value):
            return True
        return any(_scan_identity(item, str(key)) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_scan_identity(item, parent_key) for item in value)
    if isinstance(value, str):
        # 整串是规范 UUID 且位于机器元数据 id 键下时才跳过值模式（程序化标识符，
        # 非 PII；否则随机 source_message_id 会被身份证正则误判 → 同一条上下文随机
        # CONTEXT_PRIVACY_INVALID，flaky）。任意业务值 / note 里的 UUID 字符串不豁免。
        if parent_key in _MACHINE_ID_KEYS and _is_uuid_string(value):
            return False
        return any(pattern.search(value) for pattern in _PII_PATTERNS)
    return False


def _is_identity_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    tokens = tuple(token for token in normalized.split("_") if token)
    suffix_forms = frozenset("".join(tokens[index:]) for index in range(len(tokens)))
    return bool(suffix_forms.intersection(_IDENTITY_FACT_FORMS)) or bool(
        set(tokens).intersection({"name", "phone", "mobile", "telephone"})
    )


def _contains_forbidden_authority_key(raw: Any) -> bool:
    return _contains_key(raw, _FORBIDDEN_AUTHORITY_KEYS)


def _contains_evidence_authority_key(raw: Any) -> bool:
    return _contains_key(raw, _EVIDENCE_KEYS)


def _contains_key(raw: Any, forbidden: frozenset[str]) -> bool:
    if isinstance(raw, BaseModel):
        keys = set(raw.__dict__)
        extra = getattr(raw, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            keys.update(extra)
        if {str(key).lower() for key in keys} & forbidden:
            return True
        return any(_contains_key(value, forbidden) for value in raw.__dict__.values()) or (
            isinstance(extra, dict) and any(_contains_key(value, forbidden) for value in extra.values())
        )
    if isinstance(raw, dict):
        if {str(key).lower() for key in raw} & forbidden:
            return True
        return any(_contains_key(value, forbidden) for value in raw.values())
    if isinstance(raw, list | tuple):
        return any(_contains_key(value, forbidden) for value in raw)
    return False


def _has_undeclared_fields(raw: Any, canonical: Any) -> bool:
    if isinstance(canonical, BaseModel):
        allowed = set(type(canonical).model_fields)
        if isinstance(raw, BaseModel):
            raw_keys = set(raw.__dict__)
            extra = getattr(raw, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                raw_keys.update(extra)
            if raw_keys - allowed:
                return True
            return any(_has_undeclared_fields(getattr(raw, name, None), getattr(canonical, name)) for name in allowed)
        if isinstance(raw, dict):
            if set(raw) - allowed:
                return True
            return any(_has_undeclared_fields(raw.get(name), getattr(canonical, name)) for name in allowed)
        return True
    if isinstance(canonical, list | tuple):
        if not isinstance(raw, list | tuple) or len(raw) != len(canonical):
            return True
        return any(_has_undeclared_fields(raw_item, item) for raw_item, item in zip(raw, canonical, strict=True))
    if isinstance(canonical, dict):
        return not isinstance(raw, dict)
    return isinstance(raw, BaseModel | dict | list | tuple)


def _check(name: str, code: SyndromeVerificationFailureCode | None) -> SyndromeCheckResult:
    return SyndromeCheckResult(
        verifier=name,
        status=SyndromeCheckStatus.PASSED if code is None else SyndromeCheckStatus.FAILED,
        failure_code=code,
    )


def _report(checks: list[SyndromeCheckResult], artifact: RunArtifact) -> SyndromeVerificationReport:
    first = next((check.failure_code for check in checks if check.failure_code is not None), None)
    return SyndromeVerificationReport(
        passed=first is None,
        checks=tuple(checks),
        failure_code=first,
        subject_digest=run_artifact_subject_digest(artifact),
    )
