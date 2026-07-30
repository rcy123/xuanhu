from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select, text
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.langgraph_intake as langgraph_intake_module
from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.state import XuanhuGraphState, default_state, validate_state_json_safe
from app.api.advance import _run_langgraph_advance as _production_run_langgraph_advance
from app.core.exceptions import InsufficientInquiryError, ModelGatewayUnavailableError
from app.core.exceptions import ValidationError as XuanhuValidationError
from app.db.session import _build_async_pg_url
from app.main import app
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    DomainCommandCommit,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    OutboxEvent,
    SafetyFactAssertion,
)
from app.models.http_command import HttpCommandClaim
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    CandidateSeverity,
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionOutput,
    LactationDelta,
    ObservationDelta,
    PatientSafetyDelta,
    PregnancyDelta,
    RedFlagCandidate,
    RedFlagCategory,
    SafetyListDelta,
)
from app.schemas.message import MessageCreateRequest, MessageCreateResponse
from app.schemas.question import QuestionComposerModelOutput
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION
from app.services.langgraph_intake import (
    LangGraphIntakeMessageRunner as _ProductionLangGraphIntakeMessageRunner,
)
from app.services.langgraph_intake import _payload_digest
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


class LangGraphIntakeMessageRunner(_ProductionLangGraphIntakeMessageRunner):
    """Test subclass explicitly opting direct calls into request-local runtime."""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db, allow_request_local_runtime=True)


async def _run_langgraph_advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicitly opt direct integration calls into request-local runtime."""

    kwargs["allow_request_local_runtime"] = True
    return await _production_run_langgraph_advance(*args, **kwargs)


def _state() -> XuanhuGraphState:
    session_id = str(uuid.uuid4())
    return default_state(
        session_id=session_id,
        command=XuanhuCommand.MESSAGE.value,
        command_id="cmd-intake-test",
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(uuid.uuid4()),
    )


class _E2EFakeGateway:
    def __init__(self, mode: str, *, delay: float = 0) -> None:
        self.mode = mode
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"agent_name": kwargs.get("agent_name"), "output_schema": output_schema})
        if self.delay:
            await asyncio.sleep(self.delay)
        if output_schema is IntakeExtractionOutput:
            source_id, content = _source_message(messages)
            return _intake_output(self.mode, source_id, content)
        if output_schema is QuestionComposerModelOutput:
            return QuestionComposerModelOutput(question="请结合患者目前情况补充这一项信息？")
        raise AssertionError(f"unexpected output schema: {output_schema}")

    @property
    def intake_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is IntakeExtractionOutput)

    @property
    def question_model_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is QuestionComposerModelOutput)


class _UnavailableOnceGateway(_E2EFakeGateway):
    def __init__(self) -> None:
        super().__init__("incomplete")
        self._failed = False

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> Any:
        if output_schema is IntakeExtractionOutput and not self._failed:
            self._failed = True
            self.calls.append({"agent_name": kwargs.get("agent_name"), "output_schema": output_schema})
            raise ModelGatewayUnavailableError()
        return await super().chat_structured(messages, output_schema, **kwargs)


def _source_message(messages: list[dict[str, Any]]) -> tuple[uuid.UUID, str]:
    raw = messages[-1]["content"]
    payload = json.loads(raw)
    return uuid.UUID(payload[0]["message_id"]), str(payload[0]["content"])


def _evidence_span(source: uuid.UUID, text: str, quote: str) -> EvidenceSpan:
    start = text.index(quote)
    return EvidenceSpan(
        source_message_id=source,
        start_char=start,
        end_char=start + len(quote),
        quote=quote,
    )


def _observation(source: uuid.UUID, fact_key: str, value: str) -> ObservationDelta:
    return ObservationDelta(
        fact_key=fact_key,
        value=value,
        normalized_value=value,
        source_message_id=source,
        confidence=0.95,
    )


def _safety_none(source: uuid.UUID, text: str) -> PatientSafetyDelta:
    return PatientSafetyDelta(
        allergy=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no drug allergies"),
        ),
        pregnancy=PregnancyDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            span=_evidence_span(source, text, "not pregnant"),
        ),
        lactation=LactationDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            span=_evidence_span(source, text, "not breastfeeding"),
        ),
        medications=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no current medications"),
        ),
        major_conditions=SafetyListDelta(
            status=CollectionStatus.EXPLICITLY_NONE,
            source_message_id=source,
            negation_span=_evidence_span(source, text, "no major conditions"),
        ),
    )


def _intake_output(mode: str, source: uuid.UUID, text: str) -> IntakeExtractionOutput:
    if mode == "ready":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(
                _observation(source, "chief_complaint.symptom", "headache"),
                _observation(source, "chief_complaint.course", "three_days"),
                _observation(source, "present_illness.change", "stable"),
                _observation(source, "ten_questions.cold_heat", "none"),
                _observation(source, "ten_questions.diet", "normal"),
                _observation(source, "ten_questions.sleep", "normal"),
                _observation(source, "ten_questions.stool_urine", "normal"),
            ),
            patient_safety_delta=_safety_none(source, text),
        )
    if mode == "red_flag":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            red_flag_candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.HIGH_FEVER,
                    source_message_id=source,
                    span=_evidence_span(source, text, "39.2°C"),
                    severity=CandidateSeverity.HIGH,
                    evidence="high fever",
                    confidence=0.96,
                ),
            ),
        )
    if mode == "privacy":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(_observation(source, "chief_complaint.symptom", "privacy_fact_value_778899"),),
        )
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        observations=(_observation(source, "chief_complaint.symptom", "headache"),),
    )


def _install_fake_runtime(monkeypatch: pytest.MonkeyPatch, gateway: _E2EFakeGateway) -> None:
    monkeypatch.setattr(
        langgraph_intake_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway, recorder=None),
    )


def _install_fake_advance_graph(
    monkeypatch: pytest.MonkeyPatch,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_invoke_reasoning_graph(
        *,
        session_id: str,
        command_key: str,
        run_id: uuid.UUID,
        command: XuanhuCommand,
    ) -> None:
        assert command is XuanhuCommand.ADVANCE
        sid = uuid.UUID(session_id)
        async with db_factory() as db, db.begin():
            claim = await db.scalar(
                select(IntakeCommandClaim)
                .where(
                    IntakeCommandClaim.session_id == sid,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
                .with_for_update()
            )
            session = await db.get(ConsultSession, sid, with_for_update=True)
            graph_run = await db.get(GraphRun, run_id, with_for_update=True)
            assert claim is not None and session is not None and graph_run is not None
            advance = claim.intermediate_payload.get("advance", {}) if claim.intermediate_payload else {}
            response_payload = {
                "session_id": session_id,
                "current_stage": session.current_stage,
                "from_stage": advance.get("from_stage", "inquiry"),
                "state_version": session.state_version,
                "blocked_reason": session.blocked_reason,
                "agent_name": None,
                "trace_id": advance.get("trace_id"),
            }
            claim.status = "completed"
            claim.output_state_version = session.state_version
            claim.response_payload = response_payload
            claim.updated_at = func.now()
            graph_run.status = "completed"
            graph_run.completed_at = func.now()
            existing_completed = await db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.session_id == sid, OutboxEvent.event_type == "advance.command_completed.v1")
            )
            if existing_completed == 0:
                db.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        event_type="advance.command_completed.v1",
                        session_id=sid,
                        graph_run_id=run_id,
                        state_version=session.state_version,
                        trace_id="trace:advance-test",
                        payload=response_payload,
                    )
                )

    monkeypatch.setattr("app.api.advance._invoke_reasoning_graph", fake_invoke_reasoning_graph)


@pytest.fixture(scope="module")
def migrated_database() -> str:
    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        try:
            command.downgrade(config, "20260711_0004")
            command.upgrade(config, "20260712_0006")
            command.downgrade(config, "20260711_0004")
            command.upgrade(config, "20260712_0006")
            command.upgrade(config, "head")
            yield db_url
        finally:
            command.upgrade(config, "head")


@pytest.fixture
async def db_factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from app.db.session import reset_session_factory

    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=3, max_overflow=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE http_command_claims, intake_command_claims, domain_command_commits, "
                "outbox_events, gate_results, "
                "artifact_revisions, graph_run_steps, graph_runs, safety_profiles, observations, "
                "consult_messages, consult_sessions CASCADE"
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()
        await reset_session_factory()


@pytest.mark.asyncio
async def test_langgraph_messages_e2e_incomplete_uses_model_question_and_one_intake_call(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="headache"),
            doctor_id="doctor-a",
            trace_id="messages-e2e-incomplete",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        outbox_count = await db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'intake.command_completed.v1'")
        )

    assert response.agent_message is not None
    assert response.current_stage == "inquiry"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 1
    assert outbox_count == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.intermediate_payload is not None
    assert set(claim.intermediate_payload["steps"]) >= {
        "persist_message",
        "triage_precheck",
        "build_intake_context",
        "extract_intake",
        "verify_intake",
        "reduce_observations",
        "gates_and_route",
    }
    assert claim.intermediate_payload["gates"]["route"] == "incomplete"


@pytest.mark.asyncio
async def test_bound_bare_negative_creates_pending_safety_fact_without_repeating_allergy(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 1},
                    }
                },
            )
        )
        db.add(
            ConsultMessage(
                id=question_id,
                session_id=session_id,
                role="agent",
                stage="inquiry",
                agent_name="question_composer",
                content="为补充用药安全信息，请问您目前是否有已知过敏？",
                structured_delta={
                    "selected_dimension": "safety.allergy_status",
                    "selection_kind": "required",
                },
                trace_id="bound-negative-question",
            )
        )

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content="没有",
                reply_to_message_id=question_id,
            ),
            doctor_id="doctor-a",
            trace_id="bound-negative-answer",
            x_state_version=1,
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assertion = await db.scalar(
            select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
        )
        patient_message = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
        latest_question = await db.scalar(
            select(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.agent_name == "question_composer",
                ConsultMessage.id != question_id,
            )
            .order_by(ConsultMessage.created_at.desc())
        )

    assert gateway.intake_calls == 0
    assert session is not None
    assert session.status == "active"
    assert session.state_snapshot["langgraph_intake"]["progress"]["no_new_facts_rounds"] == 0
    assert assertion is not None
    assert assertion.field_name == "allergy"
    assert assertion.status == "proposed"
    assert assertion.source_kind == "deterministic_reply_binding"
    assert patient_message is not None
    assert patient_message.structured_delta["reply_context"] == {
        "question_message_id": str(question_id),
        "selected_dimension": "safety.allergy_status",
        "selection_kind": "required",
    }
    assert response.agent_message is not None
    assert latest_question is not None
    assert latest_question.structured_delta["selected_dimension"] != "safety.allergy_status"


@pytest.mark.asyncio
async def test_explicit_stale_reply_question_is_rejected_before_patient_message_or_claim(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    stale_question_id = uuid.uuid4()
    current_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(current_question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 2},
                    }
                },
            )
        )
        db.add_all(
            [
                ConsultMessage(
                    id=stale_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="stale-reply-question",
                ),
                ConsultMessage(
                    id=current_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Are you currently taking any medications?",
                    structured_delta={
                        "selected_dimension": "safety.medication_status",
                        "selection_kind": "required",
                    },
                    trace_id="current-reply-question",
                ),
            ]
        )

    async with db_factory() as db:
        with pytest.raises(XuanhuValidationError) as captured:
            await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="no",
                    reply_to_message_id=stale_question_id,
                ),
                doctor_id="doctor-a",
                trace_id="stale-reply-answer",
                x_state_version=1,
                idempotency_key="stale-reply-answer",
            )
        assert "current intake question" in (captured.value.detail or "")

    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        session = await db.get(ConsultSession, session_id)

    assert gateway.intake_calls == 0
    assert patient_message_count == 0
    assert claim_count == 0
    assert session is not None
    assert session.state_version == 1


@pytest.mark.asyncio
async def test_explicit_cross_session_reply_question_is_rejected_before_patient_message_or_claim(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    foreign_session_id = uuid.uuid4()
    foreign_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add_all(
            [
                ConsultSession(
                    id=session_id,
                    patient_info={},
                    state_version=1,
                    agent_runtime="langgraph",
                    # Exercise the ownership check even if a corrupt/stale
                    # snapshot points at another session's otherwise valid question.
                    state_snapshot={
                        "langgraph_intake": {
                            "last_question_message_id": str(foreign_question_id),
                            "progress": {"no_new_facts_rounds": 0, "followup_rounds": 1},
                        }
                    },
                ),
                ConsultSession(
                    id=foreign_session_id,
                    patient_info={},
                    state_version=1,
                    agent_runtime="langgraph",
                ),
                ConsultMessage(
                    id=foreign_question_id,
                    session_id=foreign_session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="foreign-reply-question",
                ),
            ]
        )

    async with db_factory() as db:
        with pytest.raises(XuanhuValidationError) as captured:
            await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                MessageCreateRequest(
                    role="patient_proxy",
                    content="no",
                    reply_to_message_id=foreign_question_id,
                ),
                doctor_id="doctor-a",
                trace_id="cross-session-reply-answer",
                x_state_version=1,
                idempotency_key="cross-session-reply-answer",
            )
        assert "not a valid intake question" in (captured.value.detail or "")

    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        session = await db.get(ConsultSession, session_id)

    assert gateway.intake_calls == 0
    assert patient_message_count == 0
    assert claim_count == 0
    assert session is not None
    assert session.state_version == 1


@pytest.mark.asyncio
async def test_malformed_reply_question_uuid_is_rejected_by_schema_and_api_without_writes(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    malformed_body = {
        "role": "patient_proxy",
        "content": "no",
        "reply_to_message_id": "definitely-not-a-uuid",
    }
    with pytest.raises(PydanticValidationError):
        MessageCreateRequest.model_validate(malformed_body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=malformed_body,
            headers={
                "X-Doctor-Id": "doctor-a",
                "X-State-Version": "1",
                "X-Request-Id": "malformed-reply-question",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    async with db_factory() as db:
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(ConsultMessage.session_id == session_id, ConsultMessage.role == "patient_proxy")
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
    assert patient_message_count == 0
    assert claim_count == 0


@pytest.mark.asyncio
async def test_legacy_client_without_reply_id_binds_only_current_canonical_question(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    stale_question_id = uuid.uuid4()
    current_question_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=1,
                agent_runtime="langgraph",
                state_snapshot={
                    "langgraph_intake": {
                        "last_question_message_id": str(current_question_id),
                        "progress": {"no_new_facts_rounds": 0, "followup_rounds": 2},
                    }
                },
            )
        )
        db.add_all(
            [
                ConsultMessage(
                    id=stale_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Are you currently taking any medications?",
                    structured_delta={
                        "selected_dimension": "safety.medication_status",
                        "selection_kind": "required",
                    },
                    trace_id="legacy-stale-question",
                ),
                ConsultMessage(
                    id=current_question_id,
                    session_id=session_id,
                    role="agent",
                    stage="inquiry",
                    agent_name="question_composer",
                    content="Do you have any known allergies?",
                    structured_delta={
                        "selected_dimension": "safety.allergy_status",
                        "selection_kind": "required",
                    },
                    trace_id="legacy-current-question",
                ),
            ]
        )

    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            # Deliberately omit reply_to_message_id to emulate a legacy client.
            MessageCreateRequest(role="patient_proxy", content="no"),
            doctor_id="doctor-a",
            trace_id="legacy-bound-answer",
            x_state_version=1,
            idempotency_key="legacy-bound-answer",
        )

    async with db_factory() as db:
        patient_message = await db.scalar(
            select(ConsultMessage).where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
        assertions = (
            await db.scalars(
                select(SafetyFactAssertion).where(SafetyFactAssertion.session_id == session_id)
            )
        ).all()
        claim = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )

    assert gateway.intake_calls == 0
    assert patient_message is not None
    assert patient_message.structured_delta is not None
    assert patient_message.structured_delta["reply_context"] == {
        "question_message_id": str(current_question_id),
        "selected_dimension": "safety.allergy_status",
        "selection_kind": "required",
    }
    assert len(assertions) == 1
    assert assertions[0].field_name == "allergy"
    assert assertions[0].source_kind == "deterministic_reply_binding"
    assert claim is not None
    assert claim.patient_message_id == patient_message.id


@pytest.mark.asyncio
async def test_langgraph_messages_e2e_raw_text_red_flag_blocks_when_model_would_miss(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="突然胸痛并且呼吸困难"),
            doctor_id=None,
            trace_id="messages-e2e-deterministic-red-flag",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        session = await db.get(ConsultSession, session_id)

    assert response.agent_message is None
    assert response.current_stage == "blocked"
    assert session is not None
    assert session.recovery_status == "manual_required"
    assert gateway.intake_calls == 0
    assert gateway.question_model_calls == 0
    assert claim is not None
    assert claim.intermediate_payload is not None
    assert claim.intermediate_payload["triage_precheck"]["candidate_count"] == 2
    assert claim.intermediate_payload["triage_precheck"]["policy_version"] == "triage-raw-text-precheck.v1"
    assert claim.intermediate_payload["gates"]["route"] == "manual"
    assert claim.intermediate_payload["gates"]["completeness_disposition"] == "triage_blocked"


@pytest.mark.asyncio
async def test_langgraph_messages_e2e_model_red_flag_supplements_clear_precheck(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("red_flag")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="My temperature is 39.2°C"),
            doctor_id=None,
            trace_id="messages-e2e-model-red-flag",
            x_state_version=1,
        )

    assert response.agent_message is None
    assert response.current_stage == "blocked"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 0


@pytest.mark.parametrize(
    "content,expected_category",
    [
        ("突然胸痛", "severe_pain"),
        ("现在喘不过气", "breathing_difficulty"),
        ("患者意识不清，叫不醒", "altered_consciousness"),
        ("伤口大出血且血流不止", "severe_bleeding"),
        ("突然口角歪斜并且言语不清", "neurologic_deficit"),
        ("体温40.2℃", "high_fever"),
    ],
)
@pytest.mark.asyncio
async def test_messages_api_each_deterministic_red_flag_blocks_with_empty_model_candidates(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_category: str,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json={"role": "patient_proxy", "content": content},
            headers={
                "X-Doctor-Id": "doctor-precheck",
                "X-State-Version": "1",
                "X-Request-Id": f"precheck-api-{expected_category}",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["current_stage"] == "blocked"
    assert response.json()["data"].get("agent_message") is None
    assert gateway.intake_calls == 0
    assert gateway.question_model_calls == 0

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        gate = await db.scalar(
            select(GateResult)
            .where(GateResult.session_id == session_id, GateResult.gate_name == TRIAGE_GATE_NAME)
            .order_by(GateResult.created_at.desc())
        )
    assert session is not None
    assert session.current_stage == "blocked"
    assert session.recovery_status == "manual_required"
    assert gate is not None
    assert gate.policy_version == TRIAGE_POLICY_VERSION
    assert gate.decision == "blocked"
    assert expected_category in (gate.details or {}).get("category_counts", {})


@pytest.mark.asyncio
async def test_langgraph_messages_same_command_concurrent_replays_single_intake_call(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete", delay=0.05)
    _install_fake_runtime(monkeypatch, gateway)
    body = MessageCreateRequest(role="patient_proxy", content="headache")
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async def submit_once(trace_id: str) -> MessageCreateResponse:
        async with db_factory() as db:
            return await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                body,
                doctor_id=None,
                trace_id=trace_id,
                x_state_version=1,
                idempotency_key="messages-public-concurrent",
            )

    first, second = await asyncio.gather(
        submit_once("messages-e2e-concurrent-a"),
        submit_once("messages-e2e-concurrent-b"),
    )

    async with db_factory() as db:
        claim_count = await db.scalar(select(func.count()).select_from(IntakeCommandClaim))
        message_count = await db.scalar(
            text("SELECT count(*) FROM consult_messages WHERE session_id = :sid"), {"sid": session_id}
        )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert gateway.intake_calls == 1
    assert claim_count == 1
    assert message_count == 2


@pytest.mark.asyncio
async def test_langgraph_messages_recovers_when_claim_completion_is_interrupted_after_domain_commit(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("ready")
    _install_fake_runtime(monkeypatch, gateway)

    async def interrupted_complete_claim(
        self: LangGraphIntakeMessageRunner,
        claim_id: uuid.UUID,
        response: MessageCreateResponse,
        question_message_id: uuid.UUID | None,
        output_state_version: int,
    ) -> None:
        del self, claim_id, response, question_message_id, output_state_version
        return None

    monkeypatch.setattr(LangGraphIntakeMessageRunner, "_complete_claim", interrupted_complete_claim)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        response = await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(
                role="patient_proxy",
                content=(
                    "headache three days stable; no drug allergies; not pregnant; "
                    "not breastfeeding; no current medications; no major conditions"
                ),
            ),
            doctor_id=None,
            trace_id="messages-e2e-recover-after-commit",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        commit = await db.scalar(select(DomainCommandCommit).where(DomainCommandCommit.session_id == session_id))
        session = await db.get(ConsultSession, session_id)
        agent_message_count = await db.scalar(
            text("SELECT count(*) FROM consult_messages WHERE session_id = :sid AND role = 'agent'"),
            {"sid": session_id},
        )

    # All remaining gaps are backed by proposed safety assertions. The runtime
    # must wait for doctor confirmation rather than repeating those questions,
    # and recovery must reconstruct that exact no-question response.
    assert response.agent_message is None
    assert response.current_stage == "inquiry"
    assert response.state_version == 3
    assert gateway.intake_calls == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.response_payload is not None
    assert claim.response_payload == response.model_dump(mode="json")
    assert claim.question_message_id is None
    assert agent_message_count == 0
    assert session is not None
    assert session.state_snapshot["langgraph_intake"]["dialogue_status"] == "awaiting_safety_confirmation"
    assert commit is not None


@pytest.mark.asyncio
async def test_messages_api_repairs_ambiguous_http_claim_from_completed_intake(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("incomplete")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    idempotency_key = f"message-http-repair-{uuid.uuid4()}"
    request_body = {"role": "patient_proxy", "content": "headache"}
    headers = {
        "X-Doctor-Id": "doctor-http-repair",
        "X-Idempotency-Key": idempotency_key,
        "X-State-Version": "1",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )
        assert first.status_code == 200, first.text

        async with db_factory() as db, db.begin():
            http_claim = await db.scalar(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.message.create.v1",
                    HttpCommandClaim.scope_key == f"session:{session_id}",
                )
            )
            assert http_claim is not None
            http_claim.status = "ambiguous"
            http_claim.http_status = None
            http_claim.response_payload = cast(Any, sql_null())
            http_claim.error_payload = cast(Any, sql_null())
            http_claim.lease_expires_at = None
            http_claim.completed_at = None

        replay = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == first.json()["data"]
    assert gateway.intake_calls == 1
    async with db_factory() as db:
        repaired = await db.scalar(
            select(HttpCommandClaim).where(
                HttpCommandClaim.operation == "session.message.create.v1",
                HttpCommandClaim.scope_key == f"session:{session_id}",
            )
        )
        intake_claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )
        patient_message_count = await db.scalar(
            select(func.count())
            .select_from(ConsultMessage)
            .where(
                ConsultMessage.session_id == session_id,
                ConsultMessage.role == "patient_proxy",
            )
        )
    assert repaired is not None and repaired.status == "completed"
    assert intake_claim_count == 1
    assert patient_message_count == 1


@pytest.mark.asyncio
async def test_messages_api_same_public_command_resumes_gateway_failed_intake_without_new_message(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _UnavailableOnceGateway()
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    idempotency_key = f"message-gateway-resume-{uuid.uuid4()}"
    request_body = {"role": "patient_proxy", "content": "headache"}
    headers = {
        "X-Doctor-Id": "doctor-gateway-resume",
        "X-Idempotency-Key": idempotency_key,
        "X-State-Version": "1",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )
        assert first.status_code == 503, first.text
        assert first.json()["code"] == "AGENT_TRIGGER_FAILED"
        assert first.json()["agent_error_code"] == "MODEL_GATEWAY_UNAVAILABLE"
        assert first.json()["retryable"] is True

        async with db_factory() as db:
            failed_intake = await db.scalar(
                select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
            )
            failed_http = await db.scalar(
                select(HttpCommandClaim).where(
                    HttpCommandClaim.operation == "session.message.create.v1",
                    HttpCommandClaim.scope_key == f"session:{session_id}",
                )
            )
            assert failed_intake is not None
            failed_run = await db.get(GraphRun, failed_intake.run_id)
            message_count_after_failure = await db.scalar(
                select(func.count())
                .select_from(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role == "patient_proxy",
                )
            )
        original_claim_id = failed_intake.id
        original_message_id = failed_intake.patient_message_id
        original_run_id = failed_intake.run_id
        assert failed_intake.status == "failed"
        assert failed_intake.error_code == "MODEL_GATEWAY_UNAVAILABLE"
        assert failed_http is not None and failed_http.status == "failed"
        assert failed_run is not None and failed_run.status == "failed"
        assert failed_run.completed_at is not None
        assert message_count_after_failure == 1

        # Simulate legacy failure records written before GraphRun failure was
        # persisted atomically with the internal intake claim.
        async with db_factory() as db, db.begin():
            legacy_run = await db.get(GraphRun, original_run_id, with_for_update=True)
            assert legacy_run is not None
            legacy_run.status = "running"
            legacy_run.completed_at = None

        retry = await client.post(
            f"/api/v1/consult/sessions/{session_id}/messages",
            json=request_body,
            headers=headers,
        )

    assert retry.status_code == 200, retry.text
    assert gateway.intake_calls == 2
    async with db_factory() as db:
        completed_intake = await db.scalar(
            select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id)
        )
        completed_http = await db.scalar(
            select(HttpCommandClaim).where(
                HttpCommandClaim.operation == "session.message.create.v1",
                HttpCommandClaim.scope_key == f"session:{session_id}",
            )
        )
        completed_run = await db.get(GraphRun, original_run_id)
        patient_messages = (
            await db.scalars(
                select(ConsultMessage).where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role == "patient_proxy",
                )
            )
        ).all()
    assert completed_intake is not None
    assert completed_intake.id == original_claim_id
    assert completed_intake.patient_message_id == original_message_id
    assert completed_intake.run_id == original_run_id
    assert completed_intake.status == "completed"
    assert completed_http is not None and completed_http.status == "completed"
    assert completed_run is not None and completed_run.status == "completed"
    assert [message.id for message in patient_messages] == [original_message_id]


@pytest.mark.asyncio
async def test_langgraph_messages_recovery_metadata_does_not_persist_clinical_payloads(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    gateway = _E2EFakeGateway("privacy")
    _install_fake_runtime(monkeypatch, gateway)
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        await LangGraphIntakeMessageRunner(db).submit_message(
            str(session_id),
            MessageCreateRequest(role="patient_proxy", content="privacy_patient_text_778899"),
            doctor_id=None,
            trace_id="messages-e2e-privacy",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim_payload = await db.scalar(
            text("SELECT coalesce(string_agg(intermediate_payload::text, ' '), '') FROM intake_command_claims")
        )
        outbox_payload = await db.scalar(text("SELECT coalesce(string_agg(payload::text, ' '), '') FROM outbox_events"))
        checkpoint_payload = await db.scalar(
            text(
                """
                SELECT concat_ws(
                    ' ',
                    (SELECT coalesce(string_agg(to_jsonb(c)::text, ' '), '') FROM checkpoints c WHERE thread_id LIKE :needle),
                    (SELECT coalesce(string_agg(to_jsonb(w)::text, ' '), '') FROM checkpoint_writes w WHERE thread_id LIKE :needle),
                    (SELECT coalesce(string_agg(to_jsonb(b)::text, ' '), '') FROM checkpoint_blobs b WHERE thread_id LIKE :needle)
                )
                """
            ),
            {"needle": f"%{session_id}%"},
        )

    combined = f"{claim_payload} {outbox_payload} {checkpoint_payload}"
    assert "extraction_output" not in combined
    assert "privacy_patient_text_778899" not in combined
    assert "privacy_fact_value_778899" not in combined
    assert "chief_complaint.symptom" not in combined
    assert '"observations"' not in str(claim_payload)
    assert '"patient_safety_delta"' not in str(claim_payload)


@pytest.mark.asyncio
async def test_message_route_invokes_injected_intake_executor_without_patient_payload() -> None:
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    calls: list[dict[str, Any]] = []

    async def executor(input_state: XuanhuGraphState) -> dict[str, Any]:
        calls.append(dict(input_state))
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "domain_state_version": 3,
            "gate_results": [
                {
                    "gate_name": "triage",
                    "decision": "passed",
                    "policy_version": "triage-red-flag.v1",
                }
            ],
            "artifact_refs": [
                {
                    "kind": "message",
                    "artifact_id": str(uuid.uuid4()),
                    "revision": 1,
                }
            ],
        }

    graph = build_main_graph(checkpointer=InMemorySaver(), intake_executor=executor)
    runner = GraphRunner(graph)
    result = await runner.ainvoke(dict(state), config=config)

    assert calls
    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["domain_state_version"] == 3
    serialized = repr(result) + repr(calls)
    assert "头痛" not in serialized
    assert "patient" not in result
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_message_route_invokes_injected_intake_executor_without_contextvar() -> None:
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    calls: list[dict[str, Any]] = []

    async def executor(input_state: XuanhuGraphState) -> dict[str, Any]:
        calls.append(dict(input_state))
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "domain_state_version": 5}

    graph = build_main_graph(checkpointer=InMemorySaver(), intake_executor=executor)
    runner = GraphRunner(graph)
    result = await runner.ainvoke(dict(state), config=config)

    assert calls
    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["domain_state_version"] == 5
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_default_intake_subgraph_returns_sanitized_missing_claim_error(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    del db_factory
    state = _state()
    config = make_run_config(state["session_id"], graph_version=DEFAULT_GRAPH_VERSION)
    graph = build_main_graph(checkpointer=InMemorySaver())
    runner = GraphRunner(graph)

    result = await runner.ainvoke(dict(state), config=config)

    assert result["route"] == NODE_INTAKE_SUBGRAPH_V1
    assert result["last_error"] == {
        "code": "INTAKE_COMMAND_NOT_FOUND",
        "trace_id": state["run_id"],
        "detail": "intake command claim was not found",
    }
    validate_state_json_safe(result)


@pytest.mark.asyncio
async def test_intake_command_claim_replay_returns_stable_response_without_new_message(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    body = MessageCreateRequest(role="patient_proxy", content="头痛三天")
    trace_id = "claim-replay"
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        claim = await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            body,
            command_key="command:claim-replay",
            payload_digest=_payload_digest(body),
            doctor_id="doctor-a",
            trace_id=trace_id,
            x_state_version=1,
        )
        assert claim.message is not None
        response = MessageCreateResponse(
            message_id=str(claim.message.id),
            session_id=str(session_id),
            role="patient_proxy",
            stage="inquiry",
            content="头痛三天",
            current_stage="inquiry",
            state_version=2,
            created_at=claim.message.created_at,
            sufficiency_report=None,
        )
        await runner._complete_claim(claim.claim.id, response, None, 2)  # noqa: SLF001

    async with db_factory() as db:
        first_count = await db.scalar(select(func.count()).select_from(IntakeCommandClaim))
        message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))
        runner = LangGraphIntakeMessageRunner(db)
        replay = await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            body,
            command_key="command:claim-replay",
            payload_digest=_payload_digest(body),
            doctor_id="doctor-a",
            trace_id=trace_id,
            x_state_version=1,
        )
        second_message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))

    assert first_count == 1
    assert replay.replay_response is not None
    assert replay.replay_response.model_dump(mode="json") == response.model_dump(mode="json")
    assert second_message_count == message_count


@pytest.mark.asyncio
async def test_intake_command_claim_rejects_same_key_different_payload(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    first_body = MessageCreateRequest(role="patient_proxy", content="头痛三天")
    second_body = MessageCreateRequest(role="patient_proxy", content="胃痛一天")
    async with db_factory() as db, db.begin():
        db.add(ConsultSession(id=session_id, patient_info={}, state_version=1, agent_runtime="langgraph"))

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        await runner._claim_or_replay(  # noqa: SLF001
            session_id,
            first_body,
            command_key="command:same-key",
            payload_digest=_payload_digest(first_body),
            doctor_id=None,
            trace_id="same-key",
            x_state_version=1,
        )

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        with pytest.raises(Exception) as captured:
            await runner._claim_or_replay(  # noqa: SLF001
                session_id,
                second_body,
                command_key="command:same-key",
                payload_digest=_payload_digest(second_body),
                doctor_id=None,
                trace_id="same-key",
                x_state_version=1,
            )
        message_count = await db.scalar(text("SELECT count(*) FROM consult_messages"))

    assert type(captured.value).__name__ == "IdempotencyConflictError"
    assert message_count == 1


@pytest.mark.asyncio
async def test_intake_running_claim_recovers_from_domain_commit(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    command_key = "command:recover-claim"
    body = MessageCreateRequest(role="patient_proxy", content="å¤´ç—›ä¸‰å¤©")
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=3,
                current_stage="inquiry",
                agent_runtime="langgraph",
                state_snapshot={
                    "sufficiency_report": {
                        "sufficient": True,
                        "covered": [],
                        "missing": [],
                        "suggestions": [],
                    }
                },
            )
        )
        db.add(
            ConsultMessage(
                id=message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content=body.content,
                trace_id="recover-claim",
            )
        )
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                command_id=command_key,
                input_state_version=2,
                status="completed",
            )
        )
        db.add(
            OutboxEvent(
                id=outbox_id,
                event_type="intake.command_completed.v1",
                session_id=session_id,
                graph_run_id=run_id,
                state_version=3,
                trace_id="trace:recover-claim",
                payload={"session_id": str(session_id), "command_id": command_key},
            )
        )
        await db.flush()
        db.add(
            DomainCommandCommit(
                id=uuid.uuid4(),
                session_id=session_id,
                idempotency_key=command_key,
                input_state_version=2,
                agent_spec_version="intake-domain-delta.v1",
                delta_digest="0" * 64,
                output_state_version=3,
                changed=True,
                graph_run_id=run_id,
                outbox_event_id=outbox_id,
            )
        )
        db.add(
            IntakeCommandClaim(
                id=claim_id,
                session_id=session_id,
                idempotency_key=command_key,
                payload_digest=_payload_digest(body),
                input_state_version=2,
                status="running",
                run_id=run_id,
                patient_message_id=message_id,
            )
        )

    async with db_factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        response = await runner._wait_for_completed_claim(  # noqa: SLF001
            session_id,
            command_key,
            _payload_digest(body),
        )

    async with db_factory() as db:
        claim = await db.get(IntakeCommandClaim, claim_id)
        assert claim is not None

    assert response.message_id == str(message_id)
    assert response.state_version == 3
    assert claim.status == "completed"
    assert claim.response_payload is not None


@pytest.mark.asyncio
async def test_langgraph_advance_consumes_persisted_ready_gate(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_advance_graph(monkeypatch, db_factory)
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
                state_snapshot={"agent_runtime": "langgraph", "current_stage": "inquiry", "state_version": 2},
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-ready",
        )

    assert response["from_stage"] == "inquiry"
    assert response["current_stage"] == "syndrome"
    assert response["state_version"] == 3


@pytest.mark.asyncio
async def test_langgraph_advance_requires_current_ready_gate(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=2,
                trace_id="advance-no-gate",
            )


@pytest.mark.asyncio
async def test_langgraph_advance_ready_gate_still_requires_proposed_safety_resolution(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            ConsultMessage(
                id=source_message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content="No known allergies.",
                trace_id="advance-pending-safety-source",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )
        await db.flush()
        db.add(
            SafetyFactAssertion(
                id=uuid.uuid4(),
                session_id=session_id,
                field_name="allergy",
                value={"status": "explicitly_none", "items": []},
                value_digest="a" * 64,
                assertion_fingerprint="b" * 64,
                status="proposed",
                source_kind="structured_form",
                source_message_id=source_message_id,
                extraction_run_id=None,
                template_version="advance-gate-test.v1",
                evidence_spans=[],
                evidence_digest="c" * 64,
                proposed_by_actor_type="doctor",
                proposed_by_actor_id="doctor-a",
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError) as captured:
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=2,
                trace_id="advance-pending-safety",
            )
        assert "proposed safety facts" in (captured.value.detail or "")

    async with db_factory() as db:
        refreshed = await db.get(ConsultSession, session_id)
        claim_count = await db.scalar(
            select(func.count())
            .select_from(IntakeCommandClaim)
            .where(IntakeCommandClaim.session_id == session_id)
        )

    assert refreshed is not None
    assert refreshed.current_stage == "inquiry"
    assert refreshed.state_version == 2
    assert claim_count == 0


@pytest.mark.asyncio
async def test_langgraph_advance_rejects_stale_ready_gate_after_state_changes(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=3,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InsufficientInquiryError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=None,
                trace_id="advance-stale-gate",
            )

    async with db_factory() as db:
        refreshed = await db.get(ConsultSession, session_id)
        assert refreshed is not None
        assert refreshed.current_stage == "inquiry"
        assert refreshed.state_version == 3


@pytest.mark.asyncio
async def test_langgraph_advance_replay_is_stable_and_writes_one_outbox(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_advance_graph(monkeypatch, db_factory)
    session_id = uuid.uuid4()
    async with db_factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                state_version=2,
                current_stage="inquiry",
                agent_runtime="langgraph",
            )
        )
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session_id,
                gate_name=COMPLETENESS_GATE_NAME,
                policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=2,
                decision="passed",
                details={"disposition": "ready"},
            )
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        first = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-replay-first-trace",
            idempotency_key="advance-public-replay",
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        second = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-replay-retry-trace",
            idempotency_key="advance-public-replay",
        )
        outbox_count = await db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'advance.command_completed.v1'")
        )

    assert second == first
    assert outbox_count == 1
