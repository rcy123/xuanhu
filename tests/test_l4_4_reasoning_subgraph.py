from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.agents.formula_draft as formula_agent_module
import app.agents.syndrome_draft as syndrome_agent_module
import app.db.session as db_session_module
import app.services.langgraph_reasoning as reasoning_module
from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import NODE_REASONING_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.reasoning_subgraph import ROUTE_FORMULA_COMPLETED, ROUTE_MANUAL_REQUIRED
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    ArtifactPayloadSpec,
    PostgresDomainRepository,
    artifact_payload_digest,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.specs import AgentSpec, Capability, FailurePolicy, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.state import default_state, validate_state_json_safe
from app.agent_runtime.syndrome_verifier import SyndromeCheckResult, SyndromeCheckStatus, SyndromeVerificationReport
from app.agent_runtime.verifiers import VerificationContext
from app.api.advance import _run_langgraph_advance as _production_run_langgraph_advance
from app.core.exceptions import InvalidStateVersionError
from app.core.gateway import ModelTokenUsage, StructuredChatResponse
from app.db.session import _build_async_pg_url, reset_session_factory
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    ArtifactRevisionPayload,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    Observation,
)
from app.models.safety import SafetyRuleRun
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import ArtifactRevisionSchema, ArtifactStatus, GateDecision, ObservationStatus
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    FormulaModification,
    HerbItem,
    ModificationAction,
)
from app.schemas.syndrome import (
    SYNDROME_EVIDENCE_MODE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeFactClaim,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION
from app.services.langgraph_reasoning import run_reasoning_draft_syndrome_node
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


async def _run_langgraph_advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicitly opt direct integration calls into request-local runtime."""

    kwargs["allow_request_local_runtime"] = True
    return await _production_run_langgraph_advance(*args, **kwargs)


@pytest.fixture(scope="module")
def migrated_database() -> str:
    with destructive_database_environment() as db_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "20260712_0006")
            command.upgrade(config, "head")
            yield db_url
        finally:
            command.upgrade(config, "head")


@pytest.fixture
async def store(migrated_database: str) -> tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]]:
    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(migrated_database), pool_size=5, max_overflow=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE domain_command_commits, outbox_events, gate_results, graph_run_steps, "
                "artifact_revision_payloads, artifact_revisions, graph_runs, safety_rule_runs, "
                "safety_profiles, observations, consult_messages, consult_sessions CASCADE"
            )
        )
    reasoning_module._SYNDROME_RESULT_CACHE.clear()
    reasoning_module._FORMULA_ROUTE_CACHE.clear()
    reasoning_module._REASONING_AUTHORITY_CACHE.clear()
    try:
        yield PostgresDomainRepository(factory), factory
    finally:
        reasoning_module._SYNDROME_RESULT_CACHE.clear()
        reasoning_module._FORMULA_ROUTE_CACHE.clear()
        await reset_session_factory()
        await engine.dispose()


class _ReasoningFakeGateway:
    def __init__(self, outcomes: Sequence[BaseModel]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[type[BaseModel]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> BaseModel:
        del messages, kwargs
        self.calls.append(output_schema)
        if not self.outcomes:
            raise AssertionError(f"unexpected model call for {output_schema.__name__}")
        outcome = self.outcomes.pop(0)
        if not isinstance(outcome, output_schema):
            raise AssertionError(f"expected {type(outcome).__name__}, got request for {output_schema.__name__}")
        return outcome

    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> StructuredChatResponse:
        requested_model = kwargs.get("model")
        output = await self.chat_structured(messages, output_schema, **kwargs)
        return StructuredChatResponse(
            output=output,
            model_actual=requested_model if isinstance(requested_model, str) else None,
            usage=ModelTokenUsage(),
        )


def _install_gateway(monkeypatch: pytest.MonkeyPatch, gateway: _ReasoningFakeGateway) -> None:
    monkeypatch.setattr(
        reasoning_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway, recorder=None),
    )


def _graph_state(session_id: uuid.UUID, command_key: str, run_id: uuid.UUID) -> dict[str, Any]:
    return dict(
        default_state(
            session_id=str(session_id),
            command=XuanhuCommand.ADVANCE.value,
            command_id=command_key,
            graph_version=DEFAULT_GRAPH_VERSION,
            run_id=str(run_id),
        )
    )


def _observation_specs(session_id: uuid.UUID, source_message_id: uuid.UUID) -> tuple[Observation, ...]:
    facts = (
        ("chief_complaint.symptom", "headache"),
        ("chief_complaint.course", "three_days"),
        ("present_illness.change", "stable"),
        ("ten_questions.cold_heat", "none"),
        ("ten_questions.sleep", "normal"),
        ("ten_questions.stool_urine", "normal"),
    )
    return tuple(
        Observation(
            id=uuid.uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            normalized_value=value,
            source_message_id=source_message_id,
            status=ObservationStatus.ACTIVE.value,
            confidence=0.95,
        )
        for key, value in facts
    )


async def _ready_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    stage: str = "inquiry",
    state_version: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]:
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    intake_run_id = uuid.uuid4()
    triage_gate_id = uuid.uuid4()
    completeness_gate_id = uuid.uuid4()
    observations = _observation_specs(session_id, message_id)
    snapshot: dict[str, Any] = {"sufficiency_report": {"sufficient": True}}
    if stage == "syndrome":
        snapshot.update(
            {
                "agent_runtime": "langgraph",
                "current_stage": "syndrome",
                "state_version": state_version,
                "advance": {
                    "source_gate_id": str(completeness_gate_id),
                    "source_gate_state_version": 1,
                    "trace_id": "setup",
                },
            }
        )
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage=stage,
                status="active",
                agent_runtime="langgraph",
                state_version=state_version,
                state_snapshot=snapshot,
            )
        )
        db.add(
            ConsultMessage(
                id=message_id,
                session_id=session_id,
                role="patient_proxy",
                stage="inquiry",
                content="structured test input",
            )
        )
        db.add_all(observations)
        db.add(
            GraphRun(
                id=intake_run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
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
    return session_id, completeness_gate_id, tuple(item.id for item in observations)


def _syndrome_completed(fact_ids: tuple[uuid.UUID, ...]) -> SyndromeDraft:
    return SyndromeDraft(
        decision=SyndromeDraftDecision.COMPLETED,
        syndrome="wind cold headache",
        syndrome_basis=(SyndromeFactClaim(claim="active facts support the syndrome", fact_ids=fact_ids),),
        differential=(SyndromeFactClaim(claim="no heat signs are present", fact_ids=(fact_ids[0],)),),
        treatment_principle="release exterior and relieve pain",
        confidence=0.55,
        evidence_mode=SYNDROME_EVIDENCE_MODE,
        claim_evidence_links=(),
        missing_inputs=(),
        review_required=True,
    )


def _syndrome_needs_more_info() -> SyndromeDraft:
    from app.schemas.completeness import InquiryDimension

    return SyndromeDraft(
        decision=SyndromeDraftDecision.NEEDS_MORE_INFO,
        confidence=0.2,
        missing_inputs=(InquiryDimension.TEN_SLEEP,),
        review_required=True,
    )


def _syndrome_abstained() -> SyndromeDraft:
    return SyndromeDraft(
        decision=SyndromeDraftDecision.ABSTAINED,
        confidence=0.1,
        review_required=True,
    )


def _formula_completed(fact_ids: tuple[uuid.UUID, ...]) -> FormulaDraft:
    base = FormulaComposition(
        name="Test Formula",
        composition=(
            HerbItem(herb="Chuanxiong", dose=10.0, unit="g"),
            HerbItem(herb="Jingjie", dose=10.0, unit="g"),
        ),
        rationale="release exterior and relieve pain",
        basis=(FormulaFactClaim(claim="syndrome and symptoms support base formula", fact_ids=fact_ids),),
    )
    add_herb = FormulaModification(
        action=ModificationAction.ADD,
        herb="Baizhi",
        dose=10.0,
        unit="g",
        reason="headache remains prominent",
        basis=FormulaFactClaim(claim="headache supports adding Baizhi", fact_ids=(fact_ids[0],)),
    )
    return FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=base,
        modifications=(add_herb,),
        candidate_formula=base.model_copy(
            update={"composition": (*base.composition, HerbItem(herb="Baizhi", dose=10.0, unit="g"))}
        ),
        rationale="candidate follows the syndrome treatment principle",
        confidence=0.55,
        evidence_mode=FORMULA_EVIDENCE_MODE,
        claim_evidence_links=(),
        missing_inputs=(),
        review_required=True,
    )


def _formula_abstained() -> FormulaDraft:
    return FormulaDraft(
        decision=FormulaDraftDecision.ABSTAINED,
        confidence=0.1,
        review_required=True,
    )


def _formula_consistency_mismatch(fact_ids: tuple[uuid.UUID, ...]) -> FormulaDraft:
    draft = _formula_completed(fact_ids)
    assert draft.base_formula is not None
    return draft.model_copy(update={"candidate_formula": draft.base_formula})


def _formula_needs_more_info(raw: str = "sleep") -> FormulaDraft:
    return FormulaDraft(
        decision=FormulaDraftDecision.NEEDS_MORE_INFO,
        confidence=0.2,
        missing_inputs=(raw,),
        review_required=True,
    )


def _passed_syndrome_report() -> SyndromeVerificationReport:
    return SyndromeVerificationReport(
        passed=True,
        checks=(SyndromeCheckResult(verifier="test", status=SyndromeCheckStatus.PASSED),),
        subject_digest="0" * 64,
    )


def _run_artifact_payload_for_assert(artifact: RunArtifact) -> dict[str, object]:
    return {
        "output": artifact.output.model_dump(mode="json"),
        "model_actual": artifact.model_actual,
        "attempts": artifact.attempts,
        "latency_ms": artifact.latency_ms,
        "trace_id": artifact.trace_id,
        "run_id": str(artifact.run_id),
        "agent_spec_version": artifact.agent_spec_version,
        "prompt_version": artifact.prompt_version,
        "usage": artifact.usage.model_dump(mode="json"),
        "evidence_ids": list(artifact.evidence_ids),
    }


def _context(
    session_id: uuid.UUID,
    delta: DomainDelta,
    *,
    idempotency_key: str,
    state: DomainState | None = None,
) -> VerificationContext:
    agent_spec = AgentSpec(
        name="l4-4-test-agent",
        version="l4-4-test-agent.v1",
        input_schema=DomainState,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="test-model", max_attempts=1),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=("test",),
        failure_policy=FailurePolicy(),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=session_id,
        state_version=delta.expected_state_version,
        stage="reasoning_reduce",
        agent_spec_version=agent_spec.version,
        prompt_version="l4-4-test",
        policy_version="l4-4-test-policy.v1",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=idempotency_key,
    )
    return VerificationContext(
        agent_spec=agent_spec,
        run_spec=run_spec,
        artifact=RunArtifact(
            output=delta,
            model_actual="test-model",
            attempts=1,
            latency_ms=0,
            trace_id=run_spec.trace_id,
            run_id=run_spec.run_id,
            agent_spec_version=agent_spec.version,
            prompt_version=run_spec.prompt_version,
        ),
        state=state or DomainState(session_id=session_id, state_version=delta.expected_state_version),
        allowed_stages=frozenset({"reasoning_reduce"}),
    )


async def _insert_running_reasoning_claim(
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    *,
    command_key: str,
    input_state_version: int,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                command_id=command_key,
                input_state_version=input_state_version,
                status="running",
            )
        )
        db.add(
            IntakeCommandClaim(
                id=uuid.uuid4(),
                session_id=session_id,
                idempotency_key=command_key,
                payload_digest="0" * 64,
                input_state_version=input_state_version,
                status="running",
                run_id=run_id,
            )
        )
    return run_id


async def _replace_artifact_payload(
    factory: async_sessionmaker[AsyncSession],
    record: Any,
    *,
    schema_version: str,
    payload: dict[str, object],
) -> str:
    digest = artifact_payload_digest(schema_version, payload)
    async with factory() as db, db.begin():
        await db.execute(
            update(ArtifactRevisionPayload)
            .where(ArtifactRevisionPayload.id == record.row_id)
            .values(payload=payload, content_digest=digest)
        )
    return digest


def test_restore_symbols_and_raw_syndrome_results_do_not_grant_trust() -> None:
    assert not hasattr(syndrome_agent_module, "_restore_trusted_syndrome_execution")

    raw = syndrome_agent_module.SyndromeExecutionResult(
        status=syndrome_agent_module.SyndromeExecutionStatus.SUCCEEDED,
        output=_syndrome_completed((uuid.uuid4(),)),
        verification=_passed_syndrome_report(),
    )

    assert syndrome_agent_module._consume_trusted_syndrome_execution(raw) is None  # noqa: SLF001
    assert syndrome_agent_module._consume_trusted_syndrome_execution(raw.model_copy(deep=True)) is None  # noqa: SLF001


async def test_recovery_boundary_accepts_only_artifact_ref_and_digest() -> None:
    signature = inspect.signature(syndrome_agent_module.recover_trusted_syndrome_from_repository)
    assert set(signature.parameters) == {"session_id", "artifact_id", "revision", "expected_content_digest"}

    with pytest.raises(TypeError):
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            repository=object(),
            session_id=uuid.uuid4(),
            artifact_id=uuid.uuid4(),
            revision=1,
            expected_content_digest="0" * 64,
        )

    with pytest.raises(TypeError):
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=uuid.uuid4(),
            artifact_id=uuid.uuid4(),
            revision=1,
            expected_content_digest="0" * 64,
            current_authority=object(),
            record=object(),
        )


async def test_recovery_boundary_rejects_missing_postgres_artifact_ref(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    del store
    result = await syndrome_agent_module.recover_trusted_syndrome_from_repository(
        session_id=uuid.uuid4(),
        artifact_id=uuid.uuid4(),
        revision=1,
        expected_content_digest="0" * 64,
    )

    assert result is None


async def test_forged_repository_and_method_replacement_cannot_restore_trust(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id = uuid.uuid4()
    fake_record = SimpleNamespace(artifact_id=uuid.uuid4(), revision=1, content_digest="0" * 64)

    class ForgedRepository(PostgresDomainRepository):
        async def get_artifact_payload(self, *args: Any, **kwargs: Any) -> Any:
            return fake_record

        async def get_state(self, session_id: uuid.UUID) -> Any:
            raise AssertionError("forged state must not be trusted")

        async def get_reasoning_authority(self, session_id: uuid.UUID, state_version: int) -> Any:
            raise AssertionError("forged authority must not be trusted")

    forged = ForgedRepository(factory)
    forged_result = await reasoning_module._load_trusted_syndrome_result(forged, session_id, object())  # noqa: SLF001
    assert forged_result is None

    async def fake_get_artifact_payload(*args: Any, **kwargs: Any) -> Any:
        return fake_record

    repository.get_artifact_payload = fake_get_artifact_payload  # type: ignore[method-assign]
    replaced_result = await reasoning_module._load_trusted_syndrome_result(repository, session_id, object())  # noqa: SLF001
    assert replaced_result is None


async def test_session_factory_replacement_cannot_supply_forged_recovery_source(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del store

    def fake_session_factory() -> Any:
        raise AssertionError("caller-replaced session factory must not be used")

    monkeypatch.setattr(db_session_module, "get_session_factory", fake_session_factory)

    result = await syndrome_agent_module.recover_trusted_syndrome_from_repository(
        session_id=uuid.uuid4(),
        artifact_id=uuid.uuid4(),
        revision=1,
        expected_content_digest="0" * 64,
    )

    assert result is None


async def test_advance_route_invokes_reasoning_subgraph_and_keeps_state_json_safe() -> None:
    calls: list[str] = []

    async def executor(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(state["command"])
        return {
            "route": NODE_REASONING_SUBGRAPH_V1,
            "reasoning_route": ROUTE_FORMULA_COMPLETED,
            "artifact_refs": [{"kind": "formula_draft", "artifact_id": str(uuid.uuid4()), "revision": 1}],
        }

    session_id = str(uuid.uuid4())
    state = default_state(
        session_id=session_id,
        command=XuanhuCommand.ADVANCE.value,
        command_id="advance:test",
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(uuid.uuid4()),
    )
    graph = build_main_graph(checkpointer=InMemorySaver(), reasoning_executor=executor)
    result = await graph.ainvoke(dict(state), config=make_run_config(session_id))

    validate_state_json_safe(result)
    assert calls == [XuanhuCommand.ADVANCE.value]
    assert result["route"] == NODE_REASONING_SUBGRAPH_V1
    assert "syndrome" not in result and "formula" not in result


async def test_artifact_payload_roundtrip_uses_exact_revision(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repository, factory = store
    session_id, _, _ = await _ready_session(factory, stage="syndrome", state_version=2)
    artifact_id = uuid.uuid4()
    run_id = uuid.uuid4()
    artifact = ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type="formula_draft",
        revision=1,
        session_id=session_id,
        input_state_version=2,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=run_id,
        created_at=datetime.now(UTC),
    )
    delta = DomainDelta(
        delta_id=uuid.uuid4(),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=2,
        artifact_revisions=(artifact,),
    )
    payload = {"kind": "formula_draft", "output": {"decision": "completed"}}
    digest = artifact_payload_digest("test-payload.v1", payload)
    state = await repository.get_state(session_id)

    await repository.commit(
        delta,
        _context(session_id, delta, idempotency_key="payload-roundtrip", state=state),
        graph_version=DEFAULT_GRAPH_VERSION,
        artifact_payloads=(
            ArtifactPayloadSpec(
                session_id=session_id,
                artifact_id=artifact_id,
                revision=1,
                payload_schema_version="test-payload.v1",
                payload=payload,
                content_digest=digest,
            ),
        ),
    )

    record = await repository.get_artifact_payload(session_id, artifact_type="formula_draft", artifact_id=artifact_id)
    assert record is not None
    assert record.revision == 1
    assert record.payload == payload
    assert record.content_digest == digest


async def test_repository_bound_recovery_rejects_cross_refs_and_tampered_provenance(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, _, fact_ids = await _ready_session(factory, stage="syndrome", state_version=2)
    command_key = "advance:repo-recovery-guard"
    run_id = await _insert_running_reasoning_claim(
        factory,
        session_id,
        command_key=command_key,
        input_state_version=2,
    )
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    syndrome_update = await run_reasoning_draft_syndrome_node(_graph_state(session_id, command_key, run_id))
    assert syndrome_update["route"] == NODE_REASONING_SUBGRAPH_V1
    assert gateway.calls == [SyndromeDraft]

    record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.SYNDROME_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(session_id, reasoning_module.SYNDROME_ARTIFACT_TYPE),  # noqa: SLF001
        status="current",
    )
    assert record is not None

    ok = await syndrome_agent_module.recover_trusted_syndrome_from_repository(
        session_id=session_id,
        artifact_id=record.artifact_id,
        revision=record.revision,
        expected_content_digest=record.content_digest,
    )
    assert ok is not None
    assert syndrome_agent_module._consume_trusted_syndrome_execution(ok) is not None  # noqa: SLF001
    assert syndrome_agent_module._consume_trusted_syndrome_execution(ok.model_copy(deep=True)) is None  # noqa: SLF001

    assert (
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=session_id,
            artifact_id=record.artifact_id,
            revision=record.revision,
            expected_content_digest="f" * 64,
        )
        is None
    )
    assert (
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=session_id,
            artifact_id=record.artifact_id,
            revision=record.revision + 1,
            expected_content_digest=record.content_digest,
        )
        is None
    )
    assert (
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=uuid.uuid4(),
            artifact_id=record.artifact_id,
            revision=record.revision,
            expected_content_digest=record.content_digest,
        )
        is None
    )

    tampered_run_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=tampered_run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                command_id="advance:tampered-run",
                input_state_version=record.input_state_version,
                status="completed",
                completed_at=func.now(),
            )
        )
        await db.flush()
        await db.execute(
            update(ArtifactRevision)
            .where(ArtifactRevision.id == record.artifact_revision_row_id)
            .values(produced_by_run_id=tampered_run_id)
        )
    assert (
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=session_id,
            artifact_id=record.artifact_id,
            revision=record.revision,
            expected_content_digest=record.content_digest,
        )
        is None
    )

    async with factory() as db, db.begin():
        await db.execute(
            update(ArtifactRevision)
            .where(ArtifactRevision.id == record.artifact_revision_row_id)
            .values(produced_by_run_id=record.produced_by_run_id, input_state_version=record.input_state_version + 1)
        )
    assert (
        await syndrome_agent_module.recover_trusted_syndrome_from_repository(
            session_id=session_id,
            artifact_id=record.artifact_id,
            revision=record.revision,
            expected_content_digest=record.content_digest,
        )
        is None
    )


async def test_persisted_payload_uses_trusted_run_artifact_for_syndrome_and_formula(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    syndrome_model = "actual-syndrome-model"
    formula_model = "actual-formula-model"
    captured: dict[str, dict[str, object]] = {}

    monkeypatch.setattr(
        reasoning_module,
        "build_syndrome_agent_spec",
        lambda: syndrome_agent_module.build_syndrome_agent_spec(model=syndrome_model),
    )
    monkeypatch.setattr(
        reasoning_module,
        "build_formula_agent_spec",
        lambda: formula_agent_module.build_formula_agent_spec(model=formula_model),
    )
    original_syndrome_commit = reasoning_module._commit_syndrome_artifact  # noqa: SLF001
    original_formula_commit = reasoning_module._commit_formula_artifact  # noqa: SLF001

    async def spy_syndrome_commit(
        repository: PostgresDomainRepository,
        claim: IntakeCommandClaim,
        result: syndrome_agent_module.SyndromeExecutionResult,
        *,
        trace_id: str,
    ) -> dict[str, Any] | None:
        trusted = syndrome_agent_module._consume_trusted_syndrome_execution(result)  # noqa: SLF001
        assert trusted is not None
        captured["syndrome"] = _run_artifact_payload_for_assert(trusted.artifact)
        return await original_syndrome_commit(repository, claim, result, trace_id=trace_id)

    async def spy_formula_commit(
        repository: PostgresDomainRepository,
        claim: IntakeCommandClaim,
        result: formula_agent_module.FormulaExecutionResult,
        consistency: Any,
        *,
        trace_id: str,
    ) -> dict[str, Any] | None:
        trusted = formula_agent_module._consume_trusted_formula_execution(result)  # noqa: SLF001
        assert trusted is not None
        captured["formula"] = _run_artifact_payload_for_assert(trusted.artifact)
        return await original_formula_commit(repository, claim, result, consistency, trace_id=trace_id)

    monkeypatch.setattr(reasoning_module, "_commit_syndrome_artifact", spy_syndrome_commit)
    monkeypatch.setattr(reasoning_module, "_commit_formula_artifact", spy_formula_commit)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-real-run-artifact",
        )

    syndrome_record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.SYNDROME_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(session_id, reasoning_module.SYNDROME_ARTIFACT_TYPE),  # noqa: SLF001
        status="current",
    )
    formula_record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.FORMULA_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(session_id, reasoning_module.FORMULA_ARTIFACT_TYPE),  # noqa: SLF001
        status="current",
    )

    assert syndrome_record is not None and formula_record is not None
    assert syndrome_record.payload["run_artifact"] == captured["syndrome"]
    assert formula_record.payload["run_artifact"] == captured["formula"]
    assert captured["syndrome"]["model_actual"] == syndrome_model
    assert captured["formula"]["model_actual"] == formula_model
    assert captured["syndrome"]["model_actual"] != "fake-model"
    assert captured["formula"]["model_actual"] != "fake-model"


async def test_advance_returns_committed_syndrome_when_formula_stage_fails(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="advance-syndrome-committed",
        )

    assert response["current_stage"] == "syndrome"
    assert response["from_stage"] == "inquiry"
    assert response["state_version"] == 2
    assert response["artifact_refs"][0]["kind"] == "syndrome_draft"

    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key.startswith("advance:"),
            )
            .order_by(IntakeCommandClaim.created_at.desc())
            .limit(1)
        )
        assert claim is not None
        assert claim.status == "completed"
        assert claim.response_payload is not None
        graph_run = await db.get(GraphRun, claim.run_id)
        assert graph_run is not None and graph_run.status == "completed"
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        assert session.current_stage == "syndrome"
        assert session.recovery_status == "normal"

    formula_record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.FORMULA_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(  # noqa: SLF001
            session_id,
            reasoning_module.FORMULA_ARTIFACT_TYPE,
        ),
        status="current",
    )
    assert formula_record is None


async def test_syndrome_recovery_rejects_any_run_artifact_provenance_tamper(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, _, fact_ids = await _ready_session(factory, stage="syndrome", state_version=2)
    command_key = "advance:syndrome-provenance-tamper"
    run_id = await _insert_running_reasoning_claim(
        factory,
        session_id,
        command_key=command_key,
        input_state_version=2,
    )
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    await run_reasoning_draft_syndrome_node(_graph_state(session_id, command_key, run_id))
    record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.SYNDROME_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(session_id, reasoning_module.SYNDROME_ARTIFACT_TYPE),  # noqa: SLF001
        status="current",
    )
    assert record is not None
    original_payload = copy.deepcopy(record.payload)

    for field in (
        "model_actual",
        "attempts",
        "latency_ms",
        "trace_id",
        "run_id",
        "agent_spec_version",
        "prompt_version",
        "output",
    ):
        tampered = copy.deepcopy(original_payload)
        run_artifact = tampered["run_artifact"]
        assert isinstance(run_artifact, dict)
        if field == "model_actual":
            run_artifact[field] = "tampered-model"
        elif field == "attempts":
            run_artifact[field] = 2
        elif field == "latency_ms":
            run_artifact[field] = int(run_artifact[field]) + 1
        elif field == "trace_id":
            run_artifact[field] = "tampered-trace"
        elif field == "run_id":
            run_artifact[field] = str(uuid.uuid4())
        elif field == "agent_spec_version":
            run_artifact[field] = "tampered-agent-spec"
        elif field == "prompt_version":
            run_artifact[field] = "tampered_prompt.jinja2"
        else:
            output = run_artifact["output"]
            assert isinstance(output, dict)
            output["confidence"] = 0.56
        digest = await _replace_artifact_payload(
            factory,
            record,
            schema_version=reasoning_module.SYNDROME_PAYLOAD_SCHEMA_VERSION,
            payload=tampered,
        )

        assert (
            await syndrome_agent_module.recover_trusted_syndrome_from_repository(
                session_id=session_id,
                artifact_id=record.artifact_id,
                revision=record.revision,
                expected_content_digest=digest,
            )
            is None
        )


async def test_formula_route_rejects_any_run_artifact_provenance_tamper(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-formula-provenance-tamper",
        )

    record = await repository.get_artifact_payload(
        session_id,
        artifact_type=reasoning_module.FORMULA_ARTIFACT_TYPE,
        artifact_id=reasoning_module._artifact_id(session_id, reasoning_module.FORMULA_ARTIFACT_TYPE),  # noqa: SLF001
        status="current",
    )
    assert record is not None
    original_payload = copy.deepcopy(record.payload)

    for field in (
        "model_actual",
        "attempts",
        "latency_ms",
        "trace_id",
        "run_id",
        "agent_spec_version",
        "prompt_version",
        "output",
    ):
        tampered = copy.deepcopy(original_payload)
        run_artifact = tampered["run_artifact"]
        assert isinstance(run_artifact, dict)
        if field == "model_actual":
            run_artifact[field] = "tampered-model"
        elif field == "attempts":
            run_artifact[field] = 2
        elif field == "latency_ms":
            run_artifact[field] = int(run_artifact[field]) + 1
        elif field == "trace_id":
            run_artifact[field] = "tampered-trace"
        elif field == "run_id":
            run_artifact[field] = str(uuid.uuid4())
        elif field == "agent_spec_version":
            run_artifact[field] = "tampered-agent-spec"
        elif field == "prompt_version":
            run_artifact[field] = "tampered_prompt.jinja2"
        else:
            output = run_artifact["output"]
            assert isinstance(output, dict)
            output["confidence"] = 0.56
        await _replace_artifact_payload(
            factory,
            record,
            schema_version=reasoning_module.FORMULA_PAYLOAD_SCHEMA_VERSION,
            payload=tampered,
        )
        tampered_record = await repository.get_artifact_payload(
            session_id,
            artifact_type=reasoning_module.FORMULA_ARTIFACT_TYPE,
            artifact_id=record.artifact_id,
            status="current",
        )

        assert reasoning_module._formula_route_from_record(tampered_record) == ROUTE_MANUAL_REQUIRED  # noqa: SLF001


async def test_syndrome_consumer_failure_does_not_write_artifact_payload(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)
    monkeypatch.setattr(reasoning_module, "_consume_trusted_syndrome_execution", lambda result: None)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-syndrome-consumer-missing",
        )

    assert response["current_stage"] == "blocked"
    assert gateway.calls == [SyndromeDraft]
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ArtifactRevision)
                .where(ArtifactRevision.artifact_type == "syndrome_draft")
            )
            == 0
        )
        assert await db.scalar(select(func.count()).select_from(ArtifactRevisionPayload)) == 1


async def test_formula_consumer_failure_does_not_write_formula_payload(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)
    monkeypatch.setattr(reasoning_module, "_consume_trusted_formula_execution", lambda result: None)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-formula-consumer-missing",
        )

    assert response["current_stage"] == "blocked"
    assert gateway.calls == [SyndromeDraft, FormulaDraft]
    async with factory() as db:
        formula_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(ArtifactRevision.artifact_type == "formula_draft")
        )
        payload_count = await db.scalar(select(func.count()).select_from(ArtifactRevisionPayload))
        assert formula_count == 0
        assert payload_count == 2


async def test_langgraph_advance_completes_reasoning_without_safety_execution(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-completed",
        )

    assert response["current_stage"] == "safety"
    assert response["route"] == NODE_REASONING_SUBGRAPH_V1
    assert response["gate_results"][0]["gate_name"] == "ready_for_safety"
    assert gateway.calls == [SyndromeDraft, FormulaDraft]
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None and session.current_stage == "safety"
        assert await db.scalar(select(func.count()).select_from(SafetyRuleRun)) == 0
        assert await db.scalar(select(func.count()).select_from(ArtifactRevisionPayload)) == 2


async def test_syndrome_needs_more_info_invalidates_artifacts_without_formula_call(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, _ = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_needs_more_info()])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-syndrome-needs-more",
        )

    assert response["current_stage"] == "inquiry"
    assert gateway.calls == [SyndromeDraft]
    async with factory() as db:
        messages = (
            await db.scalars(
                select(ConsultMessage).where(ConsultMessage.session_id == session_id, ConsultMessage.role == "agent")
            )
        ).all()
        assert len(messages) == 1
        statuses = (
            await db.scalars(
                select(ArtifactRevision.status).where(
                    ArtifactRevision.session_id == session_id,
                    ArtifactRevision.artifact_type == "syndrome_draft",
                )
            )
        ).all()
        assert statuses and set(statuses) == {ArtifactStatus.STALE.value}


async def test_syndrome_abstained_routes_to_manual_required_without_formula_call(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, _ = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_abstained()])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-syndrome-abstained",
        )

    assert response["current_stage"] == "blocked"
    assert response["blocked_reason"] == "reasoning_manual_required"
    assert gateway.calls == [SyndromeDraft]


async def test_syndrome_verifier_failure_routes_to_manual_required_without_artifact(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    invalid_syndrome = _syndrome_completed(fact_ids).model_copy(update={"confidence": 0.95})
    gateway = _ReasoningFakeGateway([invalid_syndrome])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-syndrome-verifier-failure",
        )

    assert response["current_stage"] == "blocked"
    assert gateway.calls == [SyndromeDraft]
    async with factory() as db:
        artifact_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(ArtifactRevision.artifact_type == "syndrome_draft")
        )
        assert artifact_count == 0


async def test_formula_needs_more_info_invalidates_current_artifacts_and_returns_one_question(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_needs_more_info("sleep")])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-needs-more",
        )

    assert response["current_stage"] == "inquiry"
    assert response["route"] == NODE_REASONING_SUBGRAPH_V1 or response["route"] == "intake_subgraph_v1"
    assert response["gate_results"][0]["decision"] == "blocked"
    async with factory() as db:
        messages = (
            await db.scalars(
                select(ConsultMessage).where(ConsultMessage.session_id == session_id, ConsultMessage.role == "agent")
            )
        ).all()
        assert len(messages) == 1
        assert messages[0].content.count("?") + messages[0].content.count("？") == 1
        statuses = (
            await db.scalars(
                select(ArtifactRevision.status).where(
                    ArtifactRevision.session_id == session_id,
                    ArtifactRevision.artifact_type.in_(("syndrome_draft", "formula_draft")),
                )
            )
        ).all()
        assert statuses and set(statuses) == {ArtifactStatus.STALE.value}


async def test_formula_abstained_routes_to_manual_required(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_abstained()])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-formula-abstained",
        )

    assert response["current_stage"] == "blocked"
    assert response["blocked_reason"] == "reasoning_manual_required"
    assert gateway.calls == [SyndromeDraft, FormulaDraft]


async def test_formula_verifier_failure_routes_to_manual_required_without_formula_artifact(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    invalid_formula = _formula_completed(fact_ids).model_copy(update={"confidence": 0.95})
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), invalid_formula])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-formula-verifier-failure",
        )

    assert response["current_stage"] == "blocked"
    assert gateway.calls == [SyndromeDraft, FormulaDraft]
    async with factory() as db:
        formula_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(ArtifactRevision.artifact_type == "formula_draft")
        )
        assert formula_count == 0


async def test_formula_consistency_failure_routes_to_manual_required_without_formula_artifact(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_consistency_mismatch(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-formula-consistency-failure",
        )

    assert response["current_stage"] == "blocked"
    assert gateway.calls == [SyndromeDraft, FormulaDraft]
    async with factory() as db:
        formula_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(ArtifactRevision.artifact_type == "formula_draft")
        )
        assert formula_count == 0


async def test_formula_schema_invalid_retries_then_succeeds(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0d-2 回归：模型输出 schema 解析失败（STRUCTURED_OUTPUT_INVALID）属随机质量
    问题，开方阶段必须自动重试（_REASONING_RETRYABLE_MODEL_CODES 含该码）。

    REAL-SESSION d384ff26 复盘：chat_structured 解析 BaseFormulaDraft 失败后
    runtime.run 因 FailurePolicy.retryable_codes 为空集直接抛
    RuntimeErrorCode.STRUCTURED_OUTPUT_INVALID → _run_formula_stage_with_retry
    收到该码应重试而非一次崩死。
    """
    from app.core.gateway import ChatStructuredParseError

    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)

    class _SchemaRetryGateway(_ReasoningFakeGateway):
        def __init__(self, syndrome: BaseModel, formula: BaseModel) -> None:
            super().__init__([syndrome, formula])
            self.failed_formula_parse = False

        async def chat_structured(self, messages, output_schema, **kwargs):
            self.calls.append(output_schema)
            if output_schema is FormulaDraft and not self.failed_formula_parse:
                # 首次开方：模型输出无法解析成 FormulaDraft → 网关抛解析异常。
                self.failed_formula_parse = True
                raise ChatStructuredParseError("structured output invalid")
            if not self.outcomes:
                raise AssertionError(f"unexpected model call for {output_schema.__name__}")
            outcome = self.outcomes.pop(0)
            if not isinstance(outcome, output_schema):
                raise AssertionError(f"expected {type(outcome).__name__}, got {output_schema.__name__}")
            return outcome

    gateway = _SchemaRetryGateway(_syndrome_completed(fact_ids), _formula_completed(fact_ids))
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-schema-retry",
        )

    # 重试成功：FormulaDraft 被调用两次（首次失败 + 重试成功）。
    assert response["current_stage"] == "safety"
    assert gateway.calls == [SyndromeDraft, FormulaDraft, FormulaDraft]
    async with factory() as db:
        formula_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(
                ArtifactRevision.artifact_type == "formula_draft",
                ArtifactRevision.status == "current",
            )
        )
        assert formula_count == 1


async def test_recovery_after_syndrome_rebuilds_from_payload_without_second_syndrome_call(
    migrated_database: str,
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory, stage="syndrome", state_version=2)
    command_key = "advance:recovery"
    run_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                command_id=command_key,
                input_state_version=2,
                status="running",
            )
        )
        db.add(
            IntakeCommandClaim(
                id=uuid.uuid4(),
                session_id=session_id,
                idempotency_key=command_key,
                payload_digest="0" * 64,
                input_state_version=2,
                status="running",
                run_id=run_id,
            )
        )
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)
    state = _graph_state(session_id, command_key, run_id)

    syndrome_update = await run_reasoning_draft_syndrome_node(state)
    assert syndrome_update["route"] == NODE_REASONING_SUBGRAPH_V1
    assert gateway.calls == [SyndromeDraft]
    reasoning_module._SYNDROME_RESULT_CACHE.clear()

    async with postgres_checkpointer(migrated_database) as saver:
        graph = build_main_graph(checkpointer=saver)
        runner = GraphRunner(graph, timeout_seconds=120)
        await runner.ainvoke(state, config=make_run_config(str(session_id)))

    assert gateway.calls == [SyndromeDraft, FormulaDraft]
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None and session.current_stage == "safety"


async def test_concurrent_same_advance_command_does_not_duplicate_model_calls(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async def invoke(trace_id: str) -> dict[str, Any] | BaseException:
        try:
            async with factory() as db:
                session = await db.get(ConsultSession, session_id)
                assert session is not None
                return await _run_langgraph_advance(
                    db,
                    session,
                    session_id=str(session_id),
                    state_version=1,
                    trace_id=trace_id,
                    idempotency_key="advance-public-concurrent",
                )
        except BaseException as exc:
            return exc

    first, second = await asyncio.gather(
        invoke("l4-4-concurrent-first-trace"),
        invoke("l4-4-concurrent-retry-trace"),
    )

    responses = [item for item in (first, second) if isinstance(item, dict)]
    errors = [item for item in (first, second) if isinstance(item, BaseException)]
    assert len(responses) == 2
    assert not errors
    assert responses[0] == responses[1]
    assert gateway.calls == [SyndromeDraft, FormulaDraft]


async def test_completed_advance_command_replay_returns_cached_response_without_model_calls(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_completed(fact_ids)])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        first = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-replay-first-trace",
            idempotency_key="advance-public-replay",
        )
        second = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-replay-retry-trace",
            idempotency_key="advance-public-replay",
        )

    assert first == second
    assert gateway.calls == [SyndromeDraft, FormulaDraft]


async def test_stale_state_version_rejects_before_reasoning_model_call(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, _ = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        with pytest.raises(InvalidStateVersionError):
            await _run_langgraph_advance(
                db,
                session,
                session_id=str(session_id),
                state_version=0,
                trace_id="l4-4-state-conflict",
            )

    assert gateway.calls == []


async def test_unmapped_formula_missing_input_routes_to_manual_required(
    store: tuple[PostgresDomainRepository, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = store
    session_id, _, fact_ids = await _ready_session(factory)
    gateway = _ReasoningFakeGateway([_syndrome_completed(fact_ids), _formula_needs_more_info("constitution details")])
    _install_gateway(monkeypatch, gateway)

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        response = await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=1,
            trace_id="l4-4-manual",
        )

    assert response["current_stage"] == "blocked"
    assert response["blocked_reason"] == "reasoning_manual_required"
    async with factory() as db:
        formula_count = await db.scalar(
            select(func.count()).select_from(ArtifactRevision).where(ArtifactRevision.artifact_type == "formula_draft")
        )
        assert formula_count == 0
