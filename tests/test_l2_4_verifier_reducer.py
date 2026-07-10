"""L2-4 deterministic verifier chain and pure Domain Reducer contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.agent_runtime.reducer import (
    DomainDelta,
    DomainReducerError,
    DomainState,
    ReducerErrorCode,
    domain_delta_digest,
    reduce_domain_state,
)
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.verifiers import (
    DEFAULT_VERIFIER_CHAIN,
    CheckResult,
    CheckStatus,
    DeltaLegalityVerifier,
    OutputTypeVerifier,
    PrerequisiteVerifier,
    ProvenanceVersionVerifier,
    SchemaVerifier,
    VerificationContext,
    VerificationFailureClass,
    VerificationFailureCode,
    VerificationReport,
    VerifierName,
)
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)


class InputPayload(BaseModel):
    command: str


class WrongOutput(BaseModel):
    answer: str


def observation(
    *,
    session_id: UUID,
    source_id: UUID,
    fact_key: str = "chief_complaint",
    value: object = "headache",
    status: ObservationStatus = ObservationStatus.ACTIVE,
    target_id: UUID | None = None,
    observation_id: UUID | None = None,
) -> ObservationSchema:
    return ObservationSchema(
        observation_id=observation_id or uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value=value,
        source_message_id=source_id,
        status=status,
        supersedes_observation_id=target_id,
        created_at=datetime.now(UTC),
    )


def artifact_revision(
    *,
    session_id: UUID,
    run_id: UUID,
    artifact_id: UUID,
    revision: int,
    input_version: int,
    status: ArtifactStatus = ArtifactStatus.CURRENT,
    parent_id: UUID | None = None,
) -> ArtifactRevisionSchema:
    return ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type="formula",
        revision=revision,
        session_id=session_id,
        input_state_version=input_version,
        status=status,
        produced_by_run_id=run_id,
        parent_revision_id=parent_id,
        parent_revision=revision - 1 if revision > 1 else None,
        created_at=datetime.now(UTC),
    )


def make_delta(
    *,
    state: DomainState,
    run_id: UUID,
    observations: tuple[ObservationSchema, ...] = (),
    sources: tuple[UUID, ...] = (),
    safety_profile: SafetyProfileSchema | None = None,
    artifacts: tuple[ArtifactRevisionSchema, ...] = (),
    invalidations: tuple[UUID, ...] = (),
    expected_version: int | None = None,
) -> DomainDelta:
    return DomainDelta(
        delta_id=uuid4(),
        run_id=run_id,
        session_id=state.session_id,
        expected_state_version=expected_version or state.state_version,
        source_message_ids=sources,
        observations=observations,
        safety_profile=safety_profile,
        artifact_revisions=artifacts,
        invalidate_artifact_ids=invalidations,
    )


def make_context(
    delta: BaseModel,
    state: DomainState,
    *,
    run_id: UUID | None = None,
    sources: frozenset[UUID] = frozenset(),
    allowed_stages: frozenset[str] = frozenset({"intake"}),
    required: frozenset[str] = frozenset(),
    satisfied: frozenset[str] = frozenset(),
    output_schema: type[BaseModel] = DomainDelta,
    state_version: int | None = None,
) -> VerificationContext:
    actual_run_id = run_id or getattr(delta, "run_id", uuid4())
    spec = AgentSpec(
        name="local-delta-agent",
        version="agent-v1",
        input_schema=InputPayload,
        output_schema=output_schema,
        model_policy=ModelPolicy(model="fake-local-model"),
    )
    run_spec = RunSpec(
        run_id=actual_run_id,
        session_id=state.session_id,
        state_version=state_version or state.state_version,
        stage="intake",
        agent_spec_version=spec.version,
        prompt_version="prompt-v1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        total_attempt_budget=1,
        idempotency_key="idempotency-key",
        trace_id="trace-1",
    )
    run_artifact = RunArtifact(
        output=delta,
        model_actual="fake-local-model",
        attempts=1,
        latency_ms=0,
        trace_id=run_spec.trace_id,
        run_id=run_spec.run_id,
        agent_spec_version=spec.version,
        prompt_version=run_spec.prompt_version,
    )
    return VerificationContext(
        agent_spec=spec,
        run_spec=run_spec,
        artifact=run_artifact,
        state=state,
        allowed_source_message_ids=sources,
        allowed_stages=allowed_stages,
        required_prerequisites=required,
        satisfied_prerequisites=satisfied,
    )


def valid_context() -> tuple[VerificationContext, DomainDelta, DomainState]:
    session_id, source_id, run_id = uuid4(), uuid4(), uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    item = observation(session_id=session_id, source_id=source_id)
    delta = make_delta(state=state, run_id=run_id, observations=(item,), sources=(source_id,))
    return make_context(delta, state, sources=frozenset({source_id})), delta, state


def verified(
    delta: DomainDelta,
    state: DomainState,
    *,
    sources: frozenset[UUID] = frozenset(),
) -> VerificationReport:
    return DEFAULT_VERIFIER_CHAIN.verify(make_context(delta, state, sources=sources))


def authorized(
    delta: DomainDelta,
    state: DomainState,
    *,
    sources: frozenset[UUID] = frozenset(),
    allowed_stages: frozenset[str] = frozenset({"intake"}),
    required: frozenset[str] = frozenset(),
    satisfied: frozenset[str] = frozenset(),
    state_version: int | None = None,
) -> VerificationContext:
    return make_context(
        delta,
        state,
        sources=sources,
        allowed_stages=allowed_stages,
        required=required,
        satisfied=satisfied,
        state_version=state_version,
    )


def forged_passed_report(delta: DomainDelta) -> VerificationReport:
    checks = tuple(CheckResult(verifier=name, status=CheckStatus.PASSED) for name in VerifierName)
    return VerificationReport.from_checks(checks, subject_digest=domain_delta_digest(delta))


def test_contracts_are_frozen_and_default_chain_order_is_stable() -> None:
    context, _, _ = valid_context()
    report = DEFAULT_VERIFIER_CHAIN.verify(context)
    assert report.passed
    assert tuple(check.verifier for check in report.checks) == tuple(VerifierName)
    with pytest.raises(ValidationError):
        report.passed = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DEFAULT_VERIFIER_CHAIN.verifiers = ()  # type: ignore[misc]


def test_schema_verifier_accepts_valid_and_rejects_constructed_invalid_schema() -> None:
    context, delta, state = valid_context()
    assert SchemaVerifier().verify(context).status is CheckStatus.PASSED
    invalid = delta.model_copy(update={"expected_state_version": 0})
    result = SchemaVerifier().verify(make_context(invalid, state, run_id=delta.run_id))
    assert result.failure_code is VerificationFailureCode.SCHEMA_INVALID


def test_output_type_verifier_accepts_domain_delta_and_rejects_wrong_type() -> None:
    context, _, state = valid_context()
    assert OutputTypeVerifier().verify(context).status is CheckStatus.PASSED
    wrong = WrongOutput(answer="not a delta")
    result = OutputTypeVerifier().verify(make_context(wrong, state, output_schema=WrongOutput))
    assert result.failure_code is VerificationFailureCode.OUTPUT_NOT_DOMAIN_DELTA
    exact_type_result = OutputTypeVerifier().verify(make_context(wrong, state, output_schema=DomainDelta))
    assert exact_type_result.failure_code is VerificationFailureCode.OUTPUT_TYPE_INVALID


def test_provenance_verifier_accepts_valid_and_rejects_source_and_version() -> None:
    context, delta, state = valid_context()
    assert ProvenanceVersionVerifier().verify(context).status is CheckStatus.PASSED
    source_failure = ProvenanceVersionVerifier().verify(make_context(delta, state))
    assert source_failure.failure_code is VerificationFailureCode.SOURCE_NOT_ALLOWED
    stale_state = state.model_copy(update={"state_version": 2})
    stale_failure = ProvenanceVersionVerifier().verify(
        make_context(delta, stale_state, sources=frozenset(delta.source_message_ids), state_version=1)
    )
    assert stale_failure.failure_code is VerificationFailureCode.STATE_VERSION_CONFLICT


def test_prerequisite_verifier_accepts_satisfied_and_rejects_stage_or_missing_gate() -> None:
    context, delta, state = valid_context()
    passing = context.model_copy(
        update={
            "required_prerequisites": frozenset({"message_persisted"}),
            "satisfied_prerequisites": frozenset({"message_persisted"}),
        }
    )
    assert PrerequisiteVerifier().verify(passing).status is CheckStatus.PASSED
    stage_failure = PrerequisiteVerifier().verify(
        make_context(delta, state, sources=frozenset(delta.source_message_ids), allowed_stages=frozenset({"reasoning"}))
    )
    assert stage_failure.failure_code is VerificationFailureCode.STAGE_NOT_ALLOWED
    missing = PrerequisiteVerifier().verify(
        context.model_copy(update={"required_prerequisites": frozenset({"message_persisted"})})
    )
    assert missing.failure_code is VerificationFailureCode.PREREQUISITE_MISSING


def test_delta_legality_verifier_accepts_valid_and_rejects_empty_delta() -> None:
    context, delta, state = valid_context()
    assert DeltaLegalityVerifier().verify(context).status is CheckStatus.PASSED
    empty = delta.model_copy(update={"observations": (), "source_message_ids": ()})
    failure = DeltaLegalityVerifier().verify(make_context(empty, state, run_id=delta.run_id))
    assert failure.failure_code is VerificationFailureCode.EMPTY_DELTA


def test_report_policy_and_privacy_are_deterministic_and_payload_free() -> None:
    context, delta, state = valid_context()
    source_id = delta.source_message_ids[0]
    secret_observation = observation(
        session_id=state.session_id,
        source_id=source_id,
        value="patient Alice api-key=secret raw prompt",
    )
    secret_delta = delta.model_copy(update={"observations": (secret_observation,)})
    report = DEFAULT_VERIFIER_CHAIN.verify(
        make_context(secret_delta, state, sources=frozenset({source_id}), allowed_stages=frozenset({"reasoning"}))
    )
    assert report.failure_class is VerificationFailureClass.PRECONDITION
    assert report.retry_allowed is False
    assert report.requires_human is False
    serialized = report.model_dump_json()
    for secret in ("Alice", "secret", "prompt", "raw", str(state.session_id), str(source_id)):
        assert secret not in serialized


def test_source_rejection_and_forged_passed_report_cannot_authorize_reducer() -> None:
    context, delta, state = valid_context()
    unauthorized_context = context.model_copy(update={"allowed_source_message_ids": frozenset()})
    rejected = DEFAULT_VERIFIER_CHAIN.verify(unauthorized_context)
    assert rejected.failure_code is VerificationFailureCode.SOURCE_NOT_ALLOWED

    with pytest.raises(DomainReducerError) as forged:
        reduce_domain_state(state, delta, forged_passed_report(delta))  # type: ignore[arg-type]
    assert forged.value.code is ReducerErrorCode.VERIFICATION_CONTEXT_REQUIRED
    with pytest.raises(DomainReducerError) as real_rejection:
        reduce_domain_state(state, delta, unauthorized_context)
    assert real_rejection.value.code is ReducerErrorCode.VERIFICATION_REJECTED


@pytest.mark.parametrize(
    ("context_changes", "failure_code"),
    [
        ({"allowed_stages": frozenset({"reasoning"})}, VerificationFailureCode.STAGE_NOT_ALLOWED),
        (
            {"required_prerequisites": frozenset({"message_persisted"})},
            VerificationFailureCode.PREREQUISITE_MISSING,
        ),
    ],
)
def test_stage_or_precondition_rejection_cannot_be_bypassed_by_passed_report(
    context_changes: dict[str, object],
    failure_code: VerificationFailureCode,
) -> None:
    context, delta, state = valid_context()
    rejected_context = context.model_copy(update=context_changes)
    assert DEFAULT_VERIFIER_CHAIN.verify(rejected_context).failure_code is failure_code
    with pytest.raises(DomainReducerError) as forged:
        reduce_domain_state(state, delta, forged_passed_report(delta))  # type: ignore[arg-type]
    assert forged.value.code is ReducerErrorCode.VERIFICATION_CONTEXT_REQUIRED
    with pytest.raises(DomainReducerError) as real_rejection:
        reduce_domain_state(state, delta, rejected_context)
    assert real_rejection.value.code is ReducerErrorCode.VERIFICATION_REJECTED


def test_legal_verification_context_submits_and_other_delta_is_rejected() -> None:
    context, delta, state = valid_context()
    reduced = reduce_domain_state(state, delta, context)
    assert reduced.state_version == 2
    assert reduced.observations == delta.observations

    other = delta.model_copy(update={"delta_id": uuid4()})
    with pytest.raises(DomainReducerError) as mismatch:
        reduce_domain_state(state, other, context)
    assert mismatch.value.code is ReducerErrorCode.VERIFICATION_CONTEXT_MISMATCH


def test_reducer_deterministically_rejects_stale_delta_during_internal_reverification() -> None:
    _, delta, state = valid_context()
    newer = state.model_copy(update={"state_version": 2})
    stale_context = authorized(
        delta,
        newer,
        sources=frozenset(delta.source_message_ids),
        state_version=1,
    )
    with pytest.raises(DomainReducerError) as exc_info:
        reduce_domain_state(newer, delta, stale_context)
    assert exc_info.value.code is ReducerErrorCode.STATE_VERSION_CONFLICT


def test_observation_duplicate_is_noop_and_conflict_requires_human() -> None:
    session_id, source_id = uuid4(), uuid4()
    original = observation(session_id=session_id, source_id=source_id, value="headache")
    state = DomainState(session_id=session_id, state_version=1, observations=(original,))
    duplicate = observation(session_id=session_id, source_id=source_id, value="headache")
    duplicate_delta = make_delta(state=state, run_id=uuid4(), observations=(duplicate,), sources=(source_id,))
    replayed = reduce_domain_state(
        state,
        duplicate_delta,
        authorized(duplicate_delta, state, sources=frozenset({source_id})),
    )
    assert replayed.state_version == state.state_version
    assert replayed.observations == state.observations

    conflict = observation(session_id=session_id, source_id=uuid4(), value="no headache")
    conflict_delta = make_delta(
        state=state,
        run_id=uuid4(),
        observations=(conflict,),
        sources=(conflict.source_message_id,),
    )
    conflict_report = verified(conflict_delta, state, sources=frozenset({conflict.source_message_id}))
    assert conflict_report.failure_code is VerificationFailureCode.OBSERVATION_SOURCE_CONFLICT
    assert conflict_report.requires_human is True


def test_correction_and_retraction_are_append_only_replayable_events() -> None:
    session_id, first_source = uuid4(), uuid4()
    original = observation(session_id=session_id, source_id=first_source, value="3 days")
    state = DomainState(session_id=session_id, state_version=1, observations=(original,))

    correction_source = uuid4()
    correction = observation(
        session_id=session_id,
        source_id=correction_source,
        value="5 days",
        status=ObservationStatus.CORRECTED,
        target_id=original.observation_id,
    )
    correction_delta = make_delta(
        state=state,
        run_id=uuid4(),
        observations=(correction,),
        sources=(correction_source,),
    )
    corrected = reduce_domain_state(
        state,
        correction_delta,
        authorized(correction_delta, state, sources=frozenset({correction_source})),
    )
    assert corrected.state_version == 2
    assert corrected.observations == (original, correction)

    replay_delta = make_delta(
        state=corrected,
        run_id=uuid4(),
        observations=(correction.model_copy(update={"observation_id": uuid4()}),),
        sources=(correction_source,),
    )
    replayed = reduce_domain_state(
        corrected,
        replay_delta,
        authorized(replay_delta, corrected, sources=frozenset({correction_source})),
    )
    assert replayed.state_version == corrected.state_version
    assert replayed.observations == corrected.observations

    retract_source = uuid4()
    retraction = observation(
        session_id=session_id,
        source_id=retract_source,
        value=None,
        status=ObservationStatus.RETRACTED,
        target_id=correction.observation_id,
    )
    retract_delta = make_delta(
        state=corrected,
        run_id=uuid4(),
        observations=(retraction,),
        sources=(retract_source,),
    )
    retracted = reduce_domain_state(
        corrected,
        retract_delta,
        authorized(retract_delta, corrected, sources=frozenset({retract_source})),
    )
    assert retracted.state_version == 3
    assert retracted.observations[-1].status is ObservationStatus.RETRACTED


def test_fact_and_safety_changes_invalidate_current_artifacts() -> None:
    session_id, artifact_id, artifact_run = uuid4(), uuid4(), uuid4()
    current_artifact = artifact_revision(
        session_id=session_id,
        run_id=artifact_run,
        artifact_id=artifact_id,
        revision=1,
        input_version=1,
    )
    state = DomainState(session_id=session_id, state_version=1, artifacts=(current_artifact,))
    source_id = uuid4()
    profile = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )
    delta = make_delta(
        state=state,
        run_id=uuid4(),
        safety_profile=profile,
        sources=(source_id,),
    )
    reduced = reduce_domain_state(state, delta, authorized(delta, state, sources=frozenset({source_id})))
    assert reduced.safety_profile == profile
    assert reduced.artifacts[0].status is ArtifactStatus.STALE
    assert state.artifacts[0].status is ArtifactStatus.CURRENT


def test_artifact_revision_supersedes_current_and_explicit_invalidation_is_idempotent() -> None:
    session_id, artifact_id, first_run = uuid4(), uuid4(), uuid4()
    first = artifact_revision(
        session_id=session_id,
        run_id=first_run,
        artifact_id=artifact_id,
        revision=1,
        input_version=1,
    )
    state = DomainState(session_id=session_id, state_version=1, artifacts=(first,))
    second_run = uuid4()
    second = artifact_revision(
        session_id=session_id,
        run_id=second_run,
        artifact_id=artifact_id,
        revision=2,
        input_version=1,
        parent_id=uuid4(),
    )
    revision_delta = make_delta(state=state, run_id=second_run, artifacts=(second,))
    revised = reduce_domain_state(state, revision_delta, authorized(revision_delta, state))
    assert [item.status for item in revised.artifacts] == [ArtifactStatus.SUPERSEDED, ArtifactStatus.CURRENT]

    invalidate_delta = make_delta(state=revised, run_id=uuid4(), invalidations=(artifact_id,))
    invalidated = reduce_domain_state(revised, invalidate_delta, authorized(invalidate_delta, revised))
    assert invalidated.artifacts[-1].status is ArtifactStatus.STALE
    replay_delta = make_delta(state=invalidated, run_id=uuid4(), invalidations=(artifact_id,))
    replayed = reduce_domain_state(invalidated, replay_delta, authorized(replay_delta, invalidated))
    assert replayed == invalidated
