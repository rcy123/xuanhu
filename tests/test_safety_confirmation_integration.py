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
from app.core.exceptions import ValidationError
from app.db.session import get_session_factory
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import SafetyFactAssertion, SafetyFactTransition, SafetyProfile
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    PatientSafetyDelta,
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
        assert rejected_profile is None
        assert retracted_profile is not None
        assert retracted_profile.allergy_collection_status == "unknown"
        assert retracted_profile.allergens is None


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
        assert session is not None and session.state_version == 2
        assert profile is not None and profile.allergens == ["青霉素"]
        assert transition_count == 2


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
