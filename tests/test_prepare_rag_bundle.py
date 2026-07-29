"""Offline RAG bundle preparation tests.

These tests use only temporary JSON files. They do not load application settings,
connect to PostgreSQL/Milvus, or invoke an embedding/model service.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.prepare_rag_bundle import (
    LICENSE_STATUS,
    REDACTED_RECORD_ID,
    BundlePreparationError,
    main,
    make_case_record_key,
    prepare_bundle,
    redact_case_record_ids,
    scan_case_pii,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_bundle(input_dir: Path) -> dict[str, Path]:
    dosage_units = [
        {
            "unit_name": "汉升",
            "aliases": [],
            "to_grams": 180,
            "conversion_type": "volume_to_weight",
            "precision_note": "需按药材密度换算。",
            "is_standard": False,
            "enabled": True,
            "category": "汉代古制",
        },
        {
            "unit_name": "两",
            "aliases": ["市两"],
            "to_grams": 30,
            "conversion_type": "fixed",
            "precision_note": "现代测试口径。",
            "is_standard": False,
            "enabled": True,
            "category": "市制",
        },
    ]
    herbs = [
        {
            "name": "禁用药",
            "contraindications": ["孕妇及哺乳期妇女禁用"],
            "max_dose": 10,
            "pregnancy_contraindication": "none",
            "doc_text": "禁用药正文。",
        },
        {
            "name": "慎用药",
            "contraindications": ["孕妇及体虚者慎用"],
            "max_dose": 8,
            "pregnancy_contraindication": "none",
            "doc_text": "慎用药正文。",
        },
        {
            "name": "遵医嘱药",
            "contraindications": ["妊娠期应在医生指导下使用"],
            "max_dose": 6,
            "pregnancy_contraindication": "none",
            "doc_text": "遵医嘱药正文。",
        },
        {
            "name": "歧义药",
            "contraindications": ["孕妇用药资料不足"],
            "max_dose": 5,
            "pregnancy_contraindication": "none",
            "doc_text": "歧义药正文。",
        },
        {
            "name": "零剂量药",
            "contraindications": [],
            "max_dose": 0,
            "pregnancy_contraindication": "none",
            "doc_text": "零剂量药正文。",
        },
        {
            "name": "冲突药",
            "contraindications": [],
            "max_dose": 9,
            "pregnancy_contraindication": "none",
            "effects": "版本甲",
            "doc_text": "冲突药甲。",
        },
        {
            "name": "冲突药",
            "contraindications": [],
            "max_dose": 12,
            "pregnancy_contraindication": "none",
            "effects": "版本乙",
            "doc_text": "冲突药乙。",
        },
    ]
    formula_doc_text = "原样保留。\n第二行；标点  与空格不得重建。"
    formulas = [
        {
            "name": "测试方",
            "composition": [{"herb": "慎用药", "dose": 3, "unit": "g"}],
            "effect": "测试",
            "indications": "测试",
            "usage": "测试",
            "source": "测试来源",
            "modification_rules": [],
            "doc_text": formula_doc_text,
        }
    ]
    cases = [
        {
            "entry_type": "case",
            "title": "同名医案",
            "content": "患者女，寒凝血瘀，予方甲。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "同名医案",
            "content": "患者男，气血不足，予方乙。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "含病历号医案",
            "content": "病历号：ABC-123，住院号 998877。患者症状稳定。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "空正文医案",
            "content": "  ",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "残留手机号医案",
            "content": "联系电话：13800138000，患者症状稳定。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "完全重复医案",
            "content": "相同正文。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
        {
            "entry_type": "case",
            "title": "完全重复医案",
            "content": "相同正文。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        },
    ]

    paths = {
        "dosage_units": input_dir / "dosage_units.json",
        "herbs": input_dir / "herbs_converted.json",
        "formulas": input_dir / "formulas_converted.json",
        "cases": input_dir / "theory_cases_converted.json",
    }
    _write_json(paths["dosage_units"], dosage_units)
    _write_json(paths["herbs"], herbs)
    _write_json(paths["formulas"], formulas)
    _write_json(paths["cases"], cases)
    return paths


def test_prepare_bundle_normalizes_and_quarantines_without_mutating_sources(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    staging_dir = tmp_path / "staging"
    source_paths = _sample_bundle(input_dir)
    before_hashes = {kind: hashlib.sha256(path.read_bytes()).hexdigest() for kind, path in source_paths.items()}

    manifest, report = prepare_bundle(
        input_dir=input_dir,
        staging_dir=staging_dir,
    )

    after_hashes = {kind: hashlib.sha256(path.read_bytes()).hexdigest() for kind, path in source_paths.items()}
    assert after_hashes == before_hashes
    assert manifest["schema_version"] == "xuanhu.rag-bundle.v1"
    assert manifest["policy"]["license_status"] == LICENSE_STATUS
    assert report["summary"]["input_records"] == 17

    prepared_dosage = _read_json(staging_dir / "prepared" / "dosage_units.json")
    assert len(prepared_dosage) == 2
    mapped_unit = next(item for item in prepared_dosage if item["unit_name"] == "汉升")
    assert mapped_unit["conversion_type"] == "unsupported"
    assert mapped_unit["to_grams"] is None
    conversion_audit = mapped_unit["preparation_metadata"]["conversion_normalization"]
    assert conversion_audit["original_conversion_type"] == "volume_to_weight"
    assert conversion_audit["original_to_grams"] == 180

    prepared_herbs = _read_json(staging_dir / "prepared" / "herbs.json")
    herbs_by_name = {item["name"]: item for item in prepared_herbs}
    assert set(herbs_by_name) == {"禁用药", "慎用药", "遵医嘱药", "零剂量药"}
    assert herbs_by_name["禁用药"]["pregnancy_contraindication"] == "forbidden"
    assert herbs_by_name["慎用药"]["pregnancy_contraindication"] == "caution"
    assert herbs_by_name["遵医嘱药"]["pregnancy_contraindication"] == "caution"
    assert herbs_by_name["零剂量药"]["max_dose"] is None
    assert herbs_by_name["零剂量药"]["preparation_metadata"]["max_dose_normalization"]["review_required"]

    quarantined_herbs = _read_json(staging_dir / "quarantine" / "herbs.json")
    reason_sets = [set(entry["reason_codes"]) for entry in quarantined_herbs]
    assert sum("duplicate_name_conflict" in reasons for reasons in reason_sets) == 2
    assert sum("ambiguous_pregnancy_text_review_required" in reasons for reasons in reason_sets) == 1
    pregnancy_report = report["by_kind"]["herbs"]["pregnancy"]
    assert pregnancy_report["original_counts"]["none"] == 7
    assert pregnancy_report["normalized_counts"]["forbidden"] == 1
    assert pregnancy_report["normalized_counts"]["caution"] == 2
    assert pregnancy_report["normalized_counts"]["review_required"] == 1
    assert pregnancy_report["ambiguous_quarantined"] == 1

    raw_formula = _read_json(source_paths["formulas"])[0]
    prepared_formula = _read_json(staging_dir / "prepared" / "formulas.json")[0]
    assert prepared_formula["doc_text"] == raw_formula["doc_text"]
    assert prepared_formula == raw_formula

    prepared_cases = _read_json(staging_dir / "prepared" / "cases.json")
    assert len(prepared_cases) == 3
    duplicate_titles = [item for item in prepared_cases if item["metadata"]["original_title"] == "同名医案"]
    assert len(duplicate_titles) == 2
    assert len({item["title"] for item in duplicate_titles}) == 2
    assert all(item["title"].startswith("同名医案 [") for item in duplicate_titles)
    assert len({item["metadata"]["record_key"] for item in duplicate_titles}) == 2

    redacted_case = next(item for item in prepared_cases if item["metadata"]["original_title"] == "含病历号医案")
    assert redacted_case["content"].count(REDACTED_RECORD_ID) == 2
    assert "ABC-123" not in redacted_case["content"]
    assert "998877" not in redacted_case["content"]
    assert redacted_case["metadata"]["license_status"] == LICENSE_STATUS
    assert redacted_case["metadata"]["source_license_label"] == "reference"
    assert len(redacted_case["metadata"]["redactions"]) == 2
    assert scan_case_pii(redacted_case["content"]) == []

    quarantined_cases = _read_json(staging_dir / "quarantine" / "cases.json")
    assert len(quarantined_cases) == 4
    case_reason_sets = [set(entry["reason_codes"]) for entry in quarantined_cases]
    assert sum("empty_content" in reasons for reasons in case_reason_sets) == 1
    assert sum("residual_pii_mobile_phone" in reasons for reasons in case_reason_sets) == 1
    assert sum("residual_pii_labeled_phone" in reasons for reasons in case_reason_sets) == 1
    assert sum("duplicate_record_key" in reasons for reasons in case_reason_sets) == 2
    assert report["by_kind"]["cases"]["prepared_residual_pii_blockers"] == 0

    output_by_path = {item["relative_path"]: item for item in manifest["outputs"]}
    for relative_path, output_entry in output_by_path.items():
        assert hashlib.sha256((staging_dir / relative_path).read_bytes()).hexdigest() == output_entry["sha256"]


def test_dry_run_performs_validation_without_writing_any_output(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    staging_dir = tmp_path / "staging"
    manifest_path = tmp_path / "custom" / "manifest.json"
    report_path = tmp_path / "custom" / "report.json"
    _sample_bundle(input_dir)

    manifest, report = prepare_bundle(
        input_dir=input_dir,
        staging_dir=staging_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        dry_run=True,
    )

    assert manifest["dry_run"] is True
    assert report["dry_run"] is True
    assert report["summary"]["prepared_records"] > 0
    assert not staging_dir.exists()
    assert not manifest_path.exists()
    assert not report_path.exists()


def test_cli_dry_run_returns_summary_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_dir = tmp_path / "raw"
    staging_dir = tmp_path / "staging"
    _sample_bundle(input_dir)

    exit_code = main(
        [
            "--input-dir",
            str(input_dir),
            "--staging-dir",
            str(staging_dir),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert exit_code == 0
    assert summary["dry_run"] is True
    assert summary["summary"]["input_records"] == 17
    assert not staging_dir.exists()


def test_case_record_key_uses_normalized_content() -> None:
    key_one = make_case_record_key("测试平台", "同一标题", "第一行\r\n第二行")
    key_two = make_case_record_key(" 测试平台 ", "同一标题", "第一行  第二行")
    assert key_one == key_two


def test_record_id_redaction_is_deterministic_and_auditable() -> None:
    content = "病历号：ABC-123；住院编号为998877；门诊号#ZX-9。"
    first, first_records = redact_case_record_ids(content)
    second, second_records = redact_case_record_ids(content)

    assert first == second
    assert first_records == second_records
    assert first.count(REDACTED_RECORD_ID) == 3
    assert scan_case_pii(first) == []
    assert {record["label"] for record in first_records} == {"病历号", "住院编号", "门诊号"}


def test_case_title_with_direct_identifier_is_quarantined(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    staging_dir = tmp_path / "staging"
    source_paths = _sample_bundle(input_dir)
    cases = _read_json(source_paths["cases"])
    cases.append(
        {
            "entry_type": "case",
            "title": "联系电话 13800138000",
            "content": "患者症状稳定。",
            "source": "测试平台",
            "metadata": {"deidentified": True, "license": "reference"},
        }
    )
    _write_json(source_paths["cases"], cases)

    _, report = prepare_bundle(input_dir=input_dir, staging_dir=staging_dir)

    quarantined = _read_json(staging_dir / "quarantine" / "cases.json")
    title_pii_entries = [
        entry
        for entry in quarantined
        if "residual_pii_title_mobile_phone" in entry["reason_codes"]
    ]
    assert len(title_pii_entries) == 1
    assert report["by_kind"]["cases"]["prepared_residual_pii_blockers"] == 0


def test_output_paths_cannot_overlap_read_only_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    _sample_bundle(input_dir)

    with pytest.raises(BundlePreparationError, match="must not be inside"):
        prepare_bundle(
            input_dir=input_dir,
            staging_dir=input_dir / "staging",
            dry_run=True,
        )
