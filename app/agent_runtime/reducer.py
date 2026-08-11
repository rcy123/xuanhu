"""Pure Domain State contracts and reducer for the L2-4 Harness boundary.

Only L2-1 Observation, SafetyProfile, and ArtifactRevision objects are accepted.
This module has no persistence, graph, model, network, logging, or stage logic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent_runtime.observation_projection import project_current_observations
from app.agent_runtime.question_contract import QuestionContractIntegrityError, evaluate_contract_coverage
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from app.schemas.question_contract import QuestionContract, QuestionCoverageEvent

if TYPE_CHECKING:
    from app.agent_runtime.verifiers import VerificationContext


class ReducerErrorCode(StrEnum):
    VERIFICATION_CONTEXT_REQUIRED = "VERIFICATION_CONTEXT_REQUIRED"
    VERIFICATION_CONTEXT_MISMATCH = "VERIFICATION_CONTEXT_MISMATCH"
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    SESSION_MISMATCH = "SESSION_MISMATCH"
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


class DomainReducerError(ValueError):
    """A fixed-code rejection that never includes domain payload text."""

    def __init__(self, code: ReducerErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class DomainState(BaseModel):
    """In-memory authoritative-state snapshot consumed by the pure reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    state_version: int = Field(ge=1)
    observations: tuple[ObservationSchema, ...] = ()
    safety_profile: SafetyProfileSchema | None = None
    artifacts: tuple[ArtifactRevisionSchema, ...] = ()
    question_contracts: tuple[QuestionContract, ...] = ()
    question_coverage_events: tuple[QuestionCoverageEvent, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> DomainState:
        if any(item.session_id != self.session_id for item in self.observations):
            raise ValueError("observation session mismatch")
        if self.safety_profile is not None and self.safety_profile.session_id != self.session_id:
            raise ValueError("safety profile session mismatch")
        if any(item.session_id != self.session_id for item in self.artifacts):
            raise ValueError("artifact session mismatch")
        if any(item.session_id != self.session_id for item in self.question_contracts):
            raise ValueError("question contract session mismatch")
        if any(item.session_id != self.session_id for item in self.question_coverage_events):
            raise ValueError("question coverage event session mismatch")
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("duplicate observation id")
        artifact_keys = [(item.artifact_id, item.revision) for item in self.artifacts]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("duplicate artifact revision")
        current_ids = [item.artifact_id for item in self.artifacts if item.status is ArtifactStatus.CURRENT]
        if len(current_ids) != len(set(current_ids)):
            raise ValueError("multiple current revisions")
        contract_ids = [item.contract_id for item in self.question_contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("duplicate question contract id")
        event_ids = [item.event_id for item in self.question_coverage_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("duplicate question coverage event id")
        _validate_contract_chains(self.question_contracts, self.question_coverage_events)
        return self


class DomainDelta(BaseModel):
    """Restricted, immutable proposal accepted by the verifier/reducer pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delta_id: UUID
    run_id: UUID
    session_id: UUID
    expected_state_version: int = Field(ge=1)
    source_message_ids: tuple[UUID, ...] = ()
    observations: tuple[ObservationSchema, ...] = ()
    safety_profile: SafetyProfileSchema | None = None
    artifact_revisions: tuple[ArtifactRevisionSchema, ...] = ()
    invalidate_artifact_ids: tuple[UUID, ...] = ()
    question_contracts: tuple[QuestionContract, ...] = ()
    question_coverage_events: tuple[QuestionCoverageEvent, ...] = ()

    @field_validator("source_message_ids", "invalidate_artifact_ids")
    @classmethod
    def unique_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate identifiers are not allowed")
        return value

    @model_validator(mode="after")
    def unique_operation_keys(self) -> DomainDelta:
        observation_ids = [item.observation_id for item in self.observations]
        artifact_keys = [(item.artifact_id, item.revision) for item in self.artifact_revisions]
        contract_ids = [item.contract_id for item in self.question_contracts]
        event_ids = [item.event_id for item in self.question_coverage_events]
        if (
            len(observation_ids) != len(set(observation_ids))
            or len(artifact_keys) != len(set(artifact_keys))
            or len(contract_ids) != len(set(contract_ids))
            or len(event_ids) != len(set(event_ids))
        ):
            raise ValueError("duplicate operations are not allowed")
        return self


def _json_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainReducerError(ReducerErrorCode.VALUE_NOT_JSON) from exc


def _model_key(model: BaseModel) -> str:
    return _json_value(model.model_dump(mode="json"))


def domain_delta_digest(delta: DomainDelta) -> str:
    """Bind a report to one exact Delta without retaining its sensitive text."""

    payload = _model_key(delta).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _observation_value_key(observation: ObservationSchema) -> str:
    value = observation.normalized_value if observation.normalized_value is not None else observation.value
    return _json_value(value)


def _current_observations(observations: Sequence[ObservationSchema]) -> list[ObservationSchema]:
    # Chain heads are the current semantic truths: CORRECTED successors count,
    # superseded targets and RETRACTED heads do not.  The projection is the
    # single deterministic source so every consumer agrees on chain position.
    return list(project_current_observations(observations))


def _same_observation_event(left: ObservationSchema, right: ObservationSchema) -> bool:
    return (
        left.session_id == right.session_id
        and left.fact_key == right.fact_key
        and left.source_message_id == right.source_message_id
        and left.status is right.status
        and left.supersedes_observation_id == right.supersedes_observation_id
        and _observation_value_key(left) == _observation_value_key(right)
    )


def _merge_append_only[AppendOnlyModel: BaseModel](
    existing: Sequence[AppendOnlyModel],
    incoming: Sequence[AppendOnlyModel],
    *,
    identity: Callable[[AppendOnlyModel], UUID],
    conflict_code: ReducerErrorCode,
) -> tuple[tuple[AppendOnlyModel, ...], bool]:
    """Append-only merge: same identity byte-equal is a no-op; same identity
    with a different payload is an append-only violation and fails closed."""
    merged = list(existing)
    changed = False
    by_key = {identity(item): item for item in merged}
    for candidate in incoming:
        candidate_key = identity(candidate)
        current = by_key.get(candidate_key)
        if current is not None:
            if _model_key(current) != _model_key(candidate):
                raise DomainReducerError(conflict_code)
            continue
        by_key[candidate_key] = candidate
        merged.append(candidate)
        changed = True
    return tuple(merged), changed


def _validate_contract_chains(
    contracts: Sequence[QuestionContract],
    events: Sequence[QuestionCoverageEvent],
) -> None:
    """Validate every root contract chain independently via the deterministic
    coverage fold.  A session may hold multiple roots, and the fold assumes a
    single chain, so each root group is folded on its own."""
    answer_ids = [item.answer_message_id for item in events]
    if len(answer_ids) != len(set(answer_ids)):
        raise DomainReducerError(ReducerErrorCode.QUESTION_COVERAGE_CONFLICT)
    grouped: dict[UUID, tuple[list[QuestionContract], list[QuestionCoverageEvent]]] = {}
    for contract in contracts:
        grouped.setdefault(contract.root_contract_id, ([], []))[0].append(contract)
    for event in events:
        grouped.setdefault(event.root_contract_id, ([], []))[1].append(event)
    for root_contracts, root_events in grouped.values():
        try:
            evaluate_contract_coverage(root_contracts, root_events)
        except QuestionContractIntegrityError:
            raise DomainReducerError(ReducerErrorCode.QUESTION_CONTRACT_CHAIN_INVALID) from None


def _validate_common(state: DomainState, delta: DomainDelta) -> None:
    if delta.session_id != state.session_id:
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)
    if delta.expected_state_version != state.state_version:
        raise DomainReducerError(ReducerErrorCode.STATE_VERSION_CONFLICT)
    has_operation = bool(
        delta.observations
        or delta.safety_profile is not None
        or delta.artifact_revisions
        or delta.invalidate_artifact_ids
        or delta.question_contracts
        or delta.question_coverage_events
    )
    if not has_operation:
        raise DomainReducerError(ReducerErrorCode.EMPTY_DELTA)
    if (
        delta.observations
        or delta.safety_profile is not None
        or delta.question_contracts
        or delta.question_coverage_events
    ) and delta.artifact_revisions:
        raise DomainReducerError(ReducerErrorCode.MIXED_FACT_AND_ARTIFACT_CHANGE)

    sources = set(delta.source_message_ids)
    if any(item.source_message_id not in sources for item in delta.observations):
        raise DomainReducerError(ReducerErrorCode.OBSERVATION_SOURCE_UNDECLARED)
    if delta.safety_profile is not None and not sources:
        raise DomainReducerError(ReducerErrorCode.SAFETY_SOURCE_REQUIRED)
    if any(item.answer_message_id not in sources for item in delta.question_coverage_events):
        raise DomainReducerError(ReducerErrorCode.QUESTION_COVERAGE_SOURCE_UNDECLARED)
    if any(item.session_id != state.session_id for item in delta.observations):
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)
    if delta.safety_profile is not None and delta.safety_profile.session_id != state.session_id:
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)
    if any(item.session_id != state.session_id for item in delta.artifact_revisions):
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)
    if any(item.session_id != state.session_id for item in delta.question_contracts):
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)
    if any(item.session_id != state.session_id for item in delta.question_coverage_events):
        raise DomainReducerError(ReducerErrorCode.SESSION_MISMATCH)

    observation_ids = [item.observation_id for item in delta.observations]
    artifact_keys = [(item.artifact_id, item.revision) for item in delta.artifact_revisions]
    contract_ids = [item.contract_id for item in delta.question_contracts]
    event_ids = [item.event_id for item in delta.question_coverage_events]
    if (
        len(observation_ids) != len(set(observation_ids))
        or len(artifact_keys) != len(set(artifact_keys))
        or len(contract_ids) != len(set(contract_ids))
        or len(event_ids) != len(set(event_ids))
    ):
        raise DomainReducerError(ReducerErrorCode.DUPLICATE_OPERATION)
    if len(delta.invalidate_artifact_ids) != len(set(delta.invalidate_artifact_ids)):
        raise DomainReducerError(ReducerErrorCode.DUPLICATE_OPERATION)

    _model_key(delta)


def _apply_observations(
    existing: tuple[ObservationSchema, ...], incoming: tuple[ObservationSchema, ...]
) -> tuple[tuple[ObservationSchema, ...], bool]:
    observations = list(existing)
    changed = False
    for candidate in incoming:
        by_id = next((item for item in observations if item.observation_id == candidate.observation_id), None)
        if by_id is not None:
            if _model_key(by_id) != _model_key(candidate):
                raise DomainReducerError(ReducerErrorCode.OBSERVATION_ID_CONFLICT)
            continue

        if candidate.status is ObservationStatus.RETRACTED:
            if candidate.value is not None or candidate.normalized_value is not None:
                raise DomainReducerError(ReducerErrorCode.RETRACTION_VALUE_FORBIDDEN)
        elif candidate.value is None and candidate.normalized_value is None:
            raise DomainReducerError(ReducerErrorCode.OBSERVATION_VALUE_REQUIRED)

        # Semantic replay of an already-applied CORRECT/RETRACT event (new
        # observation id, identical provenance) is a no-op.  ACTIVE facts are
        # decided against the current chain head below: their "current value"
        # is the head's effective value, not any historical row, so re-adding
        # an old superseded value must conflict rather than replay.
        if candidate.status is not ObservationStatus.ACTIVE:
            semantic_duplicate = next(
                (item for item in observations if _same_observation_event(item, candidate)),
                None,
            )
            if semantic_duplicate is not None:
                continue

        current = _current_observations(observations)
        if candidate.status is ObservationStatus.ACTIVE:
            same_key = [item for item in current if item.fact_key == candidate.fact_key]
            if same_key:
                if any(_observation_value_key(item) == _observation_value_key(candidate) for item in same_key):
                    # Same canonical fact_key and same effective value as a
                    # current semantic fact: true no-op regardless of
                    # observation_id/source/confidence/created_at.
                    continue
                raise DomainReducerError(ReducerErrorCode.OBSERVATION_SOURCE_CONFLICT)
        else:
            target = next(
                (item for item in observations if item.observation_id == candidate.supersedes_observation_id),
                None,
            )
            if target is None:
                raise DomainReducerError(ReducerErrorCode.OBSERVATION_TARGET_NOT_FOUND)
            if all(item.observation_id != target.observation_id for item in current):
                raise DomainReducerError(ReducerErrorCode.OBSERVATION_TARGET_NOT_CURRENT)
            if target.fact_key != candidate.fact_key:
                raise DomainReducerError(ReducerErrorCode.OBSERVATION_FACT_KEY_MISMATCH)
            if candidate.status is ObservationStatus.CORRECTED:
                if _observation_value_key(target) == _observation_value_key(candidate):
                    # CORRECT to the same effective value as the exact current
                    # chain head is a true no-op regardless of new id/source.
                    continue
                # A different-value CORRECT must leave exactly one current truth
                # for its canonical key: any other current head of the same key
                # would produce two truths, which a single delta may not create.
                other_heads = [
                    item
                    for item in current
                    if item.fact_key == candidate.fact_key and item.observation_id != target.observation_id
                ]
                if other_heads:
                    raise DomainReducerError(ReducerErrorCode.OBSERVATION_SOURCE_CONFLICT)

        observations.append(candidate.model_copy(deep=True))
        changed = True
    return tuple(observations), changed


def _apply_artifacts(
    existing: tuple[ArtifactRevisionSchema, ...],
    incoming: tuple[ArtifactRevisionSchema, ...],
    invalidations: tuple[UUID, ...],
    *,
    run_id: UUID,
    expected_state_version: int,
    invalidate_all_current: bool,
) -> tuple[tuple[ArtifactRevisionSchema, ...], bool]:
    artifacts = [item.model_copy(deep=True) for item in existing]
    changed = False

    if invalidate_all_current:
        updated: list[ArtifactRevisionSchema] = []
        for item in artifacts:
            if item.status is ArtifactStatus.CURRENT:
                updated.append(item.model_copy(update={"status": ArtifactStatus.STALE}, deep=True))
                changed = True
            else:
                updated.append(item)
        artifacts = updated

    for artifact_id in invalidations:
        matching = [item for item in artifacts if item.artifact_id == artifact_id]
        if not matching:
            raise DomainReducerError(ReducerErrorCode.ARTIFACT_NOT_FOUND)
        updated = []
        for item in artifacts:
            if item.artifact_id == artifact_id and item.status is ArtifactStatus.CURRENT:
                updated.append(item.model_copy(update={"status": ArtifactStatus.STALE}, deep=True))
                changed = True
            else:
                updated.append(item)
        artifacts = updated

    for candidate in incoming:
        if candidate.status is not ArtifactStatus.CURRENT:
            raise DomainReducerError(ReducerErrorCode.ARTIFACT_STATUS_INVALID)
        if candidate.produced_by_run_id != run_id or candidate.input_state_version != expected_state_version:
            raise DomainReducerError(ReducerErrorCode.ARTIFACT_REVISION_CONFLICT)
        same_key = next(
            (
                item
                for item in artifacts
                if item.artifact_id == candidate.artifact_id and item.revision == candidate.revision
            ),
            None,
        )
        if same_key is not None:
            if _model_key(same_key) != _model_key(candidate):
                raise DomainReducerError(ReducerErrorCode.ARTIFACT_REVISION_CONFLICT)
            continue

        history = [item for item in artifacts if item.artifact_id == candidate.artifact_id]
        if not history:
            if candidate.revision != 1:
                raise DomainReducerError(ReducerErrorCode.ARTIFACT_PARENT_INVALID)
        else:
            latest = max(history, key=lambda item: item.revision)
            if (
                candidate.revision != latest.revision + 1
                or candidate.parent_revision != latest.revision
                or candidate.parent_revision_id is None
                or candidate.artifact_type != latest.artifact_type
            ):
                raise DomainReducerError(ReducerErrorCode.ARTIFACT_PARENT_INVALID)
            artifacts = [
                item.model_copy(update={"status": ArtifactStatus.SUPERSEDED}, deep=True)
                if item.artifact_id == candidate.artifact_id and item.status is ArtifactStatus.CURRENT
                else item
                for item in artifacts
            ]
        artifacts.append(candidate.model_copy(deep=True))
        changed = True
    return tuple(artifacts), changed


def _reduce_checked(state: DomainState, delta: DomainDelta) -> DomainState:
    _validate_common(state, delta)
    observations, observation_changed = _apply_observations(state.observations, delta.observations)
    safety_changed = delta.safety_profile is not None and (
        state.safety_profile is None or _model_key(state.safety_profile) != _model_key(delta.safety_profile)
    )
    safety_profile = (
        delta.safety_profile.model_copy(deep=True)
        if delta.safety_profile is not None
        else state.safety_profile.model_copy(deep=True)
        if state.safety_profile is not None
        else None
    )
    contracts, contract_changed = _merge_append_only(
        state.question_contracts,
        delta.question_contracts,
        identity=lambda item: item.contract_id,
        conflict_code=ReducerErrorCode.QUESTION_CONTRACT_CONFLICT,
    )
    events, event_changed = _merge_append_only(
        state.question_coverage_events,
        delta.question_coverage_events,
        identity=lambda item: item.event_id,
        conflict_code=ReducerErrorCode.QUESTION_COVERAGE_CONFLICT,
    )
    _validate_contract_chains(contracts, events)
    artifacts, artifact_changed = _apply_artifacts(
        state.artifacts,
        delta.artifact_revisions,
        delta.invalidate_artifact_ids,
        run_id=delta.run_id,
        expected_state_version=delta.expected_state_version,
        invalidate_all_current=observation_changed or safety_changed or contract_changed or event_changed,
    )
    changed = observation_changed or safety_changed or contract_changed or event_changed or artifact_changed
    if not changed:
        return state.model_copy(deep=True)
    return DomainState(
        session_id=state.session_id,
        state_version=state.state_version + 1,
        observations=observations,
        safety_profile=safety_profile,
        artifacts=artifacts,
        question_contracts=contracts,
        question_coverage_events=events,
    )


def validate_domain_delta(state: DomainState, delta: DomainDelta) -> None:
    """Run exactly the same legality rules used by the reducer, without mutation."""

    _reduce_checked(state, delta)


def reduce_domain_state(state: DomainState, delta: DomainDelta, context: VerificationContext) -> DomainState:
    """Re-verify a complete context, then apply its exact Delta to a new snapshot."""

    # The local import avoids a module cycle: verifiers use the reducer's
    # legality preflight, while the reducer must independently execute the
    # canonical chain at its authorization boundary.
    from app.agent_runtime.verifiers import DEFAULT_VERIFIER_CHAIN, VerificationContext

    if not isinstance(context, VerificationContext):
        raise DomainReducerError(ReducerErrorCode.VERIFICATION_CONTEXT_REQUIRED)
    if context.state != state:
        raise DomainReducerError(ReducerErrorCode.VERIFICATION_CONTEXT_MISMATCH)
    output = context.artifact.output
    if not isinstance(output, DomainDelta):
        raise DomainReducerError(ReducerErrorCode.VERIFICATION_CONTEXT_MISMATCH)
    try:
        digest = domain_delta_digest(delta)
    except DomainReducerError:
        raise
    if domain_delta_digest(output) != digest:
        raise DomainReducerError(ReducerErrorCode.VERIFICATION_CONTEXT_MISMATCH)
    report = DEFAULT_VERIFIER_CHAIN.verify(context)
    if not report.passed:
        if (
            report.failure_code is not None
            and report.failure_code.value == ReducerErrorCode.STATE_VERSION_CONFLICT.value
        ):
            raise DomainReducerError(ReducerErrorCode.STATE_VERSION_CONFLICT)
        raise DomainReducerError(ReducerErrorCode.VERIFICATION_REJECTED)
    return _reduce_checked(state, delta)
