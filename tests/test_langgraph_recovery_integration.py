from __future__ import annotations

import json
import uuid
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy import null as sql_null

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import default_state
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.domain import ArtifactRevision, IntakeCommandClaim, OutboxEvent, SafetyProfile
from app.models.review import MedicalRecord
from app.schemas.recovery import RecoveryRequest
from app.services.langgraph_recovery import RECOVERY_CONTROL_ARTIFACT_TYPE, LangGraphRecoveryService
from app.services.recovery import RecoveryService
from tests.test_l5_prod_review_integration import _seed_safety_stage, _SeededReview

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def _run_safety_block(seed: _SeededReview) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        profile = await db.scalar(
            select(SafetyProfile)
            .where(SafetyProfile.session_id == seed.session_id)
            .with_for_update()
        )
        assert profile is not None
        profile.allergy_collection_status = "collected"
        profile.allergens = [seed.herb_name]

    state = default_state(
        session_id=str(seed.session_id),
        command=XuanhuCommand.REVIEW.value,
        command_id=seed.command_id,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(seed.run_id),
    )
    state["domain_state_version"] = 2
    config = make_run_config(str(seed.session_id), graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        result = await GraphRunner(graph, timeout_seconds=30).ainvoke(dict(state), config=config)
        assert result["last_error"]["code"] == "SAFETY_GATE_BLOCKED"
        snapshot = await graph.aget_state(config)
        serialized = json.dumps(snapshot.values, ensure_ascii=False, default=str)
        assert seed.herb_name not in serialized
        assert "composition" not in serialized

    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None
        assert session.current_stage == "blocked"
        assert session.status == "blocked"
        assert session.recovery_status == "manual_required"
        assert session.blocked_reason == "safety_rule_blocked"
        assert session.state_version == 3


async def _set_request_local_runtime(enabled: bool) -> tuple[bool, object | None]:
    was_set = hasattr(app.state, "allow_request_local_langgraph_test_runtime")
    previous = getattr(app.state, "allow_request_local_langgraph_test_runtime", None)
    app.state.allow_request_local_langgraph_test_runtime = enabled
    return was_set, previous


def _restore_request_local_runtime(was_set: bool, previous: object | None) -> None:
    if was_set:
        app.state.allow_request_local_langgraph_test_runtime = previous
    else:
        delattr(app.state, "allow_request_local_langgraph_test_runtime")


@pytest.mark.asyncio
async def test_restart_recover_replays_then_continues_original_runtime_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_safety_stage()
    await _run_safety_block(seed)

    async def _forbid_legacy(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("LangGraph recovery must not invoke Legacy RecoveryService")

    monkeypatch.setattr(RecoveryService, "recover", _forbid_legacy)
    was_set, previous = await _set_request_local_runtime(True)
    recovery_key = f"recovery-restart-{uuid.uuid4()}"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            recovery = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={"action": "retry_current_stage", "reason": "integration restart"},
                headers={
                    "X-Idempotency-Key": recovery_key,
                    "X-Doctor-Id": "integration-recovery-doctor",
                },
            )
            replay = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={"action": "retry_current_stage", "reason": "integration restart"},
                headers={
                    "X-Idempotency-Key": recovery_key,
                    "X-Doctor-Id": "integration-recovery-doctor",
                },
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert recovery.status_code == 200, recovery.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == recovery.json()["data"]
    assert recovery.json()["data"]["current_stage"] == "safety"
    assert recovery.json()["data"]["status"] == "active"

    factory = get_session_factory()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, seed.session_id, with_for_update=True)
        profile = await db.scalar(
            select(SafetyProfile)
            .where(SafetyProfile.session_id == seed.session_id)
            .with_for_update()
        )
        assert session is not None and profile is not None
        assert session.agent_runtime == "langgraph"
        assert session.state_version == 4
        profile.allergy_collection_status = "explicitly_none"
        profile.allergens = None

    was_set, previous = await _set_request_local_runtime(True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            safety = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": f"recovery-safety-{uuid.uuid4()}",
                    "X-State-Version": "4",
                },
            )
            assert safety.status_code == 200, safety.text
            assert safety.json()["data"]["current_stage"] == "review"
            review_version = safety.json()["data"]["state_version"]

            review = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/review",
                json={"action": "confirm"},
                headers={
                    "X-Idempotency-Key": f"recovery-review-{uuid.uuid4()}",
                    "X-State-Version": str(review_version),
                    "X-Doctor-Id": "integration-recovery-doctor",
                },
            )
            assert review.status_code == 200, review.text
            assert review.json()["data"]["current_stage"] == "record"
            record_version = review.json()["data"]["state_version"]

            record = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/advance",
                json={"force": False},
                headers={
                    "X-Idempotency-Key": f"recovery-record-{uuid.uuid4()}",
                    "X-State-Version": str(record_version),
                    "X-Doctor-Id": "integration-recovery-doctor",
                },
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert record.status_code == 200, record.text
    assert record.json()["data"]["current_stage"] == "done"
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.status == "done"
        assert session.agent_runtime == "langgraph"
        assert await db.scalar(
            select(func.count())
            .select_from(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == seed.session_id,
                ArtifactRevision.artifact_type == RECOVERY_CONTROL_ARTIFACT_TYPE,
            )
        ) == 1
        assert await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.session_id == seed.session_id,
                AuditEvent.event_type == "session.recovered",
            )
        ) == 1
        assert await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.session_id == seed.session_id,
                OutboxEvent.event_type == "session.recovered.v1",
            )
        ) == 1
        assert await db.scalar(
            select(func.count()).select_from(MedicalRecord).where(MedicalRecord.session_id == seed.session_id)
        ) == 1


@pytest.mark.asyncio
async def test_missing_checkpoint_fails_closed_without_recovery_projection() -> None:
    seed = await _seed_safety_stage(create_claim=False)
    factory = get_session_factory()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, seed.session_id, with_for_update=True)
        assert session is not None
        session.current_stage = "blocked"
        session.status = "blocked"
        session.recovery_status = "manual_required"
        session.blocked_reason = "safety_rule_blocked"

    was_set, previous = await _set_request_local_runtime(True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={"action": "retry_current_stage"},
                headers={"X-Idempotency-Key": f"missing-checkpoint-{uuid.uuid4()}"},
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "STATE_RECOVERY_REQUIRED"
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.current_stage == "blocked"
        assert session.state_version == 2
        assert await db.scalar(
            select(func.count())
            .select_from(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == seed.session_id,
                ArtifactRevision.artifact_type == RECOVERY_CONTROL_ARTIFACT_TYPE,
            )
        ) == 0


@pytest.mark.asyncio
async def test_resume_from_pg_checkpoint_restores_control_stage_only() -> None:
    seed = await _seed_safety_stage()
    await _run_safety_block(seed)
    public_key = f"resume-checkpoint-{uuid.uuid4()}"
    was_set, previous = await _set_request_local_runtime(True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={"action": "resume_from_pg_snapshot"},
                headers={"X-Idempotency-Key": public_key},
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["current_stage"] == "safety"
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None
        serialized_snapshot = json.dumps(session.state_snapshot, ensure_ascii=False)
        assert seed.herb_name not in serialized_snapshot
        assert "composition" not in serialized_snapshot
        assert session.state_snapshot["langgraph_recovery"]["action"] == "resume_from_pg_snapshot"

    # Simulate a process dying after the atomic Domain commit but before the
    # inner claim response is published.  The same durable command must repair
    # its claim even though the session no longer looks recoverable.
    async with factory() as db, db.begin():
        claim = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == seed.session_id,
                IntakeCommandClaim.idempotency_key.like("recover:%"),
            )
            .with_for_update()
        )
        assert claim is not None and claim.status == "completed"
        claim.status = "running"
        claim.output_state_version = None
        claim.response_payload = cast(Any, sql_null())

    async with factory() as db:
        repaired = await LangGraphRecoveryService(db).recover(
            str(seed.session_id),
            RecoveryRequest(action="resume_from_pg_snapshot"),
            doctor_id=None,
            trace_id="integration-recovery-claim-repair",
            idempotency_key=public_key,
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert repaired.current_stage == "safety"
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == seed.session_id,
                IntakeCommandClaim.idempotency_key.like("recover:%"),
            )
        )
        assert claim is not None and claim.status == "completed"


@pytest.mark.asyncio
async def test_rollback_to_inquiry_invalidates_downstream_authority() -> None:
    seed = await _seed_safety_stage()
    await _run_safety_block(seed)
    was_set, previous = await _set_request_local_runtime(True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={
                    "action": "rollback_to_stage",
                    "target_stage": "inquiry",
                    "reason": "integration rollback",
                },
                headers={"X-Idempotency-Key": f"rollback-inquiry-{uuid.uuid4()}"},
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["current_stage"] == "inquiry"
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.status == "active"
        current_types = set(
            await db.scalars(
                select(ArtifactRevision.artifact_type).where(
                    ArtifactRevision.session_id == seed.session_id,
                    ArtifactRevision.status == "current",
                )
            )
        )
        assert current_types == {RECOVERY_CONTROL_ARTIFACT_TYPE}
        assert await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.session_id == seed.session_id,
                AuditEvent.event_type == "session.recovered",
            )
        ) == 1


@pytest.mark.asyncio
async def test_terminate_is_available_without_checkpoint_and_preserves_runtime() -> None:
    seed = await _seed_safety_stage(create_claim=False)
    factory = get_session_factory()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, seed.session_id, with_for_update=True)
        assert session is not None
        session.current_stage = "blocked"
        session.status = "blocked"
        session.recovery_status = "manual_required"
        session.blocked_reason = "unknown_integration_failure"

    was_set, previous = await _set_request_local_runtime(True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/consult/sessions/{seed.session_id}/recover",
                json={"action": "terminate", "reason": "integration operator termination"},
                headers={
                    "X-Idempotency-Key": f"terminate-no-checkpoint-{uuid.uuid4()}",
                    "X-Doctor-Id": "integration-recovery-doctor",
                },
            )
    finally:
        _restore_request_local_runtime(was_set, previous)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "terminated"
    assert response.json()["data"]["current_stage"] == "blocked"
    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None
        assert session.agent_runtime == "langgraph"
        assert session.status == "terminated"
        assert session.blocked_reason == "terminated_by_doctor"
        assert await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.session_id == seed.session_id,
                AuditEvent.event_type == "session.terminated",
            )
        ) == 1
        assert await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.session_id == seed.session_id,
                OutboxEvent.event_type == "session.terminated.v1",
            )
        ) == 1
