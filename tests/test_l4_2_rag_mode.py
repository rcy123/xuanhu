"""L4-2 Formula RAG 模式测试：preflight 配对 + 证据契约分派。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent_runtime.formula_verifier import (
    FORMULA_AGENT_VERSION,
    FORMULA_PROMPT_VERSION,
    FORMULA_RAG_PROMPT_VERSION,
    FormulaVerificationFailureCode,
    _verify_evidence_contract,
    validate_formula_preflight,
)
from app.agent_runtime.reducer import DomainState
from app.agent_runtime.specs import RunArtifact, RunSpec
from app.agent_runtime.syndrome_verifier import SYNDROME_AGENT_VERSION, SYNDROME_RAG_PROMPT_VERSION
from app.agents.formula_draft import build_formula_agent_spec
from app.agents.prompt_loader import PromptLoader
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
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FORMULA_NO_RAG_CONFIDENCE_MAX,
    FORMULA_POLICY_VERSION,
    FORMULA_RAG_CONFIDENCE_MAX,
    FORMULA_RAG_EVIDENCE_MODE,
    FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX,
    FORMULA_RAG_POLICY_VERSION,
    FORMULA_READY_STAGE,
    FormulaClaimEvidenceLink,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaDraftInput,
    FormulaFactClaim,
    HerbItem,
)
from app.schemas.syndrome import (
    SYNDROME_POLICY_VERSION,
    SYNDROME_RAG_POLICY_VERSION,
    SYNDROME_READY_STAGE,
    SyndromeDraft,
    SyndromeDraftDecision,
    SyndromeDraftInput,
    SyndromeFactClaim,
    SyndromeObservationContext,
)
from app.schemas.triage import TRIAGE_GATE_NAME, TRIAGE_POLICY_VERSION

MANIFEST = Path(__file__).parents[1] / "app" / "agents" / "prompts" / "manifest.yaml"


def _run(policy_version: str, prompt_version: str, session_id: uuid.UUID, state_version: int = 3) -> RunSpec:
    return RunSpec(
        run_id=uuid.uuid4(),
        session_id=session_id,
        state_version=state_version,
        stage=FORMULA_READY_STAGE,
        agent_spec_version=FORMULA_AGENT_VERSION,
        prompt_version=prompt_version,
        policy_version=policy_version,
        deadline_at=datetime.now(UTC) + timedelta(seconds=2),
        total_attempt_budget=1,
        idempotency_key="l4-2-rag-command",
        trace_id="l4-2-rag-trace",
    )


def _input(policy_version: str) -> FormulaDraftInput:
    sid = uuid.uuid4()
    obs = (
        ObservationSchema(
            observation_id=uuid.uuid4(),
            session_id=sid,
            fact_key="chief_complaint.symptom",
            value="咳嗽三天",
            normalized_value="咳嗽三天",
            source_message_id=uuid.uuid4(),
            status=ObservationStatus.ACTIVE,
            confidence=0.95,
            supersedes_observation_id=None,
            created_at=datetime.now(UTC),
        ),
    )
    context = tuple(
        SyndromeObservationContext(
            observation_id=item.observation_id,
            session_id=item.session_id,
            state_version=3,
            fact_key=item.fact_key,
            value=item.value,
            normalized_value=item.normalized_value,
            status=ObservationStatus.ACTIVE,
        )
        for item in obs
    )
    rag = policy_version == FORMULA_RAG_POLICY_VERSION
    syndrome = SyndromeDraft(
        decision=SyndromeDraftDecision.COMPLETED,
        syndrome="风寒束肺证",
        syndrome_basis=(SyndromeFactClaim(claim="咳嗽怕冷", fact_ids=(obs[0].observation_id,)),),
        differential=(),
        treatment_principle="疏风散寒",
        # rag 空证据降级合法组合（links 空 + confidence ≤ 0.5）
        confidence=0.4 if rag else 0.6,
        evidence_mode=FORMULA_RAG_EVIDENCE_MODE if rag else FORMULA_EVIDENCE_MODE,
        claim_evidence_links=(),
        missing_inputs=(),
        review_required=True,
    )
    triage = GateResultSchema(
        gate_name=TRIAGE_GATE_NAME,
        policy_version=TRIAGE_POLICY_VERSION,
        input_state_version=3,
        decision=GateDecision.PASSED,
        details={"disposition": "continue", "candidate_count": 0, "rule_ids": [], "rules": []},
    )
    completeness = GateResultSchema(
        gate_name=COMPLETENESS_GATE_NAME,
        policy_version=COMPLETENESS_POLICY_VERSION,
        input_state_version=3,
        decision=GateDecision.PASSED,
        details={"disposition": "ready"},
    )
    return FormulaDraftInput(
        session_id=sid,
        state_version=3,
        current_stage=FORMULA_READY_STAGE,
        policy_version=policy_version,
        domain_state=DomainState(
            session_id=sid,
            state_version=3,
            observations=obs,
            safety_profile=SafetyProfileSchema(
                session_id=sid,
                allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
                pregnancy_collection_status=CollectionStatus.EXPLICITLY_NONE,
                lactation_collection_status=CollectionStatus.EXPLICITLY_NONE,
                medications_collection_status=CollectionStatus.EXPLICITLY_NONE,
                major_conditions_collection_status=CollectionStatus.EXPLICITLY_NONE,
                contraindications_collection_status=CollectionStatus.EXPLICITLY_NONE,
            ),
        ),
        triage_gate=triage,
        completeness_gate=completeness,
        context_observations=context,
        syndrome_draft=syndrome,
    )


def _draft(
    *,
    evidence_mode: str = FORMULA_RAG_EVIDENCE_MODE,
    confidence: float = 0.8,
    links: tuple[tuple[str, str], ...] = (("选方主张", "ev-1"),),
) -> FormulaDraft:
    first = uuid.uuid4()
    comp = FormulaComposition(
        name="止嗽散",
        composition=(HerbItem(herb="紫菀", dose=10.0, unit="g"),),
        rationale="宣肺止咳",
        basis=(FormulaFactClaim(claim="咳嗽需宣肺", fact_ids=(first,)),),
    )
    return FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=comp,
        candidate_formula=comp,
        rationale="宣肺止咳",
        confidence=confidence,
        evidence_mode=evidence_mode,
        claim_evidence_links=tuple(
            FormulaClaimEvidenceLink(claim=claim, evidence_id=evidence_id) for claim, evidence_id in links
        ),
        missing_inputs=(),
        review_required=True,
    )


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="ev-1",
        source_type="formula",
        source_id="00000000-0000-0000-0000-000000000002",
        title="止嗽散",
        content_snippet="止嗽散：紫菀、百部……",
        score=0.9,
        rank=1,
    )


def _gate_authority(payload: FormulaDraftInput):
    from app.agent_runtime.formula_verifier import FormulaGateAuthority

    return FormulaGateAuthority(triage_gate=payload.triage_gate, completeness_gate=payload.completeness_gate)


def _syndrome_run_spec(policy_version: str, session_id: uuid.UUID, state_version: int = 3) -> RunSpec:
    prompt = SYNDROME_RAG_PROMPT_VERSION if policy_version == SYNDROME_RAG_POLICY_VERSION else "syndrome_draft_v1.jinja2"
    return RunSpec(
        run_id=uuid.uuid4(),
        session_id=session_id,
        state_version=state_version,
        stage=SYNDROME_READY_STAGE,
        agent_spec_version=SYNDROME_AGENT_VERSION,
        prompt_version=prompt,
        policy_version=policy_version,
        deadline_at=datetime.now(UTC) + timedelta(seconds=2),
        total_attempt_budget=1,
        idempotency_key="l4-1-command",
        trace_id="l4-1-trace",
    )


def _syndrome_artifact(draft: SyndromeDraft, run_spec: RunSpec) -> RunArtifact:
    return RunArtifact(
        output=draft,
        model_actual="fake-model",
        attempts=1,
        latency_ms=1,
        trace_id=run_spec.trace_id,
        run_id=run_spec.run_id,
        agent_spec_version=SYNDROME_AGENT_VERSION,
        prompt_version=run_spec.prompt_version,
    )


def _syndrome_input(payload: FormulaDraftInput) -> SyndromeDraftInput:
    rag = payload.policy_version == FORMULA_RAG_POLICY_VERSION
    return SyndromeDraftInput(
        schema_version="syndrome-draft-input.v1",
        session_id=payload.session_id,
        state_version=payload.state_version,
        current_stage=SYNDROME_READY_STAGE,
        policy_version=SYNDROME_RAG_POLICY_VERSION if rag else SYNDROME_POLICY_VERSION,
        domain_state=payload.domain_state,
        triage_gate=payload.triage_gate,
        completeness_gate=payload.completeness_gate,
        context_observations=payload.context_observations,
    )


def _preflight_authority(payload: FormulaDraftInput):
    """构造带上游 syndrome 权威的 preflight 参数。"""
    syn_policy = SYNDROME_RAG_POLICY_VERSION if payload.policy_version == FORMULA_RAG_POLICY_VERSION else SYNDROME_POLICY_VERSION
    syn_run = _syndrome_run_spec(syn_policy, payload.session_id, payload.state_version)
    return {
        "syndrome_artifact": _syndrome_artifact(payload.syndrome_draft, syn_run),
        "syndrome_run_spec": syn_run,
        "syndrome_input_payload": _syndrome_input(payload),
    }


# ---------------------------------------------------------------------------
# preflight 配对
# ---------------------------------------------------------------------------


def test_formula_preflight_accepts_rag_pair() -> None:
    payload = _input(FORMULA_RAG_POLICY_VERSION)
    run = _run(FORMULA_RAG_POLICY_VERSION, FORMULA_RAG_PROMPT_VERSION, payload.session_id)
    spec = build_formula_agent_spec(model="fake-model")
    assert validate_formula_preflight(spec, run, payload, _gate_authority(payload), **_preflight_authority(payload)) is None


def test_formula_preflight_rejects_prompt_policy_cross() -> None:
    payload = _input(FORMULA_RAG_POLICY_VERSION)
    run = _run(FORMULA_RAG_POLICY_VERSION, FORMULA_PROMPT_VERSION, payload.session_id)
    spec = build_formula_agent_spec(model="fake-model")
    assert (
        validate_formula_preflight(spec, run, payload, _gate_authority(payload), **_preflight_authority(payload))
        is FormulaVerificationFailureCode.RUN_PROVENANCE_MISMATCH
    )


def test_formula_preflight_accepts_no_rag_pair() -> None:
    payload = _input(FORMULA_POLICY_VERSION)
    run = _run(FORMULA_POLICY_VERSION, FORMULA_PROMPT_VERSION, payload.session_id)
    spec = build_formula_agent_spec(model="fake-model")
    assert validate_formula_preflight(spec, run, payload, _gate_authority(payload), **_preflight_authority(payload)) is None


# ---------------------------------------------------------------------------
# 证据契约分派
# ---------------------------------------------------------------------------


def test_formula_rag_contract_accepts_valid_links() -> None:
    output = _draft(confidence=0.85)
    assert _verify_evidence_contract(output, ("ev-1",), FORMULA_RAG_POLICY_VERSION) is None


def test_formula_rag_contract_rejects_fabricated_link() -> None:
    output = _draft(links=(("选方主张", "ev-99"),))
    assert (
        _verify_evidence_contract(output, ("ev-1",), FORMULA_RAG_POLICY_VERSION)
        is FormulaVerificationFailureCode.EVIDENCE_LINK_FABRICATED
    )


def test_formula_rag_contract_rejects_overconfidence() -> None:
    output = _draft(confidence=FORMULA_RAG_CONFIDENCE_MAX + 0.01)
    assert (
        _verify_evidence_contract(output, ("ev-1",), FORMULA_RAG_POLICY_VERSION)
        is FormulaVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    )


def test_formula_rag_empty_evidence_degrades_passes() -> None:
    output = _draft(confidence=0.4, links=())
    assert _verify_evidence_contract(output, (), FORMULA_RAG_POLICY_VERSION) is None


def test_formula_rag_empty_evidence_rejects_overconfidence() -> None:
    output = _draft(confidence=FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX + 0.05, links=())
    assert (
        _verify_evidence_contract(output, (), FORMULA_RAG_POLICY_VERSION)
        is FormulaVerificationFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    )


def test_formula_rag_policy_rejects_model_knowledge_mode() -> None:
    output = _draft(evidence_mode=FORMULA_EVIDENCE_MODE, confidence=0.4, links=())
    assert (
        _verify_evidence_contract(output, (), FORMULA_RAG_POLICY_VERSION)
        is FormulaVerificationFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
    )


def test_formula_no_rag_contract_rejects_evidence_ids() -> None:
    output = _draft(evidence_mode=FORMULA_EVIDENCE_MODE, confidence=0.6, links=())
    assert (
        _verify_evidence_contract(output, ("ev-1",), FORMULA_POLICY_VERSION)
        is FormulaVerificationFailureCode.NO_RAG_CONTRACT_VIOLATED
    )


def test_formula_no_rag_contract_accepts_no_rag_output() -> None:
    output = _draft(evidence_mode=FORMULA_EVIDENCE_MODE, confidence=FORMULA_NO_RAG_CONFIDENCE_MAX, links=())
    assert _verify_evidence_contract(output, (), FORMULA_POLICY_VERSION) is None


def test_formula_manifest_has_rag_prompt_registered() -> None:
    loader = PromptLoader(MANIFEST)
    template = loader.load("formula_draft_rag")
    assert template.prompt_version == FORMULA_RAG_PROMPT_VERSION
    assert "rag_retrieved" in template.content
    assert "claim_evidence_links" in template.content
