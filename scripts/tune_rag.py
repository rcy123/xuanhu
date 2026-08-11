"""RAG 参数网格调优：top_k × 重排权重 → IR 指标对比 + 推荐参数。

网格：
    top_k ∈ {6, 8, 12}
    重排权重 (vector_weight, fulltext_weight) ∈ {(0.65, 0.25), (0.70, 0.20), (0.55, 0.35)}
    （source_priority_weight 由 1 - v - f 自动推导，见 retriever.hybrid_search）

对每组参数复用 scripts.evaluate_rag 的 run_evaluation / IR 指标判定，
输出对比表与推荐参数（评分 = 0.4*pass_rate + 0.25*recall@k + 0.2*MRR + 0.15*NDCG@k，
仅统计非 negative_case；negative 零误召回为硬门槛，违反则降级为不推荐）。

用法:
    uv run python -m scripts.tune_rag [--queries data/rag_eval_queries_cases.json] [--top-ks 6,8,12]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import sys
from io import TextIOWrapper
from pathlib import Path
from typing import Any, cast

cast(TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from scripts.evaluate_rag import (  # noqa: E402
    DEFAULT_QUERIES_PATH,
    run_evaluation,
    validate_eval_queries,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "dev-handoff" / "rag-tune-report.md"

# 网格定义
DEFAULT_TOP_KS = (6, 8, 12)
DEFAULT_WEIGHTS = (
    (0.65, 0.25),  # 默认（现状）
    (0.70, 0.20),  # 更偏向量
    (0.55, 0.35),  # 更偏全文
)


class _WeightedRetriever:
    """RAGRetriever 薄包装：retrieve 转发到 hybrid_search 并注入重排权重。

    保持与 RAGRetriever.retrieve 相同的默认值语义（cross-source 覆盖全库、
    primary_sources 用于 source_priority 加权），仅替换权重与 top_k 参数。
    """

    def __init__(self, *, retriever: Any, vector_weight: float, fulltext_weight: float) -> None:
        self._retriever = retriever
        self._vector_weight = vector_weight
        self._fulltext_weight = fulltext_weight

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        from app.rag.schemas import VALID_SOURCE_TYPES

        settings = self._retriever._settings
        vector_top_k = getattr(settings, "rag_top_k_vector", 12)
        fulltext_top_k = getattr(settings, "rag_top_k_fulltext", 12)
        search_sources = (
            list(VALID_SOURCE_TYPES) if allow_cross_source else list(primary_sources)
        )
        return cast(list[Any], await self._retriever.hybrid_search(
            query=query,
            sources=search_sources,
            primary_sources=set(primary_sources),
            vector_weight=self._vector_weight,
            fulltext_weight=self._fulltext_weight,
            vector_top_k=vector_top_k,
            fulltext_top_k=fulltext_top_k,
            top_k=top_k,
            filters=filters,
        ))


def _combine_score(report: Any) -> float:
    """推荐评分：pass 为主、recall 次之、MRR/NDCG 修正。"""
    return float(
        0.40 * report.pass_rate
        + 0.25 * report.avg_recall_at_k
        + 0.20 * report.avg_mrr
        + 0.15 * report.avg_ndcg_at_k
    )


def _has_negative_false_positive(report: Any) -> bool:
    """negative_case query 不应命中 forbidden_topic/forbidden_title；命中即误召回。

    直接读取 run_evaluation 已判定的字段（forbidden_topic_hit/forbidden_title_hit）。
    """
    for result in report.query_results:
        if not result.negative_case:
            continue
        if result.forbidden_topic_hit or result.forbidden_title_hit:
            return True
    return False


async def _tune(
    queries: list[dict[str, Any]],
    *,
    retriever: Any,
    top_ks: tuple[int, ...],
    weights: tuple[tuple[float, float], ...],
) -> list[dict[str, Any]]:
    """跑完整网格，返回每组合的指标行。"""
    rows: list[dict[str, Any]] = []
    for top_k in top_ks:
        for v_w, f_w in weights:
            label = f"top_k={top_k} v={v_w:.2f} f={f_w:.2f}"
            print(f"  评估 {label} ...", flush=True)
            weighted = _WeightedRetriever(
                retriever=retriever, vector_weight=v_w, fulltext_weight=f_w
            )
            report = await run_evaluation(queries, retriever=weighted, top_k=top_k)
            negative_fp = _has_negative_false_positive(report)
            score = _combine_score(report)
            rows.append(
                {
                    "top_k": top_k,
                    "vector_weight": v_w,
                    "fulltext_weight": f_w,
                    "pass_rate": round(report.pass_rate, 4),
                    "recall_at_k": round(report.avg_recall_at_k, 4),
                    "precision_at_k": round(report.avg_precision_at_k, 4),
                    "mrr": round(report.avg_mrr, 4),
                    "ndcg_at_k": round(report.avg_ndcg_at_k, 4),
                    "topic_hit_rate": round(report.topic_hit_rate, 4),
                    "has_results_rate": round(report.has_results_rate, 4),
                    "error_queries": len(report.error_queries),
                    "negative_false_positive": negative_fp,
                    "score": round(score, 4),
                }
            )
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    header = (
        f"{'top_k':<6} {'v':<6} {'f':<6} {'pass':<7} {'recall@k':<9} "
        f"{'prec@k':<8} {'MRR':<7} {'NDCG@k':<8} {'topic':<7} {'err':<4} {'negFP':<6} {'score':<7}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['top_k']:<6} {row['vector_weight']:<6.2f} {row['fulltext_weight']:<6.2f} "
            f"{row['pass_rate']:<7.2f} {row['recall_at_k']:<9.4f} {row['precision_at_k']:<8.4f} "
            f"{row['mrr']:<7.4f} {row['ndcg_at_k']:<8.4f} {row['topic_hit_rate']:<7.2f} "
            f"{row['error_queries']:<4} {'YES' if row['negative_false_positive'] else 'no':<6} "
            f"{row['score']:<7.4f}"
        )


def _pick_best(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """推荐参数：硬门槛（无 negFP、无 error）内取最高 score。"""
    eligible = [r for r in rows if not r["negative_false_positive"] and r["error_queries"] == 0]
    if not eligible:
        return {"reason": "全部组合存在 negative 误召回或检索错误，暂不推荐"}
    best = max(eligible, key=lambda r: r["score"])
    return {
        "top_k": best["top_k"],
        "vector_weight": best["vector_weight"],
        "fulltext_weight": best["fulltext_weight"],
        "score": best["score"],
        "reason": "最高综合评分",
    }


def _write_report(rows: list[dict[str, Any]], best: dict[str, Any], queries_path: Path) -> None:
    lines = [
        "# RAG 参数网格调优报告",
        "",
        f"- 评估集: `{queries_path}`（{len(rows)} 组参数）",
        "- 指标口径: 复用 scripts/evaluate_rag 的 IR 判定（recall@k/precision@k/MRR/NDCG@k）",
        "- 推荐评分 = 0.4×pass_rate + 0.25×recall@k + 0.2×MRR + 0.15×NDCG@k；negative 误召回为硬门槛",
        "",
        "## 对比表",
        "",
        "| top_k | v | f | pass_rate | recall@k | prec@k | MRR | NDCG@k | topic_hit | err | negFP | score |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['top_k']} | {row['vector_weight']:.2f} | {row['fulltext_weight']:.2f} "
            f"| {row['pass_rate']:.2f} | {row['recall_at_k']:.4f} | {row['precision_at_k']:.4f} "
            f"| {row['mrr']:.4f} | {row['ndcg_at_k']:.4f} | {row['topic_hit_rate']:.2f} "
            f"| {row['error_queries']} | {'YES' if row['negative_false_positive'] else 'no'} "
            f"| {row['score']:.4f} |"
        )
    lines += ["", "## 推荐参数", ""]
    if "reason" in best:
        lines.append(f"- **不推荐**：{best['reason']}")
    else:
        lines.extend(
            [
                f"- top_k = **{best['top_k']}**",
                f"- vector_weight = **{best['vector_weight']:.2f}**",
                f"- fulltext_weight = **{best['fulltext_weight']:.2f}**",
                f"- 综合评分 = {best['score']:.4f}",
            ]
        )
    lines += ["", "## 如何应用", ""]
    lines += [
        "- 将推荐值写入 `app/core/config.py`：`rag_syndrome_top_k` / `rag_formula_top_k`（top_k）。",
        "- 权重当前为 retriever 默认常量（`app/rag/reranker.py` 的 DEFAULT_VECTOR_WEIGHT 等）；"
        "如需固化，将权重提为 config 字段并在 `hybrid_search` 调用处注入。",
    ]
    report_text = "\n".join(lines)
    DEFAULT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT_PATH.write_text(report_text, encoding="utf-8")
    print(f"\n调优报告已写入: {DEFAULT_REPORT_PATH}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG 参数网格调优")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES_PATH, help="评估集 JSON")
    parser.add_argument("--top-ks", type=str, default="6,8,12", help="逗号分隔的 top_k 列表")
    parser.add_argument("--weights", type=str, default="0.65-0.25,0.70-0.20,0.55-0.35", help="逗号分隔的 v-f 权重对")
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.queries.exists():
        print(f"评估集文件不存在: {args.queries}", file=sys.stderr)
        return 1

    top_ks = tuple(int(x) for x in args.top_ks.split(",") if x.strip())
    weights = tuple(
        (float(part[0]), float(part[1]))
        for part in (pair.split("-") for pair in args.weights.split(",") if pair.strip())
    )
    print(f"网格: top_k={top_ks} × weights={weights}")

    with open(args.queries, encoding="utf-8") as f:
        queries: list[dict[str, Any]] = json.load(f)
    errors = validate_eval_queries(queries)
    if errors:
        print(f"⚠ 评估集 schema 校验有 {len(errors)} 条问题（继续执行）")

    from app.rag.retriever import RAGRetriever

    print("初始化 RAGRetriever ...")
    retriever = RAGRetriever()
    rows = await _tune(queries, retriever=retriever, top_ks=top_ks, weights=weights)

    print(f"\n{len(queries)} 条 query × {len(top_ks) * len(weights)} 组参数对比：")
    _print_table(rows)

    best = _pick_best(rows)
    print("\n推荐参数:", json.dumps(best, ensure_ascii=False))
    _write_report(rows, best, args.queries)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(
        _main(argv),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


if __name__ == "__main__":
    sys.exit(main())
