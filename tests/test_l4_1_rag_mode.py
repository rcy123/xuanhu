"""L4-1 RAG 模式测试：policy/prompt 配对、证据契约分派、检索集成与空证据降级。

覆盖范围（阶段一 D1-D4）：
- preflight 只认 (policy_version, prompt_version) 合法配对，run_spec 与 input 同 policy
- rag 契约：claim_evidence_links ⊆ evidence_ids（防幻觉引用）、confidence 按证据空否封顶
- no-rag 契约不变：evidence_ids 必须空、links 必须空、confidence ≤ 0.65
- build_syndrome_context 按 policy 注入 retrieved_evidence（untrusted context 层）
- execute 带 FakeRetriever：检索成功填 evidence_ids；检索异常/缺省 → 空证据降级
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.agent_runtime.reducer import DomainState
from app.agent_runtime.repository import ReasoningAuthoritySnapshot
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.specs import RunArtifact, RunSpec
from app.agent_runtime.syndrome_verifier import (
    SYNDROME_AGENT_VERSION,
    SYNDROME_PROMPT_VERSION,
    SYNDROME_RAG_AGENT_NAME,
    SYNDROME_RAG_PROMPT_VERSION,
    SyndromeGateAuthority,
    SyndromeVerificationFailureCode,
    validate_syndrome_preflight,
    verify_syndrome_artifact,
)
from app.agents.prompt_loader import PromptLoader
from app.agents.syndrome_draft import (
    SyndromeExecutionStatus,
    _consume_trusted_syndrome_execution,
    build_syndrome_agent_spec,
    build_syndrome_context,
    execute_syndrome_draft,
)
from app.rag.schemas import Evidence
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.schemas.domain import (
    CollectionStatus,
    GateDecision,
    GateResultSchema,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from app.schemas.syndrome import (
    SYNDROME_EVIDENCE_MODE,
    SYNDROME_NO_RAG_CONFIDENCE_MAX,
    SYNDROME_POLICY_VERSION,
    SYNDROME_RAG_CONFIDENCE_MAX,
    SYNDROME_RAG_EVIDENCE_MODE,
    SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX,
    SYNDROME_RAG_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeClaimEvidenceLink,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
    SyndromeFactClaim,
    SyndromeObservationContext,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION

MANIFEST = Path(__file__).parents[1] / "app" / "agents" / "prompts" / "manifest.yaml"


class FakeGateway:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self, messages: list[dict[str, Any]], output_schema: type[BaseModel], **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": messages, "output_schema": output_schema, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRetriever:
    """返回固定证据或抛异常的假检索器。"""

    def __init__(self, evidence: list[Evidence] | None = None, error: Exception | None = None) -> None:
        self.evidence = evidence
        self.error = error
        self.queries: list[tuple[str, list[str]]] = []

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        self.queries.append((query, primary_sources))
        if self.error is not None:
            raise self.error
        return list(self.evidence or ())


class FakeGateRepository:
    def __init__(self, gates: tuple[GateResultSchema, ...], domain_state: DomainState) -> None:
        self.gates = gates
        self.domain_state = domain_state

    async def get_reasoning_authority(
        self,
        session_id: uuid.UUID,
        state_version: int,
    ) -> ReasoningAuthoritySnapshot | None:
        if self.domain_state.session_id != session_id or self.domain_state.state_version != state_version:
            return None
        triage = tuple(gate for gate in self.gates if gate.gate_name == TRIAGE_GATE_NAME)
        completeness = tuple(gate for gate in self.gates if gate.gate_name == COMPLETENESS_GATE_NAME)
        if len(triage) != 1 or len(completeness) != 1:
            return None
        return ReasoningAuthoritySnapshot(
            session_id=session_id,
            current_state_version=state_version,
            current_stage="syndrome",
            session_status="active",
            agent_runtime="langgraph",
            domain_state=self.domain_state,
            source_gate_id=uuid.uuid4(),
            source_gate_state_version=completeness[0].input_state_version,
            triage_gate=triage[0],
            completeness_gate=completeness[0],
            intake_graph_run_id=uuid.uuid4(),
            advance_run_id=None,
        )


def _run(
    *,
    state_version: int = 3,
    prompt_version: str = SYNDROME_PROMPT_VERSION,
    policy_version: str = SYNDROME_POLICY_VERSION,
    session_id: uuid.UUID | None = None,
) -> RunSpec:
    return RunSpec(
        run_id=uuid.uuid4(),
        session_id=session_id or uuid.uuid4(),
        state_version=state_version,
        stage=SYNDROME_READY_STAGE,
        agent_spec_version=SYNDROME_AGENT_VERSION,
        prompt_version=prompt_version,
        policy_version=policy_version,
        deadline_at=datetime.now(UTC) + timedelta(seconds=2),
        total_attempt_budget=1,
        idempotency_key="l4-1-rag-command",
        trace_id="l4-1-rag-trace",
    )


def _observation(session_id: uuid.UUID, fact_key: str, value: Any) -> ObservationSchema:
    return ObservationSchema(
        observation_id=uuid.uuid4(),
        session_id=session_id,
        fact_key=fact_key,
        value=value,
        normalized_value=value,
        source_message_id=uuid.uuid4(),
        status=ObservationStatus.ACTIVE,
        confidence=0.95,
        supersedes_observation_id=None,
        created_at=datetime.now(UTC),
    )


def _gate(name: str, version: str, state_version: int, decision: GateDecision, details: dict[str, Any]) -> GateResultSchema:
    return GateResultSchema(
        gate_name=name,
        policy_version=version,
        input_state_version=state_version,
        decision=decision,
        details=details,
    )


def _ready_triage_gate(state_version: int) -> GateResultSchema:
    return _gate(
        TRIAGE_GATE_NAME,
        TRIAGE_POLICY_VERSION,
        state_version,
        GateDecision.PASSED,
        {"disposition": "continue", "candidate_count": 0, "rule_ids": [], "rules": []},
    )


def _ready_completeness_gate(state_version: int) -> GateResultSchema:
    return _gate(
        COMPLETENESS_GATE_NAME,
        COMPLETENESS_POLICY_VERSION,
        state_version,
        GateDecision.PASSED,
        {"disposition": "ready"},
    )


def _ready_safety(session_id: uuid.UUID) -> SafetyProfileSchema:
    return SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
        pregnancy_collection_status=CollectionStatus.EXPLICITLY_NONE,
        lactation_collection_status=CollectionStatus.EXPLICITLY_NONE,
        medications_collection_status=CollectionStatus.EXPLICITLY_NONE,
        major_conditions_collection_status=CollectionStatus.EXPLICITLY_NONE,
        contraindications_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )


def _input(
    *,
    session_id: uuid.UUID | None = None,
    state_version: int = 3,
    policy_version: str = SYNDROME_RAG_POLICY_VERSION,
    observations: tuple[ObservationSchema, ...] | None = None,
) -> SyndromeDraftInput:
    sid = session_id or uuid.uuid4()
    obs = observations or (
        _observation(sid, "chief_complaint.symptom", "咳嗽三天，痰白稀"),
        _observation(sid, "chief_complaint.course", "三天"),
        _observation(sid, "ten_questions.cold_heat", "怕冷，微热"),
        _observation(sid, "ten_questions.sweat", "无汗"),
        _observation(sid, "present_illness.cough", "咳嗽"),
    )
    context = tuple(
        SyndromeObservationContext(
            observation_id=item.observation_id,
            session_id=item.session_id,
            state_version=state_version,
            fact_key=item.fact_key,
            value=item.value,
            normalized_value=item.normalized_value,
            status=ObservationStatus.ACTIVE,
        )
        for item in obs
    )
    return SyndromeDraftInput(
        session_id=sid,
        state_version=state_version,
        current_stage=SYNDROME_READY_STAGE,
        policy_version=policy_version,
        domain_state=DomainState(
            session_id=sid,
            state_version=state_version,
            observations=obs,
            safety_profile=_ready_safety(sid),
        ),
        triage_gate=_ready_triage_gate(state_version),
        completeness_gate=_ready_completeness_gate(state_version),
        context_observations=context,
    )


def _draft(
    input_payload: SyndromeDraftInput,
    *,
    evidence_mode: str = SYNDROME_RAG_EVIDENCE_MODE,
    confidence: float = 0.8,
    links: tuple[tuple[str, str], ...] = (("辨证主张", "ev-1"),),
) -> SyndromeDraft:
    all_ids = tuple(item.observation_id for item in input_payload.context_observations)
    return SyndromeDraft(
        decision=SyndromeDraftDecision.COMPLETED,
        syndrome="风寒束肺证",
        syndrome_basis=(SyndromeFactClaim(claim="咳嗽怕冷支持风寒束肺", fact_ids=all_ids),),
        differential=(),
        treatment_principle="疏风散寒，宣肺止咳",
        confidence=confidence,
        evidence_mode=evidence_mode,
        claim_evidence_links=tuple(
            SyndromeClaimEvidenceLink(claim=claim, evidence_id=evidence_id) for claim, evidence_id in links
        ),
        missing_inputs=(),
        review_required=True,
    )


def _artifact(output: BaseModel, run: RunSpec, *, evidence_ids: tuple[str, ...] = ()) -> RunArtifact:
    return RunArtifact(
        output=output,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run.trace_id,
        run_id=run.run_id,
        agent_spec_version=SYNDROME_AGENT_VERSION,
        prompt_version=run.prompt_version,
        evidence_ids=evidence_ids,
    )


def _gate_authority(payload: SyndromeDraftInput) -> SyndromeGateAuthority:
    return SyndromeGateAuthority(triage_gate=payload.triage_gate, completeness_gate=payload.completeness_gate)


def _evidence(evidence_id: str, *, source_type: str = "theory", score: float = 0.9, rank: int = 1) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id=str(uuid.uuid4()),
        title=f"证据-{evidence_id}",
        content_snippet="脾虚湿困，食少便溏……",
        score=score,
        rank=rank,
    )


async def _execute(
    input_payload: SyndromeDraftInput,
    output: Any,
    *,
    run: RunSpec | None = None,
    retriever: FakeRetriever | None = None,
) -> tuple[Any, FakeGateway]:
    actual_run = run or _run(session_id=input_payload.session_id, state_version=input_payload.state_version)
    gateway = FakeGateway([output])
    repository = FakeGateRepository(
        (input_payload.triage_gate, input_payload.completeness_gate),
        input_payload.domain_state,
    )
    result = await execute_syndrome_draft(
        runtime=AgentRuntime(gateway, recorder=None),
        repository=repository,
        run_spec=actual_run,
        input_payload=input_payload,
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        prompt_loader=PromptLoader(MANIFEST),
        retriever=retriever,
    )
    return result, gateway


# ---------------------------------------------------------------------------
# preflight：policy/prompt 配对
# ---------------------------------------------------------------------------


def test_rag_preflight_accepts_rag_pair() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    spec = build_syndrome_agent_spec(model="fake-model")
    assert validate_syndrome_preflight(spec, run, payload, _gate_authority(payload)) is None


def test_rag_preflight_rejects_prompt_policy_cross() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    spec = build_syndrome_agent_spec(model="fake-model")
    assert (
        validate_syndrome_preflight(spec, run, payload, _gate_authority(payload))
        is SyndromeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    )


def test_rag_preflight_rejects_input_run_policy_mismatch() -> None:
    payload = _input()  # policy = rag
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_PROMPT_VERSION, policy_version=SYNDROME_POLICY_VERSION)
    spec = build_syndrome_agent_spec(model="fake-model")
    assert (
        validate_syndrome_preflight(spec, run, payload, _gate_authority(payload))
        is SyndromeVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    )


def test_no_rag_preflight_accepts_no_rag_pair() -> None:
    payload = _input(policy_version=SYNDROME_POLICY_VERSION)
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_PROMPT_VERSION, policy_version=SYNDROME_POLICY_VERSION)
    spec = build_syndrome_agent_spec(model="fake-model")
    assert validate_syndrome_preflight(spec, run, payload, _gate_authority(payload)) is None


# ---------------------------------------------------------------------------
# verifier：rag 证据契约
# ---------------------------------------------------------------------------


def test_rag_verify_accepts_links_within_evidence_ids() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=0.85)
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=("ev-1", "ev-2")),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert report.passed


def test_rag_verify_rejects_fabricated_evidence_link() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, links=(("辨证主张", "ev-99"),))
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=("ev-1",)),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert not report.passed
    assert report.failure_code is SyndromeVerificationFailureCode.EVIDENCE_LINK_FABRICATED


def test_rag_verify_rejects_confidence_above_limit_with_evidence() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=SYNDROME_RAG_CONFIDENCE_MAX + 0.01)
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=("ev-1",)),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert not report.passed
    assert report.failure_code is SyndromeVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT


def test_rag_verify_empty_evidence_degrades_passes() -> None:
    """空证据降级：links 必空 + confidence ≤ 0.5 才通过。"""
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=0.4, links=())
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=()),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert report.passed


def test_rag_verify_empty_evidence_rejects_overconfidence() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX + 0.05, links=())
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=()),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert not report.passed
    assert report.failure_code is SyndromeVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT


def test_rag_policy_rejects_model_knowledge_mode() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, evidence_mode=SYNDROME_EVIDENCE_MODE, confidence=0.4, links=())
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=()),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert not report.passed
    assert report.failure_code is SyndromeVerificationFailureCode.EVIDENCE_MODE_POLICY_MISMATCH


def test_no_rag_verify_rejects_evidence_ids() -> None:
    payload = _input(policy_version=SYNDROME_POLICY_VERSION)
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_PROMPT_VERSION, policy_version=SYNDROME_POLICY_VERSION)
    output = _draft(payload, evidence_mode=SYNDROME_EVIDENCE_MODE, confidence=0.6, links=())
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=("ev-1",)),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert not report.passed
    assert report.failure_code is SyndromeVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED


def test_no_rag_verify_accepts_no_rag_output() -> None:
    payload = _input(policy_version=SYNDROME_POLICY_VERSION)
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_PROMPT_VERSION, policy_version=SYNDROME_POLICY_VERSION)
    output = _draft(payload, evidence_mode=SYNDROME_EVIDENCE_MODE, confidence=SYNDROME_NO_RAG_CONFIDENCE_MAX, links=())
    report = verify_syndrome_artifact(
        agent_spec=build_syndrome_agent_spec(model="fake-model"),
        run_spec=run,
        artifact=_artifact(output, run, evidence_ids=()),
        input_payload=payload,
        gate_authority=_gate_authority(payload),
    )
    assert report.passed


# ---------------------------------------------------------------------------
# build_syndrome_context：证据注入
# ---------------------------------------------------------------------------


def test_build_context_rag_injects_evidence() -> None:
    payload = _input()
    evidence = (_evidence("ev-1"), _evidence("ev-2", rank=2))
    packet, version = build_syndrome_context(payload, prompt_loader=PromptLoader(MANIFEST), retrieved_evidence=evidence)
    assert version == SYNDROME_RAG_PROMPT_VERSION
    context_messages = [message for message in packet.messages if message.role in ("context", "user")]
    joined = "\n".join(str(message.content) for message in context_messages)
    assert SYNDROME_RAG_EVIDENCE_MODE in joined
    assert "ev-1" in joined and "ev-2" in joined
    assert "证据-ev-1" in joined


def test_build_context_no_rag_has_no_evidence_key() -> None:
    payload = _input(policy_version=SYNDROME_POLICY_VERSION)
    packet, version = build_syndrome_context(payload, prompt_loader=PromptLoader(MANIFEST))
    assert version == SYNDROME_PROMPT_VERSION
    context_messages = [message for message in packet.messages if message.role == "context"]
    joined = "\n".join(str(message.content) for message in context_messages)
    assert SYNDROME_EVIDENCE_MODE in joined
    assert "retrieved_evidence" not in joined


# ---------------------------------------------------------------------------
# execute：检索集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rag_with_retriever_persists_evidence_ids() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    retriever = FakeRetriever([_evidence("ev-1"), _evidence("ev-2", rank=2)])
    output = _draft(payload, confidence=0.85)
    result, _gateway = await _execute(payload, output, run=run, retriever=retriever)

    assert result.status is SyndromeExecutionStatus.SUCCEEDED
    trusted = _consume_trusted_syndrome_execution(result)
    assert trusted is not None
    assert trusted.artifact.evidence_ids == ("ev-1", "ev-2")
    assert tuple(ev.evidence_id for ev in trusted.retrieved_evidence) == ("ev-1", "ev-2")
    assert retriever.queries  # 确实执行了检索
    query, primary = retriever.queries[0]
    # P2: rag_query_rewrite_enabled=true 时 query 为医学叙事文本（如"患者咳嗽三天…"），
    # 否则为 key=value 格式（如"chief_complaint.symptom=咳嗽三天"）。两种都是合法检索 query。
    assert len(query) > 0 and isinstance(query, str)
    assert primary == ["theory", "case"]


@pytest.mark.asyncio
async def test_execute_rag_retriever_failure_degrades_empty() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    retriever = FakeRetriever(error=RuntimeError("milvus down"))
    output = _draft(payload, confidence=0.4, links=())
    result, _gateway = await _execute(payload, output, run=run, retriever=retriever)

    assert result.status is SyndromeExecutionStatus.SUCCEEDED
    trusted = _consume_trusted_syndrome_execution(result)
    assert trusted is not None
    assert trusted.artifact.evidence_ids == ()
    assert trusted.retrieved_evidence == ()
    # 降级路径 confidence 被封顶到 0.5 以内（0.4 原值未超限，保持不变）
    assert trusted.output.confidence <= SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX


@pytest.mark.asyncio
async def test_execute_rag_without_retriever_degrades_empty() -> None:
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=0.4, links=())
    result, _gateway = await _execute(payload, output, run=run, retriever=None)

    assert result.status is SyndromeExecutionStatus.SUCCEEDED
    trusted = _consume_trusted_syndrome_execution(result)
    assert trusted is not None
    assert trusted.artifact.evidence_ids == ()


@pytest.mark.asyncio
async def test_execute_rag_clamps_overconfident_no_evidence_output() -> None:
    """模型在空证据降级下仍输出 0.9 → 确定性封顶到 0.5 并通过。"""
    payload = _input()
    run = _run(session_id=payload.session_id, prompt_version=SYNDROME_RAG_PROMPT_VERSION, policy_version=SYNDROME_RAG_POLICY_VERSION)
    output = _draft(payload, confidence=0.9, links=())
    result, _gateway = await _execute(payload, output, run=run, retriever=None)

    assert result.status is SyndromeExecutionStatus.SUCCEEDED
    trusted = _consume_trusted_syndrome_execution(result)
    assert trusted is not None
    assert trusted.output.confidence == SYNDROME_RAG_NO_EVIDENCE_CONFIDENCE_MAX
