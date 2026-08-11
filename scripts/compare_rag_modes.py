"""no-RAG vs RAG 端到端对比（存量会话聚合，无需重跑后端）。

数据源：artifact_revisions(status='current') 的 syndrome_draft/formula_draft payload。
按 run_spec.policy_version 分组：含 ".rag.v1" → RAG，否则 no-RAG。

指标（按 stage 分组）：
    confidence 分布（mean/min/max）
    evidence_mode 分布
    取证引用率（run_artifact.evidence_ids 非空比例）
    引用真实性（claim_evidence_links 的 evidence_id 均 ⊆ evidence_ids）
    verifier 通过率（verification.checks 全 passed 的 payload 比例）
    retry 率（run_artifact.attempts > 1）
    review_required 率（output.review_required）

另输出 agent_evidences 落库行数（RAG 证据持久化规模）。

用法:
    uv run python -m scripts.compare_rag_modes [--output docs/dev-handoff/rag-vs-norag-compare.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import statistics
import sys
from io import TextIOWrapper
from pathlib import Path
from typing import Any, cast

cast(TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from app.db.session import get_session_factory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "dev-handoff" / "rag-vs-norag-compare.md"
RAG_POLICY_MARKER = ".rag.v1"
STAGES = ("syndrome_draft", "formula_draft")


def _mode(policy_version: str) -> str:
    return "rag" if RAG_POLICY_MARKER in policy_version else "no-rag"


def _conf(text_value: Any) -> float | None:
    try:
        return float(text_value)
    except (TypeError, ValueError):
        return None


def _fq(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _stat_line(values: list[float]) -> str:
    if not values:
        return "—"
    mean = statistics.mean(values)
    return f"mean {mean:.3f}（min {min(values):.2f} / max {max(values):.2f}）"


def _aggregate_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """对一组 payload 汇总指标。"""
    confidences = [c for c in (_conf(p.get("output", {}).get("confidence")) for p in payloads) if c is not None]
    evidence_ids_nonempty = 0
    total_links = 0
    links_valid = 0
    verifier_pass = 0
    retried = 0
    review_required = 0
    mode_dist: dict[str, int] = {}
    for p in payloads:
        out = p.get("output") or {}
        mode = out.get("evidence_mode", "unknown")
        mode_dist[mode] = mode_dist.get(mode, 0) + 1

        run_artifact = p.get("run_artifact") or {}
        ids = set(run_artifact.get("evidence_ids") or [])
        if ids:
            evidence_ids_nonempty += 1
        for link in out.get("claim_evidence_links") or []:
            total_links += 1
            if link.get("evidence_id") in ids:
                links_valid += 1

        checks = (p.get("verification") or {}).get("checks") or []
        if checks and all(c.get("status") == "passed" for c in checks):
            verifier_pass += 1
        if (run_artifact.get("attempts") or 1) > 1:
            retried += 1
        if out.get("review_required"):
            review_required += 1

    n = len(payloads)
    return {
        "count": n,
        "confidence": _stat_line(confidences),
        "confidence_raw": confidences,
        "mode_dist": mode_dist,
        "evidence_citation_rate": _fq(evidence_ids_nonempty / n) if n else "—",
        "link_authenticity": f"{links_valid}/{total_links}" if total_links else "无链接",
        "verifier_pass_rate": _fq(verifier_pass / n) if n else "—",
        "retry_rate": _fq(retried / n) if n else "—",
        "review_required_rate": _fq(review_required / n) if n else "—",
    }


async def _load_payloads(db: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 stage 读取全部 current payload，返回 (no_rag, rag) 两桶。"""
    buckets: dict[str, list[dict[str, Any]]] = {"no-rag": [], "rag": []}
    for stage in STAGES:
        rows = (
            await db.execute(
                text(
                    "SELECT arp.payload FROM artifact_revision_payloads arp "
                    "JOIN artifact_revisions ar ON ar.id = arp.artifact_revision_id "
                    "WHERE ar.status='current' AND ar.artifact_type=:stage"
                ),
                {"stage": stage},
            )
        ).all()
        for (payload,) in rows:
            if not isinstance(payload, dict):
                payload = json.loads(payload)
            policy = ((payload.get("run_spec") or {}).get("policy_version")) or ""
            buckets[_mode(policy)].append(payload)
    return buckets["no-rag"], buckets["rag"]


async def _count_agent_evidences(db: Any, no_rag: list[dict[str, Any]], rag: list[dict[str, Any]]) -> dict[str, int]:
    """按 policy 统计 agent_evidences 落库行数（通过 run_spec.run_id ↔ agent_run_id 关联）。"""
    rag_run_ids = {(p.get("run_spec") or {}).get("run_id") for p in rag}
    no_rag_run_ids = {(p.get("run_spec") or {}).get("run_id") for p in no_rag}
    rows = (await db.execute(text("SELECT agent_run_id FROM agent_evidences"))).all()
    total = len(rows)
    rag_count = sum(1 for (rid,) in rows if rid in rag_run_ids)
    no_rag_count = sum(1 for (rid,) in rows if rid in no_rag_run_ids)
    return {
        "total": total,
        "rag": rag_count,
        "no_rag": no_rag_count,
        "other": total - rag_count - no_rag_count,
    }


def _render_markdown(no_rag_agg: dict[str, Any], rag_agg: dict[str, Any], by_stage: dict[str, Any], evidences: dict[str, Any]) -> str:
    lines = [
        "# no-RAG vs RAG 端到端对比",
        "",
        "- 数据源: `artifact_revisions`(current) 全部 syndrome_draft/formula_draft payload",
        "- 分组: policy_version 含 `{RAG_POLICY_MARKER}` → RAG，否则 no-RAG",
        "- 引用真实性口径: `claim_evidence_links[].evidence_id ∈ run_artifact.evidence_ids`",
        "",
        "## 汇总",
        "",
        "| 指标 | no-RAG | RAG |",
        "|---|---|---|",
        f"| payload 数 | {no_rag_agg['count']} | {rag_agg['count']} |",
        f"| confidence | {no_rag_agg['confidence']} | {rag_agg['confidence']} |",
        f"| 取证引用率（evidence_ids 非空） | {no_rag_agg['evidence_citation_rate']} | {rag_agg['evidence_citation_rate']} |",
        f"| 引用真实性（links ⊆ ids） | {no_rag_agg['link_authenticity']} | {rag_agg['link_authenticity']} |",
        f"| verifier 通过率 | {no_rag_agg['verifier_pass_rate']} | {rag_agg['verifier_pass_rate']} |",
        f"| retry 率（attempts>1） | {no_rag_agg['retry_rate']} | {rag_agg['retry_rate']} |",
        f"| review_required 率 | {no_rag_agg['review_required_rate']} | {rag_agg['review_required_rate']} |",
        f"| agent_evidences 落库 | {evidences['no_rag']} 行 | {evidences['rag']} 行（共 {evidences['total']}） |",
        "",
        "## 分阶段",
        "",
    ]
    for stage in STAGES:
        no_s = by_stage["no-rag"].get(stage, {})
        rag_s = by_stage["rag"].get(stage, {})
        lines += [
            f"### {stage}",
            "",
            "| 指标 | no-RAG | RAG |",
            "|---|---|---|",
            f"| payload 数 | {no_s.get('count', 0)} | {rag_s.get('count', 0)} |",
            f"| confidence | {no_s.get('confidence', '—')} | {rag_s.get('confidence', '—')} |",
            f"| evidence_mode 分布 | {no_s.get('mode_dist', {})} | {rag_s.get('mode_dist', {})} |",
            f"| 取证引用率 | {no_s.get('evidence_citation_rate', '—')} | {rag_s.get('evidence_citation_rate', '—')} |",
            f"| 引用真实性 | {no_s.get('link_authenticity', '—')} | {rag_s.get('link_authenticity', '—')} |",
            f"| verifier 通过率 | {no_s.get('verifier_pass_rate', '—')} | {rag_s.get('verifier_pass_rate', '—')} |",
            f"| retry 率 | {no_s.get('retry_rate', '—')} | {rag_s.get('retry_rate', '—')} |",
            f"| review_required 率 | {no_s.get('review_required_rate', '—')} | {rag_s.get('review_required_rate', '—')} |",
            "",
        ]
    return "\n".join(lines)


def _print_console(no_rag_agg: dict[str, Any], rag_agg: dict[str, Any], by_stage: dict[str, Any], evidences: dict[str, Any]) -> None:
    print(f"no-RAG payload {no_rag_agg['count']} vs RAG payload {rag_agg['count']}")
    for label, agg in (("no-RAG", no_rag_agg), ("RAG", rag_agg)):
        print(f"  [{label}] confidence {agg['confidence']} | 引用率 {agg['evidence_citation_rate']} "
              f"| 真实性 {agg['link_authenticity']} | verifier通过 {agg['verifier_pass_rate']} "
              f"| retry {agg['retry_rate']} | review {agg['review_required_rate']} | "
              f"mode_dist {agg['mode_dist']}")
    print(f"  agent_evidences: total {evidences['total']}（RAG {evidences['rag']} / no-RAG {evidences['no_rag']}）")


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="no-RAG vs RAG 对比")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH, help="markdown 输出路径")
    args = parser.parse_args(argv)

    async with get_session_factory()() as db:
        no_rag, rag = await _load_payloads(db)
        evidences = await _count_agent_evidences(db, no_rag, rag)

    by_stage: dict[str, dict[str, dict[str, Any]]] = {"no-rag": {}, "rag": {}}
    for bucket, items in (("no-rag", no_rag), ("rag", rag)):
        for stage in STAGES:
            staged = [p for p in items if p.get("kind") == stage]
            by_stage[bucket][stage] = _aggregate_payloads(staged)

    no_rag_agg = _aggregate_payloads(no_rag)
    rag_agg = _aggregate_payloads(rag)
    _print_console(no_rag_agg, rag_agg, by_stage, evidences)

    report = _render_markdown(no_rag_agg, rag_agg, by_stage, evidences)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"\n对比报告已写入: {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(
        _main(argv),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


if __name__ == "__main__":
    sys.exit(main())
