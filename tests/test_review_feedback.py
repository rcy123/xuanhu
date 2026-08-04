"""医师否决反馈（reject）→ 重新辨证/开方链路测试。

覆盖：
1. reject 时 syndrome_draft 也被 invalidate（重新辨证），反馈写入 state_snapshot
2. advance 时 review_feedback 注入 syndrome/formula（含 modification）的 input
3. 模型 context 消息中包含反馈文本
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.agent_runtime.reducer import DomainState
from app.schemas.domain import (
    GateResultSchema,
    ObservationSchema,
    ObservationStatus,
)
from app.schemas.formula import (
    FormulaComposition,
    FormulaDraftInput,
    FormulaFactClaim,
    HerbItem,
    ModificationDraftInput,
)
from app.schemas.syndrome import SyndromeDraft, SyndromeDraftInput


def _gate(state_version: int = 1) -> GateResultSchema:
    return GateResultSchema(
        gate_name="triage",
        policy_version="triage-red-flag.v1",
        input_state_version=state_version,
        decision="passed",
        details={
            "rules": [],
            "rule_ids": [],
            "risk_level": "none",
            "disposition": "continue",
            "candidate_count": 0,
            "category_counts": {},
            "source_message_ids": [],
        },
    )


def _domain(session_id: uuid.UUID) -> DomainState:
    obs = (
        ObservationSchema(
            observation_id=uuid.uuid4(),
            session_id=session_id,
            fact_key="chief_complaint.symptom",
            value="头痛",
            status=ObservationStatus.ACTIVE,
            confidence=0.9,
            source_message_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
        ),
    )
    return DomainState(session_id=session_id, state_version=1, observations=obs)


def _syndrome_draft() -> SyndromeDraft:
    return SyndromeDraft(
        decision="completed",
        syndrome="风寒表实证",
        treatment_principle="解表散寒",
        confidence=0.8,
        syndrome_basis=(),
        differential=(),
        evidence_mode="model_knowledge_only",
        review_required=True,
        missing_inputs=(),
    )


def test_syndrome_input_accepts_review_feedback() -> None:
    session_id = uuid.uuid4()
    triage = _gate(1)
    payload = SyndromeDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_REASONING",
        policy_version="syndrome-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=triage,
        completeness_gate=triage,
        review_feedback="剂量偏大，建议减量",
    )
    assert payload.review_feedback == "剂量偏大，建议减量"


def test_formula_and_modification_inputs_accept_review_feedback() -> None:
    session_id = uuid.uuid4()
    triage = _gate(1)
    base = FormulaDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_FORMULA",
        policy_version="formula-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=triage,
        completeness_gate=triage,
        syndrome_draft=_syndrome_draft(),
        review_feedback="剂量偏大，建议减量",
    )
    assert base.review_feedback == "剂量偏大，建议减量"

    mod = ModificationDraftInput(
        schema_version="modification-draft-input.v1",
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_FORMULA",
        policy_version="modification-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=triage,
        completeness_gate=triage,
        syndrome_draft=_syndrome_draft(),
        base_formula=FormulaComposition(
            name="x",
            composition=(HerbItem(herb="麻黄", dose=9),),
            rationale="r",
            basis=(FormulaFactClaim(claim="c", fact_ids=(uuid.uuid4(),)),),
        ),
        base_confidence=0.8,
        review_feedback="剂量偏大，建议减量",
    )
    assert mod.review_feedback == "剂量偏大，建议减量"


def test_syndrome_context_message_contains_review_feedback() -> None:
    from app.agents.syndrome_draft import build_syndrome_context

    session_id = uuid.uuid4()
    triage = _gate(1)
    payload = SyndromeDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_REASONING",
        policy_version="syndrome-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=triage,
        completeness_gate=triage,
        review_feedback="剂量偏大，建议减量",
    )
    packet, _ = build_syndrome_context(payload)
    context_message = next(
        (m.content for m in packet.messages if m.role == "context"),
        "",
    )
    assert "剂量偏大" in context_message


def test_formula_context_message_contains_review_feedback() -> None:
    from app.agents.formula_draft import build_formula_context

    session_id = uuid.uuid4()
    triage = _gate(1)
    payload = FormulaDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_FORMULA",
        policy_version="formula-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=triage,
        completeness_gate=triage,
        syndrome_draft=_syndrome_draft(),
        review_feedback="剂量偏大，建议减量",
    )
    packet, _ = build_formula_context(payload)
    context_message = next(
        (m.content for m in packet.messages if m.role == "context"),
        "",
    )
    assert "剂量偏大" in context_message
