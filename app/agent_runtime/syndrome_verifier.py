"""Deterministic L4-1 Syndrome Draft preflight and verifier."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.specs import AgentSpec, Capability, RunArtifact, RunSpec, run_artifact_subject_digest
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import GateDecision, GateResultSchema, ObservationSchema, ObservationStatus
from app.schemas.syndrome import (
    SYNDROME_DRAFT_SCHEMA_VERSION,
    SYNDROME_EVIDENCE_MODE,
    SYNDROME_INPUT_SCHEMA_VERSION,
    SYNDROME_NO_RAG_CONFIDENCE_MAX,
    SYNDROME_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION

SYNDROME_AGENT_NAME = "syndrome_draft"
SYNDROME_AGENT_VERSION = "syndrome-draft-agent.v1"
SYNDROME_PROMPT_VERSION = "syndrome_draft_v1.jinja2"
# Syndrom 综合 AgentSpec 单次模型调用超时上限（s）。必须 >= MODEL_GATEWAY_TIMEOUT_SECONDS，
# 否则外层会先于网关内层判超时并错误归因为 MODEL_GATEWAY_TIMEOUT。
SYNDROME_MODEL_TIMEOUT_SECONDS = 75
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
        or run_spec.prompt_version != SYNDROME_PROMPT_VERSION
        or run_spec.policy_version != SYNDROME_POLICY_VERSION
        or run_spec.total_attempt_budget != 1
        or run_spec.session_id != input_payload.session_id
        or run_spec.state_version != input_payload.state_version
        or input_payload.policy_version != SYNDROME_POLICY_VERSION
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
    checks.append(_check("no_rag_contract", _verify_no_rag(output)))
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
    active = tuple(_active_observations(input_payload.domain_state.observations))
    active_by_id = {item.observation_id: item for item in active}
    context_by_id = {item.observation_id: item for item in input_payload.context_observations}
    if set(active_by_id) != set(context_by_id):
        return SyndromeVerificationFailureCode.CONTEXT_NOT_ACTIVE
    for item in input_payload.context_observations:
        source = active_by_id.get(item.observation_id)
        if (
            source is None
            or source.session_id != input_payload.session_id
            or source.status is not ObservationStatus.ACTIVE
            or item.status != ObservationStatus.ACTIVE
            or item.state_version != input_payload.state_version
            or item.fact_key != source.fact_key
            or item.value != source.value
            or item.normalized_value != source.normalized_value
        ):
            return SyndromeVerificationFailureCode.CONTEXT_NOT_ACTIVE
    if _contains_identity_key_or_value(input_payload.context_observations):
        return SyndromeVerificationFailureCode.CONTEXT_PRIVACY_INVALID
    return None


def _verify_fact_links(output: SyndromeDraft, input_payload: SyndromeDraftInput) -> SyndromeVerificationFailureCode | None:
    active_ids = {item.observation_id for item in input_payload.context_observations}
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


def _verify_no_rag(output: SyndromeDraft) -> SyndromeVerificationFailureCode | None:
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
    superseded = frozenset(
        item.supersedes_observation_id
        for item in observations
        if item.status is not ObservationStatus.ACTIVE and item.supersedes_observation_id is not None
    )
    return tuple(
        item
        for item in observations
        if item.status is ObservationStatus.ACTIVE and item.observation_id not in superseded
    )


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


def _valid_clinical_text(value: str | None) -> bool:
    if value is None:
        return False
    normalized = re.sub(r"\s+", "", value).lower()
    return bool(normalized) and normalized not in _PSEUDO_COMPLETED


def _contains_identity_key_or_value(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return _contains_identity_key_or_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        if any(_is_identity_key(str(key)) for key in value):
            return True
        return any(_contains_identity_key_or_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity_key_or_value(item) for item in value)
    if isinstance(value, str):
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
    if isinstance(raw, (list, tuple)):
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
    if isinstance(canonical, (list, tuple)):
        if not isinstance(raw, (list, tuple)) or len(raw) != len(canonical):
            return True
        return any(_has_undeclared_fields(raw_item, item) for raw_item, item in zip(raw, canonical, strict=True))
    if isinstance(canonical, dict):
        return not isinstance(raw, dict)
    return isinstance(raw, (BaseModel, dict, list, tuple))


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
