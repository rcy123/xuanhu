"""MVP 重排模块 — 加权分数合并与排序。

重排策略（详细设计 §8.4）：
    final_score = vector_weight * vector_score + fulltext_weight * fulltext_score
                  + source_priority_weight * source_priority

默认权重（可通过 hybrid_search 参数覆盖）：
    vector_weight=0.65, fulltext_weight=0.25, source_priority_weight=0.10

source_priority：
- 命中 primary_sources → 1.0
- 跨库命中 → 0.3

后续可替换为 cross-encoder 或 LLM rerank，但必须保留 evidence_id 和原始得分。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.rag.schemas import Evidence, MergedHit

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
