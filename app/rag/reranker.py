"""MVP 重排模块 — 加权分数合并与排序 + Cross-Encoder / LLM Reranker 升级。

重排策略（详细设计 §8.4）：
    final_score = vector_weight * vector_score + fulltext_weight * fulltext_score
                  + source_priority_weight * source_priority

默认权重（可通过 hybrid_search 参数覆盖）：
    vector_weight=0.65, fulltext_weight=0.25, source_priority_weight=0.10

source_priority：
- 命中 primary_sources → 1.0
- 跨库命中 → 0.3

Cross-Encoder / LLM Reranker 在 MVP 加权求和之上提供深度语义相关性判断；
失败时自动降级为 MVP 路径（不阻断检索）。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from app.core.config import get_settings
from app.rag.schemas import Evidence, MergedHit

logger = logging.getLogger("xuanhu.rag.reranker")

# MVP 默认加权系数（详细设计 §8.4）
DEFAULT_VECTOR_WEIGHT = 0.65
DEFAULT_FULLTEXT_WEIGHT = 0.25
DEFAULT_SOURCE_PRIORITY_WEIGHT = 0.10

# source_priority 取值
PRIMARY_PRIORITY = 1.0
CROSS_PRIORITY = 0.3


def compute_source_priority(is_primary: bool) -> float:
    """根据是否命中主查库返回 source_priority。"""
    return PRIMARY_PRIORITY if is_primary else CROSS_PRIORITY


def compute_final_score(
    vector_score: float,
    fulltext_score: float,
    is_primary: bool,
    *,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    fulltext_weight: float = DEFAULT_FULLTEXT_WEIGHT,
    source_priority_weight: float = DEFAULT_SOURCE_PRIORITY_WEIGHT,
) -> float:
    """计算 MVP 加权最终得分。

    Args:
        vector_score: 向量相似度得分 [0, 1]。
        fulltext_score: 全文检索得分 [0, 1]。
        is_primary: 是否命中 primary_sources。
        vector_weight: 向量得分权重。
        fulltext_weight: 全文得分权重。
        source_priority_weight: source_priority 权重。
    """
    source_priority = compute_source_priority(is_primary)
    return (
        vector_weight * vector_score
        + fulltext_weight * fulltext_score
        + source_priority_weight * source_priority
    )


def rerank(
    merged_hits: Sequence[MergedHit],
    *,
    top_k: int = 8,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    fulltext_weight: float = DEFAULT_FULLTEXT_WEIGHT,
    source_priority_weight: float = DEFAULT_SOURCE_PRIORITY_WEIGHT,
) -> list[Evidence]:
    """对合并去重后的结果进行 MVP 加权重排。

    Args:
        merged_hits: 合并去重后的命中列表。
        top_k: 最终返回条数。
        vector_weight: 向量得分权重（默认 0.65）。
        fulltext_weight: 全文得分权重（默认 0.25）。
        source_priority_weight: source_priority 权重（默认 0.10）。

    Returns:
        排序后的 Evidence 列表，rank 从 1 开始。
    """
    # 计算最终得分
    scored: list[tuple[MergedHit, float]] = []
    for hit in merged_hits:
        final_score = compute_final_score(
            vector_score=hit.vector_score,
            fulltext_score=hit.fulltext_score,
            is_primary=hit.is_primary,
            vector_weight=vector_weight,
            fulltext_weight=fulltext_weight,
            source_priority_weight=source_priority_weight,
        )
        scored.append((hit, final_score))

    # 按 final_score 降序排列
    scored.sort(key=lambda x: x[1], reverse=True)

    # 截取 top_k 并构造 Evidence
    evidences: list[Evidence] = []
    for rank, (hit, final_score) in enumerate(scored[:top_k], start=1):
        evidence = Evidence(
            evidence_id=uuid.uuid4().hex,
            source_type=hit.source_type,
            source_id=hit.source_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            content_snippet=hit.content_snippet,
            score=round(final_score, 6),
            rank=rank,
            metadata={
                "vector_score": hit.vector_score,
                "fulltext_score": hit.fulltext_score,
                "source_priority": compute_source_priority(hit.is_primary),
            },
        )
        evidences.append(evidence)

    return evidences


# ---------------------------------------------------------------------------
# Cross-Encoder / LLM Reranker 升级
# ---------------------------------------------------------------------------

_LLM_RERANK_SYSTEM = """你是中医文献相关性评审员。
对每个候选文献片段，根据其与查询的医学相关性打分（0-10 分）。

评分标准：
- 9-10: 高度相关，直接回答查询中的医学问题
- 7-8: 相关，提供了有用的背景知识
- 5-6: 部分相关，涉及相同主题但不够精准
- 3-4: 弱相关，仅涉及边缘概念
- 0-2: 不相关

返回 JSON 格式：{"scores": [8, 6, 3, ...]}（与输入顺序一一对应）"""


def _parse_llm_rerank_scores(raw: str, expected_count: int) -> list[float]:
    """从 LLM 返回的原始文本中解析相关度分数列表。

    返回 [0.0-1.0] 归一化分数。解析失败时返回全零列表（安全降级）。
    """
    # 尝试直接用 json.loads 解析 JSON 文本
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            scores = parsed.get("scores")
            if isinstance(scores, list) and len(scores) == expected_count:
                return [max(0.0, min(1.0, float(s) / 10.0)) for s in scores]
        if isinstance(parsed, list) and len(parsed) == expected_count:
            return [max(0.0, min(1.0, float(s) / 10.0)) for s in parsed]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # 尝试正则提取数字
    import re
    numbers = re.findall(r"\b(\d+(?:\.\d+)?)\b", raw)
    if numbers and len(numbers) >= expected_count:
        return [max(0.0, min(1.0, float(n) / 10.0)) for n in numbers[:expected_count]]
    logger.warning("LLM reranker 分数解析失败，回退到全部 0 分: raw=%s...", raw[:200])
    return [0.0] * expected_count


async def _call_reranker_api(
    gateway: Any,
    query: str,
    documents: list[str],
    *,
    model: str,
    timeout: float = 5.0,
    trace_id: str = "rag-reranker",
) -> list[float]:
    """调用 Cross-Encoder reranker API 批量打分。

    通过 ``ModelGatewayClient`` 的底层 ``_request_with_retry`` POST 到
    ``/rerank`` endpoint。返回 [0.0, 1.0] 归一化分数列表。

    Raises:
        Exception: 网络/API 错误，调用方应降级为 MVP 路径。
    """
    settings = get_settings()
    reranker_model = model or settings.rag_reranker_model or "jina-reranker-m0"

    # 使用 Cohere-style 格式（dmxapi / 通用 reranker API 兼容）：
    #   {"model": "...", "query": "...", "documents": ["...", ...], "top_n": N}
    payload = {
        "model": reranker_model,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }

    try:
        response = await gateway._request_with_retry(
            method="POST",
            path="/rerank",
            payload=payload,
        )
    except Exception:
        logger.warning(
            "Cross-Encoder reranker API 调用失败 (model=%s)，降级为 MVP 加权求和",
            reranker_model,
        )
        raise

    data = response.json()
    # 尝试解析不同格式的 reranker 响应
    # Cohere-style: {"results": [{"index": 0, "relevance_score": 0.85}, ...]}
    # Jina-style:  {"results": [{"index": 0, "relevance_score": 0.85}, ...]}
    # OpenAI-style: {"results": [{"index": 0, "score": 0.85}, ...]}
    results = data.get("results", data.get("data", []))
    if isinstance(results, list) and len(results) > 0:
        if isinstance(results[0], dict):
            scores = []
            for r in sorted(results, key=lambda x: x.get("index", 0)):
                score = r.get("relevance_score", r.get("score", 0.5))
                scores.append(max(0.0, min(1.0, float(score))))
            if len(scores) == len(documents):
                return scores
        elif isinstance(results[0], (int, float)):
            scores = [max(0.0, min(1.0, float(s))) for s in results]
            if len(scores) == len(documents):
                return scores

    # 如果格式不匹配，尝试直接取 scores 字段
    scores_raw = data.get("scores", [])
    if isinstance(scores_raw, list) and len(scores_raw) == len(documents):
        return [max(0.0, min(1.0, float(s))) for s in scores_raw]

    logger.warning("Cross-Encoder reranker 响应结构不匹配，降级为 MVP: %s...", str(data)[:200])
    raise ValueError("Unrecognized reranker API response format")


async def cross_encoder_rerank(
    query: str,
    merged_hits: Sequence[MergedHit],
    *,
    gateway: Any,
    model: str = "",
    top_k: int = 8,
    timeout: float = 5.0,
) -> list[Evidence]:
    """使用 Cross-Encoder 模型对候选 chunk 逐对打分重排。

    Args:
        query: 原始检索 query。
        merged_hits: 合并去重后的候选列表。
        gateway: ``ModelGatewayClient`` 实例（用于调用 reranker API）。
        model: Cross-Encoder 模型名（为空时使用配置值）。
        top_k: 最终返回条数。
        timeout: 超时秒数。

    Returns:
        按 Cross-Encoder 相关度分数降序排列的 Evidence 列表。
    """
    if not merged_hits:
        return []

    documents = [
        hit.content_snippet or hit.title or ""
        for hit in merged_hits
    ]

    try:
        scores = await _call_reranker_api(
            gateway=gateway,
            query=query,
            documents=documents,
            model=model,
            timeout=timeout,
        )
    except Exception:
        logger.warning("Cross-Encoder reranker 调用失败，降级为 MVP 加权求和")
        return rerank(merged_hits, top_k=top_k)

    # 按新分数重排
    scored = list(zip(merged_hits, scores))
    scored.sort(key=lambda x: x[1], reverse=True)

    evidence_model = model or get_settings().rag_reranker_model or "cross-encoder"

    evidences: list[Evidence] = []
    for rank, (hit, score) in enumerate(scored[:top_k], start=1):
        evidence = Evidence(
            evidence_id=uuid.uuid4().hex,
            source_type=hit.source_type,
            source_id=hit.source_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            content_snippet=hit.content_snippet,
            score=round(max(0.0, min(1.0, score)), 6),
            rank=rank,
            metadata={
                "vector_score": hit.vector_score,
                "fulltext_score": hit.fulltext_score,
                "reranker_score": round(score, 6),
                "reranker_model": evidence_model,
                "reranker_provider": "cross_encoder",
            },
        )
        evidences.append(evidence)

    return evidences


async def llm_rerank(
    query: str,
    merged_hits: Sequence[MergedHit],
    *,
    gateway: Any,
    model: str = "",
    top_k: int = 8,
    timeout: float = 5.0,
    trace_id: str = "rag-llm-reranker",
) -> list[Evidence]:
    """使用 LLM 对候选 chunk 做 0-10 分相关性评判。

    一次性将 query + N 个 chunks 送入 LLM，让 LLM 对每个 chunk 打分。
    精度最高但延迟较大。

    Args:
        query: 原始检索 query。
        merged_hits: 合并去重后的候选列表。
        gateway: ``ModelGatewayClient`` 实例（用于 chat 调用）。
        model: LLM 模型名（为空时复用 chat_model）。
        top_k: 最终返回条数。
        timeout: 超时秒数。
        trace_id: 请求链路 ID。
    """
    if not merged_hits:
        return []

    settings = get_settings()
    llm_model = model or settings.rag_reranker_model or settings.chat_model

    chunks_text = "\n\n".join(
        f"[{i}] {hit.title}\n{hit.content_snippet[:300] if hit.content_snippet else ''}"
        for i, hit in enumerate(merged_hits)
    )

    user_message = (
        f"查询：{query}\n\n"
        f"候选文献（共 {len(merged_hits)} 条）：\n"
        f"{chunks_text}\n\n"
        f"请返回 JSON：{{\"scores\": [分数1, 分数2, ...]}}，共 {len(merged_hits)} 个分数。"
    )

    try:
        raw_response = await gateway.chat(
            messages=[
                {"role": "system", "content": _LLM_RERANK_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            model=llm_model,
            temperature=0.0,
            max_tokens=500,
            trace_id=trace_id,
            agent_name="llm_reranker",
        )
        scores = _parse_llm_rerank_scores(raw_response, len(merged_hits))
    except Exception:
        logger.warning("LLM reranker 调用失败，降级为 MVP 加权求和")
        return rerank(merged_hits, top_k=top_k)

    # 按新分数重排
    scored = list(zip(merged_hits, scores))
    scored.sort(key=lambda x: x[1], reverse=True)

    evidence_model = llm_model

    evidences: list[Evidence] = []
    for rank, (hit, score) in enumerate(scored[:top_k], start=1):
        evidence = Evidence(
            evidence_id=uuid.uuid4().hex,
            source_type=hit.source_type,
            source_id=hit.source_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            content_snippet=hit.content_snippet,
            score=round(max(0.0, min(1.0, score)), 6),
            rank=rank,
            metadata={
                "vector_score": hit.vector_score,
                "fulltext_score": hit.fulltext_score,
                "reranker_score": round(score, 6),
                "reranker_model": evidence_model,
                "reranker_provider": "llm",
            },
        )
        evidences.append(evidence)

    return evidences
