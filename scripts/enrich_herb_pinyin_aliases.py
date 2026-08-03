# -*- coding: utf-8 -*-
"""Enrich the herbs table with pinyin aliases so the safety engine can match
pinyin herb names emitted by formula models (e.g. "Bai Bu" -> 百部).

Idempotent: existing aliases are preserved; duplicates are dropped.

用法: .venv\Scripts\python.exe scripts\enrich_herb_pinyin_aliases.py
"""
import asyncio
import json
import sys

from sqlalchemy import text

from app.db.session import get_session_factory


def _pinyin_variants(chinese: str) -> list[str]:
    from pypinyin import lazy_pinyin

    syllables = lazy_pinyin(chinese)
    if not syllables or not chinese.strip():
        return []
    joined = " ".join(syllables)
    return [
        joined,
        joined.upper(),
        " ".join(word.capitalize() for word in syllables),
        "".join(syllables),
        "".join(word.capitalize() for word in syllables),
    ]


async def main() -> None:
    factory = get_session_factory()
    async with factory() as db:
        rows = (await db.execute(text("SELECT id, name, aliases FROM herbs WHERE deleted_at IS NULL"))).fetchall()
        updated = 0
        for herb_id, name, aliases in rows:
            aliases = list(aliases or [])
            base_names = [name, *aliases]
            pinyin_aliases = []
            for base in base_names:
                for variant in _pinyin_variants(str(base)):
                    if variant and variant not in aliases and variant not in pinyin_aliases:
                        pinyin_aliases.append(variant)
            if pinyin_aliases:
                merged = [*aliases, *pinyin_aliases]
                await db.execute(
                    text("UPDATE herbs SET aliases = CAST(:aliases AS jsonb) WHERE id = :id"),
                    {"aliases": json.dumps(merged, ensure_ascii=False), "id": herb_id},
                )
                updated += 1
        await db.commit()
        print(f"enriched {updated}/{len(rows)} herbs with pinyin aliases")


if __name__ == "__main__":
    asyncio.run(main())
