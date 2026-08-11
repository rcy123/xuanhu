"""Deterministic, composable verification chain for L2-4 model products."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_runtime.reducer import (
    DomainDelta,
    DomainReducerError,
    DomainState,
    ReducerErrorCode,
    domain_delta_digest,
    validate_domain_delta,
)
from app.agent_runtime.specs import AgentSpec, RunArtifact, RunSpec


class VerifierName(StrEnum):
    SCHEMA = "schema"
    OUTPUT_TYPE = "output_type"
    PROVENANCE_VERSION = "provenance_version"
    PREREQUISITES = "prerequisites"
    DELTA_LEGALITY = "delta_legality"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationFailureClass(StrEnum):
    SCHEMA = "schema"
    TYPE = "type"
    PROVENANCE = "provenance"
    VERSION_CONFLICT = "version_conflict"
    PRECONDITION = "precondition"
    DELTA = "delta"
    CHAIN = "chain"


class VerificationFailureCode(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    OUTPUT_TYPE_INVALID = "OUTPUT_TYPE_INVALID"
    OUTPUT_NOT_DOMAIN_DELTA = "OUTPUT_NOT_DOMAIN_DELTA"
    RUN_PROVENANCE_MISMATCH = "RUN_PROVENANCE_MISMATCH"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    SOURCE_NOT_ALLOWED = "SOURCE_NOT_ALLOWED"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    STAGE_NOT_ALLOWED = "STAGE_NOT_ALLOWED"
    PREREQUISITE_MISSING = "PREREQUISITE_MISSING"
    EMPTY_DELTA = "EMPTY_DELTA"
    DUPLICATE_OPERATION = "DUPLICATE_OPERATION"
    VALUE_NOT_JSON = "VALUE_NOT_JSON"
    OBSERVATION_SOURCE_UNDECLARED = "OBSERVATION_SOURCE_UNDECLARED"
    OBSERVATION_VALUE_REQUIRED = "OBSERVATION_VALUE_REQUIRED"
    RETRACTION_VALUE_FORBIDDEN = "RETRACTION_VALUE_FORBIDDEN"
    OBSERVATION_ID_CONFLICT = "OBSERVATION_ID_CONFLICT"
    OBSERVATION_SOURCE_CONFLICT = "OBSERVATION_SOURCE_CONFLICT"
    OBSERVATION_TARGET_NOT_FOUND = "OBSERVATION_TARGET_NOT_FOUND"
    OBSERVATION_TARGET_NOT_CURRENT = "OBSERVATION_TARGET_NOT_CURRENT"
    OBSERVATION_FACT_KEY_MISMATCH = "OBSERVATION_FACT_KEY_MISMATCH"
    SAFETY_SOURCE_REQUIRED = "SAFETY_SOURCE_REQUIRED"
    MIXED_FACT_AND_ARTIFACT_CHANGE = "MIXED_FACT_AND_ARTIFACT_CHANGE"
    ARTIFACT_REVISION_CONFLICT = "ARTIFACT_REVISION_CONFLICT"
    ARTIFACT_PARENT_INVALID = "ARTIFACT_PARENT_INVALID"
    ARTIFACT_STATUS_INVALID = "ARTIFACT_STATUS_INVALID"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    QUESTION_CONTRACT_CONFLICT = "QUESTION_CONTRACT_CONFLICT"
    QUESTION_COVERAGE_CONFLICT = "QUESTION_COVERAGE_CONFLICT"
    QUESTION_COVERAGE_SOURCE_UNDECLARED = "QUESTION_COVERAGE_SOURCE_UNDECLARED"
    QUESTION_CONTRACT_CHAIN_INVALID = "QUESTION_CONTRACT_CHAIN_INVALID"
    DELTA_INVALID = "DELTA_INVALID"
    VERIFIER_CHAIN_INCOMPLETE = "VERIFIER_CHAIN_INCOMPLETE"


_FAILURE_POLICY: dict[
    VerificationFailureCode,
    tuple[VerificationFailureClass, bool, bool],
] = {
    VerificationFailureCode.SCHEMA_INVALID: (VerificationFailureClass.SCHEMA, True, False),
    VerificationFailureCode.OUTPUT_TYPE_INVALID: (VerificationFailureClass.TYPE, True, False),
    VerificationFailureCode.OUTPUT_NOT_DOMAIN_DELTA: (VerificationFailureClass.TYPE, False, False),
    VerificationFailureCode.RUN_PROVENANCE_MISMATCH: (VerificationFailureClass.PROVENANCE, True, False),
    VerificationFailureCode.SESSION_MISMATCH: (VerificationFailureClass.PROVENANCE, False, False),
    VerificationFailureCode.SOURCE_NOT_ALLOWED: (VerificationFailureClass.PROVENANCE, True, False),
    VerificationFailureCode.STATE_VERSION_CONFLICT: (VerificationFailureClass.VERSION_CONFLICT, True, False),
    VerificationFailureCode.STAGE_NOT_ALLOWED: (VerificationFailureClass.PRECONDITION, False, False),
    VerificationFailureCode.PREREQUISITE_MISSING: (VerificationFailureClass.PRECONDITION, False, False),
    VerificationFailureCode.VERIFIER_CHAIN_INCOMPLETE: (VerificationFailureClass.CHAIN, False, False),
}

_HUMAN_DELTA_FAILURES = frozenset(
    {
        VerificationFailureCode.OBSERVATION_ID_CONFLICT,
        VerificationFailureCode.OBSERVATION_SOURCE_CONFLICT,
        VerificationFailureCode.OBSERVATION_TARGET_NOT_CURRENT,
        VerificationFailureCode.ARTIFACT_REVISION_CONFLICT,
        VerificationFailureCode.ARTIFACT_PARENT_INVALID,
        VerificationFailureCode.QUESTION_CONTRACT_CONFLICT,
        VerificationFailureCode.QUESTION_COVERAGE_CONFLICT,
    }
)


def _failure_policy(
    code: VerificationFailureCode,
) -> tuple[VerificationFailureClass, bool, bool]:
    if code in _FAILURE_POLICY:
        return _FAILURE_POLICY[code]
    return VerificationFailureClass.DELTA, False, code in _HUMAN_DELTA_FAILURES


class CheckResult(BaseModel):
    """Immutable and payload-free result from one deterministic verifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: VerifierName
    status: CheckStatus
    failure_code: VerificationFailureCode | None = None

    @model_validator(mode="after")
    def validate_status(self) -> CheckResult:
        if (self.status is CheckStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("failure_code is present exactly for failed checks")
        return self


class VerificationReport(BaseModel):
    """Stable audit report containing no Prompt, output, exception, or identity text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checks: tuple[CheckResult, ...] = Field(min_length=1)
    failure_class: VerificationFailureClass | None = None
    failure_code: VerificationFailureCode | None = None
    retry_allowed: bool = False
    requires_human: bool = False
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deterministic_policy(self) -> VerificationReport:
        passed = all(check.status is CheckStatus.PASSED for check in self.checks)
        first_failure = next(
            (check.failure_code for check in self.checks if check.status is CheckStatus.FAILED),
            None,
        )
        if not passed and first_failure is None:
            first_failure = VerificationFailureCode.VERIFIER_CHAIN_INCOMPLETE
        failure_class: VerificationFailureClass | None = None
        retry_allowed = False
        requires_human = False
        if first_failure is not None:
            failure_class, retry_allowed, requires_human = _failure_policy(first_failure)
        if (
            self.passed != passed
            or self.failure_code is not first_failure
            or self.failure_class is not failure_class
            or self.retry_allowed != retry_allowed
            or self.requires_human != requires_human
        ):
            raise ValueError("report outcome must match deterministic check policy")
        return self

    @classmethod
    def from_checks(cls, checks: tuple[CheckResult, ...], *, subject_digest: str) -> VerificationReport:
        passed = all(check.status is CheckStatus.PASSED for check in checks)
        first_failure = next(
            (check.failure_code for check in checks if check.status is CheckStatus.FAILED),
            None,
        )
        if not passed and first_failure is None:
            first_failure = VerificationFailureCode.VERIFIER_CHAIN_INCOMPLETE
        failure_class: VerificationFailureClass | None = None
        retry_allowed = False
        requires_human = False
        if first_failure is not None:
            failure_class, retry_allowed, requires_human = _failure_policy(first_failure)
        return cls(
            passed=passed,
            checks=checks,
            failure_class=failure_class,
            failure_code=first_failure,
            retry_allowed=retry_allowed,
            requires_human=requires_human,
            subject_digest=subject_digest,
        )


class VerificationContext(BaseModel):
    """Ephemeral verifier input; it is never part of VerificationReport."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_spec: AgentSpec
    run_spec: RunSpec
    artifact: RunArtifact
    state: DomainState
    allowed_source_message_ids: frozenset[UUID] = frozenset()
    allowed_stages: frozenset[str] = Field(min_length=1)
    required_prerequisites: frozenset[str] = frozenset()
    satisfied_prerequisites: frozenset[str] = frozenset()


def _passed(name: VerifierName) -> CheckResult:
    return CheckResult(verifier=name, status=CheckStatus.PASSED)


def _failed(name: VerifierName, code: VerificationFailureCode) -> CheckResult:
    return CheckResult(verifier=name, status=CheckStatus.FAILED, failure_code=code)


def _skipped(name: VerifierName) -> CheckResult:
    return CheckResult(verifier=name, status=CheckStatus.SKIPPED)


class Verifier(BaseModel, ABC):
    """Immutable verifier contract. Implementations must be stateless and local."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: VerifierName

    @abstractmethod
    def verify(self, context: VerificationContext) -> CheckResult:
        raise NotImplementedError


class SchemaVerifier(Verifier):
    name: Literal[VerifierName.SCHEMA] = VerifierName.SCHEMA

    def verify(self, context: VerificationContext) -> CheckResult:
        try:
            payload = context.artifact.output.model_dump(round_trip=True)
            context.agent_spec.output_schema.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            return _failed(self.name, VerificationFailureCode.SCHEMA_INVALID)
        return _passed(self.name)


class OutputTypeVerifier(Verifier):
    name: Literal[VerifierName.OUTPUT_TYPE] = VerifierName.OUTPUT_TYPE

    def verify(self, context: VerificationContext) -> CheckResult:
        try:
            is_delta_schema = issubclass(context.agent_spec.output_schema, DomainDelta)
        except TypeError:
            is_delta_schema = False
        if not is_delta_schema:
            return _failed(self.name, VerificationFailureCode.OUTPUT_NOT_DOMAIN_DELTA)
        if type(context.artifact.output) is not context.agent_spec.output_schema:
            return _failed(self.name, VerificationFailureCode.OUTPUT_TYPE_INVALID)
        return _passed(self.name)


class ProvenanceVersionVerifier(Verifier):
    name: Literal[VerifierName.PROVENANCE_VERSION] = VerifierName.PROVENANCE_VERSION

    def verify(self, context: VerificationContext) -> CheckResult:
        output = context.artifact.output
        if not isinstance(output, DomainDelta):
            return _skipped(self.name)
        try:
            if (
                context.artifact.run_id != context.run_spec.run_id
                or output.run_id != context.run_spec.run_id
                or context.artifact.trace_id != context.run_spec.trace_id
                or context.artifact.agent_spec_version != context.agent_spec.version
                or context.run_spec.agent_spec_version != context.agent_spec.version
                or context.artifact.prompt_version != context.run_spec.prompt_version
            ):
                return _failed(self.name, VerificationFailureCode.RUN_PROVENANCE_MISMATCH)
            if output.session_id != context.run_spec.session_id or output.session_id != context.state.session_id:
                return _failed(self.name, VerificationFailureCode.SESSION_MISMATCH)
            if (
                output.expected_state_version != context.run_spec.state_version
                or output.expected_state_version != context.state.state_version
            ):
                return _failed(self.name, VerificationFailureCode.STATE_VERSION_CONFLICT)
            allowed_sources = context.allowed_source_message_ids
            if any(source_id not in allowed_sources for source_id in output.source_message_ids):
                return _failed(self.name, VerificationFailureCode.SOURCE_NOT_ALLOWED)
            if any(
                item.source_message_id not in output.source_message_ids or item.source_message_id not in allowed_sources
                for item in output.observations
            ):
                return _failed(self.name, VerificationFailureCode.SOURCE_NOT_ALLOWED)
            if any(
                item.answer_message_id not in output.source_message_ids or item.answer_message_id not in allowed_sources
                for item in output.question_coverage_events
            ):
                return _failed(self.name, VerificationFailureCode.SOURCE_NOT_ALLOWED)
            if any(
                item.produced_by_run_id != context.run_spec.run_id
                or item.input_state_version != context.run_spec.state_version
                or item.session_id != context.run_spec.session_id
                for item in output.artifact_revisions
            ):
                return _failed(self.name, VerificationFailureCode.RUN_PROVENANCE_MISMATCH)
        except (AttributeError, TypeError, ValueError):
            return _failed(self.name, VerificationFailureCode.RUN_PROVENANCE_MISMATCH)
        return _passed(self.name)


class PrerequisiteVerifier(Verifier):
    name: Literal[VerifierName.PREREQUISITES] = VerifierName.PREREQUISITES

    def verify(self, context: VerificationContext) -> CheckResult:
        if context.run_spec.stage not in context.allowed_stages:
            return _failed(self.name, VerificationFailureCode.STAGE_NOT_ALLOWED)
        if not context.required_prerequisites.issubset(context.satisfied_prerequisites):
            return _failed(self.name, VerificationFailureCode.PREREQUISITE_MISSING)
        return _passed(self.name)


_REDUCER_FAILURE_MAP: dict[ReducerErrorCode, VerificationFailureCode] = {
    ReducerErrorCode.STATE_VERSION_CONFLICT: VerificationFailureCode.STATE_VERSION_CONFLICT,
    ReducerErrorCode.SESSION_MISMATCH: VerificationFailureCode.SESSION_MISMATCH,
    ReducerErrorCode.EMPTY_DELTA: VerificationFailureCode.EMPTY_DELTA,
    ReducerErrorCode.DUPLICATE_OPERATION: VerificationFailureCode.DUPLICATE_OPERATION,
    ReducerErrorCode.VALUE_NOT_JSON: VerificationFailureCode.VALUE_NOT_JSON,
    ReducerErrorCode.OBSERVATION_SOURCE_UNDECLARED: VerificationFailureCode.OBSERVATION_SOURCE_UNDECLARED,
    ReducerErrorCode.OBSERVATION_VALUE_REQUIRED: VerificationFailureCode.OBSERVATION_VALUE_REQUIRED,
    ReducerErrorCode.RETRACTION_VALUE_FORBIDDEN: VerificationFailureCode.RETRACTION_VALUE_FORBIDDEN,
    ReducerErrorCode.OBSERVATION_ID_CONFLICT: VerificationFailureCode.OBSERVATION_ID_CONFLICT,
    ReducerErrorCode.OBSERVATION_SOURCE_CONFLICT: VerificationFailureCode.OBSERVATION_SOURCE_CONFLICT,
    ReducerErrorCode.OBSERVATION_TARGET_NOT_FOUND: VerificationFailureCode.OBSERVATION_TARGET_NOT_FOUND,
    ReducerErrorCode.OBSERVATION_TARGET_NOT_CURRENT: VerificationFailureCode.OBSERVATION_TARGET_NOT_CURRENT,
    ReducerErrorCode.OBSERVATION_FACT_KEY_MISMATCH: VerificationFailureCode.OBSERVATION_FACT_KEY_MISMATCH,
    ReducerErrorCode.SAFETY_SOURCE_REQUIRED: VerificationFailureCode.SAFETY_SOURCE_REQUIRED,
    ReducerErrorCode.MIXED_FACT_AND_ARTIFACT_CHANGE: VerificationFailureCode.MIXED_FACT_AND_ARTIFACT_CHANGE,
    ReducerErrorCode.ARTIFACT_REVISION_CONFLICT: VerificationFailureCode.ARTIFACT_REVISION_CONFLICT,
    ReducerErrorCode.ARTIFACT_PARENT_INVALID: VerificationFailureCode.ARTIFACT_PARENT_INVALID,
    ReducerErrorCode.ARTIFACT_STATUS_INVALID: VerificationFailureCode.ARTIFACT_STATUS_INVALID,
    ReducerErrorCode.ARTIFACT_NOT_FOUND: VerificationFailureCode.ARTIFACT_NOT_FOUND,
    ReducerErrorCode.QUESTION_CONTRACT_CONFLICT: VerificationFailureCode.QUESTION_CONTRACT_CONFLICT,
    ReducerErrorCode.QUESTION_COVERAGE_CONFLICT: VerificationFailureCode.QUESTION_COVERAGE_CONFLICT,
    ReducerErrorCode.QUESTION_COVERAGE_SOURCE_UNDECLARED: VerificationFailureCode.QUESTION_COVERAGE_SOURCE_UNDECLARED,
    ReducerErrorCode.QUESTION_CONTRACT_CHAIN_INVALID: VerificationFailureCode.QUESTION_CONTRACT_CHAIN_INVALID,
}


class DeltaLegalityVerifier(Verifier):
    name: Literal[VerifierName.DELTA_LEGALITY] = VerifierName.DELTA_LEGALITY

    def verify(self, context: VerificationContext) -> CheckResult:
        output = context.artifact.output
        if not isinstance(output, DomainDelta):
            return _skipped(self.name)
        try:
            validate_domain_delta(context.state, output)
        except DomainReducerError as exc:
            return _failed(self.name, _REDUCER_FAILURE_MAP.get(exc.code, VerificationFailureCode.DELTA_INVALID))
        except Exception:
            return _failed(self.name, VerificationFailureCode.DELTA_INVALID)
        return _passed(self.name)


class VerifierChain(BaseModel):
    """A tuple-backed chain: caller order is preserved and duplicate checks are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifiers: tuple[Verifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_verifiers(self) -> VerifierChain:
        names = [verifier.name for verifier in self.verifiers]
        if len(names) != len(set(names)):
            raise ValueError("verifier names must be unique")
        return self

    def verify(self, context: VerificationContext) -> VerificationReport:
        checks = tuple(verifier.verify(context) for verifier in self.verifiers)
        return VerificationReport.from_checks(checks, subject_digest=_subject_digest(context))


def _subject_digest(context: VerificationContext) -> str:
    output = context.artifact.output
    if isinstance(output, DomainDelta):
        try:
            return domain_delta_digest(output)
        except DomainReducerError:
            pass
    try:
        payload = json.dumps(
            {
                "run_id": str(context.run_spec.run_id),
                "type": f"{type(output).__module__}.{type(output).__qualname__}",
                "output": output.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        payload = f"{context.run_spec.run_id}:{type(output).__module__}.{type(output).__qualname__}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


DEFAULT_VERIFIER_CHAIN = VerifierChain(
    verifiers=(
        SchemaVerifier(),
        OutputTypeVerifier(),
        ProvenanceVersionVerifier(),
        PrerequisiteVerifier(),
        DeltaLegalityVerifier(),
    )
)
