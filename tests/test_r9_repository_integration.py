"""Real-PostgreSQL acceptance tests for R9 contract/coverage persistence.

Follows the ``test_l2_5_repository_outbox`` pattern.  Requires a guarded test
database and the destructive-test sentinel; never runs in the unit suite
(``pytest.mark.integration`` and the conftest isolation guard enforce this).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
)
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.verifiers import VerificationContext
from app.db.session import _build_async_pg_url
from app.models.consult import ConsultMessage, ConsultSession
from app.models.question_contract import QuestionContractRecord, QuestionCoverageEventRecord
from app.schemas.question_contract import (
    CoverageCandidateItem,
    CoverageEvidenceCandidate,
    QuestionCoverageCandidate,
    build_coverage_event,
    build_question_contract,
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
                "consult_sessions, question_coverage_events, question_contracts CASCADE"
            )
        )
    try:
        yield PostgresDomainRepository(factory), factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE domain_command_commits, outbox_events, gate_results, artifact_revisions, "
                    "graph_run_steps, graph_runs, safety_profiles, observations, consult_messages, "
                    "consult_sessions, question_coverage_events, question_contracts CASCADE"
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
                content="有痰",
            )
        )
    return session_id, message_id


def _contract(*, session_id: UUID, question_message_id: UUID):
    return build_question_contract(
        session_id=session_id,
        question_message_id=question_message_id,
        question_text="咳嗽是干咳还是有痰，痰的颜色和量如何？",
        dimension="ten_questions.respiratory",
        selection_kind="required",
        aspect_criteria=("咳嗽性质", "痰液颜色", "痰液量"),
    )


def _answered_event(contract, *, answer_id: UUID, content: str = "有痰"):
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
    return build_coverage_event(contract=contract, candidate=candidate, message_contents={answer_id: content})


def _delta(
    *,
    state: DomainState,
    sources: tuple[UUID, ...],
    contracts: tuple = (),
    events: tuple = (),
) -> DomainDelta:
    return DomainDelta(
        delta_id=uuid4(),
        run_id=uuid4(),
        session_id=state.session_id,
        expected_state_version=state.state_version,
        source_message_ids=sources,
        question_contracts=contracts,
        question_coverage_events=events,
    )


def _context(
    state: DomainState,
    delta: DomainDelta,
    *,
    idempotency_key: str,
) -> VerificationContext:
    agent_spec = AgentSpec(
        name="repository-test-agent",
        version="test-agent-v1",
        input_schema=DomainState,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="fake-model"),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=delta.session_id,
        state_version=delta.expected_state_version,
        stage="inquiry",
        agent_spec_version="test-agent-v1",
        prompt_version="prompt-v1",
        policy_version="test-repository-policy.v1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=f"trace-{delta.run_id}",
    )
    artifact = RunArtifact(
        output=delta,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run_spec.trace_id,
        run_id=delta.run_id,
        agent_spec_version="test-agent-v1",
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


async def _contract_row_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as db:
        return (await db.scalar(select(func.count()).select_from(QuestionContractRecord))) or 0


async def _event_row_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as db:
        return (await db.scalar(select(func.count()).select_from(QuestionCoverageEventRecord))) or 0


async def test_contract_and_coverage_commit_persists_and_reloads(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, question_message_id = await _session_and_message(factory)
    contract = _contract(session_id=session_id, question_message_id=question_message_id)
    state = await repository.get_state(session_id)
    contract_delta = _delta(state=state, sources=(), contracts=(contract,))
    await repository.commit(contract_delta, _context(state, contract_delta, idempotency_key="r9-contract"))

    after_contract = await repository.get_state(session_id)
    assert after_contract.state_version == 2
    assert after_contract.question_contracts == (contract,)
    assert await _contract_row_count(factory) == 1

    answer_id = uuid4()
    event = _answered_event(contract, answer_id=answer_id)
    event_delta = _delta(state=after_contract, sources=(answer_id,), events=(event,))
    await repository.commit(event_delta, _context(after_contract, event_delta, idempotency_key="r9-coverage"))

    after_event = await repository.get_state(session_id)
    assert after_event.state_version == 3
    assert after_event.question_coverage_events == (event,)
    assert await _event_row_count(factory) == 1

    async with factory() as db:
        row = await db.get(QuestionCoverageEventRecord, event.event_id)
        assert row is not None
        stored_items = json.dumps(row.items, ensure_ascii=False)
        assert "quote_sha256" in stored_items
        assert "有痰" not in stored_items


async def test_idempotent_replay_does_not_insert_rows_or_bump_version(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, question_message_id = await _session_and_message(factory)
    contract = _contract(session_id=session_id, question_message_id=question_message_id)
    state = await repository.get_state(session_id)
    delta = _delta(state=state, sources=(), contracts=(contract,))
    first = await repository.commit(delta, _context(state, delta, idempotency_key="r9-replay"))

    replayed = await repository.commit(delta, _context(state, delta, idempotency_key="r9-replay"))
    assert replayed.changed is False
    assert replayed.output_state_version == first.output_state_version
    assert await _contract_row_count(factory) == 1


async def test_tampered_stored_contract_fails_closed_on_load(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, question_message_id = await _session_and_message(factory)
    contract = _contract(session_id=session_id, question_message_id=question_message_id)
    state = await repository.get_state(session_id)
    delta = _delta(state=state, sources=(), contracts=(contract,))
    await repository.commit(delta, _context(state, delta, idempotency_key="r9-tamper"))

    async with factory() as db, db.begin():
        row = await db.get(QuestionContractRecord, contract.contract_id)
        assert row is not None
        row.contract_digest = "0" * 64

    with pytest.raises(RepositoryError) as exc:
        await repository.get_state(session_id)
    assert exc.value.code is RepositoryErrorCode.TRANSACTION_FAILED


async def test_tampered_stored_event_fails_closed_on_load(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, question_message_id = await _session_and_message(factory)
    contract = _contract(session_id=session_id, question_message_id=question_message_id)
    state = await repository.get_state(session_id)
    contract_delta = _delta(state=state, sources=(), contracts=(contract,))
    await repository.commit(contract_delta, _context(state, contract_delta, idempotency_key="r9-tamper-c"))

    answer_id = uuid4()
    event = _answered_event(contract, answer_id=answer_id)
    after_contract = await repository.get_state(session_id)
    event_delta = _delta(state=after_contract, sources=(answer_id,), events=(event,))
    await repository.commit(event_delta, _context(after_contract, event_delta, idempotency_key="r9-tamper-e"))

    async with factory() as db, db.begin():
        row = await db.get(QuestionCoverageEventRecord, event.event_id)
        assert row is not None
        row.event_digest = "1" * 64

    with pytest.raises(RepositoryError) as exc:
        await repository.get_state(session_id)
    assert exc.value.code is RepositoryErrorCode.TRANSACTION_FAILED


async def test_deleted_intermediate_event_fails_closed_with_envelope(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """D2: structural ledger damage must surface as TRANSACTION_FAILED, never as
    a raw pydantic ValidationError leaking past the repository boundary.

    Deleting the root's coverage event leaves the follow-up chain without a
    parent event: the fold rejects it during DomainState construction, and the
    construction now lives inside the repository error envelope."""
    repository, factory = store
    session_id, question_message_id = await _session_and_message(factory)
    contract = _contract(session_id=session_id, question_message_id=question_message_id)
    state = await repository.get_state(session_id)
    contract_delta = _delta(state=state, sources=(), contracts=(contract,))
    await repository.commit(contract_delta, _context(state, contract_delta, idempotency_key="r9-d2-root"))

    after_root = await repository.get_state(session_id)
    answer_id = uuid4()
    event = _answered_event(contract, answer_id=answer_id)  # partial: aspect0 addressed
    followup = build_question_contract(
        session_id=session_id,
        question_message_id=uuid4(),
        question_text="剩余两项能确认吗？",
        parent_contract=contract,
        residual_aspects=tuple(contract.aspects[1:]),
    )
    chain_delta = _delta(
        state=after_root,
        sources=(answer_id,),
        contracts=(followup,),
        events=(event,),
    )
    await repository.commit(chain_delta, _context(after_root, chain_delta, idempotency_key="r9-d2-chain"))

    async with factory() as db, db.begin():
        row = await db.get(QuestionCoverageEventRecord, event.event_id)
        assert row is not None
        await db.delete(row)

    with pytest.raises(RepositoryError) as exc:
        await repository.get_state(session_id)
    assert exc.value.code is RepositoryErrorCode.TRANSACTION_FAILED
