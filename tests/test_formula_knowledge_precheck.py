"""开方预检（知识库未收录药 → 修正提示重试）单元测试。"""

from __future__ import annotations

import uuid

import pytest

from app.schemas.formula import FormulaDraftInput
from app.services.langgraph_reasoning import _herb_correction_hint


def test_model_schema_failures_are_retryable() -> None:
    """0d-2 回归（REAL-SESSION d384ff26）：模型输出解析失败属随机质量问题，
    开方阶段必须纳入重试预算，否则一次解析失败就 manual_required 崩死。

    覆盖三类解析/输出失败码：chat_structured 解析失败、截断、以及
    runtime 对有效响应做 output_schema.model_validate 失败。
    """
    from app.services.langgraph_reasoning import _reasoning_failure_is_retryable

    assert _reasoning_failure_is_retryable("STRUCTURED_OUTPUT_INVALID")
    assert _reasoning_failure_is_retryable("OUTPUT_SCHEMA_INVALID")
    assert _reasoning_failure_is_retryable("MODEL_OUTPUT_TRUNCATED")
    # 未声明的码 / None 不得误入重试集合。
    assert not _reasoning_failure_is_retryable("NONEXISTENT_CODE")
    assert not _reasoning_failure_is_retryable(None)


def test_failed_consistency_report_placeholder_is_valid() -> None:
    """0d-2 回归（REAL-SESSION d384ff26）：模型执行失败的重试结果占位 report
    必须满足 FormulaConsistencyReport 全部必填字段与其 outcome_is_derived 校验。

    此前直接 FormulaConsistencyReport(passed=False, failure_code=None) 缺 4 个
    必填字段 → ValidationError 逃逸 → GraphRunnerError → advance 503 崩死。
    """
    from app.services.langgraph_reasoning import _failed_consistency_report

    report = _failed_consistency_report()
    assert report.passed is False
    assert report.requires_human is True
    assert report.failure_code is not None
    assert len(report.checks) >= 10


def test_herb_correction_hint_mentions_unknown_and_suggestion() -> None:
    hint = _herb_correction_hint(("紫苏子",))
    assert "紫苏子" in hint
    assert "苏子" in hint
    assert "知识库" in hint
    multi = _herb_correction_hint(("紫苏子", "生石膏"))
    assert "、".join(("紫苏子", "生石膏")) in multi


def test_unit_correction_hint_forces_grams() -> None:
    """剂量单位修正提示必须明确要求克（g），并给出计数药换算示例。"""
    from app.services.langgraph_reasoning import _formula_unit_correction_hint

    hint = _formula_unit_correction_hint()
    assert "克" in hint
    assert "g" in hint
    # 明确禁止非克单位
    for forbidden in ("枚", "个", "片", "条", "适量", "少许"):
        assert forbidden in hint
    # 给出可执行的换算示例（大枣 3 枚 → 克）
    assert "大枣" in hint


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


def test_authoritative_input_preserves_knowledge_correction_and_review_feedback() -> None:
    """3.1 回归：_authoritative_input 重建输入时不得丢弃修正提示/复核反馈。

    REAL-SESSION 342f70ae/cb5fe635 复盘：_authoritative_input 在
    build_formula_context 之前执行，此前它丢弃 knowledge_correction 与
    review_feedback → 重试路径注入的提示从未到达模型，每次重试都是盲重放。
    """
    from app.agent_runtime.repository import ReasoningAuthoritySnapshot
    from app.agents.formula_draft import _authoritative_input

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
        knowledge_correction="半夏剂量超限，请降至 9g 以内",
        review_feedback="医师反馈：患者脾胃虚弱，酌减燥湿药",
    )
    authority = ReasoningAuthoritySnapshot(
        session_id=session_id,
        current_state_version=1,
        current_stage="syndrome",
        session_status="active",
        agent_runtime="langgraph",
        domain_state=payload.domain_state,
        source_gate_id=uuid.uuid4(),
        source_gate_state_version=1,
        triage_gate=payload.triage_gate,
        completeness_gate=payload.completeness_gate,
        intake_graph_run_id=uuid.uuid4(),
    )
    rebuilt = _authoritative_input(payload, authority, payload.syndrome_draft)
    assert rebuilt.knowledge_correction == payload.knowledge_correction
    assert rebuilt.review_feedback == payload.review_feedback


def test_safety_violations_in_candidate_extracts_blocking_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    """3.1：候选方超剂量 → draft-time 安全预检返回 issues.suggestion 修正提示。"""
    import asyncio
    from types import SimpleNamespace

    from app.schemas.domain import CollectionStatus, SafetyProfileSchema
    from app.schemas.formula import FormulaDraftDecision
    from app.services.langgraph_reasoning import _safety_violations_in_candidate

    session_id = uuid.uuid4()
    domain = _domain(session_id)
    profile = SafetyProfileSchema(
        session_id=session_id,
        allergy_collection_status=CollectionStatus.EXPLICITLY_NONE,
        pregnancy_collection_status=CollectionStatus.EXPLICITLY_NONE,
        lactation_collection_status=CollectionStatus.EXPLICITLY_NONE,
        medications_collection_status=CollectionStatus.EXPLICITLY_NONE,
        major_conditions_collection_status=CollectionStatus.EXPLICITLY_NONE,
        contraindications_collection_status=CollectionStatus.EXPLICITLY_NONE,
    )
    state_with_profile = domain.model_copy(update={"safety_profile": profile})

    repo = SimpleNamespace(get_state=None)

    async def _fake_get_state(*_a, **_k):
        return state_with_profile

    repo.get_state = _fake_get_state

    # 候选方：半夏 10g 超上限 → 安全引擎应返回 HIGH 拦截。
    candidate = SimpleNamespace(
        name="香砂六君子汤",
        composition=(SimpleNamespace(herb="半夏", dose=10.0, unit="g", note=None),),
        rationale="test",
    )
    output = SimpleNamespace(decision=FormulaDraftDecision.COMPLETED, candidate_formula=candidate)

    class _FakeSafetyResult:
        passed = False
        issues = [
            SimpleNamespace(
                type="dose_limit",
                severity="high",
                herbs=["半夏"],
                rule_source="《中国药典》",
                suggestion="「半夏」剂量 10.0g 超过上限 9.0g（一般超量）。请调整剂量。",
            )
        ]

    class _FakeSafetyEngine:
        def __init__(self, _db):
            pass

        async def evaluate(self, formula, patient_info):
            return _FakeSafetyResult()

    # _safety_violations_in_candidate 在函数内 `from app.safety.engine import SafetyRuleEngine`，
    # 因此 patch 其定义模块；get_session_factory 是模块级 import，patch langgraph_reasoning。
    monkeypatch.setattr("app.safety.engine.SafetyRuleEngine", _FakeSafetyEngine)

    class _FakeSessionFactory:
        def __init__(self):
            self._ctx = None

        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(
        "app.services.langgraph_reasoning.get_session_factory",
        _FakeSessionFactory(),
    )

    hints = asyncio.run(_safety_violations_in_candidate(output, repo, session_id))
    assert hints
    assert any("半夏" in h and "超过上限" in h for h in hints)


def test_safety_violations_skipped_when_no_candidate() -> None:
    """3.1：非 completed / 无 candidate 时降级为空（safety 硬门禁兜底）。"""
    import asyncio
    from types import SimpleNamespace

    from app.schemas.formula import FormulaDraftDecision
    from app.services.langgraph_reasoning import _safety_violations_in_candidate

    # 非 completed
    out = SimpleNamespace(decision=FormulaDraftDecision.ABSTAINED, candidate_formula=None)
    hints = asyncio.run(_safety_violations_in_candidate(out, SimpleNamespace(), uuid.uuid4()))
    assert hints == ()

    # completed 但无 candidate
    out2 = SimpleNamespace(decision=FormulaDraftDecision.COMPLETED, candidate_formula=None)
    hints2 = asyncio.run(_safety_violations_in_candidate(out2, SimpleNamespace(), uuid.uuid4()))
    assert hints2 == ()


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
