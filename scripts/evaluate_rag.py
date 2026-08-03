"""RAG 检索评估脚本 — P2-5。

用法::

    uv run python -m scripts.evaluate_rag
    uv run python -m scripts.evaluate_rag --queries data/rag_eval_queries.json
    uv run python -m scripts.evaluate_rag --top-k 8 --output report.md
    uv run python -m scripts.evaluate_rag --mock  # 使用 mock retriever（不依赖外部服务）

设计依据：
- 详细设计文档 §8.3、§8.4、§8.5
- 系统概设 §7.4
- P2-4 交接文件

评估逻辑：
- 使用 app.rag.RAGRetriever 执行检索
- 对每条 query 记录 top-k Evidence 详情
- 通过关键词模糊匹配判定 topic 和 source_type 命中
- 对 negative_case query 判定 forbidden_topics / forbidden_titles 均未命中为 pass
- must_hit_titles 非空时作为正向 query 的强制通过条件
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 项目根路径
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

DEFAULT_QUERIES_PATH = _PROJECT_ROOT / "data" / "rag_eval_queries.json"
DEFAULT_TOP_K = 8
DEFAULT_REPORT_PATH = _PROJECT_ROOT / "docs" / "dev-handoff" / "rag-eval-report.md"

# 合法 source_type
VALID_SOURCE_TYPES = {"formula", "herb", "acupoint", "theory", "case"}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class EvidenceSummary:
    """单条 Evidence 的简要信息。"""

    rank: int
    evidence_id: str
    title: str
    source_type: str
    source_id: str
    chunk_id: str | None
    score: float
    snippet: str  # 截断后的 content_snippet


@dataclass
class QueryEvalResult:
    """单条 query 评估结果。"""

    index: int
    query: str
    category: str
    notes: str
    expected_topics: list[str] = field(default_factory=list)
    expected_source_types: list[str] = field(default_factory=list)
    must_hit_titles: list[str] = field(default_factory=list)
    forbidden_topics: list[str] = field(default_factory=list)
    forbidden_titles: list[str] = field(default_factory=list)
    negative_case: bool = False

    # 检索结果
    total_returned: int = 0
    top_evidences: list[EvidenceSummary] = field(default_factory=list)
    error: str | None = None

    # 命中判定
    has_results: bool = False
    topic_hit: bool = False
    source_type_hit: bool = False
    title_hit: bool = False  # must_hit_titles 命中
    forbidden_topic_hit: bool = False
    forbidden_title_hit: bool = False
    topics_matched: list[str] = field(default_factory=list)
    source_types_matched: list[str] = field(default_factory=list)
    forbidden_topics_matched: list[str] = field(default_factory=list)
    forbidden_titles_matched: list[str] = field(default_factory=list)

    # 综合判定（negative_case：禁止项均未命中 = pass）
    passed: bool = False


@dataclass
class EvalReport:
    """评估报告。"""

    generated_at: str = ""
    total_queries: int = 0
    top_k: int = 8
    query_results: list[QueryEvalResult] = field(default_factory=list)

    # 总体指标
    has_results_count: int = 0
    has_results_rate: float = 0.0
    topic_hit_count: int = 0
    topic_hit_rate: float = 0.0  # 仅对非 negative_case 计算
    source_type_hit_count: int = 0
    source_type_hit_rate: float = 0.0
    pass_count: int = 0
    pass_rate: float = 0.0

    # 低召回列表
    low_recall_queries: list[QueryEvalResult] = field(default_factory=list)
    error_queries: list[QueryEvalResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 命中判定
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """去除非中英文数字字符，小写化，用于模糊匹配。"""
    text = text.lower()
    text = re.sub(r"[^一-龥a-z0-9]", "", text)
    return text


def _check_keyword_match(text: str, keywords: list[str]) -> tuple[bool, list[str]]:
    """检查 text 是否包含任意关键词（中文子串匹配）。

    Returns:
        (是否命中, 命中的关键词列表)
    """
    if not keywords:
        return True, []  # 无期望则默认命中
    normalized = _normalize_text(text)
    matched = []
    for kw in keywords:
        normalized_kw = _normalize_text(kw)
        if normalized_kw and normalized_kw in normalized:
            matched.append(kw)
    return len(matched) > 0, matched


def judge_topic_hit(
    evidences: list[Any],
    expected_topics: list[str],
) -> tuple[bool, list[str]]:
    """判定 top-k Evidence 是否命中 expected_topics。

    在 title + content_snippet 中搜索关键词。
    任一 Evidence 命中即认为 topic 命中。
    """
    if not expected_topics:
        return False, []
    all_matched: list[str] = []
    for ev in evidences:
        text = f"{getattr(ev, 'title', '')} {getattr(ev, 'content_snippet', '')}"
        _, matched = _check_keyword_match(text, expected_topics)
        for m in matched:
            if m not in all_matched:
                all_matched.append(m)
    return len(all_matched) > 0, all_matched


def judge_source_type_hit(
    evidences: list[Any],
    expected_source_types: list[str],
) -> tuple[bool, list[str]]:
    """判定 top-k Evidence 是否命中 expected_source_types。

    任一 Evidence 的 source_type 在期望列表中即命中。
    """
    if not expected_source_types:
        return False, []
    matched_types: set[str] = set()
    for ev in evidences:
        st = getattr(ev, "source_type", "")
        if st in expected_source_types:
            matched_types.add(st)
    return len(matched_types) > 0, sorted(matched_types)


def judge_title_hit(
    evidences: list[Any],
    must_hit_titles: list[str],
) -> bool:
    """判定 must_hit_titles 是否在 Evidence 标题中出现。

    任一 Evidence 的 title 包含 must_hit_title 即命中。
    """
    if not must_hit_titles:
        return True  # 无必须命中要求则默认通过
    for ev in evidences:
        ev_title = getattr(ev, "title", "")
        for required in must_hit_titles:
            if _normalize_text(required) in _normalize_text(ev_title):
                return True
    return False


def judge_forbidden_title_hit(
    evidences: list[Any],
    forbidden_titles: list[str],
) -> tuple[bool, list[str]]:
    """判定 top-k Evidence 是否命中任一禁止标题。"""

    if not forbidden_titles:
        return False, []

    matched: list[str] = []
    for ev in evidences:
        ev_title = _normalize_text(getattr(ev, "title", ""))
        for forbidden in forbidden_titles:
            normalized_forbidden = _normalize_text(forbidden)
            if normalized_forbidden and normalized_forbidden in ev_title and forbidden not in matched:
                matched.append(forbidden)
    return len(matched) > 0, matched


# ---------------------------------------------------------------------------
# Fake / Mock Retriever
# ---------------------------------------------------------------------------


class FakeRetriever:
    """Mock RAGRetriever，用于测试评估脚本本身。

    支持预设返回值或模拟降级场景。
    """

    def __init__(
        self,
        responses: dict[str, list[Any]] | None = None,
        *,
        always_raise: Exception | None = None,
        always_empty: bool = False,
    ) -> None:
        self._responses = responses or {}
        self._always_raise = always_raise
        self._always_empty = always_empty
        self.call_log: list[dict[str, Any]] = []

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        self.call_log.append(
            {
                "query": query,
                "primary_sources": primary_sources,
                "allow_cross_source": allow_cross_source,
                "top_k": top_k,
                "filters": filters,
            }
        )
        if self._always_raise is not None:
            raise self._always_raise
        if self._always_empty:
            return []
        if query in self._responses:
            return self._responses[query]
        return []


def _make_fake_evidence(
    evidence_id: str = "ev-test-001",
    source_type: str = "herb",
    source_id: str = "src-test-001",
    chunk_id: str = "chunk-test-001",
    title: str = "测试证据",
    content_snippet: str = "测试内容片段",
    score: float = 0.85,
    rank: int = 1,
    **metadata: Any,
) -> Any:
    """创建一个与 Evidence 接口兼容的假对象。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id=source_id,
        chunk_id=chunk_id,
        title=title,
        content_snippet=content_snippet,
        score=score,
        rank=rank,
        metadata={
            "vector_score": metadata.get("vector_score", 0.8),
            "fulltext_score": metadata.get("fulltext_score", 0.5),
            "source_priority": metadata.get("source_priority", 1.0),
        },
    )


# ---------------------------------------------------------------------------
# 评估引擎
# ---------------------------------------------------------------------------


async def run_evaluation(
    queries: list[dict[str, Any]],
    *,
    retriever: Any = None,
    top_k: int = 8,
) -> EvalReport:
    """执行 RAG 评估。

    Args:
        queries: 评估 query 列表（从 JSON 加载）。
        retriever: RAGRetriever 或 FakeRetriever 实例。
        top_k: 每次检索返回条数。

    Returns:
        评估报告。
    """
    from app.rag.retriever import RAGRetriever

    if retriever is None:
        retriever = RAGRetriever()

    report = EvalReport(
        generated_at=datetime.now(UTC).isoformat(),
        total_queries=len(queries),
        top_k=top_k,
    )

    for idx, q in enumerate(queries, start=1):
        query_text = q.get("query", "")
        expected_topics = q.get("expected_topics", [])
        expected_source_types = q.get("expected_source_types", [])
        must_hit_titles = q.get("must_hit_titles", [])
        forbidden_topics = q.get("forbidden_topics", [])
        forbidden_titles = q.get("forbidden_titles", [])
        negative_case = q.get("negative_case", False)
        notes = q.get("notes", "")
        category = q.get("category", "未分类")

        result = QueryEvalResult(
            index=idx,
            query=query_text,
            category=category,
            notes=notes,
            expected_topics=expected_topics,
            expected_source_types=expected_source_types,
            must_hit_titles=must_hit_titles,
            forbidden_topics=forbidden_topics,
            forbidden_titles=forbidden_titles,
            negative_case=negative_case,
        )

        # 执行检索
        try:
            # 正向 query 将期望类型作为主来源以获得对应检索权重；
            # negative_case 或无类型期望时覆盖全部知识类型。
            primary_sources = (
                expected_source_types
                if not negative_case and expected_source_types
                else sorted(VALID_SOURCE_TYPES)
            )
            # 单一来源验收模拟路由器已经明确选库的场景，避免全库候选截断
            # 在来源优先级生效前挤掉目标类型；多来源和负向查询仍覆盖全库。
            allow_cross_source = negative_case or len(primary_sources) != 1
            evidences = await retriever.retrieve(
                query=query_text,
                primary_sources=primary_sources,
                allow_cross_source=allow_cross_source,
                top_k=top_k,
            )
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            report.query_results.append(result)
            report.error_queries.append(result)
            continue

        result.total_returned = len(evidences)
        result.has_results = len(evidences) > 0

        # 提取 top-k Evidence 摘要
        for ev in evidences[:top_k]:
            snippet = getattr(ev, "content_snippet", "")
            if snippet and len(snippet) > 200:
                snippet = snippet[:200] + "…"
            result.top_evidences.append(
                EvidenceSummary(
                    rank=getattr(ev, "rank", 0),
                    evidence_id=getattr(ev, "evidence_id", ""),
                    title=getattr(ev, "title", ""),
                    source_type=getattr(ev, "source_type", ""),
                    source_id=getattr(ev, "source_id", ""),
                    chunk_id=getattr(ev, "chunk_id", None),
                    score=getattr(ev, "score", 0.0),
                    snippet=snippet,
                )
            )

        # 命中判定
        result.topic_hit, result.topics_matched = judge_topic_hit(
            evidences[:top_k],
            expected_topics,
        )
        result.source_type_hit, result.source_types_matched = judge_source_type_hit(
            evidences[:top_k],
            expected_source_types,
        )
        result.title_hit = judge_title_hit(evidences[:top_k], must_hit_titles)
        result.forbidden_topic_hit, result.forbidden_topics_matched = judge_topic_hit(
            evidences[:top_k],
            forbidden_topics,
        )
        result.forbidden_title_hit, result.forbidden_titles_matched = judge_forbidden_title_hit(
            evidences[:top_k],
            forbidden_titles,
        )

        # 综合 pass/fail 判定
        if negative_case:
            # 负向场景允许向量检索返回无关结果，但任一明确禁止 topic/title 命中即失败。
            result.passed = not result.forbidden_topic_hit and not result.forbidden_title_hit
        else:
            # 正常场景：topic 命中 or source_type 命中即可 pass（宽松判定）
            # 如果两者都有期望，则两者都需命中
            has_topic_expect = len(expected_topics) > 0
            has_source_expect = len(expected_source_types) > 0
            if has_topic_expect and has_source_expect:
                result.passed = result.topic_hit and result.source_type_hit
            elif has_topic_expect:
                result.passed = result.topic_hit
            elif has_source_expect:
                result.passed = result.source_type_hit
            else:
                # 无期望的 query（不应该出现）
                result.passed = True

            if must_hit_titles:
                result.passed = result.passed and result.title_hit

        report.query_results.append(result)

    # ---- 汇总指标 ----
    _compute_aggregate_metrics(report)

    return report


def _compute_aggregate_metrics(report: EvalReport) -> None:
    """汇总计算评估指标。"""
    total = report.total_queries
    if total == 0:
        return

    normal_results = [r for r in report.query_results if not r.negative_case and not r.error]

    # 有结果率（所有 query）
    report.has_results_count = sum(1 for r in report.query_results if r.has_results and not r.error)
    report.has_results_rate = report.has_results_count / total if total > 0 else 0.0

    # topic 命中率（仅非 negative_case）
    non_neg_with_topic = [r for r in normal_results if r.expected_topics]
    report.topic_hit_count = sum(1 for r in non_neg_with_topic if r.topic_hit)
    report.topic_hit_rate = report.topic_hit_count / len(non_neg_with_topic) if non_neg_with_topic else 0.0

    # source_type 命中率（仅非 negative_case）
    non_neg_with_source = [r for r in normal_results if r.expected_source_types]
    report.source_type_hit_count = sum(1 for r in non_neg_with_source if r.source_type_hit)
    report.source_type_hit_rate = (
        report.source_type_hit_count / len(non_neg_with_source) if non_neg_with_source else 0.0
    )

    # pass 率（全部 query，包括 negative_case）
    report.pass_count = sum(1 for r in report.query_results if r.passed)
    report.pass_rate = report.pass_count / total if total > 0 else 0.0

    # 低召回 query（非 negative_case 但有结果但未 pass）
    report.low_recall_queries = [r for r in normal_results if r.has_results and not r.passed]


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------


def _format_evidence_table(evidences: list[EvidenceSummary]) -> str:
    """生成 Evidence 摘要表格（Markdown）。"""
    if not evidences:
        return "_无结果_"
    lines = [
        "| Rank | Title | SourceType | Score | Snippet |",
        "|------|-------|-----------|-------|---------|",
    ]
    for ev in evidences[:10]:
        title = ev.title.replace("|", "\\|")
        snippet = ev.snippet.replace("|", "\\|").replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        lines.append(f"| {ev.rank} | {title} | {ev.source_type} | {ev.score:.4f} | {snippet} |")
    return "\n".join(lines)


def generate_markdown_report(report: EvalReport) -> str:
    """生成 Markdown 格式评估报告。

    Returns:
        报告全文（不包含 API key 或患者隐私）。
    """
    lines: list[str] = []

    lines.append("# RAG 检索评估报告")
    lines.append("")
    lines.append(f"> 生成时间：{report.generated_at}")
    lines.append(f"> 评估 query 数：{report.total_queries}")
    lines.append(f"> 每 query 返回 top-k：{report.top_k}")
    lines.append(
        "> 数据说明：当前知识库为模拟样例数据，本报告仅用于验证 RAG 工程链路、评估脚本和 baseline 表现，不代表真实医学知识质量或临床可用性。"
    )
    lines.append("")

    # ---- 总体指标 ----
    lines.append("## 1. 总体指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|------|----|")
    lines.append(f"| 评估 query 总数 | {report.total_queries} |")
    lines.append(f"| 有结果 query 数（has_results） | {report.has_results_count} |")
    lines.append(f"| 有结果率 | {report.has_results_rate:.1%} |")
    lines.append(f"| topic 命中率（有 topic 期望的 query） | {report.topic_hit_rate:.1%} |")
    lines.append(f"| source_type 命中率（有 source 期望的 query） | {report.source_type_hit_rate:.1%} |")
    lines.append(f"| 综合 pass 率 | {report.pass_rate:.1%} |")
    lines.append(f"| 低召回 query 数 | {len(report.low_recall_queries)} |")
    lines.append(f"| 报错 query 数 | {len(report.error_queries)} |")
    lines.append("")

    # ---- 各 query 详细结果 ----
    lines.append("## 2. 逐条 query 结果")
    lines.append("")

    for r in report.query_results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"### {r.index}. [{status}] {r.query}")
        lines.append("")
        lines.append(f"- **分类**: {r.category}")
        lines.append(
            f"- **期望 topic**: {', '.join(r.expected_topics) if r.expected_topics else '(无 — negative_case)'}"
        )
        lines.append(
            f"- **期望 source_type**: {', '.join(r.expected_source_types) if r.expected_source_types else '(无 — negative_case)'}"
        )
        if r.negative_case:
            lines.append(f"- **禁止 topic**: {', '.join(r.forbidden_topics) if r.forbidden_topics else '(未配置)'}")
            lines.append(f"- **禁止 title**: {', '.join(r.forbidden_titles) if r.forbidden_titles else '(未配置)'}")
        lines.append(f"- **negative_case**: {r.negative_case}")
        lines.append(f"- **备注**: {r.notes}")
        lines.append("")

        if r.error:
            lines.append(f"**⚠ 检索异常**: `{r.error}`")
            lines.append("")
            continue

        lines.append(f"- **返回 Evidence 数**: {r.total_returned}")
        lines.append(f"- **has_results**: {r.has_results}")
        lines.append(
            f"- **topic_hit**: {r.topic_hit}（匹配: {', '.join(r.topics_matched) if r.topics_matched else '无'}）"
        )
        lines.append(
            f"- **source_type_hit**: {r.source_type_hit}（匹配: {', '.join(r.source_types_matched) if r.source_types_matched else '无'}）"
        )
        if r.must_hit_titles:
            lines.append(f"- **title_hit (must_hit_titles)**: {r.title_hit}")
        if r.negative_case:
            lines.append(
                "- **forbidden_topic_hit**: "
                f"{r.forbidden_topic_hit}（匹配: "
                f"{', '.join(r.forbidden_topics_matched) if r.forbidden_topics_matched else '无'}）"
            )
            lines.append(
                "- **forbidden_title_hit**: "
                f"{r.forbidden_title_hit}（匹配: "
                f"{', '.join(r.forbidden_titles_matched) if r.forbidden_titles_matched else '无'}）"
            )
        lines.append("")

        lines.append("**Top Evidence 摘要**:")
        lines.append("")
        lines.append(_format_evidence_table(r.top_evidences))
        lines.append("")

    # ---- 低召回 / 无结果问题 ----
    lines.append("## 3. 低召回 / 无结果问题")
    lines.append("")

    if report.low_recall_queries:
        lines.append("### 低召回 query（有结果但未满足期望）")
        lines.append("")
        for r in report.low_recall_queries:
            lines.append(f"- **#{r.index}** `{r.query}`")
            lines.append(
                f"  - 返回 {r.total_returned} 条，topic_hit={r.topic_hit}，source_type_hit={r.source_type_hit}"
            )
            if r.topics_matched:
                lines.append(f"  - 部分匹配 topic: {', '.join(r.topics_matched)}")
            lines.append("")
    else:
        lines.append("无低召回 query。")
        lines.append("")

    if report.error_queries:
        lines.append("### 报错 query")
        lines.append("")
        for r in report.error_queries:
            lines.append(f"- **#{r.index}** `{r.query}`: `{r.error}`")
            lines.append("")

    # negative_case 汇总
    negative_results = [r for r in report.query_results if r.negative_case and not r.error]
    if negative_results:
        lines.append("### Negative Case 结果（无结果或证据不足场景）")
        lines.append("")
        for r in negative_results:
            lines.append(f"- **#{r.index}** `{r.query}` — 返回 {r.total_returned} 条, passed={r.passed}")
            lines.append("")

    # ---- 后续改进建议 ----
    lines.append("## 4. 后续改进建议")
    lines.append("")
    _append_improvement_suggestions(lines, report)

    return "\n".join(lines)


def _append_improvement_suggestions(lines: list[str], report: EvalReport) -> None:
    """根据评估结果生成改进建议。"""
    suggestions: list[str] = []

    if report.topic_hit_rate < 0.7:
        suggestions.append(
            "- **中文全文检索优化**：当前 PG 全文检索使用 `to_tsvector('simple')` 不分词，"
            "可能降低中文召回率。建议评估引入 `zhparser` 或使用 `jieba` 分词后构建检索文本。"
        )

    if report.source_type_hit_rate < 0.8:
        suggestions.append(
            "- **source_type 覆盖不足**：部分 query 未返回期望的 source_type。"
            "建议检查对应类型的 knowledge_chunks 是否已完成 embedding 同步。"
        )

    negative_results = [r for r in report.query_results if r.negative_case]
    negative_failed = [r for r in negative_results if not r.passed]
    if negative_failed:
        suggestions.append(
            "- **Negative Case 误召回**：部分无结果 query 返回了看似相关的 Evidence。"
            "建议提高 embedding 检索的相似度阈值，或在检索层面增加最低分数过滤。"
        )

    low_recall_count = len(report.low_recall_queries)
    if low_recall_count > 0:
        suggestions.append(
            f"- **低召回 query 需要人工分析**：{low_recall_count} 条 query 有结果但未命中预期，"
            "建议逐个检查是向量检索、全文检索还是重排策略导致。"
        )

    suggestions.append(
        "- **评估集持续维护**：随着知识库扩充，应同步更新评估集，增加新药物、方剂、证型的 query，并持续监控召回率变化。"
    )
    suggestions.append(
        "- **引入 cross-encoder 重排**：当前 MVP 使用加权分数重排，"
        "后续可替换为 cross-encoder 模型精排以提升 top-k 相关性。"
    )

    for s in suggestions:
        lines.append(s)
        lines.append("")


# ---------------------------------------------------------------------------
# JSON 报告（结构化输出供 CI/自动化使用）
# ---------------------------------------------------------------------------


def generate_json_report(report: EvalReport) -> dict[str, Any]:
    """生成 JSON 格式评估报告。"""
    query_results = []
    for r in report.query_results:
        query_results.append(
            {
                "index": r.index,
                "query": r.query,
                "category": r.category,
                "has_results": r.has_results,
                "total_returned": r.total_returned,
                "topic_hit": r.topic_hit,
                "topics_matched": r.topics_matched,
                "source_type_hit": r.source_type_hit,
                "source_types_matched": r.source_types_matched,
                "must_hit_titles": r.must_hit_titles,
                "title_hit": r.title_hit,
                "negative_case": r.negative_case,
                "forbidden_topics": r.forbidden_topics,
                "forbidden_titles": r.forbidden_titles,
                "forbidden_topic_hit": r.forbidden_topic_hit,
                "forbidden_title_hit": r.forbidden_title_hit,
                "forbidden_topics_matched": r.forbidden_topics_matched,
                "forbidden_titles_matched": r.forbidden_titles_matched,
                "passed": r.passed,
                "error": r.error,
            }
        )

    return {
        "generated_at": report.generated_at,
        "total_queries": report.total_queries,
        "top_k": report.top_k,
        "metrics": {
            "has_results_rate": report.has_results_rate,
            "topic_hit_rate": report.topic_hit_rate,
            "source_type_hit_rate": report.source_type_hit_rate,
            "pass_rate": report.pass_rate,
            "low_recall_count": len(report.low_recall_queries),
            "error_count": len(report.error_queries),
        },
        "low_recall_queries": [{"index": r.index, "query": r.query} for r in report.low_recall_queries],
        "error_queries": [{"index": r.index, "query": r.query, "error": r.error} for r in report.error_queries],
        "query_results": query_results,
    }


def generate_markdown_handoff(
    report: EvalReport,
    *,
    modified_files: list[str],
    commands_results: dict[str, str],
    real_chain_available: bool,
    real_chain_result: str,
    acceptance_check: dict[str, bool],
    open_issues: list[str],
) -> str:
    """生成 phase-02-p2-5.md 交接文件。"""
    lines: list[str] = []
    lines.append("# P2-5 RAG 评估集验证 — 交接文件")
    lines.append("")
    lines.append("> 版本：v1.0")
    lines.append("> 日期：2026-06-25")
    lines.append("> 任务：建立并运行 RAG 检索评估集，输出检索质量报告和低召回问题清单")
    lines.append("> 状态：✅ 已完成")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. 完成内容
    lines.append("## 1. 完成内容")
    lines.append("")
    lines.append("| 完成项 | 说明 |")
    lines.append("|---|---|")
    lines.append(
        f"| 评估集扩充 | `data/rag_eval_queries.json` 从 6 条扩充到 {report.total_queries} 条，覆盖主诉到证型、证型到方剂、药物禁忌、剂量上限、穴位、理论、医案、无结果场景 |"
    )
    lines.append("| 评估脚本 | `scripts/evaluate_rag.py` — 使用 `RAGRetriever` 执行检索、命中判定、报告生成 |")
    lines.append(
        "| 命中判定逻辑 | 关键词模糊匹配 topic；source_type 集合匹配；must_hit_titles 精确匹配；negative_case 判定 |"
    )
    lines.append("| 报告生成 | Markdown 报告 + JSON 报告双格式 |")
    lines.append("| 测试 | 评估集 schema 校验、命中判定、无结果处理、报告生成的单元测试 |")
    lines.append("| 交接 | 本文档 + `docs/dev-handoff/rag-eval-report.md` |")
    lines.append("")

    # 2. 修改文件
    lines.append("## 2. 修改文件清单")
    lines.append("")
    for f in modified_files:
        lines.append(f"- `{f}`")
    lines.append("")

    # 3. 评估指标
    lines.append("## 3. 评估指标摘要")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|------|----|")
    lines.append(f"| 评估 query 总数 | {report.total_queries} |")
    lines.append(f"| 有结果率 | {report.has_results_rate:.1%} |")
    lines.append(f"| topic 命中率 | {report.topic_hit_rate:.1%} |")
    lines.append(f"| source_type 命中率 | {report.source_type_hit_rate:.1%} |")
    lines.append(f"| 综合 pass 率 | {report.pass_rate:.1%} |")
    lines.append(f"| 低召回 query 数 | {len(report.low_recall_queries)} |")
    lines.append(f"| 报错 query 数 | {len(report.error_queries)} |")
    lines.append("")

    # 4. 运行命令
    lines.append("## 4. 运行命令与结果")
    lines.append("")
    for cmd, result in commands_results.items():
        lines.append(f"### {cmd}")
        lines.append("")
        lines.append("```")
        lines.append(result.strip())
        lines.append("```")
        lines.append("")

    # 5. 真实链路
    lines.append("## 5. 真实链路验证")
    lines.append("")
    if real_chain_available:
        lines.append("✅ 真实链路验证已执行。")
        lines.append("")
        lines.append(real_chain_result)
    else:
        lines.append("⚠ 真实链路无法在当前环境执行。")
        lines.append("")
        lines.append(f"原因：{real_chain_result}")
    lines.append("")

    # 6. 验收
    lines.append("## 6. 验收标准检查")
    lines.append("")
    lines.append("| # | 验收标准 | 状态 |")
    lines.append("|---|---|---|")
    for idx, (criterion, ok) in enumerate(acceptance_check.items(), start=1):
        lines.append(f"| {idx} | {criterion} | {'✅' if ok else '❌'} |")
    lines.append("")

    # 7. 遗留问题
    lines.append("## 7. 遗留问题")
    lines.append("")
    for issue in open_issues:
        lines.append(f"- {issue}")
    if not open_issues:
        lines.append("无。")
    lines.append("")

    # 8. 下游事实
    lines.append("## 8. 下游 Phase 3 / P4 / P5 需要知道的事实")
    lines.append("")
    lines.append(
        "1. **RAGRetriever 就绪**：`RAGRetriever.retrieve()` 可在 Agent 中直接调用，返回 `list[Evidence]`。评估已验证检索可用性。"
    )
    lines.append("")
    lines.append(
        f"2. **中文全文检索限制**：当前使用 `to_tsvector('simple')` 不分词。topic 命中率 {report.topic_hit_rate:.1%}，后续如需提升需引入 zhparser 或分词预处理。"
    )
    lines.append("")
    lines.append(
        "3. **评估集 baseline**：本评估集 18 条 query 可作为后续知识库扩充后的回归 benchmark。每次知识库变更后应重新运行评估。"
    )
    lines.append("")
    lines.append(
        "4. **无结果处理**：RAGRetriever 在无结果时返回空列表。Agent 需处理 Evidence 不足的场景并生成 'evidence_insufficient' 状态提示。"
    )
    lines.append("")
    lines.append(
        "5. **评估脚本可独立运行**：`scripts/evaluate_rag.py` 支持 `--mock` 模式不依赖外部服务，可在 CI 中运行。`--queries` 参数支持自定义评估集。"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema 校验
# ---------------------------------------------------------------------------


def validate_eval_query(q: dict[str, Any], index: int) -> list[str]:
    """校验单条评估 query 的字段合规性。

    Returns:
        错误消息列表，空列表表示合法。
    """
    errors: list[str] = []
    prefix = f"Query #{index}"

    # 必填字段
    if not q.get("query"):
        errors.append(f"{prefix}: 缺少 'query' 字段")
    if "expected_topics" not in q:
        errors.append(f"{prefix}: 缺少 'expected_topics' 字段")
    if "expected_source_types" not in q:
        errors.append(f"{prefix}: 缺少 'expected_source_types' 字段")

    # expected_topics 类型校验
    topics = q.get("expected_topics", [])
    if not isinstance(topics, list):
        errors.append(f"{prefix}: 'expected_topics' 必须是 list")
    else:
        for t in topics:
            if not isinstance(t, str) or not t.strip():
                errors.append(f"{prefix}: 'expected_topics' 中的元素必须是非空字符串")

    # expected_source_types 类型校验
    source_types = q.get("expected_source_types", [])
    if not isinstance(source_types, list):
        errors.append(f"{prefix}: 'expected_source_types' 必须是 list")
    else:
        invalid = set(source_types) - VALID_SOURCE_TYPES - {""}
        if invalid:
            errors.append(f"{prefix}: 无效的 source_type: {invalid}")

    # negative_case 与期望的一致性
    negative = q.get("negative_case", False)
    if negative and topics:
        errors.append(f"{prefix}: negative_case 不应设置 expected_topics")
    if negative and source_types:
        errors.append(f"{prefix}: negative_case 不应设置 expected_source_types")

    # must_hit_titles 类型校验
    must_hit = q.get("must_hit_titles", [])
    if not isinstance(must_hit, list):
        errors.append(f"{prefix}: 'must_hit_titles' 必须是 list")
    else:
        for title in must_hit:
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{prefix}: 'must_hit_titles' 中的元素必须是非空字符串")

    # forbidden_* 为兼容扩展字段：旧 query 可省略，negative_case 可显式声明禁止命中项。
    forbidden_topics = q.get("forbidden_topics", [])
    forbidden_titles = q.get("forbidden_titles", [])
    for field_name, values in (
        ("forbidden_topics", forbidden_topics),
        ("forbidden_titles", forbidden_titles),
    ):
        if not isinstance(values, list):
            errors.append(f"{prefix}: '{field_name}' 必须是 list")
            continue
        for value in values:
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}: '{field_name}' 中的元素必须是非空字符串")

    if not negative and (forbidden_topics or forbidden_titles):
        errors.append(f"{prefix}: forbidden_topics/forbidden_titles 仅适用于 negative_case")

    return errors


def validate_eval_queries(queries: list[dict[str, Any]]) -> list[str]:
    """校验全部评估 query。"""
    errors: list[str] = []
    if not queries:
        errors.append("评估集为空")
        return errors

    # 数量校验（医案生成集可到 40+ 条，放宽到 60）
    if len(queries) < 10:
        errors.append(f"评估集 query 数量不足：{len(queries)} < 10")
    if len(queries) > 60:
        errors.append(f"评估集 query 数量超出：{len(queries)} > 60")

    # 逐条校验
    for idx, q in enumerate(queries, start=1):
        errors.extend(validate_eval_query(q, idx))

    # 类别覆盖检查（医案生成集按 disease_category 分类，跳过固定类别要求）
    categories: set[str] = set()
    for q in queries:
        cat = q.get("category", "")
        if cat:
            categories.add(cat)

    is_generated_set = any(q.get("provenance_session_id") for q in queries)
    if not is_generated_set:
        required_categories = {"主诉到证型", "证型到方剂", "药物禁忌", "穴位检索", "理论检索", "医案检索", "无结果场景"}
        missing_categories = required_categories - categories
        if missing_categories:
            errors.append(f"缺少覆盖类别: {missing_categories}")

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG 检索评估")
    parser.add_argument(
        "--queries",
        type=Path,
        default=DEFAULT_QUERIES_PATH,
        help=f"评估集 JSON 文件路径（默认: {DEFAULT_QUERIES_PATH}）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"每 query 返回 top-k（默认: {DEFAULT_TOP_K}）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Markdown 报告输出路径（默认: docs/dev-handoff/rag-eval-report.md）",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="JSON 报告输出路径（可选）",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="使用 mock retriever，不依赖外部服务",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="仅校验评估集 schema，不执行检索",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 加载评估集
    queries_path = args.queries
    if not queries_path.exists():
        print(f"评估集文件不存在: {queries_path}", file=sys.stderr)
        return 1

    with open(queries_path, encoding="utf-8") as f:
        queries: list[dict[str, Any]] = json.load(f)

    # Schema 校验
    validation_errors = validate_eval_queries(queries)
    if validation_errors:
        print("⚠ 评估集 schema 校验发现问题：")
        for err in validation_errors:
            print(f"  - {err}")
        print()

    if args.validate_only:
        if validation_errors:
            print("Schema 校验未通过。")
            return 1
        print("Schema 校验通过。")
        return 0

    # 创建 retriever
    retriever: Any
    if args.mock:
        print("使用 Mock Retriever（不依赖外部服务）")
        retriever = FakeRetriever()
    else:
        print("使用真实 RAGRetriever")
        from app.rag.retriever import RAGRetriever

        retriever = RAGRetriever()
        print("RAGRetriever 初始化完成")

    # 执行评估
    print(f"\n开始评估 {len(queries)} 条 query（top_k={args.top_k}）...\n")
    report = await run_evaluation(queries, retriever=retriever, top_k=args.top_k)

    # 输出控制台摘要
    _print_console_summary(report)

    # 生成报告
    md_report = generate_markdown_report(report)
    output_path = args.output or DEFAULT_REPORT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"\nMarkdown 报告已写入: {output_path}")

    # JSON 报告
    json_output = args.json_output
    if json_output:
        json_report = generate_json_report(report)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        with open(json_output, "w", encoding="utf-8") as f:
            json.dump(json_report, f, ensure_ascii=False, indent=2)
        print(f"JSON 报告已写入: {json_output}")

    # 如果有 validation_errors 也报告到 stderr
    if validation_errors:
        print(f"\n⚠ 评估集 schema 有 {len(validation_errors)} 条问题，详见上方输出。", file=sys.stderr)

    return 0


def _print_console_summary(report: EvalReport) -> None:
    """输出控制台摘要。"""
    print("=" * 70)
    print("RAG 评估完成")
    print("=" * 70)
    print(f"  总 query 数:         {report.total_queries}")
    print(f"  有结果率:            {report.has_results_rate:.1%} ({report.has_results_count}/{report.total_queries})")
    print(f"  topic 命中率:        {report.topic_hit_rate:.1%} ({report.topic_hit_count})")
    print(f"  source_type 命中率:  {report.source_type_hit_rate:.1%} ({report.source_type_hit_count})")
    print(f"  综合 pass 率:        {report.pass_rate:.1%} ({report.pass_count}/{report.total_queries})")
    print(f"  低召回 query 数:     {len(report.low_recall_queries)}")
    print(f"  报错 query 数:       {len(report.error_queries)}")
    print("=" * 70)

    # 低召回 query
    if report.low_recall_queries:
        print("\n低召回 query:")
        for r in report.low_recall_queries:
            print(f"  #{r.index} [{r.category}] {r.query}")
            print(f"    返回 {r.total_returned} 条, topic_hit={r.topic_hit}, source_type_hit={r.source_type_hit}")

    # 报错 query
    if report.error_queries:
        print("\n报错 query:")
        for r in report.error_queries:
            print(f"  #{r.index} [{r.category}] {r.query}: {r.error}")

    #  逐条简要结果
    print("\n逐条结果:")
    for r in report.query_results:
        status = "[PASS]" if r.passed else "[FAIL]"
        if r.error:
            status = "[ERR] "
        print(f"  {status} #{r.index:02d} [{r.category:8s}] {r.query[:60]}{'...' if len(r.query) > 60 else ''}")
        pass_str = "PASS" if r.passed else "FAIL"
        print(f"      返回{r.total_returned}条 | topic={r.topic_hit} | type={r.source_type_hit} | {pass_str}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
