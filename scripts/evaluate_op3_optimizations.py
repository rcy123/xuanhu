"""真实后端评测脚本 — OP3 辨证-开方-加减方优化。
=====================================================

覆盖：
  P0: Modification vs Formula RAG 检索差异（真实 Milvus）
  P1: 多方案基础方 LLM 输出验证（真实 LLM）
  P2: Query 改写 A/B 对比（真实 LLM + 真实 Milvus）
  Reranker: 候选评分对比（MVP vs LLM Reranker 路径）

用法::

    uv run python -m scripts.evaluate_op3_optimizations
    uv run python -m scripts.evaluate_op3_optimizations --skip-llm  # 仅 P0
    uv run python -m scripts.evaluate_op3_optimizations --output eval_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _PROJECT_ROOT / "scripts" / "op3_eval_results.json"


# ============================================================================
# 评测数据结构
# ============================================================================


@dataclass
class P0Result:
    """P0: Modification vs Formula RAG 差异评测结果."""
    patient_case: str
    syndrome_type: str
    base_formula_name: str
    formula_evidence_count: int
    modification_evidence_count: int
    formula_source_types: list[str]
    modification_source_types: list[str]
    overlap_count: int
    overlap_ratio: float
    distinct_modification_ids: list[str]
    formula_top_titles: list[str]
    modification_top_titles: list[str]
    verdict: str  # "PASS" / "FAIL"


@dataclass
class P2Result:
    """P2: Query 改写 A/B 对比结果."""
    patient_case: str
    original_query: str
    rewritten_query: str
    rewrite_latency_ms: float
    original_evidence_count: int
    rewritten_evidence_count: int
    original_top_titles: list[str]
    rewritten_top_titles: list[str]
    overlap_count: int
    verdict: str


@dataclass
class RerankerResult:
    """Reranker 路径对比结果."""
    query: str
    mvp_top_titles: list[str]
    llm_rerank_top_titles: list[str] | None
    cross_encoder_top_titles: list[str] | None
    mvp_latency_ms: float
    llm_rerank_latency_ms: float | None
    cross_encoder_latency_ms: float | None
    verdict: str


@dataclass
class EvalReport:
    generated_at: str = ""
    p0_results: list[P0Result] = field(default_factory=list)
    p2_results: list[P2Result] = field(default_factory=list)
    reranker_results: list[RerankerResult] = field(default_factory=list)
    p0_pass_rate: float = 0.0
    p2_overlap_improvement: float = 0.0
    summary: str = ""


# ============================================================================
# 测试用例
# ============================================================================

# 模拟真实辨证结果的测试用例（证型 + 基础方 + 简要症状）
P0_TEST_CASES: list[dict[str, Any]] = [
    {
        "case_name": "风寒束表证—麻黄汤",
        "syndrome": {"syndrome": "风寒束表证", "treatment_principle": "辛温解表，宣肺散寒"},
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "恶寒发热，无汗头痛3天", "normalized_value": "恶寒发热，无汗头痛3天"},
            {"observation_id": str(uuid4()), "fact_key": "cough", "value": "咳嗽，痰白稀", "normalized_value": "咳嗽，痰白稀"},
            {"observation_id": str(uuid4()), "fact_key": "nasal", "value": "鼻塞清涕", "normalized_value": "鼻塞清涕"},
            {"observation_id": str(uuid4()), "fact_key": "tongue_coating", "value": "苔薄白", "normalized_value": "苔薄白"},
            {"observation_id": str(uuid4()), "fact_key": "pulse_manifestation", "value": "脉浮紧", "normalized_value": "脉浮紧"},
        ],
        "base_formula": {
            "name": "麻黄汤",
            "herbs": [{"herb": "麻黄", "dose": 9.0, "unit": "g"}, {"herb": "桂枝", "dose": 6.0, "unit": "g"},
                       {"herb": "杏仁", "dose": 9.0, "unit": "g"}, {"herb": "甘草", "dose": 6.0, "unit": "g"}],
        },
    },
    {
        "case_name": "脾虚湿困证—参苓白术散",
        "syndrome": {"syndrome": "脾虚湿困证", "treatment_principle": "健脾化湿，益气和中"},
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "大便溏稀两个月", "normalized_value": "大便溏稀两个月"},
            {"observation_id": str(uuid4()), "fact_key": "appetite", "value": "食少纳呆", "normalized_value": "食少纳呆"},
            {"observation_id": str(uuid4()), "fact_key": "fatigue", "value": "乏力倦怠", "normalized_value": "乏力倦怠"},
            {"observation_id": str(uuid4()), "fact_key": "tongue_coating", "value": "苔白腻", "normalized_value": "苔白腻"},
            {"observation_id": str(uuid4()), "fact_key": "pulse_manifestation", "value": "脉濡缓", "normalized_value": "脉濡缓"},
        ],
        "base_formula": {
            "name": "参苓白术散",
            "herbs": [{"herb": "党参", "dose": 12.0, "unit": "g"}, {"herb": "白术", "dose": 12.0, "unit": "g"},
                       {"herb": "茯苓", "dose": 12.0, "unit": "g"}, {"herb": "山药", "dose": 12.0, "unit": "g"}],
        },
    },
    {
        "case_name": "肝郁气滞证—柴胡疏肝散",
        "syndrome": {"syndrome": "肝郁气滞证", "treatment_principle": "疏肝理气，解郁止痛"},
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "胸胁胀痛一周", "normalized_value": "胸胁胀痛一周"},
            {"observation_id": str(uuid4()), "fact_key": "emotion", "value": "情志不舒", "normalized_value": "情志不舒"},
            {"observation_id": str(uuid4()), "fact_key": "belching", "value": "嗳气频作", "normalized_value": "嗳气频作"},
            {"observation_id": str(uuid4()), "fact_key": "tongue_coating", "value": "苔薄白", "normalized_value": "苔薄白"},
            {"observation_id": str(uuid4()), "fact_key": "pulse_manifestation", "value": "脉弦", "normalized_value": "脉弦"},
        ],
        "base_formula": {
            "name": "柴胡疏肝散",
            "herbs": [{"herb": "柴胡", "dose": 9.0, "unit": "g"}, {"herb": "白芍", "dose": 9.0, "unit": "g"},
                       {"herb": "枳壳", "dose": 6.0, "unit": "g"}, {"herb": "川芎", "dose": 6.0, "unit": "g"}],
        },
    },
]

# P2 测试用例（用于 query 改写评测）——来自真实医案数据
P2_TEST_CASES: list[dict[str, Any]] = [
    {
        "case_name": "风寒咳嗽案",
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "受凉后咳嗽三天", "normalized_value": "受凉后咳嗽三天"},
            {"observation_id": str(uuid4()), "fact_key": "cough", "value": "咳嗽", "normalized_value": "咳嗽"},
            {"observation_id": str(uuid4()), "fact_key": "sputum", "value": "痰白稀", "normalized_value": "痰白稀"},
            {"observation_id": str(uuid4()), "fact_key": "aversion_cold", "value": "怕冷明显", "normalized_value": "怕冷明显"},
            {"observation_id": str(uuid4()), "fact_key": "sweat", "value": "无汗", "normalized_value": "无汗"},
            {"observation_id": str(uuid4()), "fact_key": "nasal", "value": "流清涕", "normalized_value": "流清涕"},
        ],
    },
    {
        "case_name": "心脾两虚失眠案",
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "失眠多梦三个月", "normalized_value": "失眠多梦三个月"},
            {"observation_id": str(uuid4()), "fact_key": "palpitations", "value": "心悸健忘", "normalized_value": "心悸健忘"},
            {"observation_id": str(uuid4()), "fact_key": "fatigue", "value": "神疲乏力", "normalized_value": "神疲乏力"},
            {"observation_id": str(uuid4()), "fact_key": "dizziness", "value": "头晕", "normalized_value": "头晕"},
        ],
    },
    {
        "case_name": "脾阳虚泄泻案",
        "observations": [
            {"observation_id": str(uuid4()), "fact_key": "chief_complaint", "value": "大便溏稀两个月", "normalized_value": "大便溏稀两个月"},
            {"observation_id": str(uuid4()), "fact_key": "appetite", "value": "食少", "normalized_value": "食少"},
            {"observation_id": str(uuid4()), "fact_key": "fatigue", "value": "乏力", "normalized_value": "乏力"},
            {"observation_id": str(uuid4()), "fact_key": "bloating", "value": "腹胀", "normalized_value": "腹胀"},
        ],
    },
]


# ============================================================================
# P0 评测：Modification vs Formula RAG 检索差异
# ============================================================================


async def evaluate_p0(retriever: Any) -> list[P0Result]:
    """使用真实 Milvus 对比 formula 和 modification 检索结果差异."""
    from types import SimpleNamespace

    from app.rag.reasoning_retrieval import (
        build_formula_query,
        build_modification_query,
        retrieve_formula_evidence,
        retrieve_modification_evidence,
    )
    from app.schemas.formula import FormulaComposition, FormulaFactClaim, HerbItem

    results: list[P0Result] = []

    for case in P0_TEST_CASES:
        case_name = case["case_name"]
        syndrome_data = case["syndrome"]
        observations_raw = case["observations"]
        bf_raw = case["base_formula"]

        print(f"\n  [{case_name}]")

        # 构造 observation objects
        observations = tuple(
            SimpleNamespace(
                observation_id=o["observation_id"],
                fact_key=o["fact_key"],
                value=o["value"],
                normalized_value=o.get("normalized_value", o["value"]),
            )
            for o in observations_raw
        )

        # 构造 SyndromeDraft-like object
        syndrome = SimpleNamespace(
            syndrome=syndrome_data["syndrome"],
            treatment_principle=syndrome_data["treatment_principle"],
        )

        # 构造 base_formula
        claim = FormulaFactClaim(claim="test", fact_ids=(uuid4(),))
        herbs = tuple(
            HerbItem(herb=h["herb"], dose=h.get("dose"), unit=h.get("unit", "g"))
            for h in bf_raw["herbs"]
        )
        base_formula = FormulaComposition(
            name=bf_raw["name"],
            composition=herbs,
            rationale="test",
            basis=(claim,),
        )

        # ---- Formula RAG ----
        f_start = time.perf_counter()
        try:
            f_evidence = await retrieve_formula_evidence(retriever, syndrome, observations)
        except Exception as e:
            print(f"    Formula RAG error: {e}")
            f_evidence = []
        f_latency = (time.perf_counter() - f_start) * 1000

        # ---- Modification RAG ----
        m_start = time.perf_counter()
        try:
            m_evidence = await retrieve_modification_evidence(retriever, syndrome, observations, base_formula)
        except Exception as e:
            print(f"    Modification RAG error: {e}")
            m_evidence = []
        m_latency = (time.perf_counter() - m_start) * 1000

        # 分析
        f_ids = {e.evidence_id for e in f_evidence}
        m_ids = {e.evidence_id for e in m_evidence}
        overlap = f_ids & m_ids
        overlap_ratio = len(overlap) / max(len(f_ids | m_ids), 1)

        f_sources = [e.source_type for e in f_evidence]
        m_sources = [e.source_type for e in m_evidence]
        distinct_m = [eid for eid in m_ids if eid not in f_ids]

        # Verdict: modification 检索应该与 formula 检索有差异（P0 的核心目标）
        # 如果 modification evidence 全等于 formula evidence → FAIL（没区分开）
        # 如果 modification 有独有证据 → PASS
        has_distinct = len(distinct_m) > 0
        # 同时检查 modification 不应该返回 formula 类型的来源（因用 herb+case）
        has_formula_source = "formula" in m_sources
        source_correct = not has_formula_source

        verdict = "PASS" if (has_distinct and source_correct) else "FAIL"

        r = P0Result(
            patient_case=case_name,
            syndrome_type=syndrome_data["syndrome"],
            base_formula_name=bf_raw["name"],
            formula_evidence_count=len(f_evidence),
            modification_evidence_count=len(m_evidence),
            formula_source_types=sorted(set(f_sources)),
            modification_source_types=sorted(set(m_sources)),
            overlap_count=len(overlap),
            overlap_ratio=round(overlap_ratio, 3),
            distinct_modification_ids=distinct_m[:5],
            formula_top_titles=[f"{e.title} ({e.source_type})" for e in f_evidence[:3]],
            modification_top_titles=[f"{e.title} ({e.source_type})" for e in m_evidence[:3]],
            verdict=verdict,
        )

        print(f"    Formula: {len(f_evidence)} results ({f_latency:.0f}ms), sources={sorted(set(f_sources))}")
        print(f"    Modification: {len(m_evidence)} results ({m_latency:.0f}ms), sources={sorted(set(m_sources))}")
        print(f"    Overlap: {r.overlap_count}/{len(f_ids | m_ids)} ({r.overlap_ratio:.1%}), "
              f"distinct_mod={len(distinct_m)}, source_correct={source_correct}")
        print(f"    Top formula: {r.formula_top_titles}")
        print(f"    Top modification: {r.modification_top_titles}")
        print(f"    Verdict: {verdict}")

        results.append(r)

    return results


# ============================================================================
# P2 评测：Query 改写 A/B 对比
# ============================================================================


async def evaluate_p2(retriever: Any, gateway: Any) -> list[P2Result]:
    """使用真实 LLM 改写 query，然后对比改写前后的检索质量."""
    from types import SimpleNamespace

    from app.rag.reasoning_retrieval import (
        _format_observations_for_rewrite,
        build_syndrome_query,
        rewrite_syndrome_query,
    )
    from app.rag.schemas import Evidence

    results: list[P2Result] = []

    for case in P2_TEST_CASES:
        case_name = case["case_name"]
        observations_raw = case["observations"]

        print(f"\n  [{case_name}]")

        observations = tuple(
            SimpleNamespace(
                observation_id=o["observation_id"],
                fact_key=o["fact_key"],
                value=o["value"],
                normalized_value=o.get("normalized_value", o["value"]),
            )
            for o in observations_raw
        )

        # 原始 query
        original_query = build_syndrome_query(observations)
        print(f"    Original query: {original_query[:100]}...")

        # 改写 query
        rw_start = time.perf_counter()
        rewritten_query = await rewrite_syndrome_query(
            observations, gateway=gateway,
        )
        rw_latency = (time.perf_counter() - rw_start) * 1000

        if rewritten_query == original_query:
            print(f"    Rewrite returned original (fallback). Latency: {rw_latency:.0f}ms")
        else:
            print(f"    Rewritten query: {rewritten_query[:150]}...")
            print(f"    Rewrite latency: {rw_latency:.0f}ms")

        # 用原始 query 检索
        orig_evidence = await _retrieve_with_query(retriever, original_query)
        orig_titles = [e.title for e in orig_evidence[:5]]

        # 用改写 query 检索
        rw_evidence = await _retrieve_with_query(retriever, rewritten_query)
        rw_titles = [e.title for e in rw_evidence[:5]]

        # 对比
        orig_ids = {e.evidence_id for e in orig_evidence}
        rw_ids = {e.evidence_id for e in rw_evidence}
        overlap = len(orig_ids & rw_ids)

        # Verdict: 改写后的 query 应该检索到不同（更叙事化匹配）的证据
        has_difference = len(orig_ids ^ rw_ids) > 0
        verdict = "PASS" if has_difference else "NO_DIFFERENCE"

        r = P2Result(
            patient_case=case_name,
            original_query=original_query[:200],
            rewritten_query=rewritten_query[:300],
            rewrite_latency_ms=round(rw_latency, 1),
            original_evidence_count=len(orig_evidence),
            rewritten_evidence_count=len(rw_evidence),
            original_top_titles=orig_titles,
            rewritten_top_titles=rw_titles,
            overlap_count=overlap,
            verdict=verdict,
        )

        print(f"    Original: {len(orig_evidence)} results, top: {orig_titles[:3]}")
        print(f"    Rewritten: {len(rw_evidence)} results, top: {rw_titles[:3]}")
        print(f"    Overlap: {overlap}/{max(len(orig_ids), len(rw_ids), 1)}, "
              f"new_in_rewritten={len(rw_ids - orig_ids)}, lost={len(orig_ids - rw_ids)}")
        print(f"    Verdict: {verdict}")

        results.append(r)

    return results


async def _retrieve_with_query(retriever: Any, query: str) -> list[Any]:
    """用给定 query 执行一次简单检索（绕过 reasoning 层的 query 构造）."""
    try:
        return await retriever.hybrid_search(
            query=query,
            sources=("theory", "case", "herb", "formula"),
            top_k=8,
        )
    except Exception:
        return []


# ============================================================================
# Reranker 评测：路径对比
# ============================================================================


async def evaluate_reranker(retriever: Any, gateway: Any) -> list[RerankerResult]:
    """对比 MVP vs LLM Reranker 路径的排序结果."""
    import uuid as _uuid

    from app.core.config import get_settings
    from app.rag.reranker import (
        DEFAULT_FULLTEXT_WEIGHT,
        DEFAULT_SOURCE_PRIORITY_WEIGHT,
        DEFAULT_VECTOR_WEIGHT,
        llm_rerank,
        rerank,
    )
    from app.rag.schemas import Evidence, MergedHit

    settings = get_settings()
    results: list[RerankerResult] = []

    # 选取几条有代表性的 query
    test_queries = [
        "患者受凉后咳嗽三天，痰白稀，怕冷，流清涕，应如何加减用药？",
        "患者胸胁胀痛一周，情志不舒，嗳气频作，舌苔薄白，脉弦，应用什么方？",
        "足三里用于脾胃虚弱时的定位和主治是什么？",
    ]

    for query in test_queries:
        print(f"\n  [Query: {query[:80]}...]")

        # 先用 MVP 检索获取候选
        try:
            candidates = await retriever._hybrid_search_internal(
                query=query,
                sources=("formula", "herb", "case", "theory"),
                top_k=16,
            )
        except AttributeError:
            # _hybrid_search_internal may not exist, skip
            print("    Skipped: retriever internal API not accessible")
            continue
        except Exception as e:
            print(f"    Retrieve error: {e}")
            continue

        if not candidates:
            print("    No candidates found, skipping")
            continue

        print(f"    Candidates: {len(candidates)}")

        # --- MVP path ---
        mvp_start = time.perf_counter()
        merged = _to_merged_hits(candidates)
        mvp_evidence = rerank(merged, top_k=5)
        mvp_latency = (time.perf_counter() - mvp_start) * 1000
        mvp_titles = [e.title for e in mvp_evidence]

        # --- LLM Reranker path ---
        llm_titles = None
        llm_latency = None
        if gateway is not None:
            try:
                llm_start = time.perf_counter()
                llm_evidence = await llm_rerank(
                    query=query,
                    merged_hits=merged,
                    gateway=gateway,
                    top_k=5,
                )
                llm_latency = (time.perf_counter() - llm_start) * 1000
                llm_titles = [e.title for e in llm_evidence]
                print(f"    LLM Reranker: {llm_latency:.0f}ms, top: {llm_titles[:3]}")
            except Exception as e:
                print(f"    LLM Reranker error: {e}")

        print(f"    MVP: {mvp_latency:.0f}ms, top: {mvp_titles[:3]}")

        # 对比
        if llm_titles:
            rank_correlation = _rank_correlation(mvp_titles, llm_titles)
            verdict = "CORRELATED" if rank_correlation >= 0.5 else "DIVERGENT"
        else:
            verdict = "MVP_ONLY"

        r = RerankerResult(
            query=query[:120],
            mvp_top_titles=mvp_titles,
            llm_rerank_top_titles=llm_titles,
            cross_encoder_top_titles=None,
            mvp_latency_ms=round(mvp_latency, 1),
            llm_rerank_latency_ms=round(llm_latency, 1) if llm_latency else None,
            cross_encoder_latency_ms=None,
            verdict=verdict,
        )
        results.append(r)

    return results


def _to_merged_hits(candidates: list[Any]) -> list[Any]:
    """Ensure candidates are MergedHit-like objects."""
    from app.rag.schemas import MergedHit

    if candidates and isinstance(candidates[0], MergedHit):
        return candidates

    converted = []
    for c in candidates:
        if hasattr(c, "vector_score"):
            hit = MergedHit(
                source_type=getattr(c, "source_type", "unknown"),
                source_id=getattr(c, "source_id", ""),
                chunk_id=getattr(c, "chunk_id", None),
                title=getattr(c, "title", ""),
                content_snippet=getattr(c, "content_snippet", ""),
                vector_score=getattr(c, "vector_score", 0.0),
                fulltext_score=getattr(c, "fulltext_score", 0.0),
                is_primary=getattr(c, "is_primary", True),
            )
            converted.append(hit)
    return converted


def _rank_correlation(a: list[str], b: list[str]) -> float:
    """Simple rank correlation: fraction of common items in top positions."""
    if not a or not b:
        return 0.0
    common = set(a[:3]) & set(b[:3])
    return len(common) / 3


# ============================================================================
# 报告生成
# ============================================================================


def generate_report(
    p0_results: list[P0Result],
    p2_results: list[P2Result],
    reranker_results: list[RerankerResult],
) -> EvalReport:
    p0_pass = sum(1 for r in p0_results if r.verdict == "PASS")
    p0_pass_rate = p0_pass / max(len(p0_results), 1)

    # P2 overlap improvement: 改写后应该有新的独特证据
    if p2_results:
        total_new = sum(len(set(r.rewritten_top_titles) - set(r.original_top_titles)) for r in p2_results)
        p2_improvement = total_new / max(len(p2_results), 1)
    else:
        p2_improvement = 0.0

    lines = [
        f"# OP3 优化方案真实后端评测报告",
        f"",
        f"生成时间：{datetime.now(UTC).isoformat()}",
        f"",
        f"## P0: Modification RAG 差异化",
        f"",
        f"- 通过率：{p0_pass}/{len(p0_results)} ({p0_pass_rate:.0%})",
        f"- 核心指标：modification vs formula 检索证据重叠率、source type 正确性",
        f"",
    ]

    for r in p0_results:
        lines.append(f"### {r.patient_case}")
        lines.append(f"- Syndrome: {r.syndrome_type}, Base formula: {r.base_formula_name}")
        lines.append(f"- Formula RAG: {r.formula_evidence_count} results, sources={r.formula_source_types}")
        lines.append(f"- Modification RAG: {r.modification_evidence_count} results, sources={r.modification_source_types}")
        lines.append(f"- Overlap: {r.overlap_count} ({r.overlap_ratio:.1%}), distinct mod IDs: {len(r.distinct_modification_ids)}")
        lines.append(f"- Top formula: {r.formula_top_titles}")
        lines.append(f"- Top modification: {r.modification_top_titles}")
        lines.append(f"- Verdict: **{r.verdict}**")
        lines.append("")

    lines.append("## P2: Query 改写 A/B 对比")
    lines.append("")
    for r in p2_results:
        lines.append(f"### {r.patient_case}")
        lines.append(f"- Original: {r.original_query[:120]}...")
        lines.append(f"- Rewritten ({r.rewrite_latency_ms:.0f}ms): {r.rewritten_query[:150]}...")
        lines.append(f"- Original results: {r.original_evidence_count}, top: {r.original_top_titles}")
        lines.append(f"- Rewritten results: {r.rewritten_evidence_count}, top: {r.rewritten_top_titles}")
        lines.append(f"- Overlap: {r.overlap_count}, Verdict: **{r.verdict}**")
        lines.append("")

    lines.append("## Reranker")
    lines.append("")
    for r in reranker_results:
        lines.append(f"### Query: {r.query}...")
        lines.append(f"- MVP ({r.mvp_latency_ms:.0f}ms): {r.mvp_top_titles}")
        if r.llm_rerank_top_titles:
            lines.append(f"- LLM ({r.llm_rerank_latency_ms:.0f}ms): {r.llm_rerank_top_titles}")
        if r.cross_encoder_top_titles:
            lines.append(f"- Cross-Encoder ({r.cross_encoder_latency_ms:.0f}ms): {r.cross_encoder_top_titles}")
        lines.append(f"- Verdict: **{r.verdict}**")
        lines.append("")

    return EvalReport(
        generated_at=datetime.now(UTC).isoformat(),
        p0_results=p0_results,
        p2_results=p2_results,
        reranker_results=reranker_results,
        p0_pass_rate=p0_pass_rate,
        p2_overlap_improvement=p2_improvement,
        summary="\n".join(lines),
    )


# ============================================================================
# 主入口
# ============================================================================


async def main() -> None:
    parser = argparse.ArgumentParser(description="OP3 优化方案真实后端评测")
    parser.add_argument("--skip-llm", action="store_true", help="跳过需要 LLM 调用 的评测（P2 + Reranker LLM 路径）")
    parser.add_argument("--skip-reranker", action="store_true", help="跳过 Reranker 评测")
    parser.add_argument("--output", type=str, default=str(_DEFAULT_OUTPUT), help="输出 JSON 路径")
    args = parser.parse_args()

    print("=" * 60)
    print("OP3 辨证-开方-加减方 Agent 优化 — 真实后端评测")
    print("=" * 60)

    # 初始化 RAGRetriever
    print("\n[1/4] 初始化 RAGRetriever...")
    from app.rag.retriever import RAGRetriever

    retriever = RAGRetriever()
    print("  RAGRetriever 初始化完成")

    # 初始化 LLM Gateway（用于 P2 改写）
    gateway = None
    if not args.skip_llm:
        print("\n 初始化 LLM Gateway...")
        try:
            from app.core.config import get_settings
            from app.core.gateway import ModelGatewayClient

            settings = get_settings()
            gateway = ModelGatewayClient(settings=settings)
            print(f"  LLM Gateway 初始化完成: {settings.model_gateway_base_url}")
        except Exception as e:
            print(f"  LLM Gateway 初始化失败: {e}")
            print("  P2 / Reranker LLM 评测将跳过")
            gateway = None

    # ======== P0 ========
    print("\n[2/4] P0: Modification RAG 差异化评测（真实 Milvus）...")
    print("-" * 40)
    p0_results = await evaluate_p0(retriever)
    p0_pass = sum(1 for r in p0_results if r.verdict == "PASS")
    print(f"\n  P0 结果：{p0_pass}/{len(p0_results)} 通过")

    # ======== P2 ========
    p2_results: list[P2Result] = []
    if args.skip_llm:
        print("\n[3/4] P2: Query 改写评测 — 已跳过 (--skip-llm)")
    elif gateway is None:
        print("\n[3/4] P2: Query 改写评测 — 已跳过 (gateway 不可用)")
    else:
        print("\n[3/4] P2: Query 改写 A/B 对比评测（真实 LLM + 真实 Milvus）...")
        print("-" * 40)
        p2_results = await evaluate_p2(retriever, gateway)

    # ======== Reranker ========
    reranker_results: list[RerankerResult] = []
    if args.skip_reranker or args.skip_llm:
        print("\n[4/4] Reranker 评测 — 已跳过")
    elif gateway is None:
        print("\n[4/4] Reranker 评测 — 已跳过 (gateway 不可用)")
    else:
        print("\n[4/4] Reranker 路径对比评测（MVP vs LLM Reranker）...")
        print("-" * 40)
        reranker_results = await evaluate_reranker(retriever, gateway)

    # 生成报告
    print("\n" + "=" * 60)
    print("生成评测报告...")
    report = generate_report(p0_results, p2_results, reranker_results)
    print(report.summary)

    # 写入 JSON
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        "generated_at": report.generated_at,
        "p0_pass_rate": report.p0_pass_rate,
        "p2_overlap_improvement": report.p2_overlap_improvement,
        "p0_results": [
            {
                "case": r.patient_case,
                "syndrome": r.syndrome_type,
                "base_formula": r.base_formula_name,
                "formula_count": r.formula_evidence_count,
                "modification_count": r.modification_evidence_count,
                "formula_sources": r.formula_source_types,
                "modification_sources": r.modification_source_types,
                "overlap_ratio": r.overlap_ratio,
                "distinct_mod_count": len(r.distinct_modification_ids),
                "formula_top": r.formula_top_titles,
                "modification_top": r.modification_top_titles,
                "verdict": r.verdict,
            }
            for r in p0_results
        ],
        "p2_results": [
            {
                "case": r.patient_case,
                "original_query": r.original_query,
                "rewritten_query": r.rewritten_query,
                "rewrite_latency_ms": r.rewrite_latency_ms,
                "original_count": r.original_evidence_count,
                "rewritten_count": r.rewritten_evidence_count,
                "original_top": r.original_top_titles,
                "rewritten_top": r.rewritten_top_titles,
                "overlap": r.overlap_count,
                "verdict": r.verdict,
            }
            for r in p2_results
        ],
        "reranker_results": [
            {
                "query": r.query,
                "mvp_top": r.mvp_top_titles,
                "mvp_latency_ms": r.mvp_latency_ms,
                "llm_top": r.llm_rerank_top_titles,
                "llm_latency_ms": r.llm_rerank_latency_ms,
                "verdict": r.verdict,
            }
            for r in reranker_results
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    print(f"\nJSON 报告已写入: {output_path}")

    # 最终判定
    print("\n" + "=" * 60)
    all_pass = p0_pass == len(p0_results)
    if all_pass:
        print("P0 评测通过！Modification RAG 与 Formula RAG 已正确差异化。")
    else:
        print("P0 评测有失败项，请检查。")
    if p2_results:
        p2_pass = sum(1 for r in p2_results if r.verdict == "PASS")
        print(f"P2 评测：{p2_pass}/{len(p2_results)} 改写后产生差异化检索结果。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
