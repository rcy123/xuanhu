from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.langgraph_intake as langgraph_intake_module
from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.state import XuanhuGraphState, default_state, validate_state_json_safe
from app.api.advance import _run_langgraph_advance
from app.core.exceptions import InsufficientInquiryError
from app.db.session import _build_async_pg_url
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import DomainCommandCommit, GateResult, GraphRun, IntakeCommandClaim, OutboxEvent
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import CollectionStatus
from app.schemas.intake import (
    CandidateSeverity,
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
from app.services.langgraph_intake import LangGraphIntakeMessageRunner, _payload_digest


def _state() -> XuanhuGraphState:
    session_id = str(uuid.uuid4())
    return default_state(
        session_id=session_id,
        command=XuanhuCommand.MESSAGE.value,
        command_id="cmd-intake-test",
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(uuid.uuid4()),
    )


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


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
            source_id = _source_message_id(messages)
            return _intake_output(self.mode, source_id)
        if output_schema is QuestionComposerModelOutput:
            return QuestionComposerModelOutput(question="请补充一个关键信息。")
        raise AssertionError(f"unexpected output schema: {output_schema}")

    @property
    def intake_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is IntakeExtractionOutput)

    @property
    def question_model_calls(self) -> int:
        return sum(1 for item in self.calls if item["output_schema"] is QuestionComposerModelOutput)


def _source_message_id(messages: list[dict[str, Any]]) -> uuid.UUID:
    raw = messages[-1]["content"]
    payload = json.loads(raw)
    return uuid.UUID(payload[0]["message_id"])


def _observation(source: uuid.UUID, fact_key: str, value: str) -> ObservationDelta:
    return ObservationDelta(
        fact_key=fact_key,
        value=value,
        normalized_value=value,
        source_message_id=source,
        confidence=0.95,
    )


def _safety_none(source: uuid.UUID) -> PatientSafetyDelta:
    empty = SafetyListDelta(status=CollectionStatus.EXPLICITLY_NONE, source_message_id=source)
    return PatientSafetyDelta(
        allergy=empty,
        pregnancy=PregnancyDelta(status=CollectionStatus.EXPLICITLY_NONE, source_message_id=source),
        lactation=LactationDelta(status=CollectionStatus.EXPLICITLY_NONE, source_message_id=source),
        medications=empty,
        major_conditions=empty,
    )


def _intake_output(mode: str, source: uuid.UUID) -> IntakeExtractionOutput:
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
            patient_safety_delta=_safety_none(source),
        )
    if mode == "red_flag":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            red_flag_candidates=(
                RedFlagCandidate(
                    category=RedFlagCategory.HIGH_FEVER,
                    source_message_id=source,
                    severity=CandidateSeverity.HIGH,
                    evidence="high fever",
                    confidence=0.96,
                ),
            ),
        )
    if mode == "privacy":
        return IntakeExtractionOutput(
            decision=IntakeExtractionDecision.EXTRACTED,
            observations=(
                _observation(source, "chief_complaint.symptom", "privacy_fact_value_778899"),
            ),
        )
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        observations=(_observation(source, "chief_complaint.symptom", "headache"),),
    )


def _install_fake_runtime(monkeypatch: pytest.MonkeyPatch, gateway: _E2EFakeGateway) -> None:
    monkeypatch.setattr(langgraph_intake_module, "AgentRuntime", lambda: AgentRuntime(gateway))


@pytest.fixture(scope="module")
def migrated_database() -> str:
    import os

    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL is required for L3-5 PostgreSQL verification")
    config = Config("alembic.ini")
    command.downgrade(config, "20260711_0004")
    command.upgrade(config, "20260712_0006")
    command.downgrade(config, "20260711_0004")
    command.upgrade(config, "20260712_0006")
    return db_url


@pytest.fixture
async def db_factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from app.db.session import reset_session_factory

    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=3, max_overflow=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE intake_command_claims, domain_command_commits, outbox_events, gate_results, "
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
async def test_langgraph_messages_e2e_incomplete_uses_template_question_and_one_intake_call(
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
    assert gateway.question_model_calls == 0
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
async def test_langgraph_messages_e2e_red_flag_blocks_without_question(
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
            MessageCreateRequest(role="patient_proxy", content="high fever"),
            doctor_id=None,
            trace_id="messages-e2e-red-flag",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        session = await db.get(ConsultSession, session_id)

    assert response.agent_message is None
    assert response.current_stage == "blocked"
    assert session is not None
    assert session.recovery_status == "manual_required"
    assert gateway.intake_calls == 1
    assert gateway.question_model_calls == 0
    assert claim is not None
    assert claim.intermediate_payload is not None
    assert claim.intermediate_payload["gates"]["route"] == "manual"
    assert claim.intermediate_payload["gates"]["completeness_disposition"] == "triage_blocked"


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

    async def submit_once() -> MessageCreateResponse:
        async with db_factory() as db:
            return await LangGraphIntakeMessageRunner(db).submit_message(
                str(session_id),
                body,
                doctor_id=None,
                trace_id="messages-e2e-concurrent",
                x_state_version=1,
            )

    first, second = await asyncio.gather(submit_once(), submit_once())

    async with db_factory() as db:
        claim_count = await db.scalar(select(func.count()).select_from(IntakeCommandClaim))
        message_count = await db.scalar(text("SELECT count(*) FROM consult_messages WHERE session_id = :sid"), {"sid": session_id})

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
            MessageCreateRequest(role="patient_proxy", content="headache three days stable"),
            doctor_id=None,
            trace_id="messages-e2e-recover-after-commit",
            x_state_version=1,
        )

    async with db_factory() as db:
        claim = await db.scalar(select(IntakeCommandClaim).where(IntakeCommandClaim.session_id == session_id))
        commit = await db.scalar(select(DomainCommandCommit).where(DomainCommandCommit.session_id == session_id))

    assert response.agent_message is None
    assert response.current_stage == "inquiry"
    assert response.state_version == 3
    assert gateway.intake_calls == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.response_payload is not None
    assert commit is not None


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

    assert type(captured.value).__name__ == "ValidationError"
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
            trace_id="advance-replay",
        )

    async with db_factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        second = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="advance-replay",
        )
        outbox_count = await db.scalar(
            text("SELECT count(*) FROM outbox_events WHERE event_type = 'advance.command_completed.v1'")
        )

    assert second == first
    assert outbox_count == 1
