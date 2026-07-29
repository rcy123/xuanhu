"""Prepare a deterministic, auditable RAG data bundle without touching runtime services.

The tool reads the four JSON files in a source bundle, validates their shape, applies
the narrow normalization rules documented in ``POLICY``, and writes a staging bundle
that a separate database importer can consume.

It deliberately has no imports from ``app`` and never connects to PostgreSQL, Milvus,
Redis, an embedding gateway, or any external model.

Example::

    uv run python -m scripts.prepare_rag_bundle \
        --input-dir D:\\project\\xuanhu_data\\new_data \
        --staging-dir D:\\tmp\\xuanhu-rag-staging \
        --dry-run

    uv run python -m scripts.prepare_rag_bundle \
        --input-dir D:\\project\\xuanhu_data\\new_data \
        --staging-dir D:\\tmp\\xuanhu-rag-staging
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "xuanhu.rag-bundle.v1"
DATASET_ID = "xuanhu-new-data"
LICENSE_STATUS = "UNVERIFIED_REFERENCE_ONLY"
REDACTED_RECORD_ID = "[REDACTED_RECORD_ID]"

INPUT_FILES: dict[str, str] = {
    "dosage_units": "dosage_units.json",
    "herbs": "herbs_converted.json",
    "formulas": "formulas_converted.json",
    "cases": "theory_cases_converted.json",
}

PREPARED_FILES: dict[str, str] = {
    "dosage_units": "prepared/dosage_units.json",
    "herbs": "prepared/herbs.json",
    "formulas": "prepared/formulas.json",
    "cases": "prepared/cases.json",
}

QUARANTINE_FILES: dict[str, str] = {
    "dosage_units": "quarantine/dosage_units.json",
    "herbs": "quarantine/herbs.json",
    "formulas": "quarantine/formulas.json",
    "cases": "quarantine/cases.json",
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "dosage_units": ("unit_name", "conversion_type", "aliases", "to_grams"),
    "herbs": (
        "name",
        "contraindications",
        "max_dose",
        "pregnancy_contraindication",
        "doc_text",
    ),
    "formulas": ("name", "composition", "doc_text"),
    "cases": ("entry_type", "title", "content", "source", "metadata"),
}

ALLOWED_CONVERSION_TYPES = {"standard", "fixed", "herb_specific", "unsupported"}
CONSERVATIVE_CONVERSION_MAP = {
    "volume_to_weight": "unsupported",
    "length_to_weight": "unsupported",
}
ALLOWED_PREGNANCY_VALUES = {"none", "caution", "forbidden"}

PREGNANCY_MENTION_RE = re.compile(r"孕妇|妊娠|孕期|怀孕")
PREGNANCY_FORBIDDEN_RE = re.compile(r"禁用|忌用|禁忌|忌服|禁止|严禁|不可使用|不得使用|勿用")
PREGNANCY_CAUTION_RE = re.compile(r"慎用|慎服|不宜|遵医嘱|医师指导|医生指导|医嘱指导|权衡利弊|谨慎")

RECORD_ID_RE = re.compile(
    r"(?P<label>(?:病历|病案|病例|住院|门诊|就诊|档案|挂号|患者)(?:号|编号))"
    r"(?P<separator>\s*(?:(?:[:：=＃#]|为|是)\s*)?)"
    r"(?P<value>(?!\[REDACTED_RECORD_ID\])[A-Za-z0-9][A-Za-z0-9._/\-]{2,})"
)

PII_SCAN_PATTERNS: dict[str, re.Pattern[str]] = {
    "national_id": re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    "mobile_phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "email": re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    "labeled_name": re.compile(r"(?:患者姓名|姓名)\s*[:：=]\s*[^\s，,；;。]{2,20}"),
    "labeled_address": re.compile(r"(?:家庭住址|现住址|住址|家庭地址)\s*[:：=]\s*[^；;。\n]{4,100}"),
    "labeled_phone": re.compile(r"(?:联系电话|联系方式|手机号|手机|电话)\s*[:：=]\s*[0-9+\-\s]{7,25}"),
}

POLICY: dict[str, Any] = {
    "license_status": LICENSE_STATUS,
    "dosage_conversion": {
        "volume_to_weight": "unsupported",
        "length_to_weight": "unsupported",
    },
    "max_dose_zero": "set_null_and_review_warning",
    "duplicate_herb_name": "quarantine_entire_conflict_group",
    "pregnancy_text_precedence": ["forbidden", "caution", "review_required", "none"],
    "pregnancy_ambiguous": "quarantine",
    "case_empty_content": "quarantine",
    "case_record_identifier": f"replace_with_{REDACTED_RECORD_ID}",
    "case_residual_pii": "quarantine",
    "case_record_key": "sha256(source + original_title + normalized_redacted_content)",
    "duplicate_case_title": "append_record_key_short_hash",
    "exact_duplicate_case_record_key": "quarantine_entire_conflict_group",
}

JsonObject = dict[str, Any]


class BundlePreparationError(ValueError):
    """Raised for a fatal bundle or output-contract error."""


@dataclass
class LoadedSource:
    """A parsed source file plus immutable provenance."""

    kind: str
    path: Path
    filename: str
    raw_sha256: str
    raw_size: int
    records: list[Any]
    schema: JsonObject


@dataclass
class KindResult:
    """Prepared/quarantined records and compact audit metrics for one kind."""

    prepared: list[JsonObject]
    quarantined: list[JsonObject]
    report: JsonObject


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _schema_summary(records: list[Any], required_fields: tuple[str, ...]) -> JsonObject:
    observed_fields: set[str] = set()
    observed_types: dict[str, set[str]] = defaultdict(set)
    missing_required_counts: Counter[str] = Counter()
    non_object_records = 0

    for record in records:
        if not isinstance(record, dict):
            non_object_records += 1
            for field_name in required_fields:
                missing_required_counts[field_name] += 1
            continue

        observed_fields.update(record)
        for field_name, value in record.items():
            observed_types[field_name].add(_type_name(value))
        for field_name in required_fields:
            if field_name not in record:
                missing_required_counts[field_name] += 1

    return {
        "root_type": "array",
        "required_fields": list(required_fields),
        "observed_fields": sorted(observed_fields),
        "observed_types": {field_name: sorted(type_names) for field_name, type_names in sorted(observed_types.items())},
        "missing_required_counts": {
            field_name: missing_required_counts.get(field_name, 0) for field_name in required_fields
        },
        "non_object_records": non_object_records,
    }


def _load_source(kind: str, input_dir: Path) -> LoadedSource:
    filename = INPUT_FILES[kind]
    path = input_dir / filename
    if not path.is_file():
        raise BundlePreparationError(f"missing required source file: {path}")

    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BundlePreparationError(f"source file is not valid UTF-8: {path}") from exc

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise BundlePreparationError(f"source file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(parsed, list):
        raise BundlePreparationError(f"source JSON root must be an array: {path}")

    return LoadedSource(
        kind=kind,
        path=path.resolve(),
        filename=filename,
        raw_sha256=_sha256_bytes(raw),
        raw_size=len(raw),
        records=parsed,
        schema=_schema_summary(parsed, REQUIRED_FIELDS[kind]),
    )


def _normalize_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_content_for_key(value: str) -> str:
    """Normalize content only for deterministic identity calculation."""

    normalized = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"\s+", " ", normalized).strip()


def make_case_record_key(source: str, original_title: str, content: str) -> str:
    """Build a stable case key from source, title, and normalized redacted content."""

    identity = {
        "source": _normalize_identity(source),
        "original_title": _normalize_identity(original_title),
        "content": normalize_content_for_key(content),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact_case_record_ids(content: str) -> tuple[str, list[JsonObject]]:
    """Replace labeled clinical record identifiers without retaining their values."""

    label_counts: Counter[str] = Counter()

    def _replace(match: re.Match[str]) -> str:
        label = match.group("label")
        label_counts[label] += 1
        return f"{label}{match.group('separator')}{REDACTED_RECORD_ID}"

    redacted = RECORD_ID_RE.sub(_replace, content)
    records = [
        {
            "kind": "record_id",
            "label": label,
            "replacement": REDACTED_RECORD_ID,
            "count": count,
        }
        for label, count in sorted(label_counts.items())
    ]
    return redacted, records


def scan_case_pii(content: str) -> list[JsonObject]:
    """Return PII finding counts without copying matched values into the report."""

    findings: list[JsonObject] = []

    residual_record_ids = list(RECORD_ID_RE.finditer(content))
    if residual_record_ids:
        findings.append({"code": "residual_record_id", "count": len(residual_record_ids)})

    for code, pattern in PII_SCAN_PATTERNS.items():
        matches = list(pattern.finditer(content))
        if matches:
            findings.append({"code": code, "count": len(matches)})
    return findings


def _with_preparation_metadata(record: JsonObject, key: str, value: Any) -> None:
    current = record.get("preparation_metadata")
    if not isinstance(current, dict):
        current = {}
    current[key] = value
    record["preparation_metadata"] = current


def _quarantine_entry(index: int, reason_codes: list[str], record: Any) -> JsonObject:
    return {
        "source_record_index": index,
        "reason_codes": sorted(set(reason_codes)),
        "record": record,
    }


def _common_kind_report(
    *,
    input_count: int,
    prepared: list[JsonObject],
    quarantined: list[JsonObject],
    warnings: Counter[str],
    extra: JsonObject | None = None,
) -> JsonObject:
    quarantine_reasons: Counter[str] = Counter()
    for entry in quarantined:
        quarantine_reasons.update(entry.get("reason_codes", []))

    report: JsonObject = {
        "input_count": input_count,
        "prepared_count": len(prepared),
        "quarantined_count": len(quarantined),
        "warnings_count": sum(warnings.values()),
        "warnings_by_code": dict(sorted(warnings.items())),
        "blockers_count": sum(quarantine_reasons.values()),
        "quarantine_reasons": dict(sorted(quarantine_reasons.items())),
    }
    if extra:
        report.update(extra)
    return report


def _prepare_dosage_units(source: LoadedSource) -> KindResult:
    prepared: list[JsonObject] = []
    quarantined: list[JsonObject] = []
    warnings: Counter[str] = Counter()
    conversion_original: Counter[str] = Counter()
    conversion_normalized: Counter[str] = Counter()

    for index, raw_record in enumerate(source.records):
        if not isinstance(raw_record, dict):
            quarantined.append(_quarantine_entry(index, ["record_not_object"], raw_record))
            continue

        record = copy.deepcopy(raw_record)
        reasons: list[str] = []
        unit_name = record.get("unit_name")
        original_type = record.get("conversion_type")

        if not isinstance(unit_name, str) or not unit_name.strip():
            reasons.append("missing_or_invalid_unit_name")
        if not isinstance(original_type, str) or not original_type.strip():
            reasons.append("missing_or_invalid_conversion_type")
        else:
            conversion_original[original_type] += 1
            if original_type in CONSERVATIVE_CONVERSION_MAP:
                record["conversion_type"] = CONSERVATIVE_CONVERSION_MAP[original_type]
                original_to_grams = record.get("to_grams")
                record["to_grams"] = None
                _with_preparation_metadata(
                    record,
                    "conversion_normalization",
                    {
                        "original_conversion_type": original_type,
                        "normalized_conversion_type": "unsupported",
                        "original_to_grams": original_to_grams,
                        "rule": "conservative_non_mass_unit_mapping",
                    },
                )
                warnings[f"conversion_{original_type}_to_unsupported"] += 1
            elif original_type not in ALLOWED_CONVERSION_TYPES:
                reasons.append("unsupported_conversion_type")

        normalized_type = record.get("conversion_type")
        if isinstance(normalized_type, str):
            conversion_normalized[normalized_type] += 1

        if normalized_type in {"standard", "fixed"}:
            to_grams = record.get("to_grams")
            if isinstance(to_grams, bool) or not isinstance(to_grams, (int, float)) or to_grams <= 0:
                reasons.append("invalid_positive_to_grams")

        if reasons:
            quarantined.append(_quarantine_entry(index, reasons, record))
        else:
            prepared.append(record)

    report = _common_kind_report(
        input_count=len(source.records),
        prepared=prepared,
        quarantined=quarantined,
        warnings=warnings,
        extra={
            "conversion_type_original_counts": dict(sorted(conversion_original.items())),
            "conversion_type_normalized_counts": dict(sorted(conversion_normalized.items())),
        },
    )
    return KindResult(prepared=prepared, quarantined=quarantined, report=report)


def _pregnancy_fragments(contraindications: list[Any]) -> list[str]:
    fragments: list[str] = []
    for value in contraindications:
        if not isinstance(value, str):
            continue
        for fragment in re.split(r"[；;。\n]", value):
            normalized = fragment.strip()
            if normalized and PREGNANCY_MENTION_RE.search(normalized):
                fragments.append(normalized)
    return fragments


def _classify_pregnancy_text(contraindications: list[Any]) -> tuple[str, int]:
    fragments = _pregnancy_fragments(contraindications)
    if not fragments:
        return "none", 0
    if any(PREGNANCY_FORBIDDEN_RE.search(fragment) for fragment in fragments):
        return "forbidden", len(fragments)
    if any(PREGNANCY_CAUTION_RE.search(fragment) for fragment in fragments):
        return "caution", len(fragments)
    return "review_required", len(fragments)


def _prepare_herbs(source: LoadedSource) -> KindResult:
    prepared: list[JsonObject] = []
    quarantined: list[JsonObject] = []
    warnings: Counter[str] = Counter()
    original_pregnancy: Counter[str] = Counter()
    normalized_pregnancy: Counter[str] = Counter()
    pregnancy_rules: Counter[str] = Counter()

    name_to_indices: dict[str, list[int]] = defaultdict(list)
    for index, raw_record in enumerate(source.records):
        if isinstance(raw_record, dict):
            raw_name = raw_record.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                name_to_indices[_normalize_identity(raw_name)].append(index)

    duplicate_names = {name: indices for name, indices in name_to_indices.items() if len(indices) > 1}
    duplicate_indices = {index for indices in duplicate_names.values() for index in indices}

    for index, raw_record in enumerate(source.records):
        if not isinstance(raw_record, dict):
            quarantined.append(_quarantine_entry(index, ["record_not_object"], raw_record))
            continue

        record = copy.deepcopy(raw_record)
        reasons: list[str] = []
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            reasons.append("missing_or_invalid_name")
        if index in duplicate_indices:
            reasons.append("duplicate_name_conflict")

        max_dose = record.get("max_dose")
        if isinstance(max_dose, bool) or (max_dose is not None and not isinstance(max_dose, (int, float))):
            reasons.append("invalid_max_dose_type")
        elif isinstance(max_dose, (int, float)) and not isinstance(max_dose, bool):
            if max_dose < 0:
                reasons.append("negative_max_dose")
            elif max_dose == 0:
                record["max_dose"] = None
                _with_preparation_metadata(
                    record,
                    "max_dose_normalization",
                    {
                        "original_max_dose": 0,
                        "normalized_max_dose": None,
                        "review_required": True,
                        "rule": "zero_means_not_safely_interpretable_as_dose_ceiling",
                    },
                )
                warnings["max_dose_zero_to_null_review_required"] += 1

        contraindications = record.get("contraindications")
        if not isinstance(contraindications, list):
            reasons.append("invalid_contraindications")
            contraindications = []

        original_value = record.get("pregnancy_contraindication")
        original_key = original_value if isinstance(original_value, str) else "<invalid>"
        original_pregnancy[str(original_key)] += 1

        if original_value not in ALLOWED_PREGNANCY_VALUES:
            reasons.append("invalid_pregnancy_contraindication")
            normalized_pregnancy["review_required"] += 1
            pregnancy_rules["invalid_original_value"] += 1
        else:
            text_classification, evidence_count = _classify_pregnancy_text(contraindications)
            normalized_value = original_value
            rule = "unchanged"
            review_required = False

            if text_classification == "forbidden":
                normalized_value = "forbidden"
                rule = "explicit_forbidden_text"
            elif text_classification == "caution" and original_value != "forbidden":
                normalized_value = "caution"
                rule = "caution_or_medical_guidance_text"
            elif text_classification == "review_required" and original_value == "none":
                rule = "ambiguous_pregnancy_text"
                review_required = True
                reasons.append("ambiguous_pregnancy_text_review_required")

            if review_required:
                normalized_pregnancy["review_required"] += 1
            else:
                record["pregnancy_contraindication"] = normalized_value
                normalized_pregnancy[normalized_value] += 1

            pregnancy_rules[rule] += 1
            if rule != "unchanged":
                _with_preparation_metadata(
                    record,
                    "pregnancy_normalization",
                    {
                        "original_value": original_value,
                        "normalized_value": None if review_required else normalized_value,
                        "rule": rule,
                        "pregnancy_text_fragment_count": evidence_count,
                        "review_required": review_required,
                    },
                )
                if not review_required:
                    warnings[f"pregnancy_{original_value}_to_{normalized_value}"] += 1

        if reasons:
            quarantined.append(_quarantine_entry(index, reasons, record))
        else:
            prepared.append(record)

    report = _common_kind_report(
        input_count=len(source.records),
        prepared=prepared,
        quarantined=quarantined,
        warnings=warnings,
        extra={
            "duplicate_name_group_count": len(duplicate_names),
            "duplicate_name_groups": [
                {"name": name, "source_record_indices": indices} for name, indices in sorted(duplicate_names.items())
            ],
            "pregnancy": {
                "original_counts": dict(sorted(original_pregnancy.items())),
                "normalized_counts": dict(sorted(normalized_pregnancy.items())),
                "prepared_normalized_counts": dict(
                    sorted(
                        Counter(
                            str(record.get("pregnancy_contraindication", "<invalid>")) for record in prepared
                        ).items()
                    )
                ),
                "rules_applied": dict(sorted(pregnancy_rules.items())),
                "ambiguous_quarantined": sum(
                    1 for entry in quarantined if "ambiguous_pregnancy_text_review_required" in entry["reason_codes"]
                ),
            },
        },
    )
    return KindResult(prepared=prepared, quarantined=quarantined, report=report)


def _prepare_formulas(source: LoadedSource) -> KindResult:
    prepared: list[JsonObject] = []
    quarantined: list[JsonObject] = []
    warnings: Counter[str] = Counter()

    name_to_indices: dict[str, list[int]] = defaultdict(list)
    for index, raw_record in enumerate(source.records):
        if isinstance(raw_record, dict):
            raw_name = raw_record.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                name_to_indices[_normalize_identity(raw_name)].append(index)
    duplicate_indices = {index for indices in name_to_indices.values() if len(indices) > 1 for index in indices}

    for index, raw_record in enumerate(source.records):
        if not isinstance(raw_record, dict):
            quarantined.append(_quarantine_entry(index, ["record_not_object"], raw_record))
            continue

        # No field is rebuilt: in particular, the supplied doc_text is preserved exactly.
        record = copy.deepcopy(raw_record)
        reasons: list[str] = []
        name = record.get("name")
        composition = record.get("composition")
        doc_text = record.get("doc_text")

        if not isinstance(name, str) or not name.strip():
            reasons.append("missing_or_invalid_name")
        if index in duplicate_indices:
            reasons.append("duplicate_name_conflict")
        if not isinstance(composition, list) or not composition:
            reasons.append("missing_or_invalid_composition")
        if not isinstance(doc_text, str) or not doc_text.strip():
            reasons.append("missing_or_invalid_doc_text")

        if reasons:
            quarantined.append(_quarantine_entry(index, reasons, record))
        else:
            prepared.append(record)

    duplicate_name_groups = [
        {"name": name, "source_record_indices": indices}
        for name, indices in sorted(name_to_indices.items())
        if len(indices) > 1
    ]
    report = _common_kind_report(
        input_count=len(source.records),
        prepared=prepared,
        quarantined=quarantined,
        warnings=warnings,
        extra={
            "doc_text_policy": "preserved_exactly_as_supplied",
            "duplicate_name_group_count": len(duplicate_name_groups),
            "duplicate_name_groups": duplicate_name_groups,
        },
    )
    return KindResult(prepared=prepared, quarantined=quarantined, report=report)


def _case_storage_title(original_title: str, record_key: str, *, duplicated: bool) -> str:
    if not duplicated:
        return original_title
    suffix = f" [{record_key[:12]}]"
    max_title_length = 255
    base = original_title[: max_title_length - len(suffix)].rstrip()
    return f"{base}{suffix}"


def _prepare_cases(source: LoadedSource) -> KindResult:
    prepared: list[JsonObject] = []
    quarantined: list[JsonObject] = []
    warnings: Counter[str] = Counter()
    redaction_counts: Counter[str] = Counter()
    pii_findings: Counter[str] = Counter()

    title_counts: Counter[str] = Counter()
    for raw_record in source.records:
        if isinstance(raw_record, dict):
            title = raw_record.get("title")
            if isinstance(title, str) and title.strip():
                title_counts[_normalize_identity(title)] += 1

    candidates: list[JsonObject] = []
    record_key_indices: dict[str, list[int]] = defaultdict(list)

    for index, raw_record in enumerate(source.records):
        if not isinstance(raw_record, dict):
            candidates.append(
                {
                    "index": index,
                    "record": raw_record,
                    "reasons": ["record_not_object"],
                    "record_key": None,
                    "original_title": None,
                }
            )
            continue

        record = copy.deepcopy(raw_record)
        reasons: list[str] = []
        entry_type = record.get("entry_type")
        title = record.get("title")
        content = record.get("content")
        source_name = record.get("source")
        metadata = record.get("metadata")

        if entry_type != "case":
            reasons.append("entry_type_not_case")
        if not isinstance(title, str) or not title.strip():
            reasons.append("missing_or_invalid_title")
        if not isinstance(content, str) or not content.strip():
            reasons.append("empty_content")
        if not isinstance(source_name, str) or not source_name.strip():
            reasons.append("missing_or_invalid_source")
        if not isinstance(metadata, dict):
            reasons.append("missing_or_invalid_metadata")

        original_title = title.strip() if isinstance(title, str) else None
        record_key: str | None = None

        if original_title:
            title_findings = scan_case_pii(original_title)
            for finding in title_findings:
                code = str(finding["code"])
                pii_findings[f"title_{code}"] += int(finding["count"])
                reasons.append(f"residual_pii_title_{code}")

        if isinstance(content, str) and content.strip():
            redacted_content, redaction_records = redact_case_record_ids(content)
            record["content"] = redacted_content
            for redaction in redaction_records:
                redaction_counts[str(redaction["label"])] += int(redaction["count"])

            findings = scan_case_pii(redacted_content)
            for finding in findings:
                code = str(finding["code"])
                pii_findings[code] += int(finding["count"])
                reasons.append(f"residual_pii_{code}")

            if (
                isinstance(source_name, str)
                and source_name.strip()
                and original_title
                and normalize_content_for_key(redacted_content)
            ):
                record_key = make_case_record_key(source_name, original_title, redacted_content)
                record_key_indices[record_key].append(index)

                prepared_metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
                source_license_label = prepared_metadata.pop("license", None)
                prepared_metadata.update(
                    {
                        "original_title": original_title,
                        "record_key": record_key,
                        "source": source_name,
                        "source_file": source.filename,
                        "source_file_sha256": source.raw_sha256,
                        "source_record_index": index,
                        "redactions": redaction_records,
                        "license_status": LICENSE_STATUS,
                    }
                )
                if source_license_label is not None:
                    prepared_metadata["source_license_label"] = source_license_label
                record["metadata"] = prepared_metadata

        candidates.append(
            {
                "index": index,
                "record": record,
                "reasons": reasons,
                "record_key": record_key,
                "original_title": original_title,
            }
        )

    exact_duplicate_keys = {
        record_key: indices for record_key, indices in record_key_indices.items() if len(indices) > 1
    }
    exact_duplicate_indices = {index for indices in exact_duplicate_keys.values() for index in indices}

    seen_storage_titles: set[str] = set()
    for candidate in candidates:
        index = int(candidate["index"])
        record = candidate["record"]
        reasons = list(candidate["reasons"])
        record_key = candidate["record_key"]
        original_title = candidate["original_title"]

        if index in exact_duplicate_indices:
            reasons.append("duplicate_record_key")

        if not reasons and isinstance(record, dict) and isinstance(record_key, str) and isinstance(original_title, str):
            duplicated_title = title_counts[_normalize_identity(original_title)] > 1
            storage_title = _case_storage_title(
                original_title,
                record_key,
                duplicated=duplicated_title,
            )
            if storage_title in seen_storage_titles:
                reasons.append("storage_title_collision")
            else:
                record["title"] = storage_title
                seen_storage_titles.add(storage_title)
                if duplicated_title:
                    warnings["duplicate_title_disambiguated"] += 1

        if reasons:
            quarantined.append(_quarantine_entry(index, reasons, record))
        elif isinstance(record, dict):
            # Final invariant: no known direct identifier may enter prepared output.
            final_reasons = [
                f"residual_pii_{field_name}_{finding['code']}"
                for field_name in ("title", "content")
                for finding in scan_case_pii(str(record.get(field_name, "")))
            ]
            if final_reasons:
                quarantined.append(_quarantine_entry(index, final_reasons, record))
            else:
                prepared.append(record)

    report = _common_kind_report(
        input_count=len(source.records),
        prepared=prepared,
        quarantined=quarantined,
        warnings=warnings,
        extra={
            "duplicate_title_group_count": sum(1 for count in title_counts.values() if count > 1),
            "duplicate_title_record_count": sum(count for count in title_counts.values() if count > 1),
            "exact_duplicate_record_key_group_count": len(exact_duplicate_keys),
            "exact_duplicate_record_key_record_count": sum(len(indices) for indices in exact_duplicate_keys.values()),
            "record_id_redactions_by_label": dict(sorted(redaction_counts.items())),
            "residual_pii_findings_by_code": dict(sorted(pii_findings.items())),
            "prepared_residual_pii_blockers": sum(
                sum(
                    len(scan_case_pii(str(record.get(field_name, ""))))
                    for field_name in ("title", "content")
                )
                for record in prepared
            ),
            "license_status": LICENSE_STATUS,
        },
    )
    return KindResult(prepared=prepared, quarantined=quarantined, report=report)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_output_paths(
    *,
    input_dir: Path,
    staging_dir: Path,
    manifest_path: Path,
    report_path: Path,
) -> None:
    resolved_input = input_dir.resolve()
    resolved_staging = staging_dir.resolve()
    resolved_manifest = manifest_path.resolve()
    resolved_report = report_path.resolve()

    for label, output_path in (
        ("staging directory", resolved_staging),
        ("manifest path", resolved_manifest),
        ("report path", resolved_report),
    ):
        if output_path == resolved_input or _is_within(output_path, resolved_input):
            raise BundlePreparationError(f"{label} must not be inside the read-only input directory: {output_path}")

    if resolved_manifest == resolved_report:
        raise BundlePreparationError("manifest path and report path must be different")

    reserved_paths = {
        (resolved_staging / relative_path).resolve()
        for relative_path in (*PREPARED_FILES.values(), *QUARANTINE_FILES.values())
    }
    if resolved_manifest in reserved_paths or resolved_report in reserved_paths:
        raise BundlePreparationError("manifest/report path conflicts with a prepared or quarantine output file")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)


def prepare_bundle(
    *,
    input_dir: Path,
    staging_dir: Path,
    manifest_path: Path | None = None,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[JsonObject, JsonObject]:
    """Prepare one source directory and optionally write the staging bundle."""

    resolved_input = input_dir.resolve()
    resolved_staging = staging_dir.resolve()
    resolved_manifest = (manifest_path or (resolved_staging / "manifest.json")).resolve()
    resolved_report = (report_path or (resolved_staging / "report.json")).resolve()

    if not resolved_input.is_dir():
        raise BundlePreparationError(f"input directory does not exist: {resolved_input}")

    _validate_output_paths(
        input_dir=resolved_input,
        staging_dir=resolved_staging,
        manifest_path=resolved_manifest,
        report_path=resolved_report,
    )

    sources = {kind: _load_source(kind, resolved_input) for kind in INPUT_FILES}
    results = {
        "dosage_units": _prepare_dosage_units(sources["dosage_units"]),
        "herbs": _prepare_herbs(sources["herbs"]),
        "formulas": _prepare_formulas(sources["formulas"]),
        "cases": _prepare_cases(sources["cases"]),
    }

    output_payloads: dict[str, bytes] = {}
    for kind, result in results.items():
        output_payloads[PREPARED_FILES[kind]] = _json_bytes(result.prepared)
        output_payloads[QUARANTINE_FILES[kind]] = _json_bytes(result.quarantined)

    source_files_manifest = [
        {
            "kind": kind,
            "path": str(source.path),
            "filename": source.filename,
            "sha256": source.raw_sha256,
            "bytes": source.raw_size,
            "record_count": len(source.records),
            "schema": source.schema,
        }
        for kind, source in sources.items()
    ]
    outputs_manifest = [
        {
            "kind": kind,
            "disposition": disposition,
            "relative_path": relative_path,
            "sha256": _sha256_bytes(output_payloads[relative_path]),
            "record_count": len(results[kind].prepared if disposition == "prepared" else results[kind].quarantined),
        }
        for disposition, file_mapping in (
            ("prepared", PREPARED_FILES),
            ("quarantine", QUARANTINE_FILES),
        )
        for kind, relative_path in file_mapping.items()
    ]

    total_input = sum(len(source.records) for source in sources.values())
    total_prepared = sum(len(result.prepared) for result in results.values())
    total_quarantined = sum(len(result.quarantined) for result in results.values())
    total_warnings = sum(int(result.report["warnings_count"]) for result in results.values())
    total_blockers = sum(int(result.report["blockers_count"]) for result in results.values())

    manifest: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "input_dir": str(resolved_input),
        "staging_dir": str(resolved_staging),
        "source_files": source_files_manifest,
        "outputs": outputs_manifest,
        "policy": POLICY,
    }
    report: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "generated_at": manifest["generated_at"],
        "dry_run": dry_run,
        "summary": {
            "input_records": total_input,
            "prepared_records": total_prepared,
            "quarantined_records": total_quarantined,
            "warnings": total_warnings,
            "blockers": total_blockers,
        },
        "by_kind": {
            kind: {
                **result.report,
                "source_sha256": sources[kind].raw_sha256,
                "schema": sources[kind].schema,
            }
            for kind, result in results.items()
        },
    }

    if not dry_run:
        for relative_path, payload in output_payloads.items():
            _write_bytes_atomic(resolved_staging / relative_path, payload)
        _write_bytes_atomic(resolved_manifest, _json_bytes(manifest))
        _write_bytes_atomic(resolved_report, _json_bytes(report))

    return manifest, report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线预检并规范化 RAG 新数据 bundle；不连接数据库、Milvus 或外部模型。",
    )
    parser.add_argument("--input-dir", type=Path, required=True, help="只读原始数据目录")
    parser.add_argument("--staging-dir", type=Path, required=True, help="prepared/quarantine 输出目录")
    parser.add_argument("--manifest-path", type=Path, default=None, help="manifest JSON 路径")
    parser.add_argument("--report-path", type=Path, default=None, help="预检报告 JSON 路径")
    parser.add_argument("--dry-run", action="store_true", help="完成全部预检但不创建任何文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest, report = prepare_bundle(
            input_dir=args.input_dir,
            staging_dir=args.staging_dir,
            manifest_path=args.manifest_path,
            report_path=args.report_path,
            dry_run=args.dry_run,
        )
    except BundlePreparationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "dry_run": manifest["dry_run"],
                "summary": summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
