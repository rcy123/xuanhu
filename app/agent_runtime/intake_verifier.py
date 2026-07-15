"""Pure deterministic verification boundary for L3-1 intake candidates.

It intentionally does not extend or weaken ``DEFAULT_VERIFIER_CHAIN``: that
chain remains the canonical authorization boundary for ``DomainDelta``.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.intake_grounding import (
    IntakeGroundingFailureKind,
    verify_intake_grounding,
)
from app.agent_runtime.specs import AgentSpec, Capability, RunArtifact, RunSpec
from app.schemas.intake import (
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessageRole,
    ObservationOperation,
)

INTAKE_AGENT_NAME = "intake_extraction"
INTAKE_AGENT_VERSION = "intake-extraction-agent.v2"
INTAKE_PROMPT_VERSION = "intake_extraction_v2.jinja2"
INTAKE_POLICY_VERSION = "intake-extraction-policy.v1"
INTAKE_ALLOWED_STAGES = frozenset({"inquiry"})
INTAKE_VERIFIER_CHAIN = (
    "schema",
    "run_provenance",
    "stage",
    "source_provenance",
    "grounding",
    "safety_semantics",
    "decision_consistency",
    "observation_legality",
    "authority_boundary",
)


class IntakeVerifierName(StrEnum):
    SCHEMA = "schema"
    RUN_PROVENANCE = "run_provenance"
    STAGE = "stage"
    SOURCE_PROVENANCE = "source_provenance"
    GROUNDING = "grounding"
    SAFETY_SEMANTICS = "safety_semantics"
    DECISION_CONSISTENCY = "decision_consistency"
    OBSERVATION_LEGALITY = "observation_legality"
    AUTHORITY_BOUNDARY = "authority_boundary"


class IntakeVerificationFailureCode(StrEnum):
    SCHEMA_INVALID = "INTAKE_SCHEMA_INVALID"
    OUTPUT_TYPE_INVALID = "INTAKE_OUTPUT_TYPE_INVALID"
    AGENT_SPEC_INVALID = "INTAKE_AGENT_SPEC_INVALID"
    RUN_PROVENANCE_MISMATCH = "INTAKE_RUN_PROVENANCE_MISMATCH"
    STAGE_NOT_ALLOWED = "INTAKE_STAGE_NOT_ALLOWED"
    SOURCE_NOT_ALLOWED = "INTAKE_SOURCE_NOT_ALLOWED"
    GROUNDING_SPAN_INVALID = "INTAKE_GROUNDING_SPAN_INVALID"
    GROUNDING_VALUE_MISMATCH = "INTAKE_GROUNDING_VALUE_MISMATCH"
    GROUNDING_CONTEXT_UNSAFE = "INTAKE_GROUNDING_CONTEXT_UNSAFE"
    SAFETY_SEMANTICS_INVALID = "INTAKE_SAFETY_SEMANTICS_INVALID"
    DECISION_CONTENT_MISMATCH = "INTAKE_DECISION_CONTENT_MISMATCH"
    DUPLICATE_OBSERVATION = "INTAKE_DUPLICATE_OBSERVATION"
    HISTORICAL_FACT_REEXTRACTED = "INTAKE_HISTORICAL_FACT_REEXTRACTED"
    CORRECTION_TARGET_INVALID = "INTAKE_CORRECTION_TARGET_INVALID"
    VALUE_NOT_JSON = "INTAKE_VALUE_NOT_JSON"
    AUTHORITY_FIELD_FORBIDDEN = "INTAKE_AUTHORITY_FIELD_FORBIDDEN"
    IDENTITY_FACT_FORBIDDEN = "INTAKE_IDENTITY_FACT_FORBIDDEN"


class IntakeCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class IntakeCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verifier: IntakeVerifierName
    status: IntakeCheckStatus
    failure_code: IntakeVerificationFailureCode | None = None

    @model_validator(mode="after")
    def status_matches_code(self) -> IntakeCheckResult:
        if (self.status is IntakeCheckStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failure code must exactly match failed status")
        return self


class IntakeVerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    checks: tuple[IntakeCheckResult, ...] = Field(min_length=1)
    failure_code: IntakeVerificationFailureCode | None = None
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deterministic_result(self) -> IntakeVerificationReport:
        first = next((check.failure_code for check in self.checks if check.failure_code is not None), None)
        if self.passed != (first is None) or self.failure_code is not first:
            raise ValueError("report outcome must match its checks")
        return self


class IntakeOutputBoundaryError(ValueError):
    """Fixed-code rejection from canonical output reconstruction."""

    def __init__(self, code: IntakeVerificationFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def canonicalize_intake_output(output: BaseModel) -> IntakeExtractionOutput:
    """Rebuild the exact base DTO and reject fields hidden by serialization."""

    try:
        canonical_json = IntakeExtractionOutput.__pydantic_serializer__.to_json(output, warnings=False)
        canonical = IntakeExtractionOutput.model_validate_json(canonical_json)
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        raise IntakeOutputBoundaryError(IntakeVerificationFailureCode.SCHEMA_INVALID) from exc
    if _has_undeclared_fields(output, canonical):
        raise IntakeOutputBoundaryError(IntakeVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN)
    return canonical


def verify_intake_artifact(
    *,
    agent_spec: AgentSpec,
    run_spec: RunSpec,
    artifact: RunArtifact,
    input_payload: IntakeExtractionInput,
) -> IntakeVerificationReport:
    """Verify one exact runtime artifact without persistence or side effects."""

    checks: list[IntakeCheckResult] = []
    try:
        output = canonicalize_intake_output(artifact.output)
    except IntakeOutputBoundaryError as exc:
        checks.append(_check(IntakeVerifierName.SCHEMA, exc.code))
        return _report(checks, artifact)

    failure = _verify_schema(agent_spec, output)
    checks.append(_check(IntakeVerifierName.SCHEMA, failure))
    if failure is not None or not isinstance(output, IntakeExtractionOutput):
        return _report(checks, artifact)

    verifiers = (
        (IntakeVerifierName.RUN_PROVENANCE, _verify_run(agent_spec, run_spec, artifact)),
        (IntakeVerifierName.STAGE, _verify_stage(run_spec)),
        (IntakeVerifierName.SOURCE_PROVENANCE, _verify_sources(output, input_payload)),
        (IntakeVerifierName.GROUNDING, _verify_grounding(output, input_payload)),
        (IntakeVerifierName.SAFETY_SEMANTICS, _verify_safety(output)),
        (IntakeVerifierName.DECISION_CONSISTENCY, _verify_decision(output)),
        (IntakeVerifierName.OBSERVATION_LEGALITY, _verify_observations(output, input_payload)),
        (IntakeVerifierName.AUTHORITY_BOUNDARY, _verify_authority(output)),
    )
    for name, code in verifiers:
        checks.append(_check(name, code))
    return _report(checks, artifact)


def validate_intake_preflight(agent_spec: AgentSpec, run_spec: RunSpec) -> IntakeVerificationFailureCode | None:
    """Reject an invalid L3-1 invocation before any model request."""

    if not _valid_agent_spec(agent_spec):
        return IntakeVerificationFailureCode.AGENT_SPEC_INVALID
    if run_spec.stage not in INTAKE_ALLOWED_STAGES:
        return IntakeVerificationFailureCode.STAGE_NOT_ALLOWED
    if (
        run_spec.agent_spec_version != agent_spec.version
        or run_spec.prompt_version != INTAKE_PROMPT_VERSION
        or run_spec.policy_version != INTAKE_POLICY_VERSION
        or run_spec.total_attempt_budget != 1
    ):
        return IntakeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    return None


def _valid_agent_spec(spec: AgentSpec) -> bool:
    return (
        spec.name == INTAKE_AGENT_NAME
        and spec.version == INTAKE_AGENT_VERSION
        and spec.input_schema is IntakeExtractionInput
        and spec.output_schema is IntakeExtractionOutput
        and spec.model_policy.max_attempts == 1
        and spec.tool_permissions.issubset({Capability.READ_STATE})
        and not spec.failure_policy.retryable_codes
        and spec.verifier_chain == INTAKE_VERIFIER_CHAIN
    )


def _verify_schema(
    agent_spec: AgentSpec, output: BaseModel
) -> IntakeVerificationFailureCode | None:
    if not _valid_agent_spec(agent_spec):
        return IntakeVerificationFailureCode.AGENT_SPEC_INVALID
    if type(output) is not IntakeExtractionOutput:
        return IntakeVerificationFailureCode.OUTPUT_TYPE_INVALID
    try:
        IntakeExtractionOutput.model_validate(output.model_dump(round_trip=True))
    except (ValidationError, TypeError, ValueError):
        return IntakeVerificationFailureCode.SCHEMA_INVALID
    return None


def _verify_run(
    agent_spec: AgentSpec, run_spec: RunSpec, artifact: RunArtifact
) -> IntakeVerificationFailureCode | None:
    if (
        run_spec.agent_spec_version != agent_spec.version
        or run_spec.prompt_version != INTAKE_PROMPT_VERSION
        or run_spec.policy_version != INTAKE_POLICY_VERSION
        or run_spec.total_attempt_budget != 1
        or artifact.run_id != run_spec.run_id
        or artifact.trace_id != run_spec.trace_id
        or artifact.agent_spec_version != agent_spec.version
        or artifact.prompt_version != run_spec.prompt_version
        or artifact.attempts != 1
    ):
        return IntakeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    return None


def _verify_stage(run_spec: RunSpec) -> IntakeVerificationFailureCode | None:
    if run_spec.stage not in INTAKE_ALLOWED_STAGES:
        return IntakeVerificationFailureCode.STAGE_NOT_ALLOWED
    return None


def _verify_sources(
    output: IntakeExtractionOutput, input_payload: IntakeExtractionInput
) -> IntakeVerificationFailureCode | None:
    if any(message.role is not IntakeMessageRole.PATIENT for message in input_payload.current_messages):
        return IntakeVerificationFailureCode.SOURCE_NOT_ALLOWED
    allowed = {message.message_id for message in input_payload.current_messages}
    sources: list[Any] = [item.source_message_id for item in output.observations]
    sources.extend(item.source_message_id for item in output.red_flag_candidates)
    sources.extend(item.span.source_message_id for item in output.red_flag_candidates)
    sources.extend(item.source_message_id for item in output.ambiguities)
    for field in (
        output.patient_safety_delta.allergy,
        output.patient_safety_delta.pregnancy,
        output.patient_safety_delta.lactation,
        output.patient_safety_delta.medications,
        output.patient_safety_delta.major_conditions,
        output.patient_safety_delta.contraindications,
    ):
        if field.source_message_id is not None:
            sources.append(field.source_message_id)
    for field in (
        output.patient_safety_delta.allergy,
        output.patient_safety_delta.medications,
        output.patient_safety_delta.major_conditions,
        output.patient_safety_delta.contraindications,
    ):
        if field.value_spans is not None:
            sources.extend(span.source_message_id for span in field.value_spans)
        if field.negation_span is not None:
            sources.append(field.negation_span.source_message_id)
    for field in (
        output.patient_safety_delta.pregnancy,
        output.patient_safety_delta.lactation,
    ):
        if field.span is not None:
            sources.append(field.span.source_message_id)
    if any(source not in allowed for source in sources):
        return IntakeVerificationFailureCode.SOURCE_NOT_ALLOWED
    return None


_GROUNDING_FAILURE_MAP = {
    IntakeGroundingFailureKind.SPAN_INVALID: IntakeVerificationFailureCode.GROUNDING_SPAN_INVALID,
    IntakeGroundingFailureKind.VALUE_MISMATCH: IntakeVerificationFailureCode.GROUNDING_VALUE_MISMATCH,
    IntakeGroundingFailureKind.CONTEXT_UNSAFE: IntakeVerificationFailureCode.GROUNDING_CONTEXT_UNSAFE,
}


def _verify_grounding(
    output: IntakeExtractionOutput,
    input_payload: IntakeExtractionInput,
) -> IntakeVerificationFailureCode | None:
    failure = verify_intake_grounding(output, input_payload)
    return _GROUNDING_FAILURE_MAP.get(failure) if failure is not None else None


def _verify_safety(output: IntakeExtractionOutput) -> IntakeVerificationFailureCode | None:
    # DTO validators own the detailed relation.  Revalidation here protects
    # against model_construct/subclass bypasses at the runtime boundary.
    try:
        type(output.patient_safety_delta).model_validate(
            output.patient_safety_delta.model_dump(round_trip=True)
        )
    except (ValidationError, TypeError, ValueError):
        return IntakeVerificationFailureCode.SAFETY_SEMANTICS_INVALID
    return None


def _verify_decision(output: IntakeExtractionOutput) -> IntakeVerificationFailureCode | None:
    has_candidate = bool(
        output.observations
        or output.patient_safety_delta.has_candidate()
        or output.red_flag_candidates
    )
    if output.decision is IntakeExtractionDecision.EXTRACTED and not has_candidate:
        return IntakeVerificationFailureCode.DECISION_CONTENT_MISMATCH
    if output.decision is IntakeExtractionDecision.NEEDS_CLARIFICATION and not output.ambiguities:
        return IntakeVerificationFailureCode.DECISION_CONTENT_MISMATCH
    if output.decision is IntakeExtractionDecision.ABSTAINED and (has_candidate or output.ambiguities):
        return IntakeVerificationFailureCode.DECISION_CONTENT_MISMATCH
    return None


def _verify_observations(
    output: IntakeExtractionOutput, input_payload: IntakeExtractionInput
) -> IntakeVerificationFailureCode | None:
    history = {item.observation_id: item for item in input_payload.historical_active_facts}
    semantic_keys: set[str] = set()
    historical_keys = {_fact_value_key(item.fact_key, item.value, item.normalized_value) for item in history.values()}
    for item in output.observations:
        try:
            key = _fact_value_key(item.fact_key, item.value, item.normalized_value)
        except (TypeError, ValueError, OverflowError):
            return IntakeVerificationFailureCode.VALUE_NOT_JSON
        operation_key = f"{item.operation.value}:{item.target_observation_id}:{key}"
        if operation_key in semantic_keys:
            return IntakeVerificationFailureCode.DUPLICATE_OBSERVATION
        semantic_keys.add(operation_key)
        if item.operation is ObservationOperation.ADD and key in historical_keys:
            return IntakeVerificationFailureCode.HISTORICAL_FACT_REEXTRACTED
        if item.operation is not ObservationOperation.ADD:
            if item.target_observation_id is None:
                return IntakeVerificationFailureCode.CORRECTION_TARGET_INVALID
            target = history.get(item.target_observation_id)
            if target is None or target.fact_key != item.fact_key:
                return IntakeVerificationFailureCode.CORRECTION_TARGET_INVALID
    return None


_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "next_question",
        "route",
        "stage",
        "current_stage",
        "next_stage",
        "ready",
        "sufficient",
        "safety_passed",
        "safety_approved",
        "doctor_approved",
        "doctor_review",
        "gate_result",
        "transition",
    }
)
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


def _verify_authority(output: IntakeExtractionOutput) -> IntakeVerificationFailureCode | None:
    payload = output.model_dump(mode="python")
    if _contains_forbidden_key(payload):
        return IntakeVerificationFailureCode.AUTHORITY_FIELD_FORBIDDEN
    if any(_is_identity_key(item.fact_key) for item in output.observations):
        return IntakeVerificationFailureCode.IDENTITY_FACT_FORBIDDEN
    if _contains_identity_key(payload):
        return IntakeVerificationFailureCode.IDENTITY_FACT_FORBIDDEN
    if _contains_direct_identifier(payload):
        return IntakeVerificationFailureCode.IDENTITY_FACT_FORBIDDEN
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    except (TypeError, ValueError, OverflowError):
        return IntakeVerificationFailureCode.VALUE_NOT_JSON
    return None


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        if any(str(key).lower() in _FORBIDDEN_AUTHORITY_KEYS for key in value):
            return True
        return any(_contains_forbidden_key(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _contains_identity_key(value: Any) -> bool:
    if isinstance(value, dict):
        if any(_is_identity_key(str(key)) for key in value):
            return True
        return any(_contains_identity_key(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity_key(item) for item in value)
    return False


def _is_identity_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    tokens = tuple(token for token in normalized.split("_") if token)
    suffix_forms = frozenset("".join(tokens[index:]) for index in range(len(tokens)))
    return bool(suffix_forms.intersection(_IDENTITY_FACT_FORMS)) or bool(
        set(tokens).intersection({"name", "phone", "mobile", "telephone"})
    )


def _contains_direct_identifier(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _PII_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_direct_identifier(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_direct_identifier(item) for item in value)
    return False


def _has_undeclared_fields(raw: Any, canonical: Any) -> bool:
    """Compare raw runtime objects with the canonical schema recursively."""

    if isinstance(canonical, BaseModel):
        allowed = set(type(canonical).model_fields)
        if isinstance(raw, BaseModel):
            raw_keys = set(raw.__dict__)
            extra = getattr(raw, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                raw_keys.update(extra)
            if raw_keys - allowed:
                return True
            return any(
                _has_undeclared_fields(getattr(raw, name, None), getattr(canonical, name))
                for name in allowed
            )
        if isinstance(raw, dict):
            if set(raw) - allowed:
                return True
            return any(
                _has_undeclared_fields(raw.get(name), getattr(canonical, name))
                for name in allowed
            )
        return True
    if isinstance(canonical, (list, tuple)):
        if not isinstance(raw, (list, tuple)) or len(raw) != len(canonical):
            return True
        return any(_has_undeclared_fields(raw_item, item) for raw_item, item in zip(raw, canonical, strict=True))
    if isinstance(canonical, dict):
        return not isinstance(raw, dict)
    return isinstance(raw, (BaseModel, dict, list, tuple))


def _fact_value_key(fact_key: str, value: Any, normalized_value: Any) -> str:
    selected = normalized_value if normalized_value is not None else value
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{fact_key}:{encoded}"


def _check(
    name: IntakeVerifierName, code: IntakeVerificationFailureCode | None
) -> IntakeCheckResult:
    return IntakeCheckResult(
        verifier=name,
        status=IntakeCheckStatus.PASSED if code is None else IntakeCheckStatus.FAILED,
        failure_code=code,
    )


def _report(checks: list[IntakeCheckResult], artifact: RunArtifact) -> IntakeVerificationReport:
    first = next((check.failure_code for check in checks if check.failure_code is not None), None)
    try:
        subject = json.dumps(
            {
                "run_id": str(artifact.run_id),
                "type": f"{type(artifact.output).__module__}.{type(artifact.output).__qualname__}",
                "output": artifact.output.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        subject = f"{artifact.run_id}:{type(artifact.output).__module__}.{type(artifact.output).__qualname__}"
    digest = hashlib.sha256(subject.encode()).hexdigest()
    return IntakeVerificationReport(
        passed=first is None,
        checks=tuple(checks),
        failure_code=first,
        subject_digest=digest,
    )
