"""Serializable contracts for the harness agent runtime."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Capability(StrEnum):
    READ_STATE = "read_state"
    READ_EVIDENCE = "read_evidence"
    WRITE_DATABASE = "write_database"
    WRITE_STATE = "write_state"
    TRANSITION_STAGE = "transition_stage"
    APPROVE_SAFETY = "approve_safety"
    APPROVE_DOCTOR_REVIEW = "approve_doctor_review"


class RuntimeErrorCode(StrEnum):
    AGENT_SPEC_VERSION_MISMATCH = "AGENT_SPEC_VERSION_MISMATCH"
    INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
    OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
    MODEL_GATEWAY_TIMEOUT = "MODEL_GATEWAY_TIMEOUT"
    MODEL_GATEWAY_UNAVAILABLE = "MODEL_GATEWAY_UNAVAILABLE"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    RUN_DEADLINE_EXCEEDED = "RUN_DEADLINE_EXCEEDED"
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    RECORDER_ASYNC_REQUIRED = "RECORDER_ASYNC_REQUIRED"


class ModelPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    timeout_seconds: float = Field(default=60, gt=0, le=86_400)
    max_attempts: int = Field(default=1, ge=1, le=100)


class FailurePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retryable_codes: frozenset[RuntimeErrorCode] = frozenset()


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    model_policy: ModelPolicy
    tool_permissions: frozenset[Capability] = frozenset()
    verifier_chain: tuple[str, ...] = ()
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    @field_validator("tool_permissions")
    @classmethod
    def read_only_capabilities(cls, values: frozenset[Capability]) -> frozenset[Capability]:
        forbidden = values - {Capability.READ_STATE, Capability.READ_EVIDENCE}
        if forbidden:
            raise ValueError("AgentSpec only permits read-only capabilities")
        return values


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID
    session_id: UUID
    state_version: int = Field(ge=1)
    stage: str = Field(min_length=1, max_length=100)
    agent_spec_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    deadline_at: datetime
    total_attempt_budget: int = Field(ge=1, le=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)

    @field_validator("deadline_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class RunArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    output: BaseModel
    model_actual: str = Field(min_length=1, max_length=200)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    attempts: int = Field(ge=1)
    latency_ms: int = Field(ge=0)
    evidence_ids: tuple[str, ...] = ()
    trace_id: str = Field(min_length=1)
    run_id: UUID
    agent_spec_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


def run_artifact_subject_digest(artifact: RunArtifact) -> str:
    """Stable digest for the full model-run provenance and output."""

    try:
        subject = json.dumps(
            {
                "run_id": str(artifact.run_id),
                "trace_id": artifact.trace_id,
                "agent_spec_version": artifact.agent_spec_version,
                "prompt_version": artifact.prompt_version,
                "model_actual": artifact.model_actual,
                "attempts": artifact.attempts,
                "latency_ms": artifact.latency_ms,
                "usage": artifact.usage.model_dump(mode="json"),
                "evidence_ids": list(artifact.evidence_ids),
                "type": f"{type(artifact.output).__module__}.{type(artifact.output).__qualname__}",
                "output": artifact.output.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        subject = (
            f"{artifact.run_id}:{artifact.trace_id}:{artifact.agent_spec_version}:"
            f"{artifact.prompt_version}:{artifact.model_actual}:{artifact.attempts}:"
            f"{artifact.latency_ms}:{type(artifact.output).__module__}.{type(artifact.output).__qualname__}"
        )
    return hashlib.sha256(subject.encode()).hexdigest()
