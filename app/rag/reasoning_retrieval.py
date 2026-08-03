"""推理链路 RAG 检索策略与降级封装。

本模块把「辨证/开方阶段如何检索、检索什么、检索失败怎么办」收敛为一个
可单测的纯策略层：

- ``stage_rag_enabled``：编排层按配置决定 stage 是否启用 RAG（policy 选配）。
- ``build_syndrome_query`` / ``build_formula_query``：从领域输入构造检索 query。
- ``retrieve_syndrome_evidence`` / ``retrieve_formula_evidence``：调用
  ``RAGRetriever`` 并实现 D3 降级——检索失败（含 ``RAGUnavailableError``）
  记 warning 并返回空列表，绝不把 503 传导给推理链路；空证据时 agent 走
  「空证据 RAG 模式」（evidence_mode=rag_retrieved、links 必空、confidence ≤0.5）。

设计决策：
- RAG 模式是策略级决策（policy_version 由编排层选配，见 langgraph_reasoning
  的 RunSpec 构造），本模块只提供 stage 级开关判断与检索执行。
- query 构造用主诉/关键症状（syndrome 阶段）或证型+治法+症状（formula 阶段）
  的摘要文本，截断到 ``rag_query_max_chars``。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.core.config import get_settings
from app.rag.schemas import Evidence

logger = logging.getLogger("xuanhu.rag.reasoning")

# ---------------------------------------------------------------------------
# 检索策略参数（不放 config：这是推理语义而非部署参数）
# ---------------------------------------------------------------------------

# 辨证阶段主查库：理论 + 医案（方剂/本草在开方阶段才查）。
SYNDROME_PRIMARY_SOURCES: tuple[str, ...] = ("theory", "case")
# 开方阶段主查库：方剂 + 本草 + 医案。
FORMULA_PRIMARY_SOURCES: tuple[str, ...] = ("formula", "herb", "case")

# 注入 context 的证据条数上限（token 预算：8 条≈500 token，SYNDROME_CONTEXT_TOKEN_LIMIT=4000 内可容）。
EVIDENCE_CONTEXT_MAX_ITEMS: int = 8
# 单条证据 snippet 注入上限（字符）。
EVIDENCE_SNIPPET_MAX_CHARS: int = 200

# 优先纳入 context 的 fact_key 白名单（主诉 + 现病史 + 关键十问 + 舌脉）。
_QUERY_PREFERRED_KEYS: tuple[str, ...] = (
    "chief_complaint.symptom",
    "chief_complaint.course",
    "chief_complaint.category",
    "present_illness.cough",
    "present_illness.sputum",
    "present_illness.rhinorrhea",
    "present_illness.nasal_congestion",
    "present_illness.sore_throat",
    "present_illness.chills",
    "present_illness.fever",
    "present_illness.body_ache",
    "present_illness.chest",
    "present_illness.abdomen",
    "present_illness.pain",
    "present_illness.distension",
    "present_illness.thirst",
    "present_illness.appetite",
    "present_illness.sleep",
    "present_illness.stool",
    "present_illness.urine",
    "ten_questions.cold_heat",
    "ten_questions.sweat",
    "ten_questions.head_body",
    "ten_questions.stool_urine",
    "ten_questions.diet",
    "ten_questions.chest_abdomen",
    "ten_questions.thirst",
    "ten_questions.sleep",
    "ten_questions.menses_leukorrhea",
    "ten_questions.pain",
    "ten_questions.respiratory",
    "four_diagnosis.inspection",
    "four_diagnosis.palpation",
)


def stage_rag_enabled(stage: str) -> bool:
    """编排层判断 stage 是否启用 RAG（总开关 + 阶段开关）。

    Args:
        stage: "syndrome" | "formula" | "base_formula" | "modification"
    """
    settings = get_settings()
    if not settings.rag_enabled:
        return False
    if stage == "syndrome":
        return settings.rag_syndrome_enabled
    if stage in ("formula", "base_formula", "modification"):
        return settings.rag_formula_enabled
    return False


def evidence_context_items(evidence: Sequence[Evidence]) -> list[dict[str, Any]]:
    """把检索证据投影为 context 注入项（截断条数与 snippet 长度控 token 预算）。

    供 syndrome_draft / formula_draft 的 build_*_context 共用；走 ContextBuilder
    的 context 层注入（gateway 传输边界 SECURITY NOTICE 包裹，untrusted）。
    """
    return [
        {
            "evidence_id": item.evidence_id,
            "title": item.title,
            "source_type": item.source_type,
            "score": round(item.score, 4),
            "rank": item.rank,
            "content_snippet": item.content_snippet[:EVIDENCE_SNIPPET_MAX_CHARS],
        }
        for item in evidence[:EVIDENCE_CONTEXT_MAX_ITEMS]
    ]


def _fact_text(value: Any) -> str:
    """把 observation 的 value（可能为 dict/list/str）压平成短文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # 槽位路径下 value 可能是 {slot_name: text} 的聚合；取所有叶文本。
        parts: list[str] = []
        for v in value.values():
            if isinstance(v, str) and v:
                parts.append(v)
        return "，".join(parts)
    if isinstance(value, (list, tuple)):
        return "，".join(str(v) for v in value if v)
    return str(value)


def build_syndrome_query(
    observations: Sequence[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """从 context_observations 构造辨证检索 query。

    优先取白名单 fact_key 的文本值（主诉/现病史/十问/舌脉），再按原始顺序
    补充其余 active 事实，最后整体截断到 ``rag_query_max_chars``。
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars
    preferred: list[str] = []
    rest: list[str] = []
    seen: set[str] = set()
    for item in observations:
        key = getattr(item, "fact_key", "")
        text = _fact_text(getattr(item, "value", None))
        if not text:
            continue
        if key in seen:
            continue
        seen.add(key)
        (preferred if key in _QUERY_PREFERRED_KEYS else rest).append(f"{key}={text}")
    ordered = preferred + rest
    query = "；".join(ordered)
    if len(query) > limit:
        query = query[:limit]
    return query or ""


def build_formula_query(
    syndrome: Any,
    observations: Sequence[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """从权威 syndrome 输出 + observations 构造开方检索 query。

    以证型与治法为先导，症状作支撑。证型缺失时退化为纯症状摘要。
    """
    limit = max_chars if max_chars is not None else get_settings().rag_query_max_chars
    parts: list[str] = []
    name = getattr(syndrome, "syndrome", None)
    if name:
        parts.append(f"证型={name}")
    principle = getattr(syndrome, "treatment_principle", None)
    if principle:
        parts.append(f"治法={principle}")
    symptom_query = build_syndrome_query(observations, max_chars=limit)
    if symptom_query:
        parts.append(f"症状={symptom_query}")
    query = "；".join(parts)
    if len(query) > limit:
        query = query[:limit]
    return query or ""


async def retrieve_syndrome_evidence(
    retriever: Any,
    observations: Sequence[Any],
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """辨证阶段检索。失败降级为空列表（D3），不抛出。

    Args:
        retriever: ``app.rag.retriever.RAGRetriever``（或测试 FakeRetriever）。
        observations: syndrome 阶段 context_observations。
        top_k: 覆盖配置的返回条数。
    """
    settings = get_settings()
    k = top_k or settings.rag_syndrome_top_k
    query = build_syndrome_query(observations)
    if not query:
        logger.warning("syndrome RAG: 无可检索的观察事实，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(SYNDROME_PRIMARY_SOURCES),
        top_k=k,
        stage="syndrome",
        logger_extra=logger_extra,
    )


async def retrieve_formula_evidence(
    retriever: Any,
    syndrome: Any,
    observations: Sequence[Any],
    *,
    top_k: int | None = None,
    logger_extra: dict[str, Any] | None = None,
) -> list[Evidence]:
    """开方阶段检索。失败降级为空列表（D3），不抛出。"""
    settings = get_settings()
    k = top_k or settings.rag_formula_top_k
    query = build_formula_query(syndrome, observations)
    if not query:
        logger.warning("formula RAG: 无可检索的查询，跳过检索（空证据模式）")
        return []
    return await _retrieve_with_degrade(
        retriever,
        query=query,
        primary_sources=list(FORMULA_PRIMARY_SOURCES),
        top_k=k,
        stage="formula",
        logger_extra=logger_extra,
    )


async def _retrieve_with_degrade(
    retriever: Any,
    *,
    query: str,
    primary_sources: list[str],
    top_k: int,
    stage: str,
    logger_extra: dict[str, Any] | None,
) -> list[Evidence]:
    """执行检索并降级。任何失败（含 RAGUnavailableError）→ 空证据，不 503。"""
    extra = {"query_len": len(query), "stage": stage}
    if logger_extra:
        extra.update(logger_extra)
    try:
        results = await retriever.retrieve(
            query=query,
            primary_sources=primary_sources,
            allow_cross_source=True,
            top_k=top_k,
        )
        logger.info("RAG %s 检索完成: query_len=%d hits=%d", stage, len(query), len(results), extra=extra)
        return results
    except Exception as exc:  # noqa: BLE001 - 检索失败必须降级而非阻断推理
        logger.warning(
            "RAG %s 检索失败，降级为空证据模式: %s: %s",
            stage,
            type(exc).__name__,
            str(exc),
            extra=extra,
        )
        return []
