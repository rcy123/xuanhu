"""R9 question-contract/coverage integration into the pure Domain Reducer.

Focused tests for append-only idempotency, append-only conflicts, source/session
scope, chain integrity via ``evaluate_contract_coverage``, mixed-fact/artifact
guards, and artifact invalidation on contract/coverage change.
"""

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
    validate_domain_delta,
)
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.verifiers import VerificationContext
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    ObservationSchema,
    SafetyProfileSchema,
)
from app.schemas.question_contract import (
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionContract,
    QuestionCoverageCandidate,
    QuestionCoverageEvent,
    build_coverage_event,
    build_question_contract,
)


class InputPayload(BaseModel):
    command: str


def root_contract(*, session_id: UUID, criteria=("咳嗽性质", "痰液颜色", "痰液量")) -> QuestionContract:
    return build_question_contract(
        session_id=session_id,
        question_message_id=uuid4(),
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=criteria,
    )


def followup_contract(parent: QuestionContract) -> QuestionContract:
    return build_question_contract(
        session_id=parent.session_id,
        question_message_id=uuid4(),
        question_text="剩余信息目前能确认吗？",
        parent_contract=parent,
        residual_aspects=tuple(parent.aspects[1:]),
    )


def first_answer_event(
    contract: QuestionContract, *, answer_id: UUID | None = None, content: str = "有痰"
) -> tuple[UUID, QuestionCoverageEvent]:
    answer_id = answer_id or uuid4()
    candidate = QuestionCoverageCandidate(
        contract_id=contract.contract_id,
        answer_message_id=answer_id,
        items=(
            CoverageCandidateItem(
                aspect_id=contract.aspects[0].aspect_id,
                status="addressed",
                evidence=(
                    CoverageEvidenceCandidate(
                        source_message_id=answer_id,
                        start_char=0,
                        end_char=len(content),
                        quote=content,
                    ),
                ),
            ),
            *(
                CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered")
                for aspect in contract.aspects[1:]
            ),
        ),
    )
    return answer_id, build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})


def contract_with_question(
    *, session_id: UUID, question_message_id: UUID, criteria: tuple[str, ...]
) -> QuestionContract:
    return build_question_contract(
        session_id=session_id,
        question_message_id=question_message_id,
        question_text="请说明最重要的特征？",
        dimension="present_illness.change",
        selection_kind="required",
        aspect_criteria=criteria,
    )


def artifact_revision(
    *,
    session_id: UUID,
    run_id: UUID,
    artifact_id: UUID,
    revision: int,
    input_version: int,
) -> ArtifactRevisionSchema:
    return ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type="formula",
        revision=revision,
        session_id=session_id,
        input_state_version=input_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=run_id,
        parent_revision_id=None,
        parent_revision=None,
        created_at=datetime.now(UTC),
    )


def make_delta(
    *,
    state: DomainState,
    run_id: UUID,
    sources: tuple[UUID, ...] = (),
    observations: tuple[ObservationSchema, ...] = (),
    safety_profile: SafetyProfileSchema | None = None,
    artifacts: tuple[ArtifactRevisionSchema, ...] = (),
    invalidations: tuple[UUID, ...] = (),
    contracts: tuple[QuestionContract, ...] = (),
    events: tuple[QuestionCoverageEvent, ...] = (),
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
        question_contracts=contracts,
        question_coverage_events=events,
    )


def authorized(
    delta: DomainDelta,
    state: DomainState,
    *,
    sources: frozenset[UUID] = frozenset(),
) -> VerificationContext:
    spec = AgentSpec(
        name="local-delta-agent",
        version="agent-v1",
        input_schema=InputPayload,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="fake-local-model"),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=state.session_id,
        state_version=state.state_version,
        stage="intake",
        agent_spec_version=spec.version,
        prompt_version="prompt-v1",
        policy_version="test-verifier-policy.v1",
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
        allowed_stages=frozenset({"intake"}),
        required_prerequisites=frozenset(),
        satisfied_prerequisites=frozenset(),
    )


def test_contract_append_and_coverage_append_increment_state_version() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)

    contract_delta = make_delta(state=state, run_id=uuid4(), contracts=(contract,))
    after_contract = reduce_domain_state(state, contract_delta, authorized(contract_delta, state))
    assert after_contract.state_version == 2
    assert after_contract.question_contracts == (contract,)
    assert after_contract.question_coverage_events == ()

    answer_id, event = first_answer_event(contract)
    event_delta = make_delta(state=after_contract, run_id=uuid4(), events=(event,), sources=(answer_id,))
    after_event = reduce_domain_state(
        after_contract,
        event_delta,
        authorized(event_delta, after_contract, sources=frozenset({answer_id})),
    )
    assert after_event.state_version == 3
    assert after_event.question_coverage_events == (event,)


def test_byte_equal_replay_is_noop() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    first = make_delta(state=state, run_id=uuid4(), contracts=(contract,))
    reduced = reduce_domain_state(state, first, authorized(first, state))

    replay = make_delta(state=reduced, run_id=uuid4(), contracts=(contract,))
    replayed = reduce_domain_state(reduced, replay, authorized(replay, reduced))
    assert replayed == reduced
    assert replayed.state_version == 2
    assert replayed.question_contracts == (contract,)


def test_same_contract_id_different_payload_conflicts() -> None:
    session_id = uuid4()
    question_message_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    first = contract_with_question(
        session_id=session_id,
        question_message_id=question_message_id,
        criteria=("特征一", "特征二"),
    )
    applied = make_delta(state=state, run_id=uuid4(), contracts=(first,))
    reduce_domain_state(state, applied, authorized(applied, state))

    second = contract_with_question(
        session_id=session_id,
        question_message_id=question_message_id,
        criteria=("特征一", "特征三"),
    )
    assert second.contract_id == first.contract_id
    conflicting = make_delta(
        state=reduce_domain_state(state, applied, authorized(applied, state)),
        run_id=uuid4(),
        contracts=(second,),
    )
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(reduce_domain_state(state, applied, authorized(applied, state)), conflicting)
    assert exc.value.code is ReducerErrorCode.QUESTION_CONTRACT_CONFLICT


def test_same_event_id_different_payload_conflicts() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    contract_delta = make_delta(state=state, run_id=uuid4(), contracts=(contract,))
    after_contract = reduce_domain_state(state, contract_delta, authorized(contract_delta, state))

    answer_id, first_event = first_answer_event(contract)
    applied = make_delta(state=after_contract, run_id=uuid4(), events=(first_event,), sources=(answer_id,))
    after_event = reduce_domain_state(
        after_contract, applied, authorized(applied, after_contract, sources=frozenset({answer_id}))
    )

    # Same contract + same answer -> identical event_id, but a different status
    # payload changes the canonical event digest: an append-only violation.
    alternate = build_coverage_event(
        contract=contract,
        candidate=QuestionCoverageCandidate(
            contract_id=contract.contract_id,
            answer_message_id=answer_id,
            items=tuple(
                CoverageCandidateItem(aspect_id=aspect.aspect_id, status="unanswered")
                for aspect in contract.aspects
            ),
        ),
        message_contents={answer_id: "有痰"},
    )
    assert alternate.event_id == first_event.event_id
    assert alternate.event_digest != first_event.event_digest
    conflicting = make_delta(state=after_event, run_id=uuid4(), events=(alternate,), sources=(answer_id,))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(after_event, conflicting)
    assert exc.value.code is ReducerErrorCode.QUESTION_COVERAGE_CONFLICT


def test_coverage_answer_must_be_declared_in_source_message_ids() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    answer_id, event = first_answer_event(contract)
    undeclared = make_delta(state=state, run_id=uuid4(), contracts=(contract,), events=(event,))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(state, undeclared)
    assert exc.value.code is ReducerErrorCode.QUESTION_COVERAGE_SOURCE_UNDECLARED


def test_event_for_unknown_contract_fails_closed() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    answer_id, event = first_answer_event(contract)
    orphan = make_delta(state=state, run_id=uuid4(), events=(event,), sources=(answer_id,))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(state, orphan)
    assert exc.value.code is ReducerErrorCode.QUESTION_CONTRACT_CHAIN_INVALID


def test_followup_without_parent_coverage_event_fails_closed() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    followup = followup_contract(contract)
    skipped_parent = make_delta(state=state, run_id=uuid4(), contracts=(contract, followup))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(state, skipped_parent)
    assert exc.value.code is ReducerErrorCode.QUESTION_CONTRACT_CHAIN_INVALID


def test_contract_and_artifact_change_are_mixed_and_rejected() -> None:
    session_id = uuid4()
    run_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    artifact = artifact_revision(
        session_id=session_id,
        run_id=run_id,
        artifact_id=uuid4(),
        revision=1,
        input_version=1,
    )
    mixed = make_delta(state=state, run_id=run_id, contracts=(contract,), artifacts=(artifact,))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(state, mixed)
    assert exc.value.code is ReducerErrorCode.MIXED_FACT_AND_ARTIFACT_CHANGE


def test_contract_change_invalidates_current_artifacts() -> None:
    session_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    state = DomainState(
        session_id=session_id,
        state_version=1,
        artifacts=(
            artifact_revision(
                session_id=session_id,
                run_id=run_id,
                artifact_id=artifact_id,
                revision=1,
                input_version=1,
            ),
        ),
    )
    contract = root_contract(session_id=session_id)
    contract_delta = make_delta(state=state, run_id=run_id, contracts=(contract,))
    reduced = reduce_domain_state(state, contract_delta, authorized(contract_delta, state))
    assert reduced.artifacts[0].status is ArtifactStatus.STALE
    assert state.artifacts[0].status is ArtifactStatus.CURRENT


def test_coverage_change_invalidates_current_artifacts() -> None:
    session_id = uuid4()
    run_id = uuid4()
    state = DomainState(
        session_id=session_id,
        state_version=1,
        artifacts=(
            artifact_revision(
                session_id=session_id,
                run_id=run_id,
                artifact_id=uuid4(),
                revision=1,
                input_version=1,
            ),
        ),
    )
    contract = root_contract(session_id=session_id)
    contract_delta = make_delta(state=state, run_id=run_id, contracts=(contract,))
    after_contract = reduce_domain_state(state, contract_delta, authorized(contract_delta, state))
    answer_id, event = first_answer_event(contract)
    event_delta = make_delta(state=after_contract, run_id=run_id, events=(event,), sources=(answer_id,))
    after_event = reduce_domain_state(
        after_contract,
        event_delta,
        authorized(event_delta, after_contract, sources=frozenset({answer_id})),
    )
    assert after_event.artifacts[0].status is ArtifactStatus.STALE


def test_multiple_contract_session_mismatch_rejected() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    other_session = root_contract(session_id=uuid4())
    wrong_session = make_delta(state=state, run_id=uuid4(), contracts=(other_session,))
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(state, wrong_session)
    assert exc.value.code is ReducerErrorCode.SESSION_MISMATCH


def test_multiple_independent_roots_are_supported() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    first = root_contract(session_id=session_id, criteria=("热象",))
    second = root_contract(session_id=session_id, criteria=("大便",))
    both = make_delta(state=state, run_id=uuid4(), contracts=(first, second))
    reduced = reduce_domain_state(state, both, authorized(both, state))
    assert reduced.state_version == 2
    assert set(item.contract_id for item in reduced.question_contracts) == {first.contract_id, second.contract_id}


def test_cross_root_duplicate_answer_is_rejected() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    first = root_contract(session_id=session_id)
    first_delta = make_delta(state=state, run_id=uuid4(), contracts=(first,))
    after_first = reduce_domain_state(state, first_delta, authorized(first_delta, state))

    shared_answer_id, first_event = first_answer_event(first)
    event_delta = make_delta(state=after_first, run_id=uuid4(), events=(first_event,), sources=(shared_answer_id,))
    after_event = reduce_domain_state(
        after_first, event_delta, authorized(event_delta, after_first, sources=frozenset({shared_answer_id}))
    )

    # A second root answers with the same patient message: the append-only
    # ledger must fail closed on the cross-root answer collision.
    second = root_contract(session_id=session_id, criteria=("发热程度",))
    _, second_event = first_answer_event(second, answer_id=shared_answer_id)
    assert second_event.event_id != first_event.event_id
    second_delta = make_delta(
        state=after_event,
        run_id=uuid4(),
        contracts=(second,),
        events=(second_event,),
        sources=(shared_answer_id,),
    )
    with pytest.raises(DomainReducerError) as exc:
        validate_domain_delta(after_event, second_delta)
    assert exc.value.code is ReducerErrorCode.QUESTION_COVERAGE_CONFLICT


def test_delta_digest_naturally_includes_contract_and_coverage_fields() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract_a = root_contract(session_id=session_id, criteria=("热象",))
    contract_b = root_contract(session_id=session_id, criteria=("大便",))
    delta_a = make_delta(state=state, run_id=uuid4(), contracts=(contract_a,))
    delta_b = make_delta(state=state, run_id=uuid4(), contracts=(contract_b,))
    assert domain_delta_digest(delta_a) != domain_delta_digest(delta_b)


def test_duplicate_ids_in_one_delta_are_rejected_at_construction() -> None:
    session_id = uuid4()
    state = DomainState(session_id=session_id, state_version=1)
    contract = root_contract(session_id=session_id)
    with pytest.raises(ValidationError):
        make_delta(state=state, run_id=uuid4(), contracts=(contract, contract))
    answer_id, event = first_answer_event(contract)
    with pytest.raises(ValidationError):
        make_delta(state=state, run_id=uuid4(), contracts=(contract,), events=(event, event))
