"""2.8 两阶段开方 E2E：syndrome → base_formula → modification 三次模型调用。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent_runtime.runtime import AgentRuntime
from app.api.advance import _run_langgraph_advance as _production_run_langgraph_advance
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import GateResult, GraphRun, Observation
from app.schemas.formula import (
    BaseFormulaDraft,
    FormulaComposition,
    FormulaDraftDecision,
    FormulaFactClaim,
    FormulaModification,
    HerbItem,
    ModificationAction,
    ModificationDraft,
)
from app.schemas.syndrome import SyndromeDraft, SyndromeDraftDecision, SyndromeFactClaim
from app.services import langgraph_reasoning as reasoning_module
from tests._database_safety import destructive_database_environment

pytestmark = pytest.mark.integration


async def _run_langgraph_advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicitly opt direct integration calls into request-local runtime."""
    kwargs["allow_request_local_runtime"] = True
    return await _production_run_langgraph_advance(*args, **kwargs)


@pytest.fixture(scope="module")
def two_stage_db() -> str:
    from alembic import command
    from alembic.config import Config

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


@pytest_asyncio.fixture
async def two_stage_store(two_stage_db: str):
    from app.db.session import _build_async_pg_url, reset_session_factory

    await reset_session_factory()
    engine = create_async_engine(_build_async_pg_url(two_stage_db), pool_size=3, max_overflow=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE domain_command_commits, outbox_events, gate_results, graph_run_steps, "
                "artifact_revision_payloads, artifact_revisions, graph_runs, safety_rule_runs, "
                "safety_profiles, observations, consult_messages, consult_sessions, "
                "herbs, dosage_units CASCADE"
            )
        )
    reasoning_module._SYNDROME_RESULT_CACHE.clear()
    reasoning_module._FORMULA_ROUTE_CACHE.clear()
    try:
        yield factory
    finally:
        reasoning_module._SYNDROME_RESULT_CACHE.clear()
        reasoning_module._FORMULA_ROUTE_CACHE.clear()
        await reset_session_factory()
        await engine.dispose()


class _TwoStageGateway:
    def __init__(self, syndrome: object, base: object, modification: object) -> None:
        self.by_schema = {}
        self.calls: list[str] = []

    async def chat_structured(self, messages, output_schema, **kwargs):
        from app.schemas.formula import BaseFormulaDraft, ModificationDraft
        from app.schemas.syndrome import SyndromeDraft

        if output_schema is SyndromeDraft:
            self.calls.append("syndrome")
        elif output_schema is BaseFormulaDraft:
            self.calls.append("base")
        elif output_schema is ModificationDraft:
            self.calls.append("modification")
        else:
            raise AssertionError(f"unexpected output_schema: {output_schema}")
        return self.by_schema[output_schema]

    async def chat_structured_observed(self, messages, output_schema, **kwargs):
        from app.core.gateway import ModelTokenUsage, StructuredChatResponse

        requested_model = kwargs.get("model")
        output = await self.chat_structured(messages, output_schema, **kwargs)
        return StructuredChatResponse(
            output=output,
            model_actual=requested_model if isinstance(requested_model, str) else None,
            usage=ModelTokenUsage(),
        )


def _syndrome(fact_ids: tuple[uuid.UUID, ...]) -> SyndromeDraft:
    return SyndromeDraft(
        decision=SyndromeDraftDecision.COMPLETED,
        syndrome="外感风寒证",
        syndrome_basis=(SyndromeFactClaim(claim="风寒束表", fact_ids=fact_ids),),
        differential=(),
        treatment_principle="辛温解表",
        confidence=0.45,
        evidence_mode="model_knowledge_only",
        claim_evidence_links=(),
        missing_inputs=(),
        review_required=True,
    )


def _base(fact_ids: tuple[uuid.UUID, ...]) -> BaseFormulaDraft:
    return BaseFormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=FormulaComposition(
            name="麻黄汤",
            composition=(
                HerbItem(herb="麻黄", dose=9),
                HerbItem(herb="桂枝", dose=6),
                HerbItem(herb="杏仁", dose=9),
                HerbItem(herb="甘草", dose=3),
            ),
            rationale="解表散寒",
            basis=(FormulaFactClaim(claim="风寒束表", fact_ids=fact_ids),),
        ),
        rationale="解表散寒",
        confidence=0.45,
        evidence_mode="model_knowledge_only",
    )


def _modification(fact_ids: tuple[uuid.UUID, ...]) -> ModificationDraft:
    return ModificationDraft(
        decision=FormulaDraftDecision.COMPLETED,
        rationale="去桂加姜，调和营卫",
        modifications=(
            FormulaModification(
                action=ModificationAction.REMOVE, herb="桂枝", reason="去桂", basis=FormulaFactClaim(claim="x", fact_ids=fact_ids)
            ),
            FormulaModification(
                action=ModificationAction.ADD, herb="生姜", dose=9, reason="加姜", basis=FormulaFactClaim(claim="y", fact_ids=fact_ids)
            ),
        ),
        confidence=0.45,
        evidence_mode="model_knowledge_only",
    )


async def _ready_session(factory):
    from app.agent_runtime.completeness_policy import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
    from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
    from app.agent_runtime.triage_policy import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION
    from app.schemas.domain import GateDecision

    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    intake_run_id = uuid.uuid4()
    triage_gate_id = uuid.uuid4()
    completeness_gate_id = uuid.uuid4()
    snapshot: dict[str, Any] = {
        "agent_runtime": "langgraph",
        "current_stage": "syndrome",
        "state_version": 2,
        "advance": {
            "source_gate_id": str(completeness_gate_id),
            "source_gate_state_version": 1,
            "trace_id": "setup",
        },
    }
    facts = (
        ("chief_complaint.symptom", "感冒一周"),
        ("present_illness.change", "逐渐加重"),
        ("ten_questions.cold_heat", "怕冷"),
        ("ten_questions.stool_urine", "正常"),
    )
    observations = tuple(
        Observation(
            id=uuid.uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            normalized_value=value,
            source_message_id=message_id,
            status="active",
            confidence=0.95,
        )
        for key, value in facts
    )
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage="syndrome",
                status="active",
                agent_runtime="langgraph",
                state_version=2,
                state_snapshot=snapshot,
            )
        )
        db.add(
            ConsultMessage(
                id=message_id, session_id=session_id, role="patient_proxy",
                stage="inquiry", content="structured test input",
            )
        )
        db.add_all(observations)
        db.add(
            GraphRun(
                id=intake_run_id, session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION, command_id="message:ready",
                input_state_version=1, status="completed", completed_at=func.now(),
            )
        )
        await db.flush()
        db.add(
            GateResult(
                id=triage_gate_id, session_id=session_id, graph_run_id=intake_run_id,
                gate_name=TRIAGE_GATE_NAME, policy_version=TRIAGE_POLICY_VERSION,
                input_state_version=1, decision=GateDecision.PASSED.value,
                details={"disposition": "continue", "candidate_count": 0, "rule_ids": []},
            )
        )
        db.add(
            GateResult(
                id=completeness_gate_id, session_id=session_id, graph_run_id=intake_run_id,
                gate_name=COMPLETENESS_GATE_NAME, policy_version=COMPLETENESS_POLICY_VERSION,
                input_state_version=1, decision=GateDecision.PASSED.value,
                details={"disposition": "ready", "missing_required": [], "rule_ids": []},
            )
        )
    return session_id, tuple(item.id for item in observations)


@pytest.mark.asyncio
async def test_two_stage_formula_pipeline(
    two_stage_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两阶段开方 e2e：syndrome → base_formula → modification 三次模型调用，
    最终 formula artifact 含权威 base + 确定性合成的 candidate。"""
    from app.agent_runtime.repository import PostgresDomainRepository
    from app.agents.formula_draft import (
        build_base_formula_agent_spec,
        build_modification_draft_agent_spec,
    )
    from app.agents.syndrome_draft import build_syndrome_agent_spec
    from app.schemas.formula import BaseFormulaDraft, ModificationDraft
    from app.schemas.syndrome import SyndromeDraft

    factory = two_stage_store
    session_id, fact_ids = await _ready_session(factory)
    gateway = _TwoStageGateway(None, None, None)
    gateway.by_schema = {
        SyndromeDraft: _syndrome(fact_ids),
        BaseFormulaDraft: _base(fact_ids),
        ModificationDraft: _modification(fact_ids),
    }
    monkeypatch.setattr(
        reasoning_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway, recorder=None),
    )
    monkeypatch.setattr(reasoning_module, "_rag_retriever", lambda: None)  # 空证据降级
    monkeypatch.setattr(reasoning_module, "build_syndrome_agent_spec", build_syndrome_agent_spec)
    monkeypatch.setattr(reasoning_module, "build_base_formula_agent_spec", build_base_formula_agent_spec)
    monkeypatch.setattr(
        reasoning_module, "build_modification_draft_agent_spec", build_modification_draft_agent_spec
    )

    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        assert session is not None
        await _run_langgraph_advance(
            db,
            session,
            session_id=str(session_id),
            state_version=2,
            trace_id="two-stage-e2e",
        )

    assert gateway.calls == ["syndrome", "base", "modification"], gateway.calls

    repository = PostgresDomainRepository(factory)
    record = await repository.get_artifact_payload(
        session_id,
        artifact_type="formula_draft",
        artifact_id=reasoning_module._artifact_id(session_id, "formula_draft"),
        status="current",
    )
    assert record is not None
    payload = record.payload
    assert payload["output"]["decision"] == "completed"
    base = payload["output"]["base_formula"]
    assert base["name"] == "麻黄汤"
    candidate = payload["output"]["candidate_formula"]
    herbs = [item["herb"] for item in candidate["composition"]]
    assert herbs == ["麻黄", "杏仁", "甘草", "生姜"]
