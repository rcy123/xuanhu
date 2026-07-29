from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.repository import PostgresDomainRepository
from app.api.advance import _advance_command_key, _run_langgraph_advance
from app.core.config import get_settings
from app.core.exceptions import ModelGatewayUnavailableError
from app.db.session import get_session_factory
from app.main import app
from app.models.consult import ConsultSession
from app.models.domain import (
    ArtifactRevision,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    OutboxEvent,
)
from app.models.http_command import HttpCommandClaim
from app.models.review import DoctorReview, MedicalRecord
from app.models.safety import SafetyRuleRun
from app.schemas.review import FormulaOverride, HerbOverrideItem, ReviewRequest
from app.services import langgraph_record
from app.services.langgraph_record import MEDICAL_RECORD_ARTIFACT_TYPE, RECORD_POLICY_VERSION
from app.services.langgraph_review import LangGraphReviewService
from tests.test_l5_prod_review_integration import _prepare_interrupt, _seed_safety_stage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def _confirmed_record_stage() -> tuple[uuid.UUID, int, str]:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    factory = get_session_factory()
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(action="confirm", feedback="confirmed after deterministic review"),
            doctor_id="integration-record-doctor",
            trace_id="integration-record-confirm",
            x_state_version=3,
            idempotency_key=f"record-confirm:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert response.current_stage == "record"
    return seed.session_id, response.state_version, seed.herb_name


async def _modified_record_stage() -> tuple[uuid.UUID, int, str]:
    seed = await _seed_safety_stage()
    await _prepare_interrupt(seed)
    factory = get_session_factory()
    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(
                action="modify",
                formula_override=FormulaOverride(
                    name="integration modified formula",
                    composition=[
                        HerbOverrideItem(
                            herb=seed.herb_name,
                            dose=6,
                            unit="g",
                            note="post-review override",
                        )
                    ],
                    rationale="integration modification",
                ),
                feedback="dose adjusted after review",
            ),
            doctor_id="integration-record-modifier",
            trace_id="integration-record-modify",
            x_state_version=3,
            idempotency_key=f"record-modify:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert response.current_stage == "record"
    return seed.session_id, response.state_version, seed.herb_name


async def _post_record_advance(
    session_id: uuid.UUID,
    state_version: int,
    *,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, object]]:
    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": idempotency_key or f"record-advance:{uuid.uuid4()}",
                    "X-State-Version": str(state_version),
                    "X-Doctor-Id": "integration-record-doctor",
                },
            )
    finally:
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_public_record_advance_commits_artifact_projection_done_and_replays() -> None:
    session_id, state_version, herb_name = await _confirmed_record_stage()
    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    idempotency_key = f"record-advance-{uuid.uuid4()}"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": idempotency_key,
                    "X-State-Version": str(state_version),
                    "X-Doctor-Id": "integration-record-doctor",
                },
            )
            replay = await client.post(
                f"/api/v1/consult/sessions/{session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": idempotency_key,
                    "X-State-Version": str(state_version),
                    "X-Doctor-Id": "integration-record-doctor",
                },
            )
            record_response = await client.get(
                f"/api/v1/consult/sessions/{session_id}/record",
                params={"version": "latest"},
            )
    finally:
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")

    assert response.status_code == 200, response.text
    assert replay.status_code == 200 and replay.json()["data"] == response.json()["data"]
    body = response.json()["data"]
    assert body["current_stage"] == "done"
    assert body["from_stage"] == "record"
    assert body["state_version"] == state_version + 1
    assert record_response.status_code == 200, record_response.text
    record_data = record_response.json()["data"]
    assert record_data["version"] == 1
    assert record_data["record_json"]["formula"]["composition"][0]["herb"] == herb_name

    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None and session.current_stage == "done" and session.status == "done"
        review = await db.scalar(
            select(DoctorReview)
            .where(DoctorReview.session_id == session_id)
            .order_by(DoctorReview.created_at.desc())
            .limit(1)
        )
        assert review is not None
        review_output = record_data["record_json"]["doctor_review"]
        assert review_output == {
            "review_id": str(review.id),
            "session_id": str(session_id),
            "agent_run_id": None,
            "safety_rule_run_id": str(review.safety_rule_run_id),
            "action": "confirm",
            "reviewed_by": "integration-record-doctor",
            "reviewed_at": review.created_at.isoformat(),
            "feedback": "confirmed after deterministic review",
            "original_formula": review.original_formula,
            "formula_override": None,
        }
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 1
        assert await db.scalar(
            select(func.count())
            .select_from(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == session_id,
                ArtifactRevision.artifact_type == MEDICAL_RECORD_ARTIFACT_TYPE,
                ArtifactRevision.status == "current",
            )
        ) == 1
        gate = await db.scalar(
            select(GateResult).where(
                GateResult.session_id == session_id,
                GateResult.gate_name == "record_consistency",
            )
        )
        assert gate is not None and gate.policy_version == RECORD_POLICY_VERSION
        assert await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.session_id == session_id, OutboxEvent.event_type == "session.done.v1")
        ) == 1
        assert await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "completed",
            )
        ) == 2

    config = make_run_config(str(session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        snapshot = await build_main_graph(checkpointer=saver).aget_state(config)  # type: ignore[arg-type]
        serialized = json.dumps(snapshot.values, ensure_ascii=False, default=str)
        assert herb_name not in serialized
        assert "composition" not in serialized


@pytest.mark.asyncio
async def test_record_authority_mismatch_fails_closed_without_projection() -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        review = await db.scalar(
            select(DoctorReview)
            .where(DoctorReview.session_id == session_id)
            .order_by(DoctorReview.created_at.desc())
            .limit(1)
        )
        assert review is not None
        review.action = "reject"

    fallback_was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    fallback_previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = True
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": f"tampered-record-{uuid.uuid4()}",
                    "X-State-Version": str(state_version),
                },
            )
    finally:
        if fallback_was_set:
            app.state.allow_request_local_langgraph_test_runtime = fallback_previous
        else:
            delattr(app.state, "allow_request_local_langgraph_test_runtime")

    assert response.status_code == 503
    assert response.json()["code"] == "MODEL_GATEWAY_UNAVAILABLE"
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None and session.current_stage == "record"
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0
        assert await db.scalar(
            select(func.count())
            .select_from(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == session_id,
                ArtifactRevision.artifact_type == MEDICAL_RECORD_ARTIFACT_TYPE,
            )
        ) == 0


@pytest.mark.parametrize(
    "field",
    ["reviewed_by", "created_at", "feedback", "original_formula", "formula_override"],
)
async def test_record_rejects_tampered_confirm_review_projection_field(field: str) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        review = await db.scalar(
            select(DoctorReview)
            .where(DoctorReview.session_id == session_id)
            .order_by(DoctorReview.created_at.desc())
            .limit(1)
        )
        assert review is not None
        if field == "reviewed_by":
            review.reviewed_by = "tampered-doctor"
        elif field == "created_at":
            review.created_at = (review.created_at + timedelta(minutes=5)).replace(tzinfo=None)
        elif field == "feedback":
            review.feedback = "tampered feedback"
        elif field == "original_formula":
            review.original_formula = {"composition": [{"herb": "tampered"}]}
        else:
            review.formula_override = {"composition": [{"herb": "tampered"}]}

    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 503
    assert body["code"] == "MODEL_GATEWAY_UNAVAILABLE"
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None and session.current_stage == "record"
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0


async def test_record_rejects_tampered_modify_original_formula() -> None:
    session_id, state_version, _herb_name = await _modified_record_stage()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        review = await db.scalar(
            select(DoctorReview)
            .where(DoctorReview.session_id == session_id)
            .order_by(DoctorReview.created_at.desc())
            .limit(1)
        )
        assert review is not None
        review.original_formula = {"composition": [{"herb": "tampered-original"}]}

    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 503
    assert body["code"] == "MODEL_GATEWAY_UNAVAILABLE"
    async with factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0


async def test_modified_review_fields_are_bound_into_record_output() -> None:
    session_id, state_version, herb_name = await _modified_record_stage()
    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 200, body

    factory = get_session_factory()
    async with factory() as db:
        review = await db.scalar(
            select(DoctorReview)
            .where(DoctorReview.session_id == session_id)
            .order_by(DoctorReview.created_at.desc())
            .limit(1)
        )
        record = await db.scalar(select(MedicalRecord).where(MedicalRecord.session_id == session_id))
        assert review is not None and record is not None
        assert record.record_json["formula"]["composition"][0]["herb"] == herb_name
        assert record.record_json["doctor_review"] == {
            "review_id": str(review.id),
            "session_id": str(session_id),
            "agent_run_id": None,
            "safety_rule_run_id": str(review.safety_rule_run_id),
            "action": "modify",
            "reviewed_by": "integration-record-modifier",
            "reviewed_at": review.created_at.isoformat(),
            "feedback": "dose adjusted after review",
            "original_formula": review.original_formula,
            "formula_override": review.formula_override,
        }
        assert review.original_formula != review.formula_override
        assert record.record_json["formula"] == review.formula_override


@pytest.mark.parametrize(
    "corruption",
    ["stale_review_artifact", "failed_review_graph_run", "failed_safety_projection"],
)
async def test_record_fails_closed_for_stale_or_failed_authority(corruption: str) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        review_artifact = await db.scalar(
            select(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == session_id,
                ArtifactRevision.artifact_type == "doctor_review",
                ArtifactRevision.status == "current",
            )
            .limit(1)
        )
        assert review_artifact is not None
        if corruption == "stale_review_artifact":
            review_artifact.status = "stale"
        elif corruption == "failed_review_graph_run":
            producer = await db.get(GraphRun, review_artifact.produced_by_run_id)
            assert producer is not None
            producer.status = "failed"
        else:
            review = await db.scalar(
                select(DoctorReview)
                .where(DoctorReview.session_id == session_id)
                .order_by(DoctorReview.created_at.desc())
                .limit(1)
            )
            assert review is not None and review.safety_rule_run_id is not None
            safety_run = await db.get(SafetyRuleRun, review.safety_rule_run_id)
            assert safety_run is not None
            safety_run.passed = False

    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 503
    assert body["code"] == "MODEL_GATEWAY_UNAVAILABLE"
    async with factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0


async def test_record_transaction_failure_rolls_back_every_domain_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    factory = get_session_factory()
    async with factory() as db:
        commits_before = await db.scalar(
            select(func.count())
            .select_from(DomainCommandCommit)
            .where(DomainCommandCommit.session_id == session_id)
        )

    async def fail_projection_persistence(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected product projection failure")

    monkeypatch.setattr(
        PostgresDomainRepository,
        "_persist_product_projections",
        fail_projection_persistence,
    )
    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 503
    assert body["code"] == "MODEL_GATEWAY_UNAVAILABLE"

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        assert session.current_stage == "record"
        assert session.state_version == state_version
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0
        assert await db.scalar(
            select(func.count())
            .select_from(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == session_id,
                ArtifactRevision.artifact_type == MEDICAL_RECORD_ARTIFACT_TYPE,
            )
        ) == 0
        assert await db.scalar(
            select(func.count())
            .select_from(DomainCommandCommit)
            .where(DomainCommandCommit.session_id == session_id)
        ) == commits_before


async def test_record_domain_commit_crash_repairs_claim_and_replays_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    idempotency_key = f"record-crash:{uuid.uuid4()}"
    command_key = _advance_command_key(idempotency_key)
    original_complete = langgraph_record._complete_advance_claim

    async def crash_after_domain_commit(**_kwargs: object) -> None:
        raise RuntimeError("injected crash after domain commit")

    monkeypatch.setattr(langgraph_record, "_complete_advance_claim", crash_after_domain_commit)
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(ModelGatewayUnavailableError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=state_version,
                trace_id="record-crash-first",
                idempotency_key=idempotency_key,
                allow_request_local_runtime=True,
            )

    monkeypatch.setattr(langgraph_record, "_complete_advance_claim", original_complete)
    async with factory() as db:
        terminal = await db.get(ConsultSession, session_id)
        assert terminal is not None and terminal.current_stage == "done"
        repaired = await _run_langgraph_advance(
            db,
            terminal,
            session_id=str(session_id),
            state_version=state_version,
            trace_id="record-crash-retry",
            idempotency_key=idempotency_key,
            allow_request_local_runtime=True,
        )

    assert repaired["current_stage"] == "done"
    assert repaired["from_stage"] == "record"
    assert repaired["state_version"] == state_version + 1
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        assert claim is not None
        assert claim.status == "completed"
        assert claim.response_payload == repaired
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 1
        assert await db.scalar(
            select(func.count()).select_from(GraphRun).where(GraphRun.id == claim.run_id)
        ) == 1


async def test_public_retry_repairs_failed_http_claim_from_durable_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    idempotency_key = f"record-http-crash:{uuid.uuid4()}"
    original_complete = langgraph_record._complete_advance_claim

    async def crash_after_domain_commit(**_kwargs: object) -> None:
        raise RuntimeError("injected HTTP crash after domain commit")

    monkeypatch.setattr(langgraph_record, "_complete_advance_claim", crash_after_domain_commit)
    first_status, first_body = await _post_record_advance(
        session_id,
        state_version,
        idempotency_key=idempotency_key,
    )
    assert first_status == 503
    assert first_body["code"] == "MODEL_GATEWAY_UNAVAILABLE"

    monkeypatch.setattr(langgraph_record, "_complete_advance_claim", original_complete)
    replay_status, replay_body = await _post_record_advance(
        session_id,
        state_version,
        idempotency_key=idempotency_key,
    )
    assert replay_status == 200, replay_body
    assert replay_body["data"]["current_stage"] == "done"
    assert replay_body["data"]["state_version"] == state_version + 1

    factory = get_session_factory()
    async with factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 1
        http_claims = tuple(
            await db.scalars(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.advance.v1",
                    HttpCommandClaim.scope_key == f"session:{session_id}",
                )
            )
        )
        assert len(http_claims) == 1
        assert http_claims[0].status == "completed"
        assert http_claims[0].response_payload == {
            "data": replay_body["data"],
            "message": "ok",
        }


async def test_recovery_required_blocks_record_advance_without_mutation() -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        session.recovery_status = "manual_required"
        session.blocked_reason = "record_control_cursor_requires_recovery"

    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 409
    assert body["code"] == "STATE_RECOVERY_REQUIRED"
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        assert session.current_stage == "record"
        assert session.recovery_status == "manual_required"
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == session_id)
        ) == 0


async def test_langgraph_record_never_calls_legacy_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, state_version, _herb_name = await _confirmed_record_stage()

    async def reject_legacy_path(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Legacy Supervisor.advance must not serve LangGraph Record")

    monkeypatch.setattr("app.api.advance.Supervisor.advance", reject_legacy_path)
    status_code, body = await _post_record_advance(session_id, state_version)
    assert status_code == 200, body
    assert body["data"]["current_stage"] == "done"
