"""样例知识库导入脚本 — P2-2。

用法::

    uv run python -m scripts.import_knowledge --all
    uv run python -m scripts.import_knowledge --type dosage_units
    uv run python -m scripts.import_knowledge --type herbs
    uv run python -m scripts.import_knowledge --type formulas
    uv run python -m scripts.import_knowledge --type acupoints
    uv run python -m scripts.import_knowledge --type theory
    uv run python -m scripts.import_knowledge --type cases
    uv run python -m scripts.import_knowledge --all --report-path custom_report.json

幂等保证：
- dosage_units 按 unit_name upsert
- herbs/formulas/acupoints 按 active name upsert
- theory_cases 按 active entry_type + title upsert
- knowledge_sources 按 source_type + title 查找或创建
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# 项目根路径
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VALID_PREGNANCY = {"forbidden", "caution", "none"}
VALID_CONVERSION_TYPE = {"standard", "fixed", "herb_specific", "unsupported"}
VALID_ENTRY_TYPE = {"theory", "case"}

CASE_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "national_id": re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    "mobile_phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "email": re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    "labeled_name": re.compile(r"(?:患者姓名|姓名)\s*[:：=]\s*[^\s，,；;。]{2,20}"),
    "labeled_address": re.compile(
        r"(?:家庭住址|现住址|住址|家庭地址)\s*[:：=]\s*[^；;。\n]{4,100}"
    ),
    "labeled_phone": re.compile(
        r"(?:联系电话|联系方式|手机号|手机|电话)\s*[:：=]\s*[0-9+\-\s]{7,25}"
    ),
    "record_identifier": re.compile(
        r"(?:病历|病案|病例|住院|门诊|就诊|档案|挂号|患者)(?:号|编号)"
        r"\s*(?:(?:[:：=＃#]|为|是)\s*)?"
        r"(?!\[REDACTED_RECORD_ID\])[A-Za-z0-9][A-Za-z0-9._/\-]{2,}"
    ),
}

LIANG_GRAMS = 30.0
QIAN_GRAMS = 3.0

# 知识来源默认值
SAMPLE_SOURCE_NAME = "悬壶样例数据"
SAMPLE_SOURCE_VERSION = "v0.1"
SAMPLE_LICENSE = "synthetic — 仅供研发验证"

# 默认报告输出路径
DEFAULT_REPORT_PATH = "docs/dev-handoff/phase-02-p2-2-import-report.json"


# ===================================================================
# 报告数据结构
# ===================================================================


@dataclass
class TypeImportResult:
    """单个类型的导入结果。"""

    source_type: str
    file_path: str = ""
    source_id: str | None = None
    total_in_file: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ImportReport:
    """整体导入报告。"""

    timestamp: str = ""
    overall: dict[str, int] = field(default_factory=lambda: {
        "total_files": 0, "total_records": 0,
        "inserted": 0, "updated": 0, "skipped": 0,
        "warnings": 0, "blockers": 0,
    })
    by_type: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_timestamp": self.timestamp,
            "overall": self.overall,
            "by_type": self.by_type,
        }


# ===================================================================
# doc_text 生成（纯函数，可独立测试）
# ===================================================================


def build_herb_doc_text(item: dict[str, Any]) -> str:
    """为中药条目生成稳定、非空、可检索正文。"""
    parts: list[str] = []
    name = (item.get("name") or "").strip()
    parts.append(name)

    aliases = item.get("aliases") or []
    if aliases:
        parts.append("别名：" + "、".join(str(a) for a in aliases))

    props = (item.get("properties") or "").strip()
    if props:
        parts.append("性味：" + props)

    meridians = item.get("meridians") or []
    if meridians:
        parts.append("归经：" + "、".join(str(m) for m in meridians))

    effects = (item.get("effects") or "").strip()
    if effects:
        parts.append("功效：" + effects)

    indications = (item.get("indications") or "").strip()
    if indications:
        parts.append("主治：" + indications)

    dosage = (item.get("dosage") or "").strip()
    if dosage:
        parts.append("用量：" + dosage)

    contraindications = item.get("contraindications") or []
    if contraindications:
        parts.append("禁忌：" + "；".join(str(c) for c in contraindications))

    return "。".join(parts) + "。"


def build_formula_doc_text(item: dict[str, Any]) -> str:
    """为方剂条目生成稳定、非空、可检索正文。"""
    parts: list[str] = []
    name = (item.get("name") or "").strip()
    parts.append(name)

    aliases = item.get("aliases") or []
    if aliases:
        parts.append("别名：" + "、".join(str(a) for a in aliases))

    composition = item.get("composition") or []
    if composition:
        comp_parts: list[str] = []
        for c in composition:
            herb_name = c.get("herb", "")
            dose = c.get("dose", "")
            unit = c.get("unit", "g")
            note = c.get("note", "")
            entry = f"{herb_name}{dose}{unit}" if dose else str(herb_name)
            if note:
                entry += f"（{note}）"
            comp_parts.append(entry)
        parts.append("组成：" + "、".join(comp_parts))

    effect = (item.get("effect") or "").strip()
    if effect:
        parts.append("功效：" + effect)

    indications = (item.get("indications") or "").strip()
    if indications:
        parts.append("主治：" + indications)

    usage = (item.get("usage") or "").strip()
    if usage:
        parts.append("用法：" + usage)

    source = (item.get("source") or "").strip()
    if source:
        parts.append("出处：" + source)

    return "。".join(parts) + "。"


def build_acupoint_doc_text(item: dict[str, Any]) -> str:
    """为穴位条目生成稳定、非空、可检索正文。"""
    parts: list[str] = []
    name = (item.get("name") or "").strip()
    parts.append(name)

    aliases = item.get("aliases") or []
    if aliases:
        parts.append("别名：" + "、".join(str(a) for a in aliases))

    meridian = (item.get("meridian") or "").strip()
    if meridian:
        parts.append("归经：" + meridian)

    location = (item.get("location") or "").strip()
    if location:
        parts.append("定位：" + location)

    indications = (item.get("indications") or "").strip()
    if indications:
        parts.append("主治：" + indications)

    operation = (item.get("operation") or "").strip()
    if operation:
        parts.append("操作：" + operation)

    contraindications = item.get("contraindications") or []
    if contraindications:
        parts.append("注意事项：" + "；".join(str(c) for c in contraindications))

    source = (item.get("source") or "").strip()
    if source:
        parts.append("出处：" + source)

    return "。".join(parts) + "。"


def build_theory_case_doc_text(item: dict[str, Any]) -> str:
    """为理论/医案条目生成稳定、非空、可检索正文。"""
    parts: list[str] = []
    title = (item.get("title") or "").strip()
    parts.append(title)

    entry_type = (item.get("entry_type") or "").strip()
    if entry_type:
        type_label = "理论" if entry_type == "theory" else "医案"
        parts.append("类型：" + type_label)

    disease_category = (item.get("disease_category") or "").strip()
    if disease_category:
        parts.append("病类：" + disease_category)

    syndrome = (item.get("syndrome") or "").strip()
    if syndrome:
        parts.append("证型：" + syndrome)

    treatment_principle = (item.get("treatment_principle") or "").strip()
    if treatment_principle:
        parts.append("治法：" + treatment_principle)

    formula_summary = (item.get("formula_summary") or "").strip()
    if formula_summary:
        parts.append("方药：" + formula_summary)

    content = (item.get("content") or "").strip()
    if content:
        parts.append("正文：" + content)

    source = (item.get("source") or "").strip()
    if source:
        parts.append("出处：" + source)

    return "。".join(parts) + "。"


# ===================================================================
# 校验函数（纯函数，可独立测试）
# ===================================================================


def _ensure_list(value: Any) -> list[Any]:
    """确保值是列表。"""
    if isinstance(value, list):
        return value
    return []


def validate_dosage_unit(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """校验单个剂量单位条目，返回 warning/blocker 列表。"""
    issues: list[dict[str, Any]] = []

    unit_name = (item.get("unit_name") or "").strip()
    if not unit_name:
        issues.append({"level": "blocker", "field": "unit_name", "index": index, "message": "unit_name 为空"})
        return issues

    conv_type = (item.get("conversion_type") or "").strip()
    if conv_type not in VALID_CONVERSION_TYPE:
        issues.append({
            "level": "blocker", "field": "conversion_type", "index": index,
            "message": f"非法 conversion_type: '{conv_type}'，有效值: {sorted(VALID_CONVERSION_TYPE)}",
            "value": conv_type,
        })

    to_grams = item.get("to_grams")
    if conv_type in ("standard", "fixed"):
        if to_grams is None:
            issues.append({
                "level": "blocker", "field": "to_grams", "index": index,
                "message": f"conversion_type={conv_type} 时 to_grams 必须非空",
            })
        elif not isinstance(to_grams, int | float) or to_grams <= 0:
            issues.append({
                "level": "blocker", "field": "to_grams", "index": index,
                "message": f"to_grams 必须 > 0，当前: {to_grams}",
            })

    # 验证 P0 锚定值
    if unit_name == "两" and to_grams != LIANG_GRAMS:
        issues.append({
            "level": "blocker", "field": "to_grams", "index": index,
            "message": f"两 的 to_grams 必须为 {LIANG_GRAMS}g（P0 锚定），当前: {to_grams}",
        })
    if unit_name == "钱" and to_grams != QIAN_GRAMS:
        issues.append({
            "level": "blocker", "field": "to_grams", "index": index,
            "message": f"钱 的 to_grams 必须为 {QIAN_GRAMS}g（P0 锚定），当前: {to_grams}",
        })

    return issues


def validate_herb(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """校验单个中药条目，返回 warning/blocker 列表。"""
    issues: list[dict[str, Any]] = []

    name = (item.get("name") or "").strip()
    if not name:
        issues.append({"level": "blocker", "field": "name", "index": index, "message": "药材名称为空"})
        return issues

    preg = (item.get("pregnancy_contraindication") or "none").strip()
    if preg not in VALID_PREGNANCY:
        issues.append({
            "level": "blocker", "field": "pregnancy_contraindication", "index": index,
            "message": f"非法 pregnancy_contraindication: '{preg}'，有效值: {sorted(VALID_PREGNANCY)}",
            "name": name, "value": preg,
        })

    max_dose = item.get("max_dose")
    if max_dose is None:
        issues.append({
            "level": "warning", "field": "max_dose", "index": index,
            "message": f"药材 '{name}' 缺少 max_dose（数据缺口）",
            "name": name,
        })
    elif not isinstance(max_dose, int | float) or max_dose <= 0:
        issues.append({
            "level": "blocker", "field": "max_dose", "index": index,
            "message": f"药材 '{name}' 的 max_dose 必须 > 0，当前: {max_dose}",
            "name": name, "value": max_dose,
        })

    return issues


def validate_formula(
    item: dict[str, Any],
    index: int,
    herb_lookup: dict[str, Any],
    unit_lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    """校验单个方剂条目，返回 warning/blocker 列表。

    需要已导入的 herb_lookup 和 unit_lookup 用于映射校验。
    """
    issues: list[dict[str, Any]] = []

    name = (item.get("name") or "").strip()
    if not name:
        issues.append({"level": "blocker", "field": "name", "index": index, "message": "方剂名称为空"})
        return issues

    composition = _ensure_list(item.get("composition"))
    if not composition:
        issues.append({
            "level": "blocker", "field": "composition", "index": index,
            "message": f"方剂 '{name}' 的 composition 为空",
            "name": name,
        })
        return issues

    for ci, comp in enumerate(composition):
        herb_name = (comp.get("herb") or "").strip()
        unit_name = (comp.get("unit") or "g").strip()
        dose = comp.get("dose")

        # 校验药味映射
        if herb_name:
            mapped = herb_name in herb_lookup
            if not mapped:
                issues.append({
                    "level": "warning", "field": "composition", "index": index,
                    "comp_index": ci,
                    "message": f"方剂 '{name}' 中药味 '{herb_name}' 无法映射到已知药材（数据缺口）",
                    "name": name, "unknown_herb": herb_name,
                })
        else:
            issues.append({
                "level": "blocker", "field": "composition", "index": index,
                "comp_index": ci,
                "message": f"方剂 '{name}' 的 composition[{ci}] 缺少 herb 字段",
                "name": name,
            })

        # 校验单位映射
        if unit_name:
            mapped = unit_name in unit_lookup
            if not mapped:
                issues.append({
                    "level": "warning", "field": "composition", "index": index,
                    "comp_index": ci,
                    "message": f"方剂 '{name}' 中单位 '{unit_name}'（药味: {herb_name}）无法映射到已知剂量单位（数据缺口）",
                    "name": name, "unknown_unit": unit_name, "herb": herb_name,
                })
        else:
            issues.append({
                "level": "warning", "field": "composition", "index": index,
                "comp_index": ci,
                "message": f"方剂 '{name}' 的 composition[{ci}] 缺少 unit 字段",
                "name": name,
            })

        # 校验 dose
        if dose is not None and (not isinstance(dose, int | float) or dose <= 0):
            issues.append({
                "level": "blocker", "field": "composition", "index": index,
                "comp_index": ci,
                "message": f"方剂 '{name}' 中药味 '{herb_name}' 的 dose 必须 > 0，当前: {dose}",
                "name": name, "herb": herb_name, "value": dose,
            })

    return issues


def validate_acupoint(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """校验单个穴位条目，返回 warning/blocker 列表。"""
    issues: list[dict[str, Any]] = []

    name = (item.get("name") or "").strip()
    if not name:
        issues.append({"level": "blocker", "field": "name", "index": index, "message": "穴位名称为空"})

    return issues


def validate_theory_case(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """校验单个理论/医案条目，返回 warning/blocker 列表。"""
    issues: list[dict[str, Any]] = []

    entry_type = (item.get("entry_type") or "").strip()
    if entry_type not in VALID_ENTRY_TYPE:
        issues.append({
            "level": "blocker", "field": "entry_type", "index": index,
            "message": f"非法 entry_type: '{entry_type}'，有效值: {sorted(VALID_ENTRY_TYPE)}",
            "value": entry_type,
        })

    title = (item.get("title") or "").strip()
    if not title:
        issues.append({"level": "blocker", "field": "title", "index": index, "message": "标题为空"})

    content = (item.get("content") or "").strip()
    if not content:
        issues.append({"level": "blocker", "field": "content", "index": index, "message": "正文为空"})

    # 医案脱敏检查
    if entry_type == "case":
        issues.extend(_check_case_deidentification(item, index))

    return issues


def _check_case_deidentification(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """检查医案是否已脱敏，标记疑似真实患者信息的条目。"""
    issues: list[dict[str, Any]] = []

    content = (item.get("content") or "")
    title = (item.get("title") or "")
    meta = item.get("metadata") or {}

    deidentified = meta.get("deidentified", False)

    if not deidentified:
        issues.append({
            "level": "blocker", "field": "metadata.deidentified", "index": index,
            "message": "医案缺少 deidentified 标记，疑似未脱敏",
        })

    # 只匹配结构化、可信度较高的直接标识符，避免把“患者：男”、
    # “患者家属诉”等临床叙述误判为姓名。问题报告仅包含规则与计数，
    # 不回显匹配值或原文。
    scan_text = f"{title}\n{content}"
    indicator_counts = {
        code: len(list(pattern.finditer(scan_text)))
        for code, pattern in CASE_PII_PATTERNS.items()
        if pattern.search(scan_text)
    }

    if indicator_counts:
        issues.append({
            "level": "blocker", "field": "content", "index": index,
            "message": "疑似直接患者标识符（仅报告类型与计数）",
            "indicator_counts": indicator_counts,
        })

    return issues


# ===================================================================
# 导入器
# ===================================================================


class KnowledgeImporter:
    """知识库导入器。

    使用 async session 进行幂等导入，记录统计信息。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._source_cache: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------------
    # 知识来源管理
    # ------------------------------------------------------------------

    async def _get_or_create_source(
        self, source_type: str, title: str,
        source_name: str | None = None,
        source_version: str | None = None,
        license_note: str | None = None,
    ) -> Any:
        """按 source_type + title 查找或创建 knowledge_source。"""
        from app.models.knowledge import KnowledgeSource as KS

        cache_key = (source_type, title)
        if cache_key in self._source_cache:
            return self._source_cache[cache_key]

        stmt = select(KS).where(KS.source_type == source_type, KS.title == title)
        result = await self.session.execute(stmt)
        source = result.scalar_one_or_none()

        if source is None:
            source = KS(
                source_type=source_type,
                title=title,
                source_name=source_name or SAMPLE_SOURCE_NAME,
                source_version=source_version or SAMPLE_SOURCE_VERSION,
                license_note=license_note or SAMPLE_LICENSE,
            )
            self.session.add(source)
            await self.session.flush()

        self._source_cache[cache_key] = source
        return source

    # ------------------------------------------------------------------
    # 批量导入方法
    # ------------------------------------------------------------------

    async def import_dosage_units(self, data: list[dict[str, Any]]) -> TypeImportResult:
        """导入剂量单位，按 unit_name upsert。"""
        from app.models.knowledge import DosageUnit as DU

        result = TypeImportResult(source_type="dosage_units", total_in_file=len(data))

        for i, item in enumerate(data):
            # 校验
            issues = validate_dosage_unit(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            warnings = [x for x in issues if x["level"] == "warning"]

            result.blockers.extend(blockers)
            result.warnings.extend(warnings)

            if blockers:
                result.skipped += 1
                continue

            unit_name = (item.get("unit_name") or "").strip()

            # 查找已有记录
            stmt = select(DU).where(DU.unit_name == unit_name)
            existing = (await self.session.execute(stmt)).scalar_one_or_none()

            if existing is not None:
                # 更新
                existing.aliases = _ensure_list(item.get("aliases"))
                existing.to_grams = item.get("to_grams")
                existing.conversion_type = item.get("conversion_type", "unsupported")
                existing.precision_note = item.get("precision_note")
                existing.is_standard = item.get("is_standard", False)
                existing.enabled = item.get("enabled", True)
                result.updated += 1
            else:
                record = DU(
                    unit_name=unit_name,
                    aliases=_ensure_list(item.get("aliases")),
                    to_grams=item.get("to_grams"),
                    conversion_type=item.get("conversion_type", "unsupported"),
                    precision_note=item.get("precision_note"),
                    is_standard=item.get("is_standard", False),
                    enabled=item.get("enabled", True),
                )
                self.session.add(record)
                result.inserted += 1

        await self.session.flush()
        return result

    async def import_herbs(self, data: list[dict[str, Any]], file_path: str = "") -> TypeImportResult:
        """导入中药，按 active name upsert。"""
        from app.models.knowledge import Herb

        result = TypeImportResult(
            source_type="herbs", file_path=file_path, total_in_file=len(data),
        )

        source = await self._get_or_create_source("herb", "sample_herbs.json")

        for i, item in enumerate(data):
            issues = validate_herb(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            warnings = [x for x in issues if x["level"] == "warning"]

            result.blockers.extend(blockers)
            result.warnings.extend(warnings)

            if blockers:
                result.skipped += 1
                continue

            name = (item.get("name") or "").strip()

            # 查找 active 记录
            stmt = select(Herb).where(Herb.name == name, Herb.deleted_at.is_(None))
            existing = (await self.session.execute(stmt)).scalar_one_or_none()

            doc_text = build_herb_doc_text(item)

            if existing is not None:
                # 更新
                existing.source_id = source.id
                existing.aliases = _ensure_list(item.get("aliases"))
                existing.properties = item.get("properties")
                existing.meridians = _ensure_list(item.get("meridians"))
                existing.effects = item.get("effects")
                existing.indications = item.get("indications")
                existing.dosage = item.get("dosage")
                existing.max_dose = item.get("max_dose")
                existing.contraindications = _ensure_list(item.get("contraindications"))
                existing.eighteen_incompatibilities = _ensure_list(item.get("eighteen_incompatibilities"))
                existing.nineteen_fears = _ensure_list(item.get("nineteen_fears"))
                existing.pregnancy_contraindication = (item.get("pregnancy_contraindication") or "none").strip()
                existing.incompatibilities = _ensure_list(item.get("incompatibilities"))
                existing.doc_text = doc_text
                result.updated += 1
            else:
                record = Herb(
                    source_id=source.id,
                    name=name,
                    aliases=_ensure_list(item.get("aliases")),
                    properties=item.get("properties"),
                    meridians=_ensure_list(item.get("meridians")),
                    effects=item.get("effects"),
                    indications=item.get("indications"),
                    dosage=item.get("dosage"),
                    max_dose=item.get("max_dose"),
                    contraindications=_ensure_list(item.get("contraindications")),
                    eighteen_incompatibilities=_ensure_list(item.get("eighteen_incompatibilities")),
                    nineteen_fears=_ensure_list(item.get("nineteen_fears")),
                    pregnancy_contraindication=(item.get("pregnancy_contraindication") or "none").strip(),
                    incompatibilities=_ensure_list(item.get("incompatibilities")),
                    doc_text=doc_text,
                )
                self.session.add(record)
                result.inserted += 1

            # 收集 max_dose 缺口
            if item.get("max_dose") is None:
                result.gaps.append({
                    "type": "missing_max_dose",
                    "name": name,
                    "message": f"药材 '{name}' 缺少 max_dose",
                })

        await self.session.flush()
        result.source_id = str(source.id) if source else None
        return result

    async def import_formulas(
        self, data: list[dict[str, Any]], file_path: str = "",
    ) -> TypeImportResult:
        """导入方剂，按 active name upsert。

        在校验阶段将对 composition 中药味/单位做映射检查。
        """
        from app.models.knowledge import Formula

        result = TypeImportResult(
            source_type="formulas", file_path=file_path, total_in_file=len(data),
        )

        # 构建查找映射
        herb_lookup = await self._build_herb_lookup()
        unit_lookup = await self._build_unit_lookup()

        source = await self._get_or_create_source("formula", "sample_formulas.json")

        for i, item in enumerate(data):
            issues = validate_formula(item, i, herb_lookup, unit_lookup)
            blockers = [x for x in issues if x["level"] == "blocker"]
            warnings = [x for x in issues if x["level"] == "warning"]

            result.blockers.extend(blockers)
            result.warnings.extend(warnings)

            # 收集 gaps
            for w in warnings:
                if "unknown_herb" in w:
                    result.gaps.append({
                        "type": "unmapped_herb",
                        "formula": item.get("name", ""),
                        "unknown_herb": w.get("unknown_herb"),
                        "message": w["message"],
                    })
                if "unknown_unit" in w:
                    result.gaps.append({
                        "type": "unmapped_unit",
                        "formula": item.get("name", ""),
                        "unknown_unit": w.get("unknown_unit"),
                        "herb": w.get("herb", ""),
                        "message": w["message"],
                    })

            if blockers:
                result.skipped += 1
                continue

            name = (item.get("name") or "").strip()

            # 查找 active 记录
            stmt = select(Formula).where(Formula.name == name, Formula.deleted_at.is_(None))
            existing = (await self.session.execute(stmt)).scalar_one_or_none()

            doc_text = build_formula_doc_text(item)

            if existing is not None:
                # 更新
                existing.source_id = source.id
                existing.aliases = _ensure_list(item.get("aliases"))
                existing.composition = _ensure_list(item.get("composition"))
                existing.effect = item.get("effect")
                existing.indications = item.get("indications")
                existing.usage = item.get("usage")
                existing.source = item.get("source")
                existing.modification_rules = _ensure_list(item.get("modification_rules"))
                existing.doc_text = doc_text
                result.updated += 1
            else:
                record = Formula(
                    source_id=source.id,
                    name=name,
                    aliases=_ensure_list(item.get("aliases")),
                    composition=_ensure_list(item.get("composition")),
                    effect=item.get("effect"),
                    indications=item.get("indications"),
                    usage=item.get("usage"),
                    source=item.get("source"),
                    modification_rules=_ensure_list(item.get("modification_rules")),
                    doc_text=doc_text,
                )
                self.session.add(record)
                result.inserted += 1

        await self.session.flush()
        result.source_id = str(source.id) if source else None
        return result

    async def import_acupoints(self, data: list[dict[str, Any]], file_path: str = "") -> TypeImportResult:
        """导入穴位，按 active name upsert。"""
        from app.models.knowledge import Acupoint

        result = TypeImportResult(
            source_type="acupoints", file_path=file_path, total_in_file=len(data),
        )

        source = await self._get_or_create_source("acupoint", "sample_acupoints.json")

        for i, item in enumerate(data):
            issues = validate_acupoint(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            warnings = [x for x in issues if x["level"] == "warning"]

            result.blockers.extend(blockers)
            result.warnings.extend(warnings)

            if blockers:
                result.skipped += 1
                continue

            name = (item.get("name") or "").strip()

            stmt = select(Acupoint).where(Acupoint.name == name, Acupoint.deleted_at.is_(None))
            existing = (await self.session.execute(stmt)).scalar_one_or_none()

            doc_text = build_acupoint_doc_text(item)

            if existing is not None:
                existing.source_id = source.id
                existing.aliases = _ensure_list(item.get("aliases"))
                existing.meridian = item.get("meridian")
                existing.location = item.get("location")
                existing.indications = item.get("indications")
                existing.operation = item.get("operation")
                existing.contraindications = _ensure_list(item.get("contraindications"))
                existing.source = item.get("source")
                existing.doc_text = doc_text
                result.updated += 1
            else:
                record = Acupoint(
                    source_id=source.id,
                    name=name,
                    aliases=_ensure_list(item.get("aliases")),
                    meridian=item.get("meridian"),
                    location=item.get("location"),
                    indications=item.get("indications"),
                    operation=item.get("operation"),
                    contraindications=_ensure_list(item.get("contraindications")),
                    source=item.get("source"),
                    doc_text=doc_text,
                )
                self.session.add(record)
                result.inserted += 1

        await self.session.flush()
        result.source_id = str(source.id) if source else None
        return result

    async def import_theory_cases(
        self, data: list[dict[str, Any]], file_path: str = "",
        entry_type_override: str | None = None,
    ) -> TypeImportResult:
        """导入理论/医案，按 active entry_type + title upsert。

        若提供 entry_type_override，则覆盖 JSON 中的 entry_type。
        """
        from app.models.knowledge import TheoryCase

        source_type_key = entry_type_override or "theory_cases"
        result = TypeImportResult(
            source_type=source_type_key, file_path=file_path, total_in_file=len(data),
        )

        # 根据内容类型创建 source
        if entry_type_override == "case":
            source_title = "sample_cases.json"
            source_type = "case"
        elif entry_type_override == "theory":
            source_title = "sample_theory.json"
            source_type = "theory"
        else:
            source_title = f"sample_{source_type_key}.json"
            source_type = entry_type_override or "theory"

        source = await self._get_or_create_source(source_type, source_title)

        for i, item in enumerate(data):
            # 覆盖 entry_type
            if entry_type_override:
                item = dict(item)
                item["entry_type"] = entry_type_override

            issues = validate_theory_case(item, i)
            blockers = [x for x in issues if x["level"] == "blocker"]
            warnings = [x for x in issues if x["level"] == "warning"]

            result.blockers.extend(blockers)
            result.warnings.extend(warnings)

            if blockers:
                result.skipped += 1
                continue

            entry_type = (item.get("entry_type") or "").strip()
            title = (item.get("title") or "").strip()

            # 查找 active 记录
            stmt = select(TheoryCase).where(
                TheoryCase.entry_type == entry_type,
                TheoryCase.title == title,
                TheoryCase.deleted_at.is_(None),
            )
            existing = (await self.session.execute(stmt)).scalar_one_or_none()

            doc_text = build_theory_case_doc_text(item)

            if existing is not None:
                existing.source_id = source.id
                existing.entry_type = entry_type
                existing.title = title
                existing.disease_category = item.get("disease_category")
                existing.syndrome = item.get("syndrome")
                existing.treatment_principle = item.get("treatment_principle")
                existing.formula_summary = item.get("formula_summary")
                existing.content = item.get("content") or ""
                existing.source = item.get("source")
                existing.extra_meta = item.get("metadata") or {}
                existing.doc_text = doc_text
                result.updated += 1
            else:
                record = TheoryCase(
                    source_id=source.id,
                    entry_type=entry_type,
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
                self.session.add(record)
                result.inserted += 1

        await self.session.flush()
        result.source_id = str(source.id) if source else None
        return result

    # ------------------------------------------------------------------
    # 查找映射辅助
    # ------------------------------------------------------------------

    async def _build_herb_lookup(self) -> dict[str, Any]:
        """构建药材名称/别名 → Herb 记录的映射。"""
        from app.models.knowledge import Herb

        stmt = select(Herb).where(Herb.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        herbs = result.scalars().all()

        lookup: dict[str, Any] = {}
        for h in herbs:
            lookup[h.name] = h
            for alias in h.aliases or []:
                alias_str = str(alias).strip()
                if alias_str and alias_str not in lookup:
                    lookup[alias_str] = h
        return lookup

    async def _build_unit_lookup(self) -> dict[str, Any]:
        """构建单位名称/别名 → DosageUnit 记录的映射。"""
        from app.models.knowledge import DosageUnit as DU

        stmt = select(DU)
        result = await self.session.execute(stmt)
        units = result.scalars().all()

        lookup: dict[str, Any] = {}
        for u in units:
            lookup[u.unit_name] = u
            for alias in u.aliases or []:
                alias_str = str(alias).strip()
                if alias_str and alias_str not in lookup:
                    lookup[alias_str] = u
        return lookup


# ===================================================================
# 工具函数
# ===================================================================


def load_json(path: Path) -> list[dict[str, Any]]:
    """加载 JSON 文件，返回列表。"""
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"数据文件内容必须是数组: {path}")
    return data


def print_result(result: TypeImportResult) -> None:
    """打印单个类型的导入统计到控制台。"""
    print(f"\n{'='*60}")
    print(f"  类型: {result.source_type}")
    print(f"  文件: {result.file_path}")
    print(f"  总数: {result.total_in_file}")
    print(f"  新增: {result.inserted}")
    print(f"  更新: {result.updated}")
    print(f"  跳过: {result.skipped}")
    print(f"  Warning: {len(result.warnings)}")
    print(f"  Blocker: {len(result.blockers)}")
    print(f"  缺口 : {len(result.gaps)}")

    if result.warnings:
        print(f"\n  --- Warnings ({len(result.warnings)}) ---")
        for w in result.warnings:
            print(f"    [{w.get('field','')}] {w.get('message','')}")

    if result.blockers:
        print(f"\n  --- Blockers ({len(result.blockers)}) ---")
        for b in result.blockers:
            print(f"    [{b.get('field','')}] {b.get('message','')}")

    if result.gaps:
        print(f"\n  --- Gaps ({len(result.gaps)}) ---")
        for g in result.gaps:
            print(f"    [{g.get('type','')}] {g.get('message','')}")


def print_overall(report: ImportReport) -> None:
    """打印整体导入汇总。"""
    ov = report.overall
    print(f"\n{'='*60}")
    print(f"  导入完成 — {report.timestamp}")
    print(f"  文件数: {ov['total_files']}")
    print(f"  总记录: {ov['total_records']}")
    print(f"  新增  : {ov['inserted']}")
    print(f"  更新  : {ov['updated']}")
    print(f"  跳过  : {ov['skipped']}")
    print(f"  Warnings: {ov['warnings']}")
    print(f"  Blockers: {ov['blockers']}")
    print(f"{'='*60}")


def save_report(report: ImportReport, path: Path) -> None:
    """将导入报告写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    report_data = report.to_dict()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告已保存: {path}")


# ===================================================================
# 主导入编排
# ===================================================================


async def run_import(
    *,
    types: set[str] | None = None,
    data_dir: Path | None = None,
    report_path: Path | None = None,
) -> ImportReport:
    """执行导入并返回报告。"""
    from app.db.session import get_session_factory
    from app.models import (  # noqa: F401 — 触发 ORM 注册
        Acupoint,
        DosageUnit,
        Formula,
        Herb,
        KnowledgeSource,
        TheoryCase,
    )

    if data_dir is None:
        data_dir = _PROJECT_ROOT / "data"

    if report_path is None:
        report_path = _PROJECT_ROOT / DEFAULT_REPORT_PATH

    if types is None:
        types = {"dosage_units", "herbs", "formulas", "acupoints", "theory", "cases"}

    report = ImportReport(timestamp=datetime.now(UTC).isoformat())

    session_factory = get_session_factory()

    async with session_factory() as session:
        importer = KnowledgeImporter(session)

        try:
            # 1. dosage_units（最优先，后续类型依赖它）
            if "dosage_units" in types:
                print("\n--- 导入 dosage_units ---")
                data = load_json(data_dir / "sample_dosage_units.json")
                result = await importer.import_dosage_units(data)
                result.file_path = str(data_dir / "sample_dosage_units.json")
                print_result(result)
                report.by_type["dosage_units"] = _result_to_dict(result)

            # 2. herbs（formulas 依赖它）
            if "herbs" in types:
                print("\n--- 导入 herbs ---")
                data = load_json(data_dir / "sample_herbs.json")
                result = await importer.import_herbs(data, str(data_dir / "sample_herbs.json"))
                print_result(result)
                report.by_type["herbs"] = _result_to_dict(result)

            # 3. formulas（依赖 herbs + dosage_units）
            if "formulas" in types:
                print("\n--- 导入 formulas ---")
                data = load_json(data_dir / "sample_formulas.json")
                result = await importer.import_formulas(data, str(data_dir / "sample_formulas.json"))
                print_result(result)
                report.by_type["formulas"] = _result_to_dict(result)

            # 4. acupoints
            if "acupoints" in types:
                print("\n--- 导入 acupoints ---")
                data = load_json(data_dir / "sample_acupoints.json")
                result = await importer.import_acupoints(data, str(data_dir / "sample_acupoints.json"))
                print_result(result)
                report.by_type["acupoints"] = _result_to_dict(result)

            # 5. theory
            if "theory" in types:
                print("\n--- 导入 theory ---")
                data = load_json(data_dir / "sample_theory.json")
                result = await importer.import_theory_cases(
                    data, str(data_dir / "sample_theory.json"), entry_type_override="theory",
                )
                print_result(result)
                report.by_type["theory"] = _result_to_dict(result)

            # 6. cases
            if "cases" in types:
                print("\n--- 导入 cases ---")
                data = load_json(data_dir / "sample_cases.json")
                result = await importer.import_theory_cases(
                    data, str(data_dir / "sample_cases.json"), entry_type_override="case",
                )
                print_result(result)
                report.by_type["cases"] = _result_to_dict(result)

            await session.commit()

        except Exception:
            await session.rollback()
            raise

    # 汇总
    _aggregate_report(report)
    print_overall(report)
    save_report(report, report_path)

    return report


def _result_to_dict(result: TypeImportResult) -> dict[str, Any]:
    """将 TypeImportResult 转为可序列化的 dict。"""
    return {
        "source_type": result.source_type,
        "file_path": result.file_path,
        "source_id": result.source_id,
        "total_in_file": result.total_in_file,
        "inserted": result.inserted,
        "updated": result.updated,
        "skipped": result.skipped,
        "warnings_count": len(result.warnings),
        "blockers_count": len(result.blockers),
        "gaps_count": len(result.gaps),
        "warnings": result.warnings,
        "blockers": result.blockers,
        "gaps": result.gaps,
    }


def _aggregate_report(report: ImportReport) -> None:
    """汇总各类型统计到 overall。"""
    ov = report.overall
    ov["total_files"] = len(report.by_type)
    for v in report.by_type.values():
        ov["total_records"] += v.get("total_in_file", 0)
        ov["inserted"] += v.get("inserted", 0)
        ov["updated"] += v.get("updated", 0)
        ov["skipped"] += v.get("skipped", 0)
        ov["warnings"] += v.get("warnings_count", 0)
        ov["blockers"] += v.get("blockers_count", 0)


# ===================================================================
# CLI
# ===================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="悬壶（Xuanhu）样例知识库导入 — P2-2",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="导入所有类型的样例数据")
    group.add_argument("--type", dest="import_type", choices=[
        "dosage_units", "herbs", "formulas", "acupoints", "theory", "cases",
    ], help="指定导入类型")

    parser.add_argument(
        "--data-dir", default=str(_PROJECT_ROOT / "data"),
        help=f"数据目录路径（默认: {_PROJECT_ROOT / 'data'}）",
    )
    parser.add_argument(
        "--report-path", default=str(_PROJECT_ROOT / DEFAULT_REPORT_PATH),
        help=f"报告输出路径（默认: {_PROJECT_ROOT / DEFAULT_REPORT_PATH}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    args = parse_args(argv)

    if args.all:
        types: set[str] = {"dosage_units", "herbs", "formulas", "acupoints", "theory", "cases"}
    else:
        types = {args.import_type}

    data_dir = Path(args.data_dir)
    report_path = Path(args.report_path)

    report = asyncio.run(run_import(
        types=types,
        data_dir=data_dir,
        report_path=report_path,
    ))

    # 根据 blocker 数量决定退出码
    blocker_count = report.overall.get("blockers", 0)
    if blocker_count > 0:
        print(f"\n[WARNING] 存在 {blocker_count} 个 blocker，请检查报告。")
        sys.exit(1)


if __name__ == "__main__":
    main()
