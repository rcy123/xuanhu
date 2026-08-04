"""L4.5 Session Read Model projection and API regressions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent_runtime.repository import artifact_payload_digest
from app.core.config import get_settings
from app.db.session import _build_async_pg_url, get_db, reset_session_factory
from app.main import app
from app.models.consult import ConsultSession
from app.models.domain import ArtifactRevision, ArtifactRevisionPayload, GateResult, GraphRun
from app.schemas.session_read_model import (
    SessionReadModelV1,
    SessionReadProjection,
    SessionUnresolvedReadModelV1,
)
from app.services.session_read_model import (
    ArtifactAuthorityRow,
    GateAuthorityRow,
    _merge_pending_safety_assertions,
    project_session_read_model,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_pending_safety_replaces_duplicate_missing_without_requesting_clinical_review() -> None:
    projection = SessionReadProjection(
        read_model=SessionReadModelV1(
            agent_runtime="langgraph",
            graph={"revision": 3},
            review_required=False,
            unresolved=(
                SessionUnresolvedReadModelV1(
                    source="completeness",
                    kind="missing_required",
                    key="safety.allergy_status",
                ),
                SessionUnresolvedReadModelV1(
                    source="completeness",
                    kind="missing_required",
                    key="safety.medication_status",
                ),
            ),
        )
    )

    merged = _merge_pending_safety_assertions(projection, ("allergy", "red_flag"))

    assert merged.read_model.review_required is False
    assert [(item.source, item.kind, item.key) for item in merged.read_model.unresolved] == [
        ("completeness", "missing_required", "safety.medication_status"),
        ("safety_confirmation", "unconfirmed_safety_fact", "allergy"),
    ]


def _session(*, state_version: int = 4, snapshot: dict[str, Any] | None = None) -> ConsultSession:
    now = _now()
    return ConsultSession(
        id=uuid.uuid4(),
        patient_info={"patient_ref": "READ-MODEL-TEST"},
        chief_complaint="腹胀乏力",
        current_stage="safety",
        status="active",
        agent_runtime="langgraph",
        pending_review=False,
        rollback_counts={},
        state_snapshot=snapshot,
        state_version=state_version,
        recovery_status="normal",
        created_at=now,
        updated_at=now,
    )


def _run(session_id: uuid.UUID, state_version: int) -> GraphRun:
    now = _now()
    return GraphRun(
        id=uuid.uuid4(),
        session_id=session_id,
        graph_version="v1",
        command_id=f"command-{state_version}",
        input_state_version=state_version,
        status="completed",
        created_at=now,
        completed_at=now,
    )


def _gate(
    session_id: uuid.UUID,
    run: GraphRun | None,
    *,
    name: str,
    policy: str,
    state_version: int,
    decision: str,
    details: dict[str, Any],
) -> GateAuthorityRow:
    return GateAuthorityRow(
        gate=GateResult(
            id=uuid.uuid4(),
            session_id=session_id,
            graph_run_id=run.id if run is not None else None,
            gate_name=name,
            policy_version=policy,
            input_state_version=state_version,
            decision=decision,
            details=details,
            created_at=_now(),
        ),
        graph_run=run,
    )


def _artifact(
    session_id: uuid.UUID,
    run: GraphRun,
    *,
    artifact_type: str,
    revision: int,
    schema_version: str,
    payload: dict[str, Any],
) -> tuple[ArtifactAuthorityRow, tuple[GateAuthorityRow, GateAuthorityRow]]:
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{artifact_type}:{session_id}")
    artifact_row_id = uuid.uuid4()
    digest = artifact_payload_digest(schema_version, payload)
    artifact = ArtifactRevision(
        id=artifact_row_id,
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        revision=revision,
        session_id=session_id,
        input_state_version=run.input_state_version,
        status="current",
        produced_by_run_id=run.id,
        parent_revision_id=None,
        parent_revision=None,
        created_at=_now(),
    )
    payload_row = ArtifactRevisionPayload(
        id=uuid.uuid4(),
        artifact_revision_id=artifact_row_id,
        session_id=session_id,
        artifact_id=artifact_id,
        revision=revision,
        payload_schema_version=schema_version,
        content_digest=digest,
        payload=payload,
        created_at=_now(),
    )
    if artifact_type == "syndrome_draft":
        gate_name = "syndrome_verifier"
        gate_policy = "syndrome-draft-policy.no-rag.v1"
    else:
        gate_name = "formula_consistency"
        gate_policy = "formula-consistency-policy.v1"
    gates = (
        _gate(
            session_id,
            run,
            name="canonical_verifier_chain",
            policy="l2-4-v1",
            state_version=run.input_state_version,
            decision="passed",
            details={"subject_digest": "0" * 64},
        ),
        _gate(
            session_id,
            run,
            name=gate_name,
            policy=gate_policy,
            state_version=run.input_state_version,
            decision="passed",
            details={"artifact_digest": digest},
        ),
    )
    return ArtifactAuthorityRow(artifact=artifact, payload=payload_row, graph_run=run), gates


def _syndrome_output(fact_id: uuid.UUID) -> dict[str, Any]:
    return {
        "schema_version": "syndrome-draft.v1",
        "decision": "completed",
        "syndrome": "脾虚湿盛证",
        "syndrome_basis": [{"claim": "纳差乏力支持脾虚", "fact_ids": [str(fact_id)]}],
        "differential": [],
        "treatment_principle": "健脾化湿",
        "confidence": 0.6,
        "evidence_mode": "model_knowledge_only",
        "claim_evidence_links": [],
        "missing_inputs": [],
        "review_required": True,
    }


def _formula_composition(name: str, fact_id: uuid.UUID) -> dict[str, Any]:
    return {
        "name": name,
        "composition": [{"herb": "白术", "dose": 9.0, "unit": "g", "note": None}],
        "rationale": "健脾益气",
        "basis": [{"claim": "脾虚治以健脾", "fact_ids": [str(fact_id)]}],
    }


def _formula_output(fact_id: uuid.UUID) -> dict[str, Any]:
    return {
        "schema_version": "formula-draft.v1",
        "decision": "completed",
        "base_formula": _formula_composition("四君子汤", fact_id),
        "modifications": [
            {
                "action": "add",
                "herb": "茯苓",
                "dose": 12.0,
                "unit": "g",
                "reason": "增强渗湿",
                "basis": {"claim": "湿盛需渗湿", "fact_ids": [str(fact_id)]},
            }
        ],
        "candidate_formula": {
            "name": "四君子汤加茯苓",
            "composition": [
                {"herb": "白术", "dose": 9.0, "unit": "g", "note": None},
                {"herb": "茯苓", "dose": 12.0, "unit": "g", "note": None},
            ],
            "rationale": "健脾渗湿",
            "basis": [{"claim": "脾虚湿盛需兼顾", "fact_ids": [str(fact_id)]}],
        },
        "rationale": "辨证选方并加味",
        "confidence": 0.6,
        "evidence_mode": "model_knowledge_only",
        "claim_evidence_links": [],
        "missing_inputs": [],
        "review_required": True,
    }


def _authority_fixture() -> tuple[
    ConsultSession,
    GraphRun,
    tuple[GateAuthorityRow, ...],
    tuple[ArtifactAuthorityRow, ...],
]:
    session = _session()
    fact_id = uuid.uuid4()
    intake_run = _run(session.id, 1)
    syndrome_run = _run(session.id, 2)
    formula_run = _run(session.id, 3)
    syndrome_output = _syndrome_output(fact_id)
    syndrome_payload = {
        "kind": "syndrome_draft",
        "output": syndrome_output,
        "input_payload": {"state_version": 2},
        "run_spec": {},
        "run_artifact": {},
        "verification": {"passed": True},
    }
    formula_payload = {
        "kind": "formula_draft",
        "output": _formula_output(fact_id),
        "input_payload": {"state_version": 3, "syndrome_draft": syndrome_output},
        "run_spec": {},
        "run_artifact": {},
        "verification": {"passed": True},
        "consistency": {"passed": True},
    }
    syndrome_artifact, syndrome_gates = _artifact(
        session.id,
        syndrome_run,
        artifact_type="syndrome_draft",
        revision=1,
        schema_version="syndrome-artifact-payload.v1",
        payload=syndrome_payload,
    )
    formula_artifact, formula_gates = _artifact(
        session.id,
        formula_run,
        artifact_type="formula_draft",
        revision=1,
        schema_version="formula-artifact-payload.v1",
        payload=formula_payload,
    )
    intake_gates = (
        _gate(
            session.id,
            intake_run,
            name="triage",
            policy="triage-red-flag.v1",
            state_version=1,
            decision="passed",
            details={"disposition": "continue", "candidate_count": 0},
        ),
        _gate(
            session.id,
            intake_run,
            name="completeness",
            policy="completeness-policy.v1",
            state_version=1,
            decision="passed",
            details={
                "disposition": "ready",
                "covered_dimensions": ["chief_complaint.symptom"],
                "missing_required": [],
                "missing_optional": [],
                "conflicting_dimensions": [],
            },
        ),
    )
    return (
        session,
        formula_run,
        (*intake_gates, *syndrome_gates, *formula_gates),
        (syndrome_artifact, formula_artifact),
    )


def test_read_model_projects_current_verified_l3_l4_authority_and_legacy_fields() -> None:
    session, latest_run, gates, artifacts = _authority_fixture()

    result = project_session_read_model(
        session,
        latest_graph_run=latest_run,
        gates=gates,
        artifacts=artifacts,
    )

    assert result.read_model.schema_version == "session-read-model.v1"
    assert result.read_model.agent_runtime == "langgraph"
    assert result.read_model.graph.graph_run_id == latest_run.id
    assert result.read_model.graph.graph_version == "v1"
    assert result.read_model.graph.revision == 4
    assert result.read_model.graph.status == "completed"
    assert [(item.gate_name, item.decision) for item in result.read_model.gates] == [
        ("triage", "passed"),
        ("completeness", "passed"),
    ]
    assert [(item.artifact_type, item.revision, item.status) for item in result.read_model.artifacts] == [
        ("syndrome_draft", 1, "current"),
        ("formula_draft", 1, "current"),
    ]
    assert result.read_model.evidence_mode == "model_knowledge_only"
    assert result.read_model.review_required is True
    assert result.read_model.unresolved == ()
    assert result.sufficiency_report is not None
    assert result.sufficiency_report["sufficient"] is True
    assert result.sufficiency_report["missing_items"] == []
    assert result.syndrome_result is not None
    assert result.syndrome_result["syndrome"] == "脾虚湿盛证"
    assert result.base_formula is not None and result.base_formula["name"] == "四君子汤"
    assert result.modified_formula is not None and result.modified_formula["name"] == "四君子汤加茯苓"
    assert result.modifications is not None and result.modifications[0]["herb"] == "茯苓"


def test_tampered_syndrome_payload_fails_closed_and_hides_dependent_formula() -> None:
    session, latest_run, gates, artifacts = _authority_fixture()
    syndrome_payload = artifacts[0].payload
    assert syndrome_payload is not None
    syndrome_payload.payload["output"]["syndrome"] = "篡改后的证型"

    result = project_session_read_model(
        session,
        latest_graph_run=latest_run,
        gates=gates,
        artifacts=artifacts,
    )

    assert result.read_model.artifacts == ()
    assert result.syndrome_result is None
    assert result.base_formula is None
    assert result.modified_formula is None
    assert result.modifications is None
    unavailable = {item.key for item in result.read_model.unresolved if item.kind == "artifact_unavailable"}
    assert unavailable == {"syndrome_draft", "formula_draft"}


def test_artifact_without_authoritative_verifier_gate_is_not_exposed() -> None:
    session, latest_run, gates, artifacts = _authority_fixture()
    gates_without_formula_verifier = tuple(row for row in gates if row.gate.gate_name != "formula_consistency")

    result = project_session_read_model(
        session,
        latest_graph_run=latest_run,
        gates=gates_without_formula_verifier,
        artifacts=artifacts,
    )

    assert [item.artifact_type for item in result.read_model.artifacts] == ["syndrome_draft"]
    assert result.syndrome_result is not None
    assert result.base_formula is None
    assert result.modified_formula is None


def test_state_snapshot_is_never_used_as_a_clinical_result_source() -> None:
    session = _session(
        snapshot={
            "syndrome_result": {"syndrome": "伪造快照证型"},
            "base_formula": {"name": "伪造快照处方"},
        }
    )

    result = project_session_read_model(session)

    assert result.read_model.artifacts == ()
    assert result.syndrome_result is None
    assert result.base_formula is None


def test_standalone_deterministic_triage_gate_projects_red_flag_unresolved() -> None:
    session = _session(state_version=1)
    session.current_stage = "blocked"
    session.status = "blocked"
    gate = _gate(
        session.id,
        None,
        name="triage",
        policy="triage-red-flag.v1",
        state_version=1,
        decision="blocked",
        details={"disposition": "emergency_referral", "candidate_count": 1},
    )

    result = project_session_read_model(session, gates=(gate,))

    assert len(result.read_model.gates) == 1
    assert result.read_model.gates[0].decision == "blocked"
    assert result.read_model.unresolved[0].kind == "red_flag"
    assert result.read_model.unresolved[0].key == "emergency_referral"


class _SessionResult:
    def __init__(self, session: ConsultSession) -> None:
        self._session = session

    def scalar_one_or_none(self) -> ConsultSession:
        return self._session


class _SessionDB:
    def __init__(self, session: ConsultSession) -> None:
        self._session = session

    async def execute(self, _statement: object) -> _SessionResult:
        return _SessionResult(self._session)


@pytest.mark.asyncio
async def test_get_session_api_serializes_versioned_read_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, latest_run, gates, artifacts = _authority_fixture()
    projection = project_session_read_model(
        session,
        latest_graph_run=latest_run,
        gates=gates,
        artifacts=artifacts,
    )

    async def fake_build(_self: object, loaded: ConsultSession) -> SessionReadProjection:
        assert loaded is session
        return projection

    async def override_db() -> Any:
        yield _SessionDB(session)

    monkeypatch.setattr("app.services.session.SessionReadModelService.build", fake_build)
    app.dependency_overrides[get_db] = override_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/consult/sessions/{session.id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["agent_runtime"] == "langgraph"
    assert data["read_model"]["schema_version"] == "session-read-model.v1"
    assert data["read_model"]["graph"]["revision"] == 4
    assert data["read_model"]["artifacts"][1]["artifact_type"] == "formula_draft"
    assert data["syndrome_result"]["syndrome"] == "脾虚湿盛证"
    assert data["modified_formula"]["name"] == "四君子汤加茯苓"


def test_legacy_session_returns_empty_versioned_model_without_snapshot_fallback() -> None:
    session = _session(snapshot={"syndrome_result": {"syndrome": "旧快照"}})
    session.agent_runtime = "legacy"

    result = project_session_read_model(session)

    assert result.read_model == SessionReadModelV1(
        agent_runtime="legacy",
        graph={"revision": session.state_version},
    )
    assert result.syndrome_result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_read_model_survives_fresh_http_get_session() -> None:
    """Persisted gates/artifacts, not process state, must drive a later GET."""
    session, latest_run, gates, artifacts = _authority_fixture()
    session.state_snapshot = {
        "syndrome_result": {"syndrome": "forged-snapshot-syndrome"},
        "modified_formula": {"name": "forged-snapshot-formula"},
    }

    graph_run_candidates = [row.graph_run for row in gates] + [row.graph_run for row in artifacts]
    graph_runs = {run.id: run for run in graph_run_candidates if run is not None}
    database_url = _build_async_pg_url(get_settings().database_url)
    writer_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with AsyncSession(writer_engine, expire_on_commit=False) as writer:
            writer.add(session)
            await writer.flush()
            writer.add_all(graph_runs.values())
            await writer.flush()
            writer.add_all(row.artifact for row in artifacts)
            await writer.flush()
            writer.add_all(row.payload for row in artifacts if row.payload is not None)
            writer.add_all(row.gate for row in gates)
            await writer.commit()
    finally:
        await writer_engine.dispose()

    # Force the application to open its own engine/session after the writer is
    # closed.  The request uses the real get_db dependency and PostgreSQL rows.
    await reset_session_factory()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/consult/sessions/{session.id}")

        assert response.status_code == 200
        data = response.json()["data"]
        read_model = data["read_model"]
        assert read_model["schema_version"] == "session-read-model.v1"
        assert read_model["graph"] == {
            "graph_run_id": str(latest_run.id),
            "graph_version": "v1",
            "revision": 4,
            "input_state_version": 3,
            "status": "completed",
        }
        assert [(gate["gate_name"], gate["decision"]) for gate in read_model["gates"]] == [
            ("triage", "passed"),
            ("completeness", "passed"),
        ]
        assert [artifact["artifact_type"] for artifact in read_model["artifacts"]] == [
            "syndrome_draft",
            "formula_draft",
        ]
        assert all(artifact["verification_gate"]["decision"] == "passed" for artifact in read_model["artifacts"])
        assert data["syndrome_result"]["syndrome"] != "forged-snapshot-syndrome"
        assert data["modified_formula"]["name"] != "forged-snapshot-formula"
    finally:
        await reset_session_factory()
        cleanup_engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with cleanup_engine.begin() as connection:
                await connection.execute(
                    delete(ArtifactRevisionPayload).where(ArtifactRevisionPayload.session_id == session.id)
                )
                await connection.execute(delete(ArtifactRevision).where(ArtifactRevision.session_id == session.id))
                await connection.execute(delete(GateResult).where(GateResult.session_id == session.id))
                await connection.execute(delete(GraphRun).where(GraphRun.session_id == session.id))
                await connection.execute(delete(ConsultSession).where(ConsultSession.id == session.id))
        finally:
            await cleanup_engine.dispose()
