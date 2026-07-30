"""Deterministic L4-2 Formula Draft preflight and verifier.

This module reuses the L4-1 authority pattern: it never trusts the
caller-supplied SyndromeDraft, DomainState, or gates as clinical truth.
Instead, ``execute_formula_draft`` loads a ``ReasoningAuthoritySnapshot``
from the Repository and rebuilds the input from authoritative data before
the preflight and verifier run.

AR-B-027: the public Formula boundary accepts only the exact result instance
registered by the real L4-1 success path.  Artifact and run provenance used
below come from that identity registry; they are not caller authority.  The
verifier re-runs canonical L4-1 checks against current Repository authority.

The verifier independently canonicalises the output, rejects hidden/extra
fields, and enforces the no-RAG contract, fact-link validity, and decision
consistency.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.specs import AgentSpec, Capability, RunArtifact, RunSpec, run_artifact_subject_digest
from app.agent_runtime.syndrome_verifier import (
    SYNDROME_AGENT_NAME,
    SYNDROME_AGENT_VERSION,
    SYNDROME_VERIFIER_CHAIN,
    SyndromeGateAuthority,
    SyndromeVerificationReport,
)
from app.agent_runtime.syndrome_verifier import (
    verify_syndrome_artifact as _verify_syndrome_artifact_l4,
)
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import GateDecision, GateResultSchema, ObservationSchema, ObservationStatus
from app.schemas.formula import (
    FORMULA_DRAFT_SCHEMA_VERSION,
    FORMULA_EVIDENCE_MODE,
    FORMULA_INPUT_SCHEMA_VERSION,
    FORMULA_NO_RAG_CONFIDENCE_MAX,
    FORMULA_POLICY_VERSION,
    FORMULA_READY_STAGE,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaDraftInput,
    FormulaModification,
)
from app.schemas.syndrome import (
    SYNDROME_INPUT_SCHEMA_VERSION,
    SYNDROME_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION

FORMULA_AGENT_NAME = "formula_draft"
FORMULA_AGENT_VERSION = "formula-draft-agent.v1"
FORMULA_PROMPT_VERSION = "formula_draft_v1.jinja2"
FORMULA_VERIFIER_CHAIN = (
    "schema",
    "run_provenance",
    "preconditions",
    "upstream_syndrome",
    "fact_links",
    "decision_consistency",
    "modification_basis",
    "no_rag_contract",
    "authority_boundary",
)


class FormulaVerificationFailureCode(StrEnum):
    SCHEMA_INVALID = "FORMULA_SCHEMA_INVALID"
    OUTPUT_TYPE_INVALID = "FORMULA_OUTPUT_TYPE_INVALID"
    AGENT_SPEC_INVALID = "FORMULA_AGENT_SPEC_INVALID"
    RUN_PROVENANCE_MISMATCH = "FORMULA_RUN_PROVENANCE_MISMATCH"
    STAGE_NOT_READY = "FORMULA_STAGE_NOT_READY"
    GATE_INVALID = "FORMULA_GATE_INVALID"
    RED_FLAG_UNHANDLED = "FORMULA_RED_FLAG_UNHANDLED"
    FACT_CONFLICT_BLOCKING = "FORMULA_FACT_CONFLICT_BLOCKING"
    CONTEXT_NOT_ACTIVE = "FORMULA_CONTEXT_NOT_ACTIVE"
    CONTEXT_PRIVACY_INVALID = "FORMULA_CONTEXT_PRIVACY_INVALID"
    SYNDROME_DRAFT_INVALID = "FORMULA_SYNDROME_DRAFT_INVALID"
    SYNDROME_FACT_LINK_INVALID = "FORMULA_SYNDROME_FACT_LINK_INVALID"
    TREATMENT_PRINCIPLE_MISSING = "FORMULA_TREATMENT_PRINCIPLE_MISSING"
    FACT_LINK_INVALID = "FORMULA_FACT_LINK_INVALID"
    DECISION_CONTENT_INVALID = "FORMULA_DECISION_CONTENT_INVALID"
    MODIFICATION_BASIS_MISSING = "FORMULA_MODIFICATION_BASIS_MISSING"
    CONFIDENCE_EXCEEDS_NO_RAG_LIMIT = "FORMULA_CONFIDENCE_EXCEEDS_NO_RAG_LIMIT"
    NO_RAG_CONTRACT_VIOLATED = "FORMULA_NO_RAG_CONTRACT_VIOLATED"
    AUTHORITY_FIELD_FORBIDDEN = "FORMULA_AUTHORITY_FIELD_FORBIDDEN"


class FormulaCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class FormulaCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: str = Field(min_length=1, max_length=64)
    status: FormulaCheckStatus
    failure_code: FormulaVerificationFailureCode | None = None

    @model_validator(mode="after")
    def status_matches_code(self) -> FormulaCheckResult:
        if (self.status is FormulaCheckStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failure code must exactly match failed status")
        return self


class FormulaVerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checks: tuple[FormulaCheckResult, ...] = Field(min_length=1)
    failure_code: FormulaVerificationFailureCode | None = None
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deterministic_result(self) -> FormulaVerificationReport:
        first = next((check.failure_code for check in self.checks if check.failure_code is not None), None)
        if self.passed != (first is None) or self.failure_code is not first:
            raise ValueError("report outcome must match checks")
        return self


class FormulaGateAuthority(BaseModel):
    """Same authority shape as L4-1, reused for Formula preflight."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema


class FormulaOutputBoundaryError(ValueError):
    def __init__(self, code: FormulaVerificationFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


# ---------------------------------------------------------------------------
# Canonicalisation — reject hidden/extra fields on both input and output.
# ---------------------------------------------------------------------------


def canonicalize_formula_input(input_payload: object) -> FormulaDraftInput:
    candidate = FormulaDraftInput.model_validate(input_payload)
    canonical_json = FormulaDraftInput.__pydantic_serializer__.to_json(candidate, warnings=False)
    canonical = FormulaDraftInput.model_validate_json(canonical_json)
    if _has_undeclared_fields(input_payload, canonical):
        raise FormulaOutputBoundaryError(FormulaVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    return canonical


def canonicalize_formula_output(output: object) -> FormulaDraft:
    try:
        candidate = FormulaDraft.model_validate(output)
        canonical_json = FormulaDraft.__pydantic_serializer__.to_json(candidate, warnings=False)
        canonical = FormulaDraft.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        raise FormulaOutputBoundaryError(FormulaVerificationFailureCode.SCHEMA_INVALID) from exc
    if _has_undeclared_fields(output, canonical) or _contains_forbidden_authority_key(output):
        raise FormulaOutputBoundaryError(FormulaVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    return canonical


# ---------------------------------------------------------------------------
# Preflight — runs before the model call.  Must return a failure code or None.
# ---------------------------------------------------------------------------


def validate_formula_preflight(
    agent_spec: AgentSpec,
    run_spec: RunSpec,
    input_payload: FormulaDraftInput,
    gate_authority: FormulaGateAuthority | None = None,
    syndrome_artifact: RunArtifact | None = None,
    syndrome_run_spec: RunSpec | None = None,
    syndrome_input_payload: SyndromeDraftInput | None = None,
) -> FormulaVerificationFailureCode | None:
    if not _valid_agent_spec(agent_spec):
        return FormulaVerificationFailureCode.AGENT_SPEC_INVALID
    if (
        run_spec.agent_spec_version != agent_spec.version
        or run_spec.prompt_version != FORMULA_PROMPT_VERSION
        or run_spec.policy_version != FORMULA_POLICY_VERSION
        or run_spec.total_attempt_budget != 1
        or run_spec.session_id != input_payload.session_id
        or run_spec.state_version != input_payload.state_version
        or input_payload.policy_version != FORMULA_POLICY_VERSION
    ):
        return FormulaVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    stage_failure = _verify_stage_and_gates(run_spec, input_payload, gate_authority)
    if stage_failure is not None:
        return stage_failure
    context_failure = _verify_context(input_payload)
    if context_failure is not None:
        return context_failure
    syndrome_failure = _verify_upstream_syndrome(
        input_payload,
        gate_authority=gate_authority,
        syndrome_artifact=syndrome_artifact,
        syndrome_run_spec=syndrome_run_spec,
        syndrome_input_payload=syndrome_input_payload,
    )
    if syndrome_failure is not None:
        return syndrome_failure
    if _has_active_conflicts(input_payload.domain_state.observations):
        return FormulaVerificationFailureCode.FACT_CONFLICT_BLOCKING
    return None


def verify_formula_artifact(
    *,
    agent_spec: AgentSpec,
    run_spec: RunSpec,
    artifact: RunArtifact,
    input_payload: FormulaDraftInput,
    gate_authority: FormulaGateAuthority | None = None,
    syndrome_artifact: RunArtifact | None = None,
    syndrome_run_spec: RunSpec | None = None,
    syndrome_input_payload: SyndromeDraftInput | None = None,
) -> FormulaVerificationReport:
    checks: list[FormulaCheckResult] = []
    try:
        output = canonicalize_formula_output(artifact.output)
    except FormulaOutputBoundaryError as exc:
        checks.append(_check("schema", exc.code))
        return _report(checks, artifact)

    checks.append(_check("schema", _verify_schema(agent_spec, output)))
    checks.append(_check("run_provenance", _verify_run(agent_spec, run_spec, artifact)))
    checks.append(
        _check(
            "preconditions",
            validate_formula_preflight(
                agent_spec,
                run_spec,
                input_payload,
                gate_authority,
                syndrome_artifact=syndrome_artifact,
                syndrome_run_spec=syndrome_run_spec,
                syndrome_input_payload=syndrome_input_payload,
            ),
        )
    )
    checks.append(
        _check(
            "upstream_syndrome",
            _verify_upstream_syndrome(
                input_payload,
            gate_authority=gate_authority,
            syndrome_artifact=syndrome_artifact,
            syndrome_run_spec=syndrome_run_spec,
            syndrome_input_payload=syndrome_input_payload,
        ),
    )
    )
    checks.append(_check("fact_links", _verify_fact_links(output, input_payload)))
    checks.append(_check("decision_consistency", _verify_decision(output)))
    checks.append(_check("modification_basis", _verify_modification_basis(output)))
    checks.append(_check("no_rag_contract", _verify_no_rag(output)))
    checks.append(_check("authority_boundary", _verify_authority(output)))
    return _report(checks, artifact)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

FORMULA_MODEL_TEMPERATURE = 0.1
FORMULA_MODEL_MAX_TOKENS = 2_000
FORMULA_MODEL_TIMEOUT_SECONDS = 75  # >= MODEL_GATEWAY_TIMEOUT_SECONDS（60s），避免外层先判超时


def _valid_agent_spec(spec: AgentSpec) -> bool:
    policy = spec.model_policy
    return (
        spec.name == FORMULA_AGENT_NAME
        and spec.version == FORMULA_AGENT_VERSION
        and spec.input_schema is FormulaDraftInput
        and spec.output_schema is FormulaDraft
        and policy.temperature == FORMULA_MODEL_TEMPERATURE
        and policy.max_tokens <= FORMULA_MODEL_MAX_TOKENS
        and policy.timeout_seconds <= FORMULA_MODEL_TIMEOUT_SECONDS
        and policy.max_attempts == 1
        and spec.tool_permissions == frozenset({Capability.READ_STATE})
        and spec.verifier_chain == FORMULA_VERIFIER_CHAIN
        and not spec.failure_policy.retryable_codes
    )


def valid_formula_agent_spec(spec: AgentSpec) -> bool:
    """Return whether a Formula AgentSpec stays within the fixed L4-2 policy."""

    return _valid_agent_spec(spec)


def _verify_schema(agent_spec: AgentSpec, output: FormulaDraft) -> FormulaVerificationFailureCode | None:
    if not _valid_agent_spec(agent_spec):
        return FormulaVerificationFailureCode.AGENT_SPEC_INVALID
    if type(output) is not FormulaDraft:
        return FormulaVerificationFailureCode.OUTPUT_TYPE_INVALID
    if output.schema_version != FORMULA_DRAFT_SCHEMA_VERSION:
        return FormulaVerificationFailureCode.SCHEMA_INVALID
    return None


def _verify_run(agent_spec: AgentSpec, run_spec: RunSpec, artifact: RunArtifact) -> FormulaVerificationFailureCode | None:
    if (
        artifact.run_id != run_spec.run_id
        or artifact.trace_id != run_spec.trace_id
        or artifact.agent_spec_version != agent_spec.version
        or artifact.prompt_version != run_spec.prompt_version
        or artifact.attempts != 1
    ):
        return FormulaVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    return None


def _verify_stage_and_gates(
    run_spec: RunSpec,
    input_payload: FormulaDraftInput,
    gate_authority: FormulaGateAuthority | None,
) -> FormulaVerificationFailureCode | None:
    if input_payload.schema_version != FORMULA_INPUT_SCHEMA_VERSION or input_payload.current_stage != FORMULA_READY_STAGE:
        return FormulaVerificationFailureCode.STAGE_NOT_READY
    if run_spec.stage != FORMULA_READY_STAGE:
        return FormulaVerificationFailureCode.STAGE_NOT_READY
    if gate_authority is None:
        return FormulaVerificationFailureCode.GATE_INVALID
    authority = gate_authority
    if not _same_gate(input_payload.triage_gate, authority.triage_gate):
        return FormulaVerificationFailureCode.RED_FLAG_UNHANDLED
    if not _same_gate(input_payload.completeness_gate, authority.completeness_gate):
        return FormulaVerificationFailureCode.GATE_INVALID
    if not _triage_gate_allows_reasoning(authority.triage_gate):
        return FormulaVerificationFailureCode.RED_FLAG_UNHANDLED
    if not _completeness_gate_allows_reasoning(authority.completeness_gate):
        return FormulaVerificationFailureCode.GATE_INVALID
    if authority.triage_gate.input_state_version != authority.completeness_gate.input_state_version:
        return FormulaVerificationFailureCode.GATE_INVALID
    return None


def _verify_upstream_syndrome(
    input_payload: FormulaDraftInput,
    *,
    gate_authority: FormulaGateAuthority | None = None,
    syndrome_artifact: RunArtifact | None = None,
    syndrome_run_spec: RunSpec | None = None,
    syndrome_input_payload: SyndromeDraftInput | None = None,
) -> FormulaVerificationFailureCode | None:
    """Re-validate the unsealed process-internal L4-1 execution bundle.

    Public callers cannot provide these values as authority.  The Formula
    entry rejects raw artifact/run-spec parameters and invokes this verifier
    only with immutable values recovered from a successful L4-1 result.
    """
    if syndrome_artifact is None or syndrome_run_spec is None:
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID
    if (
        syndrome_run_spec.session_id != input_payload.session_id
        or syndrome_run_spec.state_version > input_payload.state_version
    ):
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID

    try:
        from app.agent_runtime.syndrome_verifier import canonicalize_syndrome_output

        artifact_draft = canonicalize_syndrome_output(syndrome_artifact.output)
    except Exception:
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID

    if artifact_draft.decision is not SyndromeDraftDecision.COMPLETED or not _valid_clinical_text(
        artifact_draft.syndrome
    ):
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID
    if not _valid_clinical_text(artifact_draft.treatment_principle):
        return FormulaVerificationFailureCode.TREATMENT_PRINCIPLE_MISSING
    if _syndrome_digest(input_payload.syndrome_draft) != _syndrome_digest(artifact_draft):
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID

    active_ids = _active_observation_ids(input_payload.domain_state.observations)
    syndrome_ids = {
        fact_id
        for claim in (*artifact_draft.syndrome_basis, *artifact_draft.differential)
        for fact_id in claim.fact_ids
    }
    if not syndrome_ids or not syndrome_ids.issubset(active_ids):
        return FormulaVerificationFailureCode.SYNDROME_FACT_LINK_INVALID

    # Build the syndrome agent spec using the same fixed parameters as L4-1.
    syndrome_spec = _build_syndrome_agent_spec_for_reverify()

    syndrome_input = syndrome_input_payload or _build_syndrome_input_from_formula(input_payload)
    if (
        syndrome_input.session_id != input_payload.session_id
        or syndrome_input.state_version != syndrome_run_spec.state_version
        or syndrome_input.state_version > input_payload.state_version
    ):
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID

    l4_gate_authority = SyndromeGateAuthority(
        triage_gate=syndrome_input.triage_gate,
        completeness_gate=syndrome_input.completeness_gate,
    )
    if gate_authority is not None and (
        gate_authority.triage_gate != syndrome_input.triage_gate
        or gate_authority.completeness_gate != syndrome_input.completeness_gate
    ):
        return FormulaVerificationFailureCode.GATE_INVALID

    report: SyndromeVerificationReport = _verify_syndrome_artifact_l4(
        agent_spec=syndrome_spec,
        run_spec=syndrome_run_spec,
        artifact=syndrome_artifact,
        input_payload=syndrome_input,
        gate_authority=l4_gate_authority,
    )
    if not report.passed:
        return FormulaVerificationFailureCode.SYNDROME_DRAFT_INVALID

    return None


def _build_syndrome_agent_spec_for_reverify() -> AgentSpec:
    """Build a fixed syndrome AgentSpec for re-running the L4-1 verifier."""
    from app.agent_runtime.specs import FailurePolicy, ModelPolicy

    return AgentSpec(
        name=SYNDROME_AGENT_NAME,
        version=SYNDROME_AGENT_VERSION,
        input_schema=SyndromeDraftInput,
        output_schema=SyndromeDraft,
        model_policy=ModelPolicy(
            model="syndrome-reverify",
            temperature=0.1,
            max_tokens=1_500,
            timeout_seconds=20,
            max_attempts=1,
        ),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=SYNDROME_VERIFIER_CHAIN,
        failure_policy=FailurePolicy(),
    )


def _build_syndrome_input_from_formula(formula_input: FormulaDraftInput) -> SyndromeDraftInput:
    """Build a SyndromeDraftInput from the Formula input's authority data.

    The formula input's domain_state, gates, and context_observations have
    already been replaced by authoritative Repository values.  We project
    them into the L4-1 SyndromeDraftInput shape using the syndrome stage
    and policy version.
    """
    return SyndromeDraftInput(
        schema_version=SYNDROME_INPUT_SCHEMA_VERSION,
        session_id=formula_input.session_id,
        state_version=formula_input.state_version,
        current_stage=SYNDROME_READY_STAGE,
        policy_version=SYNDROME_POLICY_VERSION,
        domain_state=formula_input.domain_state,
        triage_gate=formula_input.triage_gate,
        completeness_gate=formula_input.completeness_gate,
        context_observations=formula_input.context_observations,
    )


def _syndrome_digest(draft: SyndromeDraft) -> str:
    """Compute a stable content digest for a SyndromeDraft."""
    payload = draft.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _verify_context(input_payload: FormulaDraftInput) -> FormulaVerificationFailureCode | None:
    active = tuple(_active_observations(input_payload.domain_state.observations))
    active_by_id = {item.observation_id: item for item in active}
    context_by_id = {item.observation_id: item for item in input_payload.context_observations}
    if set(active_by_id) != set(context_by_id):
        return FormulaVerificationFailureCode.CONTEXT_NOT_ACTIVE
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
            return FormulaVerificationFailureCode.CONTEXT_NOT_ACTIVE
    if _contains_identity_key_or_value(input_payload.context_observations):
        return FormulaVerificationFailureCode.CONTEXT_PRIVACY_INVALID
    return None


def _verify_fact_links(output: FormulaDraft, input_payload: FormulaDraftInput) -> FormulaVerificationFailureCode | None:
    """Output fact IDs must come from current active facts or upstream syndrome basis."""
    active_ids = _active_observation_ids(input_payload.domain_state.observations)
    syndrome_ids: set[UUID] = set()
    for syndrome_claim in input_payload.syndrome_draft.syndrome_basis:
        syndrome_ids.update(syndrome_claim.fact_ids)
    for syndrome_claim in input_payload.syndrome_draft.differential:
        syndrome_ids.update(syndrome_claim.fact_ids)
    allowed = active_ids | syndrome_ids

    compositions: list[FormulaComposition] = []
    if output.base_formula is not None:
        compositions.append(output.base_formula)
    if output.candidate_formula is not None:
        compositions.append(output.candidate_formula)
    for comp in compositions:
        for formula_claim in comp.basis:
            if not formula_claim.fact_ids or any(fact_id not in allowed for fact_id in formula_claim.fact_ids):
                return FormulaVerificationFailureCode.FACT_LINK_INVALID
    for mod in output.modifications:
        if not mod.basis.fact_ids or any(fact_id not in allowed for fact_id in mod.basis.fact_ids):
            return FormulaVerificationFailureCode.FACT_LINK_INVALID
    return None


def _verify_decision(output: FormulaDraft) -> FormulaVerificationFailureCode | None:
    if output.decision is FormulaDraftDecision.COMPLETED:
        if (
            output.base_formula is None
            or output.candidate_formula is None
            or not _valid_clinical_text(output.rationale)
            or output.missing_inputs
        ):
            return FormulaVerificationFailureCode.DECISION_CONTENT_INVALID
    elif output.decision is FormulaDraftDecision.NEEDS_MORE_INFO:
        if (
            output.base_formula is not None
            or output.candidate_formula is not None
            or output.modifications
            or output.rationale is not None
            or not output.missing_inputs
        ):
            return FormulaVerificationFailureCode.DECISION_CONTENT_INVALID
    elif output.decision is FormulaDraftDecision.ABSTAINED and (
        output.base_formula is not None
        or output.candidate_formula is not None
        or output.modifications
        or output.rationale is not None
    ):
        return FormulaVerificationFailureCode.DECISION_CONTENT_INVALID
    return None


def _verify_modification_basis(output: FormulaDraft) -> FormulaVerificationFailureCode | None:
    for mod in output.modifications:
        if not isinstance(mod, FormulaModification):
            return FormulaVerificationFailureCode.MODIFICATION_BASIS_MISSING
        if mod.basis is None or not mod.basis.fact_ids:
            return FormulaVerificationFailureCode.MODIFICATION_BASIS_MISSING
        if not _valid_clinical_text(mod.reason):
            return FormulaVerificationFailureCode.MODIFICATION_BASIS_MISSING
    return None


def _verify_no_rag(output: FormulaDraft) -> FormulaVerificationFailureCode | None:
    if output.confidence > FORMULA_NO_RAG_CONFIDENCE_MAX:
        return FormulaVerificationFailureCode.CONFIDENCE_EXCEEDS_NO_RAG_LIMIT
    if (
        output.evidence_mode != FORMULA_EVIDENCE_MODE
        or output.claim_evidence_links
        or output.review_required is not True
    ):
        return FormulaVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED
    return None


def _verify_authority(output: FormulaDraft) -> FormulaVerificationFailureCode | None:
    payload = output.model_dump(mode="python")
    if _contains_forbidden_authority_key(payload) or _contains_evidence_authority_key(payload):
        return FormulaVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN
    return None


# ---------------------------------------------------------------------------
# Shared low-level helpers (mirrors L4-1 patterns)
# ---------------------------------------------------------------------------


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


def _active_observation_ids(observations: Iterable[ObservationSchema]) -> set[UUID]:
    return {item.observation_id for item in _active_observations(observations)}


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
        "safety_decision",
        "doctor_decision",
        "doctor_review",
        "approved",
        "transition",
        "ready",
        "prescription",
    }
)
_EVIDENCE_KEYS = frozenset({"citation", "citations", "source", "sources", "source_title", "literature_title", "literature"})
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


def _check(name: str, code: FormulaVerificationFailureCode | None) -> FormulaCheckResult:
    return FormulaCheckResult(
        verifier=name,
        status=FormulaCheckStatus.PASSED if code is None else FormulaCheckStatus.FAILED,
        failure_code=code,
    )


def _report(checks: list[FormulaCheckResult], artifact: RunArtifact) -> FormulaVerificationReport:
    first = next((check.failure_code for check in checks if check.failure_code is not None), None)
    return FormulaVerificationReport(
        passed=first is None,
        checks=tuple(checks),
        failure_code=first,
        subject_digest=run_artifact_subject_digest(artifact),
    )
