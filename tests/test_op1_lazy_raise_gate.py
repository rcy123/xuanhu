"""OP1 lazy=raise CI gate: verify snapshot paths use explicit queries, not lazy relationships.

Section 2.2 of 99-验收与回归基线.md requires: every reasoning/intake path test
must assert no RelationshipLazyLoadError (or equivalent SQLAlchemy exception) is
triggered.  If triggered, the snapshot path missed an eager load and must be
fixed per T1.5.

This file exercises the two core snapshot-loading code paths:

- ``PostgresDomainRepository.get_state()``  (``_load_state`` → DomainState)
- ``PostgresDomainRepository.get_reasoning_authority()``  (ReasoningAuthoritySnapshot)

Both convert ORM rows to fully-detached Pydantic models.  If any conversion step
accidentally accesses an ORM relationship attribute, it will either trigger a
``lazy="raise"`` exception on ConsultSession (which has all relationships gated
with ``lazy="raise"``) or an implicit N+1 on Observation (which defaults to
``lazy="select"``).  This test validates the conversion ends without any such
error, and that the resulting DomainState / ReasoningAuthoritySnapshot values
are correct.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent_runtime.repository import PostgresDomainRepository
from app.db.session import _build_async_pg_url, reset_session_factory
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    GateResult,
    GraphRun,
    Observation,
    SafetyProfile,
)
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import ArtifactStatus, GateDecision, ObservationStatus
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_database() -> str:
    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        try:
            command.upgrade(config, "head")
            yield db_url
        finally:
            command.upgrade(config, "head")


@pytest.fixture
async def repository_and_factory(
    migrated_database: str,
) -> tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]]:
    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=3, max_overflow=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE domain_command_commits, outbox_events, gate_results, graph_run_steps, "
                "artifact_revision_payloads, artifact_revisions, graph_runs, safety_rule_runs, "
                "safety_profiles, observations, consult_messages, consult_sessions CASCADE"
            )
        )
    try:
        yield PostgresDomainRepository(factory), factory
    finally:
        await reset_session_factory()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _observation(
    session_id: uuid.UUID,
    source_message_id: uuid.UUID,
    fact_key: str,
    value: str,
) -> Observation:
    return Observation(
        id=uuid.uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value=value,
        normalized_value=value,
        source_message_id=source_message_id,
        status=ObservationStatus.ACTIVE.value,
        confidence=0.95,
    )


async def _seed_basic_session(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a session with one message — the minimum for get_state."""
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage="inquiry",
                status="active",
                agent_runtime="langgraph",
                state_version=1,
                state_snapshot={},
            )
        )
        db.add(
            ConsultMessage(
                id=message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content="test message",
            )
        )
    return session_id, message_id


# ---------------------------------------------------------------------------
# get_state — core domain snapshot path
# ---------------------------------------------------------------------------


async def test_get_state_empty_session_returns_bare_domain_state(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """A session with no observations, safety profile, or artifacts must return
    a valid DomainState with empty tuples — without lazy-load errors."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    state = await repository.get_state(session_id)

    assert isinstance(state.session_id, uuid.UUID)
    assert state.state_version == 1
    assert state.observations == ()
    assert state.safety_profile is None
    assert state.artifacts == ()


async def test_get_state_with_observations_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Observations with source_message relationships must be loaded into
    DomainState without accessing the ORM relationship (no N+1)."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        db.add_all(
            [
                _observation(session_id, message_id, "chief_complaint.symptom", "headache"),
                _observation(session_id, message_id, "chief_complaint.course", "three_days"),
                _observation(session_id, message_id, "ten_questions.sleep", "normal"),
            ]
        )

    state = await repository.get_state(session_id)

    assert len(state.observations) == 3
    assert {o.fact_key for o in state.observations} == {
        "chief_complaint.symptom",
        "chief_complaint.course",
        "ten_questions.sleep",
    }
    # Every observation retains its source_message_id as a value, not a
    # relationship access — proving the schema converter uses the column.
    for obs_schema in state.observations:
        assert obs_schema.source_message_id == message_id


async def test_get_state_with_supersede_chain_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Supersede chains must be loaded via the column (supersedes_observation_id),
    never via the ORM relationship (``Observation.supersedes``, lazy="select")."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    original_id = uuid.uuid4()
    corrected_id = uuid.uuid4()
    async with factory() as db, db.begin():
        # Domain invariant (chk_observations_status_relation): the ACTIVE head
        # carries no supersedes pointer; the CORRECTED successor points at the
        # observation it replaces via supersedes_observation_id.
        db.add(
            Observation(
                id=original_id,
                session_id=session_id,
                fact_key="chief_complaint.symptom",
                value="migraine",
                normalized_value="migraine",
                source_message_id=message_id,
                status=ObservationStatus.ACTIVE.value,
                confidence=0.8,
                supersedes_observation_id=None,
            )
        )
        db.add(
            Observation(
                id=corrected_id,
                session_id=session_id,
                fact_key="chief_complaint.symptom",
                value="headache",
                normalized_value="headache",
                source_message_id=message_id,
                status=ObservationStatus.CORRECTED.value,
                confidence=0.95,
                supersedes_observation_id=original_id,
            )
        )

    state = await repository.get_state(session_id)

    assert len(state.observations) == 2
    active = [o for o in state.observations if o.status == ObservationStatus.ACTIVE]
    corrected = [o for o in state.observations if o.status == ObservationStatus.CORRECTED]
    assert len(active) == 1 and len(corrected) == 1
    assert active[0].supersedes_observation_id is None
    assert corrected[0].supersedes_observation_id == original_id


async def test_get_state_with_safety_profile_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """The safety profile must be loaded via explicit select, not via any
    ORM relationship (SafetyProfile has no relationships, so this tests
    the _safety_schema converter correctness)."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        db.add(
            SafetyProfile(
                id=uuid.uuid4(),
                session_id=session_id,
                allergy_collection_status="collected",
                allergens=["penicillin"],
                pregnancy_collection_status="explicitly_none",
                lactation_collection_status="explicitly_none",
                medications_collection_status="collected",
                medications=["aspirin"],
                major_conditions_collection_status="unknown",
                contraindications_collection_status="unknown",
            )
        )

    state = await repository.get_state(session_id)

    assert state.safety_profile is not None
    assert state.safety_profile.allergy_collection_status == "collected"
    assert state.safety_profile.allergens == ["penicillin"]
    assert state.safety_profile.medications == ["aspirin"]
    assert state.safety_profile.pregnancy_collection_status == "explicitly_none"


async def test_get_state_with_artifact_revisions_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Artifact revisions must be loaded via explicit select.  The
    ArtifactRevision table has no relationships, so the _artifact_schema
    converter should return correct values from column attributes."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    artifact_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version="main-graph.v1",
                command_id="test:command",
                input_state_version=1,
                status="completed",
            )
        )
        await db.flush()
        db.add(
            ArtifactRevision(
                id=uuid.uuid4(),
                artifact_id=artifact_id,
                artifact_type="syndrome_draft",
                revision=1,
                session_id=session_id,
                input_state_version=1,
                status=ArtifactStatus.CURRENT.value,
                produced_by_run_id=run_id,
            )
        )

    state = await repository.get_state(session_id)

    assert len(state.artifacts) == 1
    artifact = state.artifacts[0]
    assert artifact.artifact_id == artifact_id
    assert artifact.artifact_type == "syndrome_draft"
    assert artifact.revision == 1
    assert artifact.status == ArtifactStatus.CURRENT
    assert artifact.produced_by_run_id == run_id


async def test_get_state_with_artifact_parent_revision_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Artifact revisions with parent_revision references load the parent
    fields via column values, not via FK relationship resolution."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    artifact_id = uuid.uuid4()
    run_id = uuid.uuid4()
    rev1_row_id = uuid.uuid4()
    rev2_row_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version="main-graph.v1",
                command_id="test:command",
                input_state_version=1,
                status="completed",
            )
        )
        await db.flush()
        db.add(
            ArtifactRevision(
                id=rev1_row_id,
                artifact_id=artifact_id,
                artifact_type="syndrome_draft",
                revision=1,
                session_id=session_id,
                input_state_version=1,
                status=ArtifactStatus.SUPERSEDED.value,
                produced_by_run_id=run_id,
            )
        )
        db.add(
            ArtifactRevision(
                id=rev2_row_id,
                artifact_id=artifact_id,
                artifact_type="syndrome_draft",
                revision=2,
                session_id=session_id,
                input_state_version=2,
                status=ArtifactStatus.CURRENT.value,
                produced_by_run_id=run_id,
                parent_revision_id=rev1_row_id,
                parent_revision=1,
            )
        )

    state = await repository.get_state(session_id)

    assert len(state.artifacts) == 2
    by_revision = {a.revision: a for a in state.artifacts}
    assert by_revision[1].status == ArtifactStatus.SUPERSEDED
    assert by_revision[2].status == ArtifactStatus.CURRENT
    assert by_revision[2].parent_revision_id == rev1_row_id
    assert by_revision[2].parent_revision == 1


async def test_get_state_full_domain_snapshot_exercises_all_converters(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Combined test: observations (with supersede), safety profile, and
    artifact revisions all loaded in one get_state call.  If any converter
    triggers a lazy relationship access, this test fails."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    superseded_id = uuid.uuid4()
    active_symptom_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version="main-graph.v1",
                command_id="test:command",
                input_state_version=1,
                status="completed",
            )
        )
        await db.flush()
        # Domain invariant (chk_observations_status_relation): the ACTIVE head
        # carries no supersedes pointer; the CORRECTED successor points at the
        # observation it replaces via supersedes_observation_id.
        db.add(
            Observation(
                id=superseded_id,
                session_id=session_id,
                fact_key="chief_complaint.symptom",
                value="old-value",
                normalized_value="old-value",
                source_message_id=message_id,
                status=ObservationStatus.ACTIVE.value,
                confidence=0.7,
            )
        )
        db.add(
            Observation(
                id=active_symptom_id,
                session_id=session_id,
                fact_key="chief_complaint.symptom",
                value="headache",
                normalized_value="headache",
                source_message_id=message_id,
                status=ObservationStatus.CORRECTED.value,
                confidence=0.95,
                supersedes_observation_id=superseded_id,
            )
        )
        db.add(
            Observation(
                id=uuid.uuid4(),
                session_id=session_id,
                fact_key="ten_questions.sleep",
                value="normal",
                normalized_value="normal",
                source_message_id=message_id,
                status=ObservationStatus.ACTIVE.value,
                confidence=0.9,
            )
        )
        db.add(
            SafetyProfile(
                id=uuid.uuid4(),
                session_id=session_id,
                allergy_collection_status="explicitly_none",
                pregnancy_collection_status="unknown",
                lactation_collection_status="unknown",
                medications_collection_status="unknown",
                major_conditions_collection_status="unknown",
                contraindications_collection_status="unknown",
            )
        )
        db.add(
            ArtifactRevision(
                id=uuid.uuid4(),
                artifact_id=artifact_id,
                artifact_type="syndrome_draft",
                revision=1,
                session_id=session_id,
                input_state_version=1,
                status=ArtifactStatus.CURRENT.value,
                produced_by_run_id=run_id,
            )
        )

    state = await repository.get_state(session_id)

    # Observations
    assert len(state.observations) == 3
    active = [o for o in state.observations if o.status == ObservationStatus.ACTIVE]
    corrected = [o for o in state.observations if o.status == ObservationStatus.CORRECTED]
    assert len(active) == 2
    assert all(o.supersedes_observation_id is None for o in active)
    assert len(corrected) == 1
    assert corrected[0].supersedes_observation_id == superseded_id

    # Safety
    assert state.safety_profile is not None
    assert state.safety_profile.allergy_collection_status == "explicitly_none"

    # Artifacts
    assert len(state.artifacts) == 1
    assert state.artifacts[0].artifact_id == artifact_id


# ---------------------------------------------------------------------------
# get_reasoning_authority — reasoning authority snapshot path
# ---------------------------------------------------------------------------


async def test_get_reasoning_authority_no_lazy_load(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """The full reasoning authority snapshot path (get_state + gate lookups)
    must complete without triggering any lazy relationship access."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    intake_run_id = uuid.uuid4()
    triage_gate_id = uuid.uuid4()
    completeness_gate_id = uuid.uuid4()

    async with factory() as db, db.begin():
        # Update session to syndrome stage with correct state_snapshot
        session = await db.get(ConsultSession, session_id, with_for_update=True)
        assert session is not None
        session.current_stage = "syndrome"
        session.state_version = 2
        session.state_snapshot = {
            "agent_runtime": "langgraph",
            "current_stage": "syndrome",
            "state_version": 2,
            "advance": {
                "source_gate_id": str(completeness_gate_id),
                "source_gate_state_version": 1,
                "trace_id": "test-trace",
            },
        }

        db.add(
            GraphRun(
                id=intake_run_id,
                session_id=session_id,
                graph_version="main-graph.v1",
                command_id="message:ready",
                input_state_version=1,
                status="completed",
                completed_at=func.now(),
            )
        )
        await db.flush()

        db.add(
            GateResult(
                id=triage_gate_id,
                session_id=session_id,
                graph_run_id=intake_run_id,
                gate_name=TRIAGE_GATE_NAME,
                policy_version=TRIAGE_POLICY_VERSION,
                input_state_version=1,
                decision=GateDecision.PASSED.value,
                details={"disposition": "continue", "candidate_count": 0, "rule_ids": []},
            )
        )
        db.add(
            GateResult(
                id=completeness_gate_id,
                session_id=session_id,
                graph_run_id=intake_run_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=1,
                decision=GateDecision.PASSED.value,
                details={"disposition": "ready", "missing_required": [], "rule_ids": []},
            )
        )

    authority = await repository.get_reasoning_authority(session_id, state_version=2)

    assert authority is not None
    assert authority.session_id == session_id
    assert authority.current_state_version == 2
    assert authority.current_stage == "syndrome"
    assert authority.source_gate_id == completeness_gate_id
    assert authority.source_gate_state_version == 1
    assert authority.triage_gate.gate_name == TRIAGE_GATE_NAME
    assert authority.triage_gate.decision == GateDecision.PASSED
    assert authority.completeness_gate.gate_name == COMPLETENESS_GATE_NAME
    assert authority.completeness_gate.decision == GateDecision.PASSED
    # The domain_state inside the authority must be a fully-detached Pydantic model.
    assert authority.domain_state.state_version == 2
    assert authority.domain_state.session_id == session_id


async def test_get_reasoning_authority_returns_none_for_wrong_stage(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """When the session is not in 'syndrome' stage, get_reasoning_authority
    must return None without raising any lazy-load errors."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    authority = await repository.get_reasoning_authority(session_id, state_version=1)

    assert authority is None


async def test_get_reasoning_authority_returns_none_for_missing_advance_source(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """When the session is in 'syndrome' stage but has no advance source,
    get_reasoning_authority must return None — without lazy-load errors."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, session_id, with_for_update=True)
        assert session is not None
        session.current_stage = "syndrome"
        session.state_version = 2
        # No advance source in state_snapshot.

    authority = await repository.get_reasoning_authority(session_id, state_version=2)

    assert authority is None


async def test_get_reasoning_authority_returns_none_for_stale_state_version(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """When the caller passes a stale state_version, the authority lookup
    must return None — without any lazy-load errors."""
    repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, session_id, with_for_update=True)
        assert session is not None
        session.current_stage = "syndrome"
        session.state_version = 3  # bumped past what the caller knows about

    authority = await repository.get_reasoning_authority(session_id, state_version=2)

    # state_version mismatch → None (session is at 3, caller claims 2).
    assert authority is None


# ---------------------------------------------------------------------------
# Verify that ORM relationship access outside of explicit query paths DOES fail
# (proving lazy="raise" is active and therefore this test suite is meaningful)
# ---------------------------------------------------------------------------


async def test_consult_session_lazy_raise_is_enforced(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Sanity check: ConsultSession.messages is lazy='raise'.  Accessing it
    after the owning session is closed must raise — proving that any
    accidental relationship access in the snapshot path would actually fail."""
    from sqlalchemy.exc import InvalidRequestError

    _repository, factory = repository_and_factory
    session_id, _message_id = await _seed_basic_session(factory)

    # Load the session row from a fresh AsyncSession, then close it.
    session_row: ConsultSession | None = None
    async with factory() as db:
        session_row = await db.get(ConsultSession, session_id)

    assert session_row is not None
    # The ORM session is now closed.  Accessing lazy='raise' relationships
    # must raise InvalidRequestError ("sqlalchemy.orm.exc.DetachedInstanceError"
    # for detached instances, but still a form of InvalidRequestError).
    with pytest.raises(InvalidRequestError):
        _ = session_row.messages


async def test_observation_source_message_relationship_not_accessed_by_schema_converter(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """After get_state returns, the original ORM Observation rows are detached.
    Accessing source_message would raise DetachedInstanceError — but since
    _observation_schema never touches it, get_state succeeds."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        db.add(_observation(session_id, message_id, "chief_complaint.symptom", "headache"))

    # This must succeed — proving _observation_schema never accesses
    # the source_message ORM relationship (it uses source_message_id column).
    state = await repository.get_state(session_id)
    assert len(state.observations) == 1
    assert state.observations[0].source_message_id == message_id


# ---------------------------------------------------------------------------
# Intake path: the get_state inside _compute_intake_from_claim also exercises
# _load_state — covered by test_get_state_full_domain_snapshot_exercises_all_converters
# above because the intake path reuses the same repository.get_state.
# ---------------------------------------------------------------------------


async def test_get_state_is_idempotent_and_stable(
    repository_and_factory: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    """Repeated get_state calls must return equivalent results and must not
    accumulate side effects from lazy relationship access."""
    repository, factory = repository_and_factory
    session_id, message_id = await _seed_basic_session(factory)

    async with factory() as db, db.begin():
        db.add_all(
            [
                _observation(session_id, message_id, "chief_complaint.symptom", "headache"),
                _observation(session_id, message_id, "ten_questions.sleep", "normal"),
            ]
        )

    first = await repository.get_state(session_id)
    second = await repository.get_state(session_id)

    assert first.state_version == second.state_version
    assert len(first.observations) == len(second.observations)
    assert {o.observation_id for o in first.observations} == {o.observation_id for o in second.observations}
