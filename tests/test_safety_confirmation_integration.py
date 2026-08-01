"""PostgreSQL acceptance tests for durable safety-fact confirmation."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.request_context import WriteRequestContext
from app.core.exceptions import InvalidStageTransitionError, ValidationError
from app.db.session import get_session_factory
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
    SafetyFactAssertion,
    SafetyFactTransition,
    SafetyProfile,
)
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    CandidateSeverity,
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    PatientSafetyDelta,
    RedFlagCandidate,
    RedFlagCategory,
    SafetyListDelta,
)
from app.services.safety_confirmation import SafetyConfirmationService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _module_database_connections() -> None:
    from app.db.session import reset_session_factory

    await reset_session_factory()
    yield
    await reset_session_factory()


def _allergy_output(message_id: uuid.UUID, text: str, allergen: str) -> IntakeExtractionOutput:
    start = text.index(allergen)
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        patient_safety_delta=PatientSafetyDelta(
            allergy=SafetyListDelta(
                status=CollectionStatus.COLLECTED,
                values=(allergen,),
                source_message_id=message_id,
                value_spans=(
                    EvidenceSpan(
                        source_message_id=message_id,
                        start_char=start,
                        end_char=start + len(allergen),
                        quote=allergen,
                    ),
                ),
            )
        ),
    )


async def _create_proposal(
    *,
    text: str = "我对青霉素过敏",
    allergen: str = "青霉素",
    session_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    factory = get_session_factory()
    sid = session_id or uuid.uuid4()
    message_id = uuid.uuid4()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, sid)
        if session is None:
            session = ConsultSession(
                id=sid,
                patient_info={},
                chief_complaint=None,
                current_stage="inquiry",
                status="active",
                agent_runtime="langgraph",
                recovery_status="normal",
                state_version=1,
                rollback_counts={},
            )
            db.add(session)
            await db.flush()
        message = ConsultMessage(
            id=message_id,
            session_id=sid,
            role="patient_proxy",
            stage="inquiry",
            content=text,
        )
        db.add(message)
        await db.flush()
        rows = await SafetyConfirmationService(db).propose_from_intake(
            session_id=sid,
            source_message=message,
            output=_allergy_output(message_id, text, allergen),
            extraction_run_id=uuid.uuid4(),
            template_version="intake_extraction_v2.jinja2",
            trace_id=uuid.uuid4().hex,
        )
        assert len(rows) == 1
        return sid, message_id, rows[0].assertion_id


def _headers(key: str) -> dict[str, str]:
    return {"X-Doctor-Id": "doctor-safety", "X-Idempotency-Key": key}


_FINAL_INQUIRY_FACT_KEYS = (
    "chief_complaint.symptom",
    "chief_complaint.course",
    "present_illness.change",
    "ten_questions.cold_heat",
    "ten_questions.sweat",
    "ten_questions.head_body",
    "ten_questions.stool_urine",
    "ten_questions.diet",
    "ten_questions.chest_abdomen",
    "ten_questions.thirst",
    "ten_questions.sleep",
)


async def _create_final_allergy_gap(
    *,
    omit_fact_key: str | None = None,
    outstanding_dimension: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
    factory = get_session_factory()
    session_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    question_id = uuid.uuid4() if outstanding_dimension is not None else None
    async with factory() as db, db.begin():
        session = ConsultSession(
            id=session_id,
            patient_info={},
            chief_complaint="test complaint",
            current_stage="inquiry",
            status="active",
            agent_runtime="langgraph",
            recovery_status="normal",
            state_version=1,
            rollback_counts={},
        )
        source = ConsultMessage(
            id=source_message_id,
            session_id=session_id,
            role="patient_proxy",
            stage="inquiry",
            content="allergy penicillin",
        )
        db.add_all((session, source))
        await db.flush()
        for fact_key in _FINAL_INQUIRY_FACT_KEYS:
            if fact_key == omit_fact_key:
                continue
            db.add(
                Observation(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    fact_key=fact_key,
                    value={"code": "present"},
                    normalized_value={"code": "present"},
                    source_message_id=source_message_id,
                    status="active",
                    confidence=1.0,
                )
            )
        db.add(
            SafetyProfile(
                id=uuid.uuid4(),
                session_id=session_id,
                allergy_collection_status="unknown",
                pregnancy_collection_status="explicitly_none",
                lactation_collection_status="explicitly_none",
                medications_collection_status="explicitly_none",
                major_conditions_collection_status="explicitly_none",
                contraindications_collection_status="unknown",
            )
        )
        if question_id is not None:
            db.add(
                ConsultMessage(
                    id=question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="existing unanswered question?",
                    structured_delta={
                        "selected_dimension": outstanding_dimension,
                        "selection_kind": "required",
                    },
                )
            )
            session.state_snapshot = {
                "langgraph_intake": {
                    "last_patient_message_id": str(source_message_id),
                    "last_question_message_id": str(question_id),
                    "progress": {"no_new_facts_rounds": 0, "followup_rounds": 3},
                }
            }
        await db.flush()
        assertions = await SafetyConfirmationService(db).propose_from_intake(
            session_id=session_id,
            source_message=source,
            output=_allergy_output(source_message_id, source.content, "penicillin"),
            extraction_run_id=uuid.uuid4(),
            template_version="intake_extraction_v2.jinja2",
            trace_id=uuid.uuid4().hex,
        )
    return session_id, assertions[0].assertion_id, question_id


async def test_proposed_fact_is_not_authoritative_until_api_confirmation(client: AsyncClient) -> None:
    session_id, _, assertion_id = await _create_proposal()
    factory = get_session_factory()
    async with factory() as db:
        assert await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id)) is None

    listed = await client.get(f"/api/v1/consult/sessions/{session_id}/safety-assertions?status=proposed")
    assert listed.status_code == 200
    assert [item["assertion_id"] for item in listed.json()["data"]["items"]] == [str(assertion_id)]
    before = await client.get(f"/api/v1/consult/sessions/{session_id}")
    assert before.status_code == 200
    assert {
        (item["source"], item["kind"], item["key"])
        for item in before.json()["data"]["read_model"]["unresolved"]
    } >= {("safety_confirmation", "unconfirmed_safety_fact", "allergy")}

    url = f"/api/v1/consult/sessions/{session_id}/safety-assertions/{assertion_id}/confirm"
    confirmed = await client.post(url, json={}, headers=_headers("confirm-allergy-1"))
    replay = await client.post(url, json={}, headers=_headers("confirm-allergy-1"))
    assert confirmed.status_code == replay.status_code == 200
    assert confirmed.json()["data"]["status"] == "confirmed"
    assert replay.json()["data"] == confirmed.json()["data"]
    after = await client.get(f"/api/v1/consult/sessions/{session_id}")
    assert after.status_code == 200
    assert not any(
        item["source"] == "safety_confirmation"
        for item in after.json()["data"]["read_model"]["unresolved"]
    )

    async with factory() as db:
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
        session = await db.get(ConsultSession, session_id)
        transitions = await db.scalar(
            select(func.count()).select_from(SafetyFactTransition).where(
                SafetyFactTransition.assertion_id == assertion_id
            )
        )
        assert profile is not None
        assert profile.allergy_collection_status == "collected"
        assert profile.allergens == ["青霉素"]
        assert session is not None and session.state_version == 2
        assert transitions == 1


async def test_final_confirmation_recomputes_gates_and_becomes_ready_without_patient_turn(
    client: AsyncClient,
) -> None:
    session_id, assertion_id, _ = await _create_final_allergy_gap()
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/safety-assertions/{assertion_id}/confirm",
        json={},
        headers=_headers("confirm-final-gap"),
    )
    assert response.status_code == 200

    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        runs = (
            await db.scalars(select(GraphRun).where(GraphRun.session_id == session_id))
        ).all()
        gates = (
            await db.scalars(select(GateResult).where(GateResult.session_id == session_id))
        ).all()
        steps = (
            await db.scalars(
                select(GraphRunStep).where(GraphRunStep.graph_run_id == runs[0].id)
            )
        ).all()
        event = await db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "safety_confirmation.recomputed.v1",
            )
        )
        assert session is not None and session.state_version == 2
        assert session.status == "active" and session.current_stage == "inquiry"
        assert session.state_snapshot is not None
        intake = session.state_snapshot["langgraph_intake"]
        assert intake["completeness"]["disposition"] == "ready"
        assert intake["dialogue_status"] == "complete"
        assert intake["last_question_message_id"] is None
        notice = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.agent_name == "question_composer",
            )
        )
        assert notice is not None
        assert notice.structured_delta is not None
        assert notice.structured_delta.get("kind") == "completion_notice"
        assert notice.structured_delta.get("source") == "intake_complete"
        assert len(runs) == 1 and runs[0].status == "completed"
        assert {(gate.gate_name, gate.decision) for gate in gates} == {
            ("triage", "passed"),
            ("completeness", "passed"),
        }
        assert len(steps) == 5
        assert event is not None
        assert event.payload["completeness_disposition"] == "ready"
        assert event.payload["question_message_id"] == str(notice.id)


async def test_rejection_recomputes_and_asks_the_rejected_missing_dimension(
    client: AsyncClient,
) -> None:
    session_id, assertion_id, _ = await _create_final_allergy_gap()
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/safety-assertions/{assertion_id}/reject",
        json={"reason_code": "EXTRACTION_INACCURATE"},
        headers=_headers("reject-final-gap"),
    )
    assert response.status_code == 200

    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        question = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.agent_name == "question_composer",
            )
        )
        event = await db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "safety_confirmation.recomputed.v1",
            )
        )
        assert session is not None and session.state_snapshot is not None
        assert question is not None
        assert question.structured_delta is not None
        assert question.structured_delta["selected_dimension"] == "safety.allergy_status"
        intake = session.state_snapshot["langgraph_intake"]
        assert intake["completeness"]["disposition"] == "incomplete"
        assert intake["dialogue_status"] == "questioning"
        assert intake["last_question_message_id"] == str(question.id)
        assert event is not None and event.payload["question_message_id"] == str(question.id)


async def test_confirmation_preserves_an_existing_unanswered_question(
    client: AsyncClient,
) -> None:
    session_id, assertion_id, existing_question_id = await _create_final_allergy_gap(
        omit_fact_key="chief_complaint.course",
        outstanding_dimension="chief_complaint.course",
    )
    assert existing_question_id is not None
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/safety-assertions/{assertion_id}/confirm",
        json={},
        headers=_headers("confirm-preserve-question"),
    )
    assert response.status_code == 200

    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        question_count = await db.scalar(
            select(func.count()).select_from(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.agent_name == "question_composer",
            )
        )
        event = await db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.session_id == session_id,
                OutboxEvent.event_type == "safety_confirmation.recomputed.v1",
            )
        )
        assert session is not None and session.state_snapshot is not None
        intake = session.state_snapshot["langgraph_intake"]
        assert question_count == 1
        assert intake["last_question_message_id"] == str(existing_question_id)
        assert intake["dialogue_status"] == "questioning"
        assert intake["progress"]["followup_rounds"] == 3
        # The outbox must not re-emit message.created for the preserved question.
        assert event is not None and event.payload["question_message_id"] is None


async def test_safety_decisions_require_active_inquiry() -> None:
    session_id, _, assertion_id = await _create_proposal()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        session.current_stage = "syndrome"

    with pytest.raises(InvalidStageTransitionError):
        async with factory() as db, db.begin():
            await SafetyConfirmationService(db).transition(
                session_id=session_id,
                assertion_id=assertion_id,
                action="confirm",
                actor_id="doctor-safety",
                context=WriteRequestContext(
                    trace_id=uuid.uuid4().hex,
                    idempotency_key="reject-non-inquiry",
                    is_idempotent=True,
                ),
                reason_code=None,
            )

    async with factory() as db:
        assertion = await db.get(SafetyFactAssertion, assertion_id)
        assert assertion is not None and assertion.status == "proposed"


async def test_red_flag_candidate_cannot_be_resolved_by_generic_safety_confirmation() -> None:
    factory = get_session_factory()
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    text = "突然胸痛"
    async with factory() as db, db.begin():
        session = ConsultSession(
            id=session_id,
            patient_info={},
            current_stage="inquiry",
            status="active",
            agent_runtime="langgraph",
            recovery_status="normal",
            state_version=1,
            rollback_counts={},
        )
        message = ConsultMessage(
            id=message_id,
            session_id=session_id,
            role="patient_proxy",
            stage="inquiry",
            content=text,
        )
        db.add_all((session, message))
        await db.flush()
        rows = await SafetyConfirmationService(db).propose_from_intake(
            session_id=session_id,
            source_message=message,
            output=IntakeExtractionOutput(
                decision=IntakeExtractionDecision.EXTRACTED,
                red_flag_candidates=(
                    RedFlagCandidate(
                        category=RedFlagCategory.SEVERE_PAIN,
                        source_message_id=message_id,
                        span=EvidenceSpan(
                            source_message_id=message_id,
                            start_char=0,
                            end_char=len(text),
                            quote=text,
                        ),
                        severity=CandidateSeverity.HIGH,
                        evidence="acute severe pain",
                        confidence=0.99,
                    ),
                ),
            ),
            extraction_run_id=uuid.uuid4(),
            template_version="triage-raw-text-precheck.v1",
            source_kind="deterministic_precheck",
            trace_id=uuid.uuid4().hex,
        )
        assertion_id = rows[0].assertion_id

    with pytest.raises(InvalidStageTransitionError) as exc_info:
        async with factory() as db, db.begin():
            await SafetyConfirmationService(db).transition(
                session_id=session_id,
                assertion_id=assertion_id,
                action="confirm",
                actor_id="doctor-safety",
                context=WriteRequestContext(
                    trace_id=uuid.uuid4().hex,
                    idempotency_key="confirm-red-flag-invalid",
                    is_idempotent=True,
                ),
                reason_code=None,
            )
    assert exc_info.value.detail is not None
    assert "triage/recovery" in exc_info.value.detail

    async with factory() as db:
        assertion = await db.get(SafetyFactAssertion, assertion_id)
        assert assertion is not None and assertion.status == "proposed"


async def test_reject_and_retract_remove_unconfirmed_or_authoritative_projection(client: AsyncClient) -> None:
    rejected_session, _, rejected_id = await _create_proposal(text="我对头孢过敏", allergen="头孢")
    reject = await client.post(
        f"/api/v1/consult/sessions/{rejected_session}/safety-assertions/{rejected_id}/reject",
        json={"reason_code": "PATIENT_DENIED"},
        headers=_headers("reject-allergy-1"),
    )
    assert reject.status_code == 200
    assert reject.json()["data"]["status"] == "rejected"

    retracted_session, _, retracted_id = await _create_proposal()
    base = f"/api/v1/consult/sessions/{retracted_session}/safety-assertions/{retracted_id}"
    assert (await client.post(f"{base}/confirm", json={}, headers=_headers("confirm-allergy-2"))).status_code == 200
    retract = await client.post(
        f"{base}/retract",
        json={"reason_code": "PATIENT_CORRECTION"},
        headers=_headers("retract-allergy-2"),
    )
    assert retract.status_code == 200
    assert retract.json()["data"]["status"] == "retracted"

    factory = get_session_factory()
    async with factory() as db:
        rejected_profile = await db.scalar(
            select(SafetyProfile).where(SafetyProfile.session_id == rejected_session)
        )
        retracted_profile = await db.scalar(
            select(SafetyProfile).where(SafetyProfile.session_id == retracted_session)
        )
        rejected_events = (
            await db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.session_id == rejected_session,
                    OutboxEvent.event_type == "safety_confirmation.recomputed.v1",
                )
            )
        ).all()
        retracted_events = (
            await db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.session_id == retracted_session,
                    OutboxEvent.event_type == "safety_confirmation.recomputed.v1",
                )
            )
        ).all()
        assert rejected_profile is None
        assert retracted_profile is not None
        assert retracted_profile.allergy_collection_status == "unknown"
        assert retracted_profile.allergens is None
        assert [event.payload["action"] for event in rejected_events] == ["reject"]
        assert {event.payload["action"] for event in retracted_events} == {"confirm", "retract"}


async def test_confirmed_correction_supersedes_prior_assertion(client: AsyncClient) -> None:
    session_id, _, first_id = await _create_proposal()
    _, _, second_id = await _create_proposal(
        session_id=session_id,
        text="更正：我对头孢过敏",
        allergen="头孢",
    )
    first_url = f"/api/v1/consult/sessions/{session_id}/safety-assertions/{first_id}/confirm"
    second_url = f"/api/v1/consult/sessions/{session_id}/safety-assertions/{second_id}/confirm"
    assert (await client.post(first_url, json={}, headers=_headers("confirm-correction-1"))).status_code == 200
    second = await client.post(second_url, json={}, headers=_headers("confirm-correction-2"))
    assert second.status_code == 200
    assert second.json()["data"]["supersedes_assertion_id"] == str(first_id)

    factory = get_session_factory()
    async with factory() as db:
        first = await db.get(SafetyFactAssertion, first_id)
        second_row = await db.get(SafetyFactAssertion, second_id)
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
        assert first is not None and first.status == "superseded"
        assert second_row is not None and second_row.status == "confirmed"
        assert profile is not None and profile.allergens == ["头孢"]


async def test_evidence_tampering_fails_closed() -> None:
    session_id, message_id, assertion_id = await _create_proposal()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        source = await db.get(ConsultMessage, message_id)
        assert source is not None
        source.content = "我对维生素过敏"

    with pytest.raises(ValidationError) as exc_info:
        async with factory() as db, db.begin():
            await SafetyConfirmationService(db).transition(
                session_id=session_id,
                assertion_id=assertion_id,
                action="confirm",
                actor_id="doctor-safety",
                context=WriteRequestContext(
                    trace_id=uuid.uuid4().hex,
                    idempotency_key="tampered-evidence",
                    is_idempotent=True,
                ),
                reason_code=None,
            )
    assert exc_info.value.detail is not None and "tampered" in exc_info.value.detail

    async with factory() as db:
        assertion = await db.get(SafetyFactAssertion, assertion_id)
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
        assert assertion is not None and assertion.status == "proposed"
        assert profile is None


async def test_concurrent_duplicate_confirmation_serializes_without_double_projection() -> None:
    session_id, _, assertion_id = await _create_proposal()
    factory = get_session_factory()

    async def confirm(key: str) -> str:
        async with factory() as db, db.begin():
            row = await SafetyConfirmationService(db).transition(
                session_id=session_id,
                assertion_id=assertion_id,
                action="confirm",
                actor_id="doctor-safety",
                context=WriteRequestContext(
                    trace_id=uuid.uuid4().hex,
                    idempotency_key=key,
                    is_idempotent=True,
                ),
                reason_code=None,
            )
            return row.status.value

    assert await asyncio.gather(confirm("concurrent-a"), confirm("concurrent-b")) == [
        "confirmed",
        "confirmed",
    ]
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
        transition_count = await db.scalar(
            select(func.count()).select_from(SafetyFactTransition).where(
                SafetyFactTransition.assertion_id == assertion_id
            )
        )
        decision_audit_count = await db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.session_id == session_id,
                AuditEvent.event_type == "safety_fact.confirm",
            )
        )
        assert session is not None and session.state_version == 2
        assert profile is not None and profile.allergens == ["青霉素"]
        assert transition_count == 1
        assert decision_audit_count == 1


async def test_explicit_structured_form_is_confirmed_with_privacy_minimal_audit(
    client: AsyncClient,
    enable_public_langgraph: None,
) -> None:
    response = await client.post(
        "/api/v1/consult/sessions",
        json={
            "agent_runtime": "langgraph",
            "patient_info": {"allergies": ["青霉素"], "current_medications": []},
        },
        headers={"X-Doctor-Id": "doctor-safety"},
    )
    assert response.status_code == 201
    session_id = uuid.UUID(response.json()["data"]["session_id"])

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.scalars(
                select(SafetyFactAssertion)
                .where(SafetyFactAssertion.session_id == session_id)
                .order_by(SafetyFactAssertion.field_name)
            )
        ).all()
        audit_rows = (
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.session_id == session_id,
                    AuditEvent.event_type == "safety_fact.confirmed_from_form",
                )
            )
        ).all()
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_id))
        assert [(row.field_name, row.status) for row in rows] == [
            ("allergy", "confirmed"),
            ("medications", "confirmed"),
        ]
        assert profile is not None and profile.allergens == ["青霉素"]
        assert profile.medications_collection_status == "explicitly_none"
        assert len(audit_rows) == 2
        assert "青霉素" not in json.dumps(
            [row.payload for row in audit_rows], ensure_ascii=False, sort_keys=True
        )
