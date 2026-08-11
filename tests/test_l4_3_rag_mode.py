"""L4-3 Formula Consistency RAG 模式测试：二次校验按 policy_version 分派。"""

from __future__ import annotations

import uuid

from app.agent_runtime.formula_consistency import (
    FormulaConsistencyFailureCode,
    _verify_evidence_contract,
)
from app.rag.schemas import Evidence
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FORMULA_NO_RAG_CONFIDENCE_MAX,
    FORMULA_RAG_CONFIDENCE_MAX,
    FORMULA_RAG_EVIDENCE_MODE,
    FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX,
    FORMULA_RAG_POLICY_VERSION,
    FormulaClaimEvidenceLink,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    HerbItem,
)


def _draft(
    *,
    evidence_mode: str = FORMULA_RAG_EVIDENCE_MODE,
    confidence: float = 0.8,
    links: tuple[tuple[str, str], ...] = (("选方主张", "ev-1"),),
) -> FormulaDraft:
    first = uuid.uuid4()
    comp = FormulaComposition(
        name="二陈汤",
        composition=(HerbItem(herb="半夏", dose=9.0, unit="g"),),
        rationale="燥湿化痰",
        basis=(FormulaFactClaim(claim="痰湿需燥湿", fact_ids=(first,)),),
    )
    return FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=comp,
        candidate_formula=comp,
        rationale="燥湿化痰",
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
        source_id="00000000-0000-0000-0000-000000000003",
        title="二陈汤",
        content_snippet="二陈汤：半夏、陈皮……",
        score=0.9,
        rank=1,
    )


def test_consistency_rag_contract_accepts_valid_links() -> None:
    output = _draft(confidence=0.85)
    assert _verify_evidence_contract(output, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset({"ev-1"})) is None


def test_consistency_rag_contract_rejects_fabricated_link() -> None:
    output = _draft(links=(("选方主张", "ev-99"),))
    assert (
        _verify_evidence_contract(output, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset({"ev-1"}))
        is FormulaConsistencyFailureCode.EVIDENCE_LINK_FABRICATED
    )


def test_consistency_rag_contract_rejects_overconfidence() -> None:
    output = _draft(confidence=FORMULA_RAG_CONFIDENCE_MAX + 0.01)
    assert (
        _verify_evidence_contract(output, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset({"ev-1"}))
        is FormulaConsistencyFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    )


def test_consistency_rag_empty_evidence_degrades() -> None:
    output = _draft(confidence=0.4, links=())
    assert _verify_evidence_contract(output, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset()) is None
    output_over = _draft(confidence=FORMULA_RAG_NO_EVIDENCE_CONFIDENCE_MAX + 0.05, links=())
    assert (
        _verify_evidence_contract(
            output_over, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset()
        )
        is FormulaConsistencyFailureCode.CONFIDENCE_EXCEEDS_RAG_LIMIT
    )


def test_consistency_no_rag_contract_unchanged() -> None:
    output = _draft(evidence_mode=FORMULA_EVIDENCE_MODE, confidence=FORMULA_NO_RAG_CONFIDENCE_MAX, links=())
    assert _verify_evidence_contract(output, policy_version=None, evidence_ids=frozenset()) is None
    # no-rag 契约下证据 ID 泄漏 → 拒绝
    assert (
        _verify_evidence_contract(output, policy_version=None, evidence_ids=frozenset({"ev-1"}))
        is FormulaConsistencyFailureCode.NO_RAG_CONTRACT_VIOLATED
    )


def test_consistency_rag_policy_rejects_model_knowledge_mode() -> None:
    output = _draft(evidence_mode=FORMULA_EVIDENCE_MODE, confidence=0.4, links=())
    assert (
        _verify_evidence_contract(output, policy_version=FORMULA_RAG_POLICY_VERSION, evidence_ids=frozenset())
        is FormulaConsistencyFailureCode.EVIDENCE_MODE_POLICY_MISMATCH
    )
