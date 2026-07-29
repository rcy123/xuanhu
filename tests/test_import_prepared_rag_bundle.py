from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.import_prepared_rag_bundle import (
    LICENSE_STATUS,
    BundleValidationError,
    load_prepared_bundle,
    validate_bundle_records,
)


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _valid_rows() -> dict[str, list[dict[str, object]]]:
    return {
        "dosage_units": [
            {
                "unit_name": "g",
                "aliases": ["克"],
                "to_grams": 1.0,
                "conversion_type": "standard",
                "precision_note": None,
                "is_standard": True,
                "enabled": True,
            }
        ],
        "herbs": [
            {
                "name": "测试药",
                "aliases": [],
                "properties": "甘，平",
                "meridians": ["脾经"],
                "effects": "测试功效",
                "indications": "测试主治",
                "dosage": "3-9g",
                "max_dose": 9.0,
                "contraindications": [],
                "eighteen_incompatibilities": [],
                "nineteen_fears": [],
                "pregnancy_contraindication": "none",
                "incompatibilities": [],
                "doc_text": "测试药。功效：测试功效。",
            }
        ],
        "formulas": [
            {
                "name": "测试方",
                "aliases": [],
                "composition": [
                    {"herb": "测试药", "dose": 3.0, "unit": "g", "note": ""}
                ],
                "effect": "测试功效",
                "indications": "测试主治",
                "usage": "水煎服",
                "source": "测试来源",
                "modification_rules": [],
                "doc_text": "测试方。组成：测试药3g。功效：测试功效。",
            }
        ],
        "cases": [
            {
                "entry_type": "case",
                "title": "测试医案 [abc12345]",
                "disease_category": "测试病",
                "syndrome": "测试证",
                "treatment_principle": "测试治法",
                "formula_summary": "测试方",
                "content": "患者甲，成年人，主诉测试症状。",
                "source": "测试来源",
                "metadata": {
                    "deidentified": True,
                    "record_key": "abc123456789",
                    "original_title": "测试医案",
                    "license_status": LICENSE_STATUS["cases"],
                    "redactions": [],
                },
            }
        ],
    }


def _build_bundle(root: Path) -> Path:
    rows = _valid_rows()
    source_names = {
        "dosage_units": "dosage_units.json",
        "herbs": "herbs_converted.json",
        "formulas": "formulas_converted.json",
        "cases": "theory_cases_converted.json",
    }
    outputs = []
    source_files = []
    for kind, records in rows.items():
        relative = f"prepared/{kind}.json"
        digest = _write_json(root / relative, records)
        outputs.append(
            {
                "kind": kind,
                "relative_path": relative,
                "sha256": digest,
                "record_count": len(records),
            }
        )
        source_files.append(
            {
                "kind": kind,
                "path": source_names[kind],
                "sha256": "f" * 64,
                "bytes": 1,
                "record_count": len(records),
                "schema": {},
            }
        )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "xuanhu.rag-bundle.v1",
            "dataset_id": "test-bundle",
            "source_files": source_files,
            "outputs": outputs,
            "policy": {},
        },
    )
    return root


def test_load_prepared_bundle_verifies_hashes_and_counts(tmp_path: Path) -> None:
    bundle = load_prepared_bundle(_build_bundle(tmp_path / "bundle"))

    assert bundle.manifest["dataset_id"] == "test-bundle"
    assert len(bundle.records["cases"]) == 1
    assert len(bundle.manifest_sha256) == 64


def test_load_prepared_bundle_rejects_tampered_output(tmp_path: Path) -> None:
    root = _build_bundle(tmp_path / "bundle")
    (root / "prepared" / "herbs.json").write_text("[]", encoding="utf-8")

    with pytest.raises(BundleValidationError, match="digest mismatch"):
        load_prepared_bundle(root)


def test_validate_bundle_records_accepts_safe_bundle(tmp_path: Path) -> None:
    bundle = load_prepared_bundle(_build_bundle(tmp_path / "bundle"))

    stats = validate_bundle_records(bundle)

    assert sum(len(item.blockers) for item in stats.values()) == 0


def test_validate_bundle_records_rejects_duplicate_case_key(tmp_path: Path) -> None:
    root = _build_bundle(tmp_path / "bundle")
    cases_path = root / "prepared" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases.append(dict(cases[0]))
    digest = _write_json(cases_path, cases)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    case_output = next(row for row in manifest["outputs"] if row["kind"] == "cases")
    case_output["sha256"] = digest
    case_output["record_count"] = 2
    _write_json(root / "manifest.json", manifest)

    stats = validate_bundle_records(load_prepared_bundle(root))

    assert any(
        issue["field"] == "metadata.record_key"
        for issue in stats["cases"].blockers
    )


@pytest.mark.parametrize(
    "content",
    [
        "病历号：1234567，患者诉头痛。",
        "住院号 AB-12345，患者诉头痛。",
        "联系电话 13800138000。",
        "身份证 11010519491231002X。",
        "联系邮箱 patient@example.com。",
    ],
)
def test_validate_bundle_records_rejects_direct_identifiers(
    tmp_path: Path,
    content: str,
) -> None:
    root = _build_bundle(tmp_path / "bundle")
    cases_path = root / "prepared" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases[0]["content"] = content
    digest = _write_json(cases_path, cases)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    case_output = next(row for row in manifest["outputs"] if row["kind"] == "cases")
    case_output["sha256"] = digest
    _write_json(root / "manifest.json", manifest)

    stats = validate_bundle_records(load_prepared_bundle(root))

    assert any(
        "direct identifier" in issue["message"]
        for issue in stats["cases"].blockers
    )
