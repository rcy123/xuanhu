"""从真实问诊医案生成 RAG 评估查询集。

每条医案 → query（主诉 + 关键症状）；expected_topics = [证型, 方名, 治法]；
expected_source_types = [case, theory, formula]。negative case 用未生成的
空缺场景（forbidden_topics 保证不误召回）。

输出 data/rag_eval_queries_cases.json（对齐 data/rag_eval_queries.json 的
schema：query/expected_topics/expected_source_types/notes/category/
forbidden_topics/negative_case）。

用法:
    uv run python -m scripts.build_rag_eval_from_cases
"""

from __future__ import annotations

import argparse
import json
import sys
from io import TextIOWrapper
from pathlib import Path
from typing import Any, cast

cast(TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "data" / "generated_cases" / "cases.json"
OUTPUT_PATH = ROOT / "data" / "rag_eval_queries_cases.json"

# 语料中刻意未生成的低频/空缺场景 → negative case（禁止命中）
NEGATIVE_SCENARIOS: list[dict[str, Any]] = [
    {
        "category": "negative-缺证场景",
        "query": "热毒炽盛，壮热烦渴，咽喉肿痛溃烂，应参考什么证型与治法？",
        "forbidden_topics": ["热毒炽盛", "烂喉痧"],
        "notes": "语料未收录热毒炽盛证——不应召回高置信医案",
    },
    {
        "category": "negative-缺证场景",
        "query": "痰热蒙蔽心窍，神昏谵语，喉中痰鸣，如何辨证？",
        "forbidden_topics": ["痰蒙心窍", "安宫牛黄"],
        "notes": "语料未收录痰蒙心窍证——不应误召回",
    },
]


def _chief_text(case: dict[str, Any]) -> str:
    """从 content 提取主诉句（首段「主诉：…」）。"""
    content = case.get("content") or ""
    for line in content.splitlines():
        if line.startswith("患者") and "主诉" in line:
            return line
    return content[:80]


def _build_query(case: dict[str, Any]) -> str:
    chief = _chief_text(case)
    # query = 主诉句 + 现病史要点
    content = case.get("content") or ""
    present = ""
    for line in content.splitlines():
        if line.startswith("现病史"):
            present = line
            break
    return f"{chief}；{present}" if present else chief


def build_eval_queries(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for case in cases:
        syndrome = case.get("syndrome") or ""
        formula_name = (case.get("formula_summary") or "").split("：")[0]
        principle = case.get("treatment_principle") or ""
        category = case.get("disease_category") or "未分类"
        query = _build_query(case)
        if not query.strip():
            continue
        expected_topics = [t for t in (syndrome, formula_name, principle) if t]
        if not expected_topics:
            continue
        queries.append(
            {
                "query": query[:200],
                "expected_topics": expected_topics,
                "expected_source_types": ["case", "theory", "formula"],
                "notes": f"真实问诊医案：{case.get('title', '')[:40]}",
                "category": category,
                "provenance_session_id": case.get("metadata", {}).get("provenance", {}).get("session_id"),
            }
        )
    queries.extend(NEGATIVE_SCENARIOS)
    return queries


def main() -> None:
    parser = argparse.ArgumentParser(description="从医案生成 RAG 评估查询集")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.cases.exists():
        print(f"cases 文件不存在: {args.cases}（先运行 scripts.build_case_dataset）")
        return

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    print(f"医案: {len(cases)} 条")
    queries = build_eval_queries(cases)
    print(f"生成评估 query: {len(queries)} 条（含 {len(NEGATIVE_SCENARIOS)} 条 negative）")
    if args.dry_run:
        for item in queries[:5]:
            print(f"  - [{item['category']}] {item['query'][:60]}... topics={item['expected_topics']}")
        return

    args.output.write_text(json.dumps(queries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入: {args.output}")


if __name__ == "__main__":
    main()
