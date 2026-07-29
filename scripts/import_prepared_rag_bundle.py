"""Import a prepared RAG bundle into PostgreSQL.

This command consumes only the output of ``scripts.prepare_rag_bundle``.  Raw
case data must never be passed to this importer because vectorisation may send
chunk text to an external embedding gateway.

The import is deliberately split from Milvus synchronisation:

1. prepare and validate files;
2. atomically upsert PostgreSQL master data;
3. build ``knowledge_chunks``;
4. reindex a new, versioned Milvus collection.

The PostgreSQL transaction is committed only when every prepared record passes
the importer validation gate.  ``--dry-run`` executes the same ORM path and
then rolls the transaction back.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.import_knowledge import (
    build_formula_doc_text,
    build_herb_doc_text,
    build_theory_case_doc_text,
    validate_dosage_unit,
    validate_formula,
    validate_herb,
    validate_theory_case,
)

SCHEMA_VERSION = "xuanhu.rag-bundle.v1"
EXPECTED_OUTPUTS = {
    "dosage_units": "prepared/dosage_units.json",
    "herbs": "prepared/herbs.json",
    "formulas": "prepared/formulas.json",
    "cases": "prepared/cases.json",
}
SOURCE_TYPES = {
    "herbs": "herb",
    "formulas": "formula",
    "cases": "case",
}
LICENSE_STATUS = {
    "herbs": "USER_PROVIDED_UNVERIFIED",
    "formulas": "USER_PROVIDED_UNVERIFIED",
    "cases": "UNVERIFIED_REFERENCE_ONLY",
}
_DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(
        r"(?:病历号|住院号|门诊号|病案号|就诊号|住院编号|病历编号)"
        r"\s*[:：#]?\s*[A-Za-z0-9][A-Za-z0-9_-]{3,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


class BundleValidationError(ValueError):
    """Raised when the prepared bundle or its digest does not match."""


class ImportBlockedError(RuntimeError):
    """Raised when record-level blockers require a full transaction rollback."""


@dataclass
class TypeImportStats:
    kind: str
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    source_id: str | None = None


@dataclass
class BundleImportReport:
    schema_version: str = "xuanhu.rag-import-report.v1"
    dataset_id: str = ""
    bundle_manifest_sha256: str = ""
    started_at: str = ""
    completed_at: str = ""
    mode: str = "validate-only"
    status: str = "pending"
    by_kind: dict[str, dict[str, Any]] = field(default_factory=dict)
    totals: dict[str, int] = field(
        default_factory=lambda: {
            "records": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "warnings": 0,
            "blockers": 0,
        }
    )

    def add(self, stats: TypeImportStats) -> None:
        self.by_kind[stats.kind] = asdict(stats)
        self.totals["records"] += stats.total
        self.totals["inserted"] += stats.inserted
        self.totals["updated"] += stats.updated
        self.totals["skipped"] += stats.skipped
        self.totals["warnings"] += len(stats.warnings)
        self.totals["blockers"] += len(stats.blockers)


@dataclass(frozen=True)
class LoadedBundle:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    records: dict[str, list[dict[str, Any]]]
    source_files: dict[str, dict[str, Any]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleValidationError(f"prepared file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"invalid JSON: {path.name}: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise BundleValidationError(f"{path.name} must contain an array of objects")
    return payload


def _normalise_relative_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def load_prepared_bundle(bundle_dir: Path) -> LoadedBundle:
    """Load a prepared bundle and verify every declared output digest."""
    root = bundle_dir.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleValidationError(f"manifest missing: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"invalid manifest JSON: {exc}") from exc

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleValidationError(
            f"unsupported schema_version: {manifest.get('schema_version')!r}"
        )
    dataset_id = str(manifest.get("dataset_id") or "").strip()
    if not dataset_id:
        raise BundleValidationError("manifest.dataset_id is required")

    outputs_by_kind: dict[str, dict[str, Any]] = {}
    for output in manifest.get("outputs") or []:
        if str(output.get("disposition") or "prepared") != "prepared":
            continue
        kind = str(output.get("kind") or "")
        if kind in outputs_by_kind:
            raise BundleValidationError(f"duplicate manifest output kind: {kind}")
        outputs_by_kind[kind] = output

    records: dict[str, list[dict[str, Any]]] = {}
    for kind, expected_relative in EXPECTED_OUTPUTS.items():
        output = outputs_by_kind.get(kind)
        if output is None:
            raise BundleValidationError(f"manifest output missing: {kind}")
        relative = _normalise_relative_path(str(output.get("relative_path") or ""))
        if relative != expected_relative:
            raise BundleValidationError(
                f"unexpected output path for {kind}: {relative!r}, "
                f"expected {expected_relative!r}"
            )
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BundleValidationError(f"output escapes bundle root: {relative}") from exc
        expected_hash = str(output.get("sha256") or "").lower()
        actual_hash = _sha256_file(path)
        if expected_hash != actual_hash:
            raise BundleValidationError(
                f"digest mismatch for {kind}: expected={expected_hash}, actual={actual_hash}"
            )
        rows = _load_json_array(path)
        if int(output.get("record_count", -1)) != len(rows):
            raise BundleValidationError(
                f"record count mismatch for {kind}: "
                f"manifest={output.get('record_count')}, actual={len(rows)}"
            )
        records[kind] = rows

    source_files: dict[str, dict[str, Any]] = {}
    for source in manifest.get("source_files") or []:
        kind = str(source.get("kind") or "")
        if kind:
            source_files[kind] = source
    missing_sources = set(EXPECTED_OUTPUTS) - set(source_files)
    if missing_sources:
        raise BundleValidationError(
            f"manifest source_files missing: {sorted(missing_sources)}"
        )

    return LoadedBundle(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=_sha256_file(manifest_path),
        records=records,
        source_files=source_files,
    )


def _build_lookup(
    rows: list[dict[str, Any]],
    *,
    name_field: str,
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get(name_field) or "").strip()
        if name:
            lookup[name] = row
        for alias in row.get("aliases") or []:
            value = str(alias).strip()
            if value:
                lookup.setdefault(value, row)
    return lookup


def validate_bundle_records(bundle: LoadedBundle) -> dict[str, TypeImportStats]:
    """Run the canonical validators without connecting to PostgreSQL."""
    units = bundle.records["dosage_units"]
    herbs = bundle.records["herbs"]
    unit_lookup = _build_lookup(units, name_field="unit_name")
    herb_lookup = _build_lookup(herbs, name_field="name")

    validators = {
        "dosage_units": lambda row, index: validate_dosage_unit(row, index),
        "herbs": lambda row, index: validate_herb(row, index),
        "formulas": lambda row, index: validate_formula(
            row, index, herb_lookup, unit_lookup
        ),
        "cases": lambda row, index: validate_theory_case(row, index),
    }

    result: dict[str, TypeImportStats] = {}
    for kind, rows in bundle.records.items():
        stats = TypeImportStats(kind=kind, total=len(rows))
        keys_seen: set[Any] = set()
        for index, row in enumerate(rows):
            for issue in validators[kind](row, index):
                if issue.get("level") == "blocker":
                    stats.blockers.append(issue)
                else:
                    stats.warnings.append(issue)
            if kind == "dosage_units":
                identity: Any = str(row.get("unit_name") or "").strip()
            elif kind in {"herbs", "formulas"}:
                identity = str(row.get("name") or "").strip()
            else:
                identity = str((row.get("metadata") or {}).get("record_key") or "").strip()
            if not identity:
                stats.blockers.append(
                    {
                        "level": "blocker",
                        "field": (
                            "metadata.record_key"
                            if kind == "cases"
                            else "unit_name"
                            if kind == "dosage_units"
                            else "name"
                        ),
                        "index": index,
                        "message": "prepared record is missing its stable identity",
                    }
                )
            elif identity in keys_seen:
                stats.blockers.append(
                    {
                        "level": "blocker",
                        "field": (
                            "metadata.record_key"
                            if kind == "cases"
                            else "unit_name"
                            if kind == "dosage_units"
                            else "name"
                        ),
                        "index": index,
                        "message": "duplicate stable identity inside prepared bundle",
                    }
                )
            keys_seen.add(identity)

            if kind == "cases":
                metadata = row.get("metadata") or {}
                if metadata.get("license_status") != LICENSE_STATUS["cases"]:
                    stats.blockers.append(
                        {
                            "level": "blocker",
                            "field": "metadata.license_status",
                            "index": index,
                            "message": "prepared case license status is missing or unsafe",
                        }
                    )
                content = str(row.get("content") or "")
                if any(pattern.search(content) for pattern in _DIRECT_IDENTIFIER_PATTERNS):
                    stats.blockers.append(
                        {
                            "level": "blocker",
                            "field": "content",
                            "index": index,
                            "message": "prepared case still contains a direct identifier pattern",
                        }
                    )
        result[kind] = stats
    return result


def _source_version(source_file: dict[str, Any]) -> str:
    source_hash = str(source_file.get("sha256") or "")
    return f"sha256-{source_hash[:12]}"


def _source_title(source_file: dict[str, Any]) -> str:
    raw_path = str(source_file.get("path") or "")
    return f"new_data/{Path(raw_path).name}"


async def _upsert_source(
    session: AsyncSession,
    *,
    kind: str,
    bundle: LoadedBundle,
) -> Any:
    from app.models.knowledge import KnowledgeSource

    source_file = bundle.source_files[kind]
    source_type = SOURCE_TYPES[kind]
    title = _source_title(source_file)
    stmt = select(KnowledgeSource).where(
        KnowledgeSource.source_type == source_type,
        KnowledgeSource.title == title,
    )
    source = (await session.execute(stmt)).scalar_one_or_none()
    extra_meta = {
        "dataset_id": bundle.manifest["dataset_id"],
        "bundle_schema_version": bundle.manifest["schema_version"],
        "source_file": Path(str(source_file.get("path") or "")).name,
        "source_file_sha256": source_file.get("sha256"),
        "source_record_count": source_file.get("record_count"),
        "license_status": LICENSE_STATUS[kind],
        "prepared_manifest_sha256": bundle.manifest_sha256,
    }
    if source is None:
        source = KnowledgeSource(
            source_type=source_type,
            title=title,
            source_name="xuanhu-curated-new-data",
            source_version=_source_version(source_file),
            license_note=LICENSE_STATUS[kind],
            extra_meta=extra_meta,
        )
        session.add(source)
        await session.flush()
    else:
        source.source_name = "xuanhu-curated-new-data"
        source.source_version = _source_version(source_file)
        source.license_note = LICENSE_STATUS[kind]
        source.extra_meta = extra_meta
    return source


def _split_issues(issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers = [issue for issue in issues if issue.get("level") == "blocker"]
    warnings = [issue for issue in issues if issue.get("level") != "blocker"]
    return blockers, warnings


async def _import_dosage_units(
    session: AsyncSession,
    rows: list[dict[str, Any]],
) -> TypeImportStats:
    from app.models.knowledge import DosageUnit

    stats = TypeImportStats(kind="dosage_units", total=len(rows))
    for index, row in enumerate(rows):
        blockers, warnings = _split_issues(validate_dosage_unit(row, index))
        stats.blockers.extend(blockers)
        stats.warnings.extend(warnings)
        if blockers:
            stats.skipped += 1
            continue
        name = str(row.get("unit_name") or "").strip()
        existing = (
            await session.execute(
                select(DosageUnit).where(DosageUnit.unit_name == name)
            )
        ).scalar_one_or_none()
        values = {
            "aliases": list(row.get("aliases") or []),
            "to_grams": row.get("to_grams"),
            "conversion_type": row.get("conversion_type"),
            "precision_note": row.get("precision_note"),
            "is_standard": bool(row.get("is_standard", False)),
            "enabled": bool(row.get("enabled", True)),
        }
        if existing is None:
            session.add(DosageUnit(unit_name=name, **values))
            stats.inserted += 1
        else:
            for field_name, value in values.items():
                setattr(existing, field_name, value)
            stats.updated += 1
    await session.flush()
    return stats


async def _import_herbs(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    source: Any,
) -> TypeImportStats:
    from app.models.knowledge import Herb

    stats = TypeImportStats(
        kind="herbs", total=len(rows), source_id=str(source.id)
    )
    for index, row in enumerate(rows):
        blockers, warnings = _split_issues(validate_herb(row, index))
        stats.blockers.extend(blockers)
        stats.warnings.extend(warnings)
        if blockers:
            stats.skipped += 1
            continue
        name = str(row.get("name") or "").strip()
        existing = (
            await session.execute(
                select(Herb).where(Herb.name == name, Herb.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        values = {
            "source_id": source.id,
            "aliases": list(row.get("aliases") or []),
            "properties": row.get("properties"),
            "meridians": list(row.get("meridians") or []),
            "effects": row.get("effects"),
            "indications": row.get("indications"),
            "dosage": row.get("dosage"),
            "max_dose": row.get("max_dose"),
            "contraindications": list(row.get("contraindications") or []),
            "eighteen_incompatibilities": list(
                row.get("eighteen_incompatibilities") or []
            ),
            "nineteen_fears": list(row.get("nineteen_fears") or []),
            "pregnancy_contraindication": str(
                row.get("pregnancy_contraindication") or "none"
            ),
            "incompatibilities": list(row.get("incompatibilities") or []),
            "doc_text": str(row.get("doc_text") or "").strip()
            or build_herb_doc_text(row),
        }
        if existing is None:
            session.add(Herb(name=name, **values))
            stats.inserted += 1
        else:
            for field_name, value in values.items():
                setattr(existing, field_name, value)
            stats.updated += 1
    await session.flush()
    return stats


async def _import_formulas(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    source: Any,
    *,
    herb_lookup: dict[str, dict[str, Any]],
    unit_lookup: dict[str, dict[str, Any]],
) -> TypeImportStats:
    from app.models.knowledge import Formula

    stats = TypeImportStats(
        kind="formulas", total=len(rows), source_id=str(source.id)
    )
    for index, row in enumerate(rows):
        blockers, warnings = _split_issues(
            validate_formula(row, index, herb_lookup, unit_lookup)
        )
        stats.blockers.extend(blockers)
        stats.warnings.extend(warnings)
        if blockers:
            stats.skipped += 1
            continue
        name = str(row.get("name") or "").strip()
        existing = (
            await session.execute(
                select(Formula).where(
                    Formula.name == name, Formula.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        values = {
            "source_id": source.id,
            "aliases": list(row.get("aliases") or []),
            "composition": list(row.get("composition") or []),
            "effect": row.get("effect"),
            "indications": row.get("indications"),
            "usage": row.get("usage"),
            "source": row.get("source"),
            "modification_rules": list(row.get("modification_rules") or []),
            # The prepared document intentionally contains modification rules
            # that the legacy builder omitted for 64 formulas.
            "doc_text": str(row.get("doc_text") or "").strip()
            or build_formula_doc_text(row),
        }
        if existing is None:
            session.add(Formula(name=name, **values))
            stats.inserted += 1
        else:
            for field_name, value in values.items():
                setattr(existing, field_name, value)
            stats.updated += 1
    await session.flush()
    return stats


async def _import_cases(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    source: Any,
) -> TypeImportStats:
    from app.models.knowledge import TheoryCase

    stats = TypeImportStats(
        kind="cases", total=len(rows), source_id=str(source.id)
    )
    existing_rows = (
        await session.execute(
            select(TheoryCase).where(
                TheoryCase.source_id == source.id,
                TheoryCase.entry_type == "case",
                TheoryCase.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    existing_by_key = {
        str((record.extra_meta or {}).get("record_key")): record
        for record in existing_rows
        if (record.extra_meta or {}).get("record_key")
    }

    seen_keys: set[str] = set()
    for index, row in enumerate(rows):
        metadata = dict(row.get("metadata") or {})
        record_key = str(metadata.get("record_key") or "").strip()
        issues = validate_theory_case(row, index)
        if not record_key:
            issues.append(
                {
                    "level": "blocker",
                    "field": "metadata.record_key",
                    "index": index,
                    "message": "prepared case is missing stable record_key",
                }
            )
        elif record_key in seen_keys:
            issues.append(
                {
                    "level": "blocker",
                    "field": "metadata.record_key",
                    "index": index,
                    "message": "duplicate record_key inside prepared bundle",
                }
            )
        seen_keys.add(record_key)
        blockers, warnings = _split_issues(issues)
        stats.blockers.extend(blockers)
        stats.warnings.extend(warnings)
        if blockers:
            stats.skipped += 1
            continue

        title = str(row.get("title") or "").strip()
        existing = existing_by_key.get(record_key)
        values = {
            "source_id": source.id,
            "entry_type": "case",
            "title": title,
            "disease_category": row.get("disease_category"),
            "syndrome": row.get("syndrome"),
            "treatment_principle": row.get("treatment_principle"),
            "formula_summary": row.get("formula_summary"),
            "content": str(row.get("content") or ""),
            "source": row.get("source"),
            "extra_meta": metadata,
            "doc_text": str(row.get("doc_text") or "").strip()
            or build_theory_case_doc_text(row),
        }
        if existing is None:
            record = TheoryCase(**values)
            session.add(record)
            existing_by_key[record_key] = record
            stats.inserted += 1
        else:
            for field_name, value in values.items():
                setattr(existing, field_name, value)
            stats.updated += 1
    await session.flush()
    return stats


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (uuid.UUID, datetime, date, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


async def snapshot_knowledge_tables(session: AsyncSession) -> dict[str, Any]:
    """Capture a small local rollback snapshot before the import transaction."""
    from app.models.knowledge import (
        DosageUnit,
        Formula,
        Herb,
        KnowledgeSource,
        TheoryCase,
    )

    snapshot: dict[str, Any] = {
        "schema_version": "xuanhu.rag-preimport-snapshot.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "tables": {},
    }
    for model in (KnowledgeSource, DosageUnit, Herb, Formula, TheoryCase):
        rows = (await session.execute(select(model))).scalars().all()
        serialised = []
        for row in rows:
            serialised.append(
                {
                    column.name: _json_safe(getattr(row, column.key))
                    for column in model.__table__.columns
                }
            )
        snapshot["tables"][model.__tablename__] = serialised
    return snapshot


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


async def import_bundle(
    bundle: LoadedBundle,
    *,
    dry_run: bool,
    report_path: Path,
    snapshot_path: Path | None,
) -> BundleImportReport:
    """Execute the atomic PostgreSQL upsert for a verified prepared bundle."""
    from app.db.session import get_session_factory
    from app.models import (  # noqa: F401
        DosageUnit,
        Formula,
        Herb,
        KnowledgeSource,
        TheoryCase,
    )

    report = BundleImportReport(
        dataset_id=str(bundle.manifest["dataset_id"]),
        bundle_manifest_sha256=bundle.manifest_sha256,
        started_at=datetime.now(UTC).isoformat(),
        mode="dry-run" if dry_run else "commit",
    )
    validation = validate_bundle_records(bundle)
    for stats in validation.values():
        if stats.blockers:
            report.add(stats)
    if report.totals["blockers"]:
        report.status = "blocked_before_db"
        report.completed_at = datetime.now(UTC).isoformat()
        _atomic_write_json(report_path, asdict(report))
        raise ImportBlockedError(
            f"prepared bundle has {report.totals['blockers']} validation blockers"
        )

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            if snapshot_path is not None:
                snapshot = await snapshot_knowledge_tables(session)
                snapshot["dataset_id"] = bundle.manifest["dataset_id"]
                snapshot["bundle_manifest_sha256"] = bundle.manifest_sha256
                _atomic_write_json(snapshot_path, snapshot)

            herb_source = await _upsert_source(
                session, kind="herbs", bundle=bundle
            )
            formula_source = await _upsert_source(
                session, kind="formulas", bundle=bundle
            )
            case_source = await _upsert_source(
                session, kind="cases", bundle=bundle
            )
            units = bundle.records["dosage_units"]
            herbs = bundle.records["herbs"]
            formulas = bundle.records["formulas"]
            unit_lookup = _build_lookup(units, name_field="unit_name")
            herb_lookup = _build_lookup(herbs, name_field="name")

            results = [
                await _import_dosage_units(session, units),
                await _import_herbs(session, herbs, herb_source),
                await _import_formulas(
                    session,
                    formulas,
                    formula_source,
                    herb_lookup=herb_lookup,
                    unit_lookup=unit_lookup,
                ),
                await _import_cases(session, bundle.records["cases"], case_source),
            ]
            for stats in results:
                report.add(stats)

            if report.totals["blockers"]:
                raise ImportBlockedError(
                    f"import produced {report.totals['blockers']} blockers"
                )

            report.status = "rolled_back_dry_run" if dry_run else "ready_to_commit"
            report.completed_at = datetime.now(UTC).isoformat()
            _atomic_write_json(report_path, asdict(report))

            if dry_run:
                await session.rollback()
            else:
                await session.commit()
                report.status = "committed"
                report.completed_at = datetime.now(UTC).isoformat()
                _atomic_write_json(report_path, asdict(report))
        except Exception:
            await session.rollback()
            if report.status not in {"rolled_back_dry_run", "committed"}:
                report.status = "rolled_back_error"
                report.completed_at = datetime.now(UTC).isoformat()
                _atomic_write_json(report_path, asdict(report))
            raise
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and import a prepared Xuanhu RAG bundle",
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--snapshot-path", type=Path)
    return parser


async def _main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        bundle = load_prepared_bundle(args.bundle_dir)
        if args.validate_only:
            report = BundleImportReport(
                dataset_id=str(bundle.manifest["dataset_id"]),
                bundle_manifest_sha256=bundle.manifest_sha256,
                started_at=datetime.now(UTC).isoformat(),
                completed_at=datetime.now(UTC).isoformat(),
                mode="validate-only",
                status="validated",
            )
            for stats in validate_bundle_records(bundle).values():
                report.add(stats)
            if report.totals["blockers"]:
                report.status = "blocked"
            _atomic_write_json(args.report_path, asdict(report))
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            return 1 if report.totals["blockers"] else 0

        if args.commit and args.snapshot_path is None:
            raise BundleValidationError("--commit requires --snapshot-path")
        report = await import_bundle(
            bundle,
            dry_run=bool(args.dry_run),
            report_path=args.report_path,
            snapshot_path=args.snapshot_path,
        )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    except (BundleValidationError, ImportBlockedError) as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
