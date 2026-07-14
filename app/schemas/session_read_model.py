"""Versioned, read-only projections for persisted L3/L4 session results."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SESSION_READ_MODEL_SCHEMA_VERSION: Literal["session-read-model.v1"] = "session-read-model.v1"


class SessionGraphReadModelV1(BaseModel):
    """Latest graph execution metadata plus the current Domain revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_run_id: UUID | None = None
    graph_version: str | None = Field(default=None, min_length=1, max_length=64)
    revision: int = Field(ge=1, description="Current authoritative Domain state revision")
    input_state_version: int | None = Field(default=None, ge=1)
    status: Literal["running", "completed", "failed", "cancelled"] | None = None


class SessionGateReadModelV1(BaseModel):
    """A persisted policy-gate projection with its graph authority reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: UUID
    graph_run_id: UUID | None = None
    gate_name: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    input_state_version: int = Field(ge=1)
    decision: Literal["passed", "failed", "blocked"]
    details: dict[str, Any] | None = None


class SessionArtifactReadModelV1(BaseModel):
    """Integrity-checked current artifact safe to expose to session readers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    artifact_type: Literal["syndrome_draft", "formula_draft"]
    revision: int = Field(ge=1)
    input_state_version: int = Field(ge=1)
    status: Literal["current"]
    produced_by_run_id: UUID
    payload_schema_version: str = Field(min_length=1, max_length=64)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["completed", "needs_more_info", "abstained"]
    evidence_mode: str = Field(min_length=1, max_length=64)
    review_required: bool
    unresolved: tuple[str, ...] = ()
    verification_gate: SessionGateReadModelV1
    output: dict[str, Any]


class SessionUnresolvedReadModelV1(BaseModel):
    """Stable, non-sensitive reason why the current session is not fully resolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal[
        "triage",
        "completeness",
        "syndrome_draft",
        "formula_draft",
        "read_model",
        "safety_confirmation",
    ]
    kind: Literal[
        "red_flag",
        "missing_required",
        "conflict",
        "missing_input",
        "artifact_unavailable",
        "unconfirmed_safety_fact",
    ]
    key: str = Field(min_length=1, max_length=128)


class SessionReadModelV1(BaseModel):
    """Versioned authoritative L3/L4 read model returned with session details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["session-read-model.v1"] = SESSION_READ_MODEL_SCHEMA_VERSION
    agent_runtime: Literal["legacy", "langgraph"]
    graph: SessionGraphReadModelV1
    gates: tuple[SessionGateReadModelV1, ...] = ()
    artifacts: tuple[SessionArtifactReadModelV1, ...] = ()
    evidence_mode: str | None = Field(default=None, min_length=1, max_length=64)
    review_required: bool = False
    unresolved: tuple[SessionUnresolvedReadModelV1, ...] = ()


class SessionReadProjection(BaseModel):
    """Read model together with legacy response-field adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_model: SessionReadModelV1
    sufficiency_report: dict[str, Any] | None = None
    syndrome_result: dict[str, Any] | None = None
    base_formula: dict[str, Any] | None = None
    modified_formula: dict[str, Any] | None = None
    modifications: list[dict[str, Any]] | None = None


__all__ = [
    "SESSION_READ_MODEL_SCHEMA_VERSION",
    "SessionArtifactReadModelV1",
    "SessionGateReadModelV1",
    "SessionGraphReadModelV1",
    "SessionReadModelV1",
    "SessionReadProjection",
    "SessionUnresolvedReadModelV1",
]
