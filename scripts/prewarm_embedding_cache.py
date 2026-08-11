"""Embedding 缓存预热脚本（离线 L1 + L2）。

用法::

    # 全量预热（L1 实体名 + L2 模板查询）
    uv run python scripts/prewarm_embedding_cache.py --all

    # 仅实体名（L1）
    uv run python scripts/prewarm_embedding_cache.py --entities

    # 仅模板查询（L2）
    uv run python scripts/prewarm_embedding_cache.py --templates

    # 清除所有预热缓存
    uv run python scripts/prewarm_embedding_cache.py --clear

    # 查看缓存统计
    uv run python scripts/prewarm_embedding_cache.py --stats

    # 预热并评测命中率
    uv run python scripts/prewarm_embedding_cache.py --all --benchmark
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from io import TextIOWrapper
from typing import Any, cast

cast(TextIOWrapper, sys.stdout).reconfigure(encoding='utf-8', errors='replace')


# ---------------------------------------------------------------------------
# 实体名提取（从 PostgreSQL knowledge_chunks 表）
# ---------------------------------------------------------------------------

async def fetch_entity_titles() -> dict[str, list[str]]:
    """从 knowledge_chunks 表提取 herb 和 formula 的 title 列表。

    Returns:
        ``{"herbs": [...], "formulas": [...]}``
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.knowledge import KnowledgeChunk

    factory = get_session_factory()
    async with factory() as session:
        # Herb titles
        herb_result = await session.execute(
            select(KnowledgeChunk.title)
            .where(
                KnowledgeChunk.source_type == "herb",
                KnowledgeChunk.deleted_at.is_(None),
            )
            .distinct()
        )
        herbs = list(herb_result.scalars().all())

        # Formula titles
        formula_result = await session.execute(
            select(KnowledgeChunk.title)
            .where(
                KnowledgeChunk.source_type == "formula",
                KnowledgeChunk.deleted_at.is_(None),
            )
            .distinct()
        )
        formulas = list(formula_result.scalars().all())

    return {"herbs": herbs, "formulas": formulas}


# ---------------------------------------------------------------------------
# 预热执行
# ---------------------------------------------------------------------------

async def _build_gateway() -> Any:
    """构建 embedding 网关客户端（优先用 embedding 专用网关）。"""
    from app.core.config import get_settings
    from app.core.embedding_gateway import build_embedding_gateway_settings
    from app.core.gateway import ModelGatewayClient

    s = get_settings()
    es = build_embedding_gateway_settings(s)
    return ModelGatewayClient(settings=es)


async def run_prewarm_entities(
    gateway: Any,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """L1: 预热 herb + formula title embedding。"""
    from app.rag.embedding_cache import batch_embed_and_cache

    print("  [L1] 提取实体名...")
    titles = await fetch_entity_titles()
    herbs = titles["herbs"]
    formulas = titles["formulas"]
    all_titles = list(dict.fromkeys(herbs + formulas))
    print(f"    herbs={len(herbs)}, formulas={len(formulas)}, unique={len(all_titles)}")

    if dry_run:
        return {"entity_count": len(all_titles), "herbs": len(herbs), "formulas": len(formulas)}
    return await batch_embed_and_cache(all_titles, gateway, trace_id="prewarm-l1")


async def run_prewarm_templates(
    gateway: Any,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """L2: 预热实体 × 模板查询 embedding。"""
    from app.rag.embedding_cache import batch_embed_and_cache, generate_template_queries

    print("  [L2] 生成模板查询...")
    titles = await fetch_entity_titles()
    queries = generate_template_queries(titles["herbs"], titles["formulas"])
    print(f"    templates: {len(queries)} (herbs×{len(titles['herbs'])} + formulas×{len(titles['formulas'])})")

    if dry_run:
        return {"template_count": len(queries)}
    return await batch_embed_and_cache(queries, gateway, trace_id="prewarm-l2")


# ---------------------------------------------------------------------------
# 缓存管理
# ---------------------------------------------------------------------------

async def run_clear() -> int:
    """清空所有 embedding 缓存。"""
    from app.rag.embedding_cache import clear_cache

    n = await clear_cache()
    print(f"  已清除 {n} 条 embedding 缓存")
    return n


async def run_stats() -> dict[str, Any]:
    """打印缓存统计。"""
    from app.rag.embedding_cache import cache_stats

    stats = await cache_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return stats


# ---------------------------------------------------------------------------
# Benchmark: 预热前后命中率对比
# ---------------------------------------------------------------------------

async def run_benchmark(
    gateway: Any,
    *,
    prewarm_first: bool = True,
) -> dict[str, Any]:
    """用一批典型查询对比预热前后的 embedding cache 命中率与延迟。

    Args:
        gateway: embedding 网关客户端。
        prewarm_first: 是否先执行预热再测试（否则测当前缓存状态）。
    """
    from app.rag.embedding_cache import (
        get_embedding,
        set_embedding,
    )

    # 构造测试查询集：实体名 + 模板 + 变体
    titles = await fetch_entity_titles()
    herbs = titles["herbs"][:30]  # 采样 30 味药
    formulas = titles["formulas"][:15]  # 采样 15 个方

    test_queries: list[str] = []
    # L1 等价查询：实体名
    test_queries.extend(herbs)
    test_queries.extend(formulas)
    # L2 等价查询：模板
    from app.rag.embedding_cache import generate_template_queries
    template_qs = generate_template_queries(herbs, formulas)
    test_queries.extend(template_qs[:30])  # 采样 30 条模板
    # 变体查询（不应命中精确缓存）
    variants = [
        "川芎的禁忌与副作用",
        "麻黄汤组成和功效",
        "风寒感冒常用方剂",
        "止咳化痰平喘药",
        "清热泻火解毒方",
    ]
    test_queries.extend(variants)

    # 去重
    test_queries = list(dict.fromkeys(test_queries))

    def _phase(name: str) -> dict[str, Any]:
        return {"phase": name, "total": 0, "hits": 0, "misses": 0, "hit_rate": 0.0, "latency_ms": 0.0}

    # ---- Phase 1: 预热前 ----
    phase1 = _phase("before_prewarm")
    t0 = time.perf_counter()
    for q in test_queries:
        cached = await get_embedding(q)
        if cached is not None:
            phase1["hits"] += 1
        else:
            phase1["misses"] += 1
            # 未命中时走真实 embed → 写缓存
            try:
                vectors = await gateway.embed([q], trace_id="bench-before")
                await set_embedding(q, vectors[0])
            except Exception:
                pass
    phase1["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    phase1["total"] = phase1["hits"] + phase1["misses"]
    phase1["hit_rate"] = round(phase1["hits"] / max(phase1["total"], 1), 3)

    # 计数预热前的已有缓存（在跑完 phase1 后可能已有一些写入）
    pre_existing_hits = phase1["hits"]

    # ---- 如果需要，先预热 ----
    if prewarm_first:
        print("  [bench] 执行预热...")
        await run_prewarm_entities(gateway)
        await run_prewarm_templates(gateway)

    # ---- Phase 2: 预热后 ----
    phase2 = _phase("after_prewarm")
    t0 = time.perf_counter()
    for q in test_queries:
        cached = await get_embedding(q)
        if cached is not None:
            phase2["hits"] += 1
        else:
            phase2["misses"] += 1
    phase2["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    phase2["total"] = phase2["hits"] + phase2["misses"]
    phase2["hit_rate"] = round(phase2["hits"] / max(phase2["total"], 1), 3)

    # ---- 分类统计 ----
    entity_hits = 0
    for q in herbs + formulas:
        if q in test_queries and await get_embedding(q) is not None:
            entity_hits += 1
    template_hits = 0
    for q in template_qs[:30]:
        if q in test_queries and await get_embedding(q) is not None:
            template_hits += 1
    variant_hits = 0
    for q in variants:
        if await get_embedding(q) is not None:
            variant_hits += 1

    result = {
        "test_query_count": len(test_queries),
        "pre_existing_cache_hits": pre_existing_hits,
        "before": phase1,
        "after": phase2,
        "hit_rate_improvement": round(phase2["hit_rate"] - phase1["hit_rate"], 3),
        "category_hits_after": {
            "entity_names": f"{entity_hits}/{len(herbs) + len(formulas)}",
            "template_queries": f"{template_hits}/{min(len(template_qs), 30)}",
            "variants": f"{variant_hits}/{len(variants)}",
        },
    }

    return result


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding 缓存预热")
    parser.add_argument("--all", action="store_true", help="全量预热（L1 + L2）")
    parser.add_argument("--entities", action="store_true", help="仅 L1 实体名预热")
    parser.add_argument("--templates", action="store_true", help="仅 L2 模板查询预热")
    parser.add_argument("--clear", action="store_true", help="清除所有缓存")
    parser.add_argument("--stats", action="store_true", help="显示缓存统计")
    parser.add_argument("--benchmark", action="store_true", help="预热前后命中率对比")
    parser.add_argument("--dry-run", action="store_true", help="仅打印统计，不实际调用 API")
    parser.add_argument(
        "--output", type=str, default="scripts/prewarm_benchmark_result.json",
        help="benchmark 结果输出文件",
    )
    args = parser.parse_args()

    if args.clear:
        await run_clear()
        return

    if args.stats:
        await run_stats()
        return

    # 仅 --all / --entities / --templates / --benchmark 需要 gateway
    needs_gateway = args.all or args.entities or args.templates or args.benchmark
    gateway = await _build_gateway() if needs_gateway else None

    results: dict[str, Any] = {}

    if args.all or args.entities:
        print("=== L1 实体名预热 ===")
        results["l1"] = await run_prewarm_entities(gateway, dry_run=args.dry_run)
        print(f"  Result: {json.dumps(results['l1'], ensure_ascii=False, default=str)}")

    if args.all or args.templates:
        print("=== L2 模板查询预热 ===")
        results["l2"] = await run_prewarm_templates(gateway, dry_run=args.dry_run)
        print(f"  Result: {json.dumps(results['l2'], ensure_ascii=False, default=str)}")

    if args.benchmark:
        print("=== Benchmark: 命中率对比 ===")
        bm_result = await run_benchmark(gateway, prewarm_first=bool(args.all))
        results["benchmark"] = bm_result

        print(f"  Test queries: {bm_result['test_query_count']}")
        print(f"  Before: hit_rate={bm_result['before']['hit_rate']:.1%} "
              f"({bm_result['before']['hits']}/{bm_result['before']['total']})")
        print(f"  After:  hit_rate={bm_result['after']['hit_rate']:.1%} "
              f"({bm_result['after']['hits']}/{bm_result['after']['total']})")
        print(f"  Improvement: +{bm_result['hit_rate_improvement']:.1%}")
        print(f"  Category hits: {bm_result['category_hits_after']}")

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nSaved to {args.output}")

    if gateway is None and not (args.clear or args.stats):
        print("请指定操作: --all, --entities, --templates, --clear, --stats, --benchmark")
    else:
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
