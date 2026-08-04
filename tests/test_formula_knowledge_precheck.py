"""开方预检（知识库未收录药 → 修正提示重试）单元测试。"""

from __future__ import annotations

import uuid

from app.schemas.formula import FormulaDraftInput
from app.services.langgraph_reasoning import _herb_correction_hint


def test_herb_correction_hint_mentions_unknown_and_suggestion() -> None:
    hint = _herb_correction_hint(("紫苏子",))
    assert "紫苏子" in hint
    assert "苏子" in hint
    assert "知识库" in hint
    multi = _herb_correction_hint(("紫苏子", "生石膏"))
    assert "、".join(("紫苏子", "生石膏")) in multi


def test_formula_input_accepts_knowledge_correction() -> None:
    session_id = uuid.uuid4()
    payload = FormulaDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_FORMULA",
        policy_version="formula-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=_gate_schema(),
        completeness_gate=_gate_schema(),
        syndrome_draft=_syndrome(),
        knowledge_correction="以下药名不在系统中药知识库中：紫苏子。",
    )
    assert payload.knowledge_correction is not None
    assert "紫苏子" in payload.knowledge_correction


def test_formula_context_message_contains_knowledge_correction() -> None:
    from app.agents.formula_draft import build_formula_context

    session_id = uuid.uuid4()
    payload = FormulaDraftInput(
        session_id=session_id,
        state_version=1,
        current_stage="READY_FOR_FORMULA",
        policy_version="formula-draft-policy.no-rag.v1",
        domain_state=_domain(session_id),
        triage_gate=_gate_schema(),
        completeness_gate=_gate_schema(),
        syndrome_draft=_syndrome(),
        knowledge_correction="紫苏子未收录，请改为苏子",
    )
    packet, _ = build_formula_context(payload)
    context_message = next(
        (m.content for m in packet.messages if m.role == "context"),
        "",
    )
    assert "紫苏子未收录" in context_message


def _domain(sid: uuid.UUID):
    from datetime import UTC, datetime

    from app.agent_runtime.reducer import DomainState
    from app.schemas.domain import ObservationSchema, ObservationStatus

    return DomainState(
        session_id=sid,
        state_version=1,
        observations=(
            ObservationSchema(
                observation_id=uuid.uuid4(), session_id=sid,
                fact_key="chief_complaint.symptom", value="头痛",
                status=ObservationStatus.ACTIVE, confidence=0.9,
                source_message_id=uuid.uuid4(), created_at=datetime.now(UTC),
            ),
        ),
    )


def _gate_schema():
    from app.schemas.domain import GateResultSchema

    return GateResultSchema(
        gate_name="triage", policy_version="triage-red-flag.v1",
        input_state_version=1, decision="passed",
        details={"rules": [], "rule_ids": [], "risk_level": "none",
                 "disposition": "continue", "candidate_count": 0,
                 "category_counts": {}, "source_message_ids": []},
    )


def _syndrome():
    from app.schemas.syndrome import SyndromeDraft

    return SyndromeDraft(
        decision="completed", syndrome="风寒表实证",
        treatment_principle="解表散寒", confidence=0.8,
        syndrome_basis=(), differential=(),
        evidence_mode="model_knowledge_only", review_required=True,
        missing_inputs=(),
    )
