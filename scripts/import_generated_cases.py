"""真实问诊医案汇入知识库（薄封装 import_knowledge 的 cases 逻辑）。

与 import_knowledge --type cases 的区别：独立 knowledge_source
（title="悬壶真实问诊医案集 v1"），与样例语料区分，便于回溯与评估。

用法:
    uv run python -m scripts.import_generated_cases
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

from sqlalchemy import select  # noqa: E402

from app.db.session import get_session_factory  # noqa: E402
from app.models.knowledge import TheoryCase  # noqa: E402
from scripts.import_knowledge import (  # noqa: E402
    SAMPLE_LICENSE,
    KnowledgeImporter,
    build_theory_case_doc_text,
    validate_theory_case,
)

ROOT = Path(__file__).resolve().parents[1]
GENERATED_SOURCE_TITLE = "悬壶真实问诊医案集 v1"


async def _import(session_factory: Any, cases_path: Path) -> None:
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    print(f"待导入医案: {len(data)} 条")

    async with session_factory() as session:
        importer = KnowledgeImporter(session)
        source = await importer._get_or_create_source(
            "case",
            GENERATED_SOURCE_TITLE,
            source_name="悬壶真实问诊",
            source_version="v1",
            license_note=SAMPLE_LICENSE,
        )

        inserted = updated = skipped = 0
        for i, item in enumerate(data):
            issues = validate_theory_case(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            if blockers:
                skipped += 1
                print(f"  SKIP #{i}: {blockers[0]['message']}")
                continue

            item = dict(item)
            item["entry_type"] = "case"
            title = (item.get("title") or "").strip()
            existing = (
                await session.execute(
                    select(TheoryCase).where(
                        TheoryCase.entry_type == "case",
                        TheoryCase.title == title,
                        TheoryCase.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            doc_text = build_theory_case_doc_text(item)

            if existing is not None:
                existing.source_id = source.id
                existing.disease_category = item.get("disease_category")
                existing.syndrome = item.get("syndrome")
                existing.treatment_principle = item.get("treatment_principle")
                existing.formula_summary = item.get("formula_summary")
                existing.content = item.get("content") or ""
                existing.source = item.get("source")
                existing.extra_meta = item.get("metadata") or {}
                existing.doc_text = doc_text
                updated += 1
            else:
                session.add(
                    TheoryCase(
                        source_id=source.id,
                        entry_type="case",
                        title=title,
                        disease_category=item.get("disease_category"),
                        syndrome=item.get("syndrome"),
                        treatment_principle=item.get("treatment_principle"),
                        formula_summary=item.get("formula_summary"),
                        content=item.get("content") or "",
                        source=item.get("source"),
                        extra_meta=item.get("metadata") or {},
                        doc_text=doc_text,
                    )
                )
                inserted += 1

        await session.commit()
        print(f"导入完成: 新增 {inserted}，更新 {updated}，跳过 {skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="真实问诊医案汇入知识库")
    parser.add_argument("--cases", type=Path, default=ROOT / "data" / "generated_cases" / "cases.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.cases.exists():
        print(f"cases 文件不存在: {args.cases}（先运行 scripts.build_case_dataset）")
        return

    asyncio.run(
        _import(get_session_factory(), args.cases),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


if __name__ == "__main__":
    main()
