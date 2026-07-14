"""L2-1 contracts, including PostgreSQL-enforced migration invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from app.db.base import Base
from app.models import ArtifactRevision, GateResult, GraphRun, GraphRunStep, Observation, SafetyProfile  # noqa: F401
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from tests._database_safety import destructive_database_environment


def _observation(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "observation_id": uuid4(),
        "session_id": uuid4(),
        "fact_key": "allergy",
        "value": ["x"],
        "source_message_id": uuid4(),
        "status": ObservationStatus.ACTIVE,
        "created_at": datetime.now(UTC),
    }
    data.update(changes)
    return data


def test_observation_requires_valid_lifecycle_relation() -> None:
    assert ObservationSchema(**_observation()).status is ObservationStatus.ACTIVE
    with pytest.raises(ValidationError):
        ObservationSchema(**_observation(status=ObservationStatus.RETRACTED))
    with pytest.raises(ValidationError):
        ObservationSchema(**_observation(supersedes_observation_id=uuid4()))


@pytest.mark.parametrize("field,value", [("pregnancy_value", "invalid"), ("lactation_value", "invalid")])
def test_safety_schema_rejects_invalid_value_domains(field: str, value: str) -> None:
    status = "pregnancy_collection_status" if field.startswith("pregnancy") else "lactation_collection_status"
    with pytest.raises(ValidationError):
        SafetyProfileSchema(session_id=uuid4(), **{status: "collected", field: value})


def test_safety_unknown_explicit_none_and_collected_are_distinct() -> None:
    assert SafetyProfileSchema(session_id=uuid4()).allergy_collection_status is CollectionStatus.UNKNOWN
    assert SafetyProfileSchema(session_id=uuid4(), allergy_collection_status="explicitly_none").allergens is None
    profile = SafetyProfileSchema(session_id=uuid4(), allergy_collection_status="collected", allergens=["penicillin"])
    assert profile.allergens == ["penicillin"]
    with pytest.raises(ValidationError):
        SafetyProfileSchema(session_id=uuid4(), allergy_collection_status="collected", allergens=[])


def test_artifact_revision_schema_requires_immediately_preceding_parent() -> None:
    base = {
        "artifact_id": uuid4(),
        "artifact_type": "formula",
        "session_id": uuid4(),
        "input_state_version": 1,
        "status": ArtifactStatus.CURRENT,
        "produced_by_run_id": uuid4(),
        "created_at": datetime.now(UTC),
    }
    assert ArtifactRevisionSchema(**base, revision=1).parent_revision_id is None
    with pytest.raises(ValidationError):
        ArtifactRevisionSchema(**base, revision=2)
    with pytest.raises(ValidationError):
        ArtifactRevisionSchema(**base, revision=2, parent_revision_id=uuid4(), parent_revision=2)


def test_orm_metadata_has_partial_current_index_and_parent_fk() -> None:
    table = Base.metadata.tables["artifact_revisions"]
    assert any(index.name == "uq_artifact_revisions_one_current" and index.unique for index in table.indexes)
    assert any(fk.name == "fk_artifact_revisions_parent_same_artifact_session" for fk in table.foreign_key_constraints)
    assert table.c.parent_revision.nullable


@pytest.fixture(scope="module")
def postgres_connection() -> psycopg.Connection[tuple[object, ...]]:
    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        connection: psycopg.Connection[tuple[object, ...]] | None = None
        try:
            command.downgrade(config, "20250624_0001")
            command.upgrade(config, "20260710_0002")
            command.downgrade(config, "20250624_0001")
            command.upgrade(config, "20260710_0002")
            connection = psycopg.connect(db_url)
            yield connection
        finally:
            if connection is not None:
                connection.close()
            # Even a failed setup must leave the isolated database at head so
            # subsequent modules never depend on fixture ordering.
            command.upgrade(config, "head")


def _session(connection: psycopg.Connection[tuple[object, ...]]) -> UUID:
    session_id = uuid4()
    connection.execute("INSERT INTO consult_sessions (id, patient_info) VALUES (%s, '{}'::jsonb)", (session_id,))
    return session_id


def _run(connection: psycopg.Connection[tuple[object, ...]], session_id: UUID) -> UUID:
    run_id = uuid4()
    connection.execute(
        "INSERT INTO graph_runs (id, session_id, graph_version, command_id, input_state_version, status) VALUES (%s, %s, 'v1', %s, 1, 'running')",
        (run_id, session_id, str(uuid4())),
    )
    return run_id


def _artifact(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    artifact_id: UUID,
    session_id: UUID,
    run_id: UUID,
    revision: int,
    status: str,
    parent_id: UUID | None = None,
    parent_revision: int | None = None,
) -> UUID:
    row_id = uuid4()
    connection.execute(
        "INSERT INTO artifact_revisions (id, artifact_id, artifact_type, revision, session_id, input_state_version, status, produced_by_run_id, parent_revision_id, parent_revision) VALUES (%s, %s, 'formula', %s, %s, 1, %s, %s, %s, %s)",
        (row_id, artifact_id, revision, session_id, status, run_id, parent_id, parent_revision),
    )
    return row_id


@pytest.mark.integration
def test_postgres_fk_actions_match_orm(postgres_connection: psycopg.Connection[tuple[object, ...]]) -> None:
    rows = postgres_connection.execute(
        "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text IN ('observations', 'safety_profiles', 'graph_runs', 'graph_run_steps', 'artifact_revisions', 'gate_results')"
    ).fetchall()
    definitions = "\n".join(str(row[1]) for row in rows)
    for expected in (
        "FOREIGN KEY (session_id) REFERENCES consult_sessions(id) ON DELETE CASCADE",
        "FOREIGN KEY (source_message_id) REFERENCES consult_messages(id) ON DELETE RESTRICT",
        "FOREIGN KEY (supersedes_observation_id) REFERENCES observations(id) ON DELETE RESTRICT",
        "FOREIGN KEY (graph_run_id) REFERENCES graph_runs(id) ON DELETE CASCADE",
        "FOREIGN KEY (produced_by_run_id) REFERENCES graph_runs(id) ON DELETE RESTRICT",
        "FOREIGN KEY (graph_run_id) REFERENCES graph_runs(id) ON DELETE SET NULL",
        "FOREIGN KEY (parent_revision_id, artifact_id, session_id, parent_revision) REFERENCES artifact_revisions(id, artifact_id, session_id, revision) ON DELETE RESTRICT",
    ):
        assert expected in definitions


@pytest.mark.integration
def test_postgres_enforces_safety_and_artifact_constraints(
    postgres_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    connection = postgres_connection
    try:
        session_id, other_session = _session(connection), _session(connection)
        run_id, other_run = _run(connection, session_id), _run(connection, other_session)
        for status_column, value_column in (
            ("pregnancy_collection_status", "pregnancy_value"),
            ("lactation_collection_status", "lactation_value"),
        ):
            with pytest.raises(psycopg.Error), connection.transaction():
                connection.execute(
                    f"INSERT INTO safety_profiles (id, session_id, {status_column}, {value_column}) "
                    "VALUES (%s, %s, 'collected', 'invalid')",
                    (uuid4(), session_id),
                )
        artifact_id = uuid4()
        first = _artifact(
            connection, artifact_id=artifact_id, session_id=session_id, run_id=run_id, revision=1, status="superseded"
        )
        second = _artifact(
            connection,
            artifact_id=artifact_id,
            session_id=session_id,
            run_id=run_id,
            revision=2,
            status="current",
            parent_id=first,
            parent_revision=1,
        )
        with pytest.raises(psycopg.Error), connection.transaction():
            _artifact(
                connection,
                artifact_id=artifact_id,
                session_id=session_id,
                run_id=run_id,
                revision=3,
                status="current",
                parent_id=second,
                parent_revision=2,
            )
        session_id, other_session = _session(connection), _session(connection)
        run_id, other_run = _run(connection, session_id), _run(connection, other_session)
        parent = _artifact(
            connection, artifact_id=uuid4(), session_id=session_id, run_id=run_id, revision=1, status="superseded"
        )
        with pytest.raises(psycopg.Error), connection.transaction():
            _artifact(
                connection,
                artifact_id=uuid4(),
                session_id=session_id,
                run_id=run_id,
                revision=2,
                status="current",
                parent_id=parent,
                parent_revision=1,
            )
        with pytest.raises(psycopg.Error), connection.transaction():
            _artifact(
                connection,
                artifact_id=uuid4(),
                session_id=other_session,
                run_id=other_run,
                revision=2,
                status="current",
                parent_id=parent,
                parent_revision=1,
            )
    finally:
        connection.rollback()
