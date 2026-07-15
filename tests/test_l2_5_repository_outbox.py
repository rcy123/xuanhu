"""Real-PostgreSQL acceptance tests for the L2-5 repository and Outbox."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.agent_runtime.repository as repository_module
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    DOMAIN_STATE_COMMITTED,
    OutboxErrorCode,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
)
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.verifiers import VerificationContext
from app.db.session import _build_async_pg_url
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
)
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    GateDecision,
    GateResultSchema,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def migrated_database() -> str:
    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        try:
            command.downgrade(config, "20260710_0002")
            command.upgrade(config, "20260711_0005")
            command.downgrade(config, "20260710_0002")
            command.upgrade(config, "20260711_0005")
            # The round-trip above validates the historical L2.5 boundary. The
            # repository itself must run against the current ORM schema.
            command.upgrade(config, "head")
            yield db_url
        finally:
            command.upgrade(config, "head")


@pytest.fixture
async def store(
    migrated_database: str,
) -> tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=5, max_overflow=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE domain_command_commits, outbox_events, gate_results, artifact_revisions, "
                "graph_run_steps, graph_runs, safety_profiles, observations, consult_messages, "
                "consult_sessions CASCADE"
            )
        )
    try:
        yield PostgresDomainRepository(factory), factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER IF EXISTS trg_l2_5_fail_outbox ON outbox_events"))
            await connection.execute(text("DROP FUNCTION IF EXISTS l2_5_fail_outbox()"))
            await connection.execute(
                text(
                    "TRUNCATE domain_command_commits, outbox_events, gate_results, artifact_revisions, "
                    "graph_run_steps, graph_runs, safety_profiles, observations, consult_messages, "
                    "consult_sessions CASCADE"
                )
            )
        await engine.dispose()


async def _session_and_message(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    session_id, message_id = uuid4(), uuid4()
    async with factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1))
        db.add(
            ConsultMessage(
                id=message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content="test input",
            )
        )
    return session_id, message_id


def _observation_delta(
    *,
    session_id: UUID,
    message_id: UUID,
    run_id: UUID | None = None,
    key: str = "symptom",
    value: object = "headache",
) -> DomainDelta:
    actual_run_id = run_id or uuid4()
    return DomainDelta(
        delta_id=uuid4(),
        run_id=actual_run_id,
        session_id=session_id,
        expected_state_version=1,
        source_message_ids=(message_id,),
        observations=(
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=key,
                value=value,
                source_message_id=message_id,
                status=ObservationStatus.ACTIVE,
                created_at=datetime.now(UTC),
            ),
        ),
    )


def _gate(name: str, version: str, state_version: int, decision: GateDecision, details: dict[str, object]) -> GateResultSchema:
    return GateResultSchema(
        gate_name=name,
        policy_version=version,
        input_state_version=state_version,
        decision=decision,
        details=details,
    )


def _ready_triage_gate(state_version: int = 1) -> GateResultSchema:
    return _gate(
        "triage",
        "triage-red-flag.v1",
        state_version,
        GateDecision.PASSED,
        {"disposition": "continue", "candidate_count": 0, "rule_ids": [], "rules": []},
    )


def _blocked_triage_gate(state_version: int = 1) -> GateResultSchema:
    return _gate(
        "triage",
        "triage-red-flag.v1",
        state_version,
        GateDecision.BLOCKED,
        {"disposition": "emergency_referral", "candidate_count": 1, "rule_ids": ["red_flag.high_fever.emergency_referral.v1"]},
    )


def _ready_completeness_gate(state_version: int = 1) -> GateResultSchema:
    return _gate(
        "completeness",
        "completeness-policy.v1",
        state_version,
        GateDecision.PASSED,
        {"disposition": "ready"},
    )


async def _reasoning_ready_session(
    repository: PostgresDomainRepository,
    factory: async_sessionmaker[AsyncSession],
    *,
    triage_gate: GateResultSchema | None = None,
    completeness_gate: GateResultSchema | None = None,
) -> tuple[UUID, UUID, UUID]:
    session_id, message_id = await _session_and_message(factory)
    async with factory() as db, db.begin():
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        session_row.agent_runtime = "langgraph"
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    result = await repository.commit(
        delta,
        _context(state, delta, idempotency_key=f"command-authority-{session_id}"),
        graph_version="graph-v2",
        gate_results=(triage_gate or _ready_triage_gate(), completeness_gate or _ready_completeness_gate()),
    )
    async with factory() as db, db.begin():
        source_gate = await db.scalar(
            select(GateResult).where(
                GateResult.session_id == session_id,
                GateResult.graph_run_id == result.graph_run_id,
                GateResult.gate_name == "completeness",
                GateResult.input_state_version == 1,
            )
        )
        assert source_gate is not None
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        session_row.current_stage = "syndrome"
        session_row.state_snapshot = {
            "agent_runtime": "langgraph",
            "current_stage": "syndrome",
            "state_version": result.output_state_version,
            "advance": {
                "source_gate_id": str(source_gate.id),
                "source_gate_state_version": source_gate.input_state_version,
            },
        }
    return session_id, result.graph_run_id, source_gate.id


def _context(
    state: DomainState,
    delta: DomainDelta,
    *,
    idempotency_key: str,
    trace_id: str | None = None,
    agent_version: str = "test-agent-v1",
) -> VerificationContext:
    agent_spec = AgentSpec(
        name="repository-test-agent",
        version=agent_version,
        input_schema=DomainState,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="fake-model"),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=delta.session_id,
        state_version=delta.expected_state_version,
        stage="inquiry",
        agent_spec_version=agent_version,
        prompt_version="prompt-v1",
        policy_version="test-repository-policy.v1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=trace_id or f"trace-{delta.run_id}",
    )
    artifact = RunArtifact(
        output=delta,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run_spec.trace_id,
        run_id=delta.run_id,
        agent_spec_version=agent_version,
        prompt_version="prompt-v1",
    )
    return VerificationContext(
        agent_spec=agent_spec,
        run_spec=run_spec,
        artifact=artifact,
        state=state,
        allowed_source_message_ids=frozenset(delta.source_message_ids),
        allowed_stages=frozenset({"inquiry"}),
    )


async def _counts(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    models = (
        Observation,
        ArtifactRevision,
        GraphRun,
        GraphRunStep,
        GateResult,
        OutboxEvent,
        DomainCommandCommit,
    )
    async with factory() as db:
        return {
            model.__tablename__: int(await db.scalar(select(func.count()).select_from(model)) or 0) for model in models
        }


async def _commit_observation(
    repository: PostgresDomainRepository,
    factory: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: str,
    key: str = "symptom",
    value: object = "headache",
) -> tuple[UUID, UUID]:
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id, key=key, value=value)
    result = await repository.commit(
        delta,
        _context(state, delta, idempotency_key=idempotency_key),
        graph_version="graph-v2",
    )
    return result.outbox_event_id, session_id


def test_orm_and_migration_have_matching_idempotency_and_outbox_constraints() -> None:
    commit_table = DomainCommandCommit.__table__
    outbox_table = OutboxEvent.__table__
    assert any(constraint.name == "uq_domain_command_commits_idempotency" for constraint in commit_table.constraints)
    assert any(index.name == "idx_outbox_events_claim" for index in outbox_table.indexes)
    assert {column.name for column in commit_table.primary_key.columns} == {"id"}


async def test_commit_rebuilds_state_and_atomically_writes_metadata_and_outbox(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    assert state == DomainState(session_id=session_id, state_version=1)
    delta = _observation_delta(session_id=session_id, message_id=message_id)

    result = await repository.commit(
        delta,
        _context(state, delta, idempotency_key="command-atomic"),
        graph_version="graph-v2",
    )

    rebuilt = await repository.get_state(session_id)
    assert rebuilt.state_version == 2
    assert rebuilt.observations == delta.observations
    assert result.output_state_version == 2
    assert result.changed is True
    counts = await _counts(factory)
    assert counts == {
        "observations": 1,
        "artifact_revisions": 0,
        "graph_runs": 1,
        "graph_run_steps": 1,
        "gate_results": 1,
        "outbox_events": 1,
        "domain_command_commits": 1,
    }
    event = await repository.get_outbox(result.outbox_event_id)
    assert event is not None
    assert event.event_type == DOMAIN_STATE_COMMITTED
    assert event.status == "pending"
    assert event.payload["output_state_version"] == 2


async def test_get_gate_results_loads_persisted_completed_run_gates_by_session_and_state(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    triage_gate = _gate(
        "triage",
        "triage-red-flag.v1",
        2,
        GateDecision.PASSED,
        {"disposition": "continue", "candidate_count": 0},
    )
    completeness_gate = _gate(
        "completeness",
        "completeness-policy.v1",
        2,
        GateDecision.PASSED,
        {"disposition": "ready"},
    )

    await repository.commit(
        delta,
        _context(state, delta, idempotency_key="command-gates"),
        graph_version="graph-v2",
        gate_results=(triage_gate, completeness_gate),
    )

    loaded = await repository.get_gate_results(session_id, 2)
    assert loaded == (triage_gate, completeness_gate)
    assert await repository.get_gate_results(session_id, 1) == ()


async def test_get_reasoning_authority_loads_current_state_and_source_gate_versions(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, graph_run_id, source_gate_id = await _reasoning_ready_session(repository, factory)

    authority = await repository.get_reasoning_authority(session_id, 2)

    assert authority is not None
    assert authority.current_state_version == 2
    assert authority.current_stage == "syndrome"
    assert authority.session_status == "active"
    assert authority.agent_runtime == "langgraph"
    assert authority.domain_state.state_version == 2
    assert authority.source_gate_id == source_gate_id
    assert authority.source_gate_state_version == 1
    assert authority.triage_gate.input_state_version == 1
    assert authority.completeness_gate.input_state_version == 1
    assert authority.intake_graph_run_id == graph_run_id


async def test_get_reasoning_authority_rejects_pre_advance_inquiry_even_with_ready_gates(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    async with factory() as db, db.begin():
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        session_row.agent_runtime = "langgraph"
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    await repository.commit(
        delta,
        _context(state, delta, idempotency_key="command-pre-advance"),
        graph_version="graph-v2",
        gate_results=(_ready_triage_gate(), _ready_completeness_gate()),
    )

    assert await repository.get_reasoning_authority(session_id, 2) is None


async def test_get_reasoning_authority_rejects_state_version_mismatch_and_forged_source(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, _, _ = await _reasoning_ready_session(repository, factory)

    assert await repository.get_reasoning_authority(session_id, 1) is None
    async with factory() as db, db.begin():
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        snapshot = dict(session_row.state_snapshot or {})
        advance = dict(snapshot["advance"])
        advance["source_gate_id"] = str(uuid4())
        snapshot["advance"] = advance
        session_row.state_snapshot = snapshot

    assert await repository.get_reasoning_authority(session_id, 2) is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("current_stage", "inquiry"),
        ("current_stage", "review"),
        ("current_stage", "record"),
        ("current_stage", "done"),
        ("current_stage", "blocked"),
        ("status", "blocked"),
        ("status", "terminated"),
        ("status", "pending_review"),
        ("agent_runtime", "legacy"),
        ("recovery_status", "manual_required"),
    ),
)
async def test_get_reasoning_authority_rejects_session_stage_status_runtime_matrix(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    field: str,
    value: str,
) -> None:
    repository, factory = store
    session_id, _, _ = await _reasoning_ready_session(repository, factory)
    async with factory() as db, db.begin():
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        setattr(session_row, field, value)

    assert await repository.get_reasoning_authority(session_id, 2) is None


async def test_get_reasoning_authority_rejects_duplicate_cross_run_noncompleted_and_blocked_gates(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store

    blocked_session, _, _ = await _reasoning_ready_session(repository, factory, triage_gate=_blocked_triage_gate())
    assert await repository.get_reasoning_authority(blocked_session, 2) is None

    duplicate_session, graph_run_id, _ = await _reasoning_ready_session(repository, factory)
    async with factory() as db, db.begin():
        db.add(
            GateResult(
                id=uuid4(),
                session_id=duplicate_session,
                graph_run_id=graph_run_id,
                gate_name="triage",
                policy_version="triage-red-flag.v1",
                input_state_version=1,
                decision="passed",
                details={"disposition": "continue", "candidate_count": 0},
            )
        )
    assert await repository.get_reasoning_authority(duplicate_session, 2) is None

    running_session, graph_run_id, _ = await _reasoning_ready_session(repository, factory)
    async with factory() as db, db.begin():
        graph_run = await db.get(GraphRun, graph_run_id)
        assert graph_run is not None
        graph_run.status = "running"
    assert await repository.get_reasoning_authority(running_session, 2) is None


async def test_stale_version_has_zero_writes(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    async with factory() as db, db.begin():
        session_row = await db.get(ConsultSession, session_id)
        assert session_row is not None
        session_row.state_version = 2
    stale_state = DomainState(session_id=session_id, state_version=1)
    delta = _observation_delta(session_id=session_id, message_id=message_id)

    with pytest.raises(RepositoryError) as captured:
        await repository.commit(
            delta,
            _context(stale_state, delta, idempotency_key="command-stale"),
            graph_version="graph-v2",
        )
    assert captured.value.code is RepositoryErrorCode.STATE_VERSION_CONFLICT
    assert all(count == 0 for count in (await _counts(factory)).values())


async def test_duplicate_returns_exact_result_without_rerunning_reducer(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    context = _context(state, delta, idempotency_key="command-replay")
    first = await repository.commit(delta, context, graph_version="graph-v2")

    def forbidden_reducer(*_args: object, **_kwargs: object) -> DomainState:
        raise AssertionError("duplicate request reran reducer")

    monkeypatch.setattr(repository_module, "reduce_domain_state", forbidden_reducer)
    second = await repository.commit(delta, context, graph_version="graph-v2")

    assert second == first
    counts = await _counts(factory)
    assert counts["observations"] == counts["graph_runs"] == counts["outbox_events"] == 1


async def test_different_idempotency_key_does_not_reuse_result(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    await repository.commit(delta, _context(state, delta, idempotency_key="command-one"), graph_version="graph-v2")

    second_delta = delta.model_copy(update={"run_id": uuid4(), "delta_id": uuid4()})
    with pytest.raises(RepositoryError) as captured:
        await repository.commit(
            second_delta,
            _context(state, second_delta, idempotency_key="command-two"),
            graph_version="graph-v2",
        )
    assert captured.value.code is RepositoryErrorCode.STATE_VERSION_CONFLICT
    assert (await _counts(factory))["domain_command_commits"] == 1


async def test_concurrent_same_version_allows_at_most_one_update(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    first = _observation_delta(session_id=session_id, message_id=message_id, key="symptom-a")
    second = _observation_delta(session_id=session_id, message_id=message_id, key="symptom-b")

    results = await asyncio.gather(
        repository.commit(first, _context(state, first, idempotency_key="concurrent-a"), graph_version="graph-v2"),
        repository.commit(second, _context(state, second, idempotency_key="concurrent-b"), graph_version="graph-v2"),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, RepositoryError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code is RepositoryErrorCode.STATE_VERSION_CONFLICT
    counts = await _counts(factory)
    assert counts["observations"] == counts["graph_runs"] == counts["outbox_events"] == 1
    assert (await repository.get_state(session_id)).state_version == 2


async def test_outbox_failure_rolls_back_domain_version_and_all_metadata(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    async with factory() as db, db.begin():
        await db.execute(
            text(
                "CREATE FUNCTION l2_5_fail_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'raw-sensitive-exception'; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER trg_l2_5_fail_outbox BEFORE INSERT ON outbox_events "
                "FOR EACH ROW EXECUTE FUNCTION l2_5_fail_outbox()"
            )
        )
    try:
        with pytest.raises(RepositoryError) as captured:
            await repository.commit(
                delta,
                _context(state, delta, idempotency_key="command-rollback"),
                graph_version="graph-v2",
            )
        assert captured.value.code is RepositoryErrorCode.TRANSACTION_FAILED
        assert "raw-sensitive-exception" not in str(captured.value)
    finally:
        async with factory() as db, db.begin():
            await db.execute(text("DROP TRIGGER trg_l2_5_fail_outbox ON outbox_events"))
            await db.execute(text("DROP FUNCTION l2_5_fail_outbox()"))
    assert all(count == 0 for count in (await _counts(factory)).values())
    assert (await repository.get_state(session_id)).state_version == 1


async def test_artifact_parent_is_resolved_to_exact_prior_database_row(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, _ = await _session_and_message(factory)
    artifact_id = uuid4()
    first_run = uuid4()
    first_delta = DomainDelta(
        delta_id=uuid4(),
        run_id=first_run,
        session_id=session_id,
        expected_state_version=1,
        artifact_revisions=(
            ArtifactRevisionSchema(
                artifact_id=artifact_id,
                artifact_type="formula",
                revision=1,
                session_id=session_id,
                input_state_version=1,
                status=ArtifactStatus.CURRENT,
                produced_by_run_id=first_run,
                created_at=datetime.now(UTC),
            ),
        ),
    )
    first_state = await repository.get_state(session_id)
    await repository.commit(
        first_delta,
        _context(first_state, first_delta, idempotency_key="artifact-one"),
        graph_version="graph-v2",
    )
    async with factory() as db:
        parent_id = await db.scalar(
            select(ArtifactRevision.id).where(
                ArtifactRevision.session_id == session_id,
                ArtifactRevision.artifact_id == artifact_id,
                ArtifactRevision.revision == 1,
            )
        )
    assert parent_id is not None

    second_state = await repository.get_state(session_id)
    second_run = uuid4()
    invalid_delta = DomainDelta(
        delta_id=uuid4(),
        run_id=second_run,
        session_id=session_id,
        expected_state_version=2,
        artifact_revisions=(
            ArtifactRevisionSchema(
                artifact_id=artifact_id,
                artifact_type="formula",
                revision=2,
                session_id=session_id,
                input_state_version=2,
                status=ArtifactStatus.CURRENT,
                produced_by_run_id=second_run,
                parent_revision_id=uuid4(),
                parent_revision=1,
                created_at=datetime.now(UTC),
            ),
        ),
    )
    with pytest.raises(RepositoryError) as captured:
        await repository.commit(
            invalid_delta,
            _context(second_state, invalid_delta, idempotency_key="artifact-invalid-parent"),
            graph_version="graph-v2",
        )
    assert captured.value.code is RepositoryErrorCode.ARTIFACT_PARENT_INVALID
    assert (await _counts(factory))["artifact_revisions"] == 1

    valid_delta = invalid_delta.model_copy(
        update={
            "delta_id": uuid4(),
            "run_id": uuid4(),
            "artifact_revisions": (
                invalid_delta.artifact_revisions[0].model_copy(
                    update={"produced_by_run_id": None, "parent_revision_id": parent_id}
                ),
            ),
        }
    )
    valid_run = valid_delta.run_id
    valid_artifact = valid_delta.artifact_revisions[0].model_copy(update={"produced_by_run_id": valid_run})
    valid_delta = valid_delta.model_copy(update={"artifact_revisions": (valid_artifact,)})
    await repository.commit(
        valid_delta,
        _context(second_state, valid_delta, idempotency_key="artifact-two"),
        graph_version="graph-v2",
    )
    rebuilt = await repository.get_state(session_id)
    assert rebuilt.state_version == 3
    assert [item.status for item in rebuilt.artifacts] == [ArtifactStatus.SUPERSEDED, ArtifactStatus.CURRENT]
    assert rebuilt.artifacts[1].parent_revision_id == parent_id


async def test_safety_profile_is_persisted_and_rebuilt_from_authoritative_table(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    run_id = uuid4()
    safety = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.COLLECTED,
        allergens=["penicillin"],
        medications_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )
    delta = DomainDelta(
        delta_id=uuid4(),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=1,
        source_message_ids=(message_id,),
        safety_profile=safety,
    )
    await repository.commit(
        delta,
        _context(state, delta, idempotency_key="safety-profile"),
        graph_version="graph-v2",
    )
    rebuilt = await repository.get_state(session_id)
    assert rebuilt.state_version == 2
    assert rebuilt.safety_profile == safety


async def test_outbox_claim_ack_retry_and_expired_lease_recovery(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    await _commit_observation(repository, factory, idempotency_key="outbox-one")
    await _commit_observation(repository, factory, idempotency_key="outbox-two")
    await _commit_observation(repository, factory, idempotency_key="outbox-three")

    first_claim, second_claim = await asyncio.gather(
        repository.claim(worker_id="worker-a", limit=1, lease_seconds=60),
        repository.claim(worker_id="worker-b", limit=1, lease_seconds=60),
    )
    assert len(first_claim) == len(second_claim) == 1
    assert first_claim[0].event_id != second_claim[0].event_id
    assert first_claim[0].attempt_count == second_claim[0].attempt_count == 1

    assert await repository.acknowledge(first_claim[0].event_id, worker_id="worker-a")
    assert not await repository.acknowledge(first_claim[0].event_id, worker_id="worker-b")
    assert await repository.release_failed(
        second_claim[0].event_id,
        worker_id="worker-b",
        error_code=OutboxErrorCode.PUBLISH_TIMEOUT,
        retry_after_seconds=0,
    )
    retried = await repository.claim(worker_id="worker-c", limit=1, lease_seconds=60)
    assert len(retried) == 1
    assert retried[0].event_id == second_claim[0].event_id
    assert retried[0].attempt_count == 2
    async with factory() as db:
        failed_row = await db.get(OutboxEvent, retried[0].event_id)
        assert failed_row is not None
        assert failed_row.last_error_code == OutboxErrorCode.PUBLISH_TIMEOUT.value

    pending = await repository.claim(worker_id="worker-restarting", limit=5, lease_seconds=60)
    pending_ids = {message.event_id for message in pending}
    assert first_claim[0].event_id not in pending_ids
    assert pending_ids
    expired_id = next(iter(pending_ids))
    async with factory() as db, db.begin():
        await db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == expired_id)
            .values(leased_until=func.now() - timedelta(seconds=1))
        )
    recovered = await repository.claim(worker_id="worker-after-restart", limit=5, lease_seconds=60)
    assert expired_id in {message.event_id for message in recovered}
    assert first_claim[0].event_id not in {message.event_id for message in recovered}


async def test_outbox_and_run_metadata_do_not_copy_sensitive_domain_text(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    secret = "patient-name-raw-prompt-api-key-secret"
    event_id, _ = await _commit_observation(
        repository,
        factory,
        idempotency_key="privacy-command",
        value={"clinical_note": secret},
    )
    async with factory() as db:
        event = await db.get(OutboxEvent, event_id)
        assert event is not None
        metadata_text = str(
            {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "payload": event.payload,
                "last_error_code": event.last_error_code,
            }
        )
        run = await db.get(GraphRun, event.graph_run_id)
        step = await db.scalar(select(GraphRunStep).where(GraphRunStep.graph_run_id == event.graph_run_id))
        gate = await db.scalar(select(GateResult).where(GateResult.graph_run_id == event.graph_run_id))
        metadata_text += str(
            (run.command_id if run else None, step.step_metadata if step else None, gate.details if gate else None)
        )
    assert secret not in metadata_text
    assert "clinical_note" not in metadata_text
    assert set(event.payload) == {
        "session_id",
        "input_state_version",
        "output_state_version",
        "observation_ids",
        "artifact_ids",
    }


async def test_free_text_trace_is_hashed_before_persistence(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, message_id = await _session_and_message(factory)
    state = await repository.get_state(session_id)
    delta = _observation_delta(session_id=session_id, message_id=message_id)
    context = _context(
        state,
        delta,
        idempotency_key="privacy-safe-command",
        trace_id="patient name raw prompt api key",
    )
    result = await repository.commit(delta, context, graph_version="graph-v2")
    event = await repository.get_outbox(result.outbox_event_id)
    assert event is not None
    assert event.trace_id.startswith("trace:")
    assert "patient name" not in event.trace_id
    async with factory() as db:
        run = await db.get(GraphRun, result.graph_run_id)
        commit_row = await db.get(DomainCommandCommit, result.commit_id)
    assert run is not None and commit_row is not None
    assert run.command_id.startswith("command:")
    assert commit_row.idempotency_key == run.command_id
    assert "privacy-safe-command" not in run.command_id
