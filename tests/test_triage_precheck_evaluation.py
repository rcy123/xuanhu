"""Engineering evaluation-package controls for the triage precheck."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import scripts.evaluate_triage_precheck as evaluation
from app.agent_runtime.triage_precheck import PrecheckDisposition, TriagePrecheckResult
from app.schemas.intake import RedFlagCategory


def _default_package() -> evaluation.EvaluationPackage:
    return evaluation.load_evaluation_package(
        evaluation.DEFAULT_DATASET_PATH,
        evaluation.DEFAULT_MANIFEST_PATH,
    )


def _copy_package(tmp_path: Path) -> tuple[Path, Path]:
    dataset_path = tmp_path / "seed.jsonl"
    manifest_path = tmp_path / "seed.manifest.json"
    dataset_path.write_bytes(evaluation.DEFAULT_DATASET_PATH.read_bytes())
    manifest_path.write_bytes(evaluation.DEFAULT_MANIFEST_PATH.read_bytes())
    return dataset_path, manifest_path


def test_seed_package_is_hash_pinned_nonclinical_and_covers_every_required_axis() -> None:
    package = _default_package()

    assert package.manifest.label_status == evaluation.ENGINEERING_LABEL_STATUS
    assert package.manifest.provenance == evaluation.ENGINEERING_PROVENANCE
    assert package.manifest.case_count == len(package.cases)
    assert evaluation.canonical_dataset_sha256(package.cases) == package.manifest.canonical_sha256
    assert all(case.label_status == evaluation.ENGINEERING_LABEL_STATUS for case in package.cases)
    assert all(case.provenance == evaluation.ENGINEERING_PROVENANCE for case in package.cases)

    high_risk_categories = {item.value for item in RedFlagCategory if item is not RedFlagCategory.OTHER}
    covered_categories = {
        category
        for case in package.cases
        if case.expected_disposition == PrecheckDisposition.RED_FLAG.value
        for category in case.expected_categories
    }
    coverage_tags = {tag for case in package.cases for tag in case.coverage_tags}
    assert high_risk_categories <= covered_categories
    assert coverage_tags >= evaluation._REQUIRED_COVERAGE_TAGS


def test_current_engineering_seed_matches_without_becoming_clinical_evidence() -> None:
    package = _default_package()
    report = evaluation.evaluate_package(package)

    assert report["status"] == "not_for_clinical_signoff"
    assert report["label_status"] == "engineering_seed_not_clinically_adjudicated"
    assert report["engineering_expectation_mismatched_cases"] == 0
    assert report["failed_case_ids"] == []
    assert report["engineering_expectation_matched_cases"] == len(package.cases)
    assert "clinical_recall" not in report
    assert "clinical_pass" not in report


def test_failure_report_contains_case_ids_but_never_source_text() -> None:
    package = _default_package()
    first_case = package.cases[0]
    mismatched_case = replace(first_case, expected_categories=(RedFlagCategory.SEVERE_PAIN.value,))
    mismatched_package = replace(package, cases=(mismatched_case, *package.cases[1:]))

    report = evaluation.evaluate_package(mismatched_package)
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["failed_case_ids"] == [first_case.case_id]
    assert "failures" not in report
    assert "failure_reasons" not in report
    assert all(case.text not in rendered for case in package.cases)


def test_canonical_hash_detects_text_tampering(tmp_path: Path) -> None:
    dataset_path, manifest_path = _copy_package(tmp_path)
    original = dataset_path.read_text(encoding="utf-8")
    dataset_path.write_text(
        original.replace("The person is unconscious now.", "The person is unconscious immediately."),
        encoding="utf-8",
    )

    with pytest.raises(evaluation.PackageValidationError) as exc_info:
        evaluation.load_evaluation_package(dataset_path, manifest_path)

    assert exc_info.value.code == "dataset_canonical_sha256_mismatch"


def test_strict_schema_rejects_extra_fields_without_echoing_values(tmp_path: Path) -> None:
    dataset_path, manifest_path = _copy_package(tmp_path)
    lines = dataset_path.read_text(encoding="utf-8").splitlines()
    first: dict[str, Any] = json.loads(lines[0])
    first["patient_name"] = "synthetic-value-that-must-not-escape"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    dataset_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(evaluation.PackageValidationError) as exc_info:
        evaluation.load_evaluation_package(dataset_path, manifest_path)

    assert exc_info.value.code == "case_schema_fields_invalid"
    assert "synthetic-value-that-must-not-escape" not in str(exc_info.value)


@pytest.mark.parametrize("target", ["case", "manifest"])
def test_clinical_label_status_cannot_be_claimed_by_editing_seed_files(tmp_path: Path, target: str) -> None:
    dataset_path, manifest_path = _copy_package(tmp_path)
    if target == "case":
        lines = dataset_path.read_text(encoding="utf-8").splitlines()
        first: dict[str, Any] = json.loads(lines[0])
        first["label_status"] = "clinically_adjudicated"
        lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
        dataset_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["label_status"] = "clinically_adjudicated"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(evaluation.PackageValidationError) as exc_info:
        evaluation.load_evaluation_package(dataset_path, manifest_path)

    assert exc_info.value.code == "clinical_label_status_not_permitted"


def test_cli_can_fail_on_mismatch_and_emits_no_case_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def always_clear(_: object, __: object) -> TriagePrecheckResult:
        return TriagePrecheckResult(disposition=PrecheckDisposition.CLEAR)

    monkeypatch.setattr(evaluation, "evaluate_raw_text_triage_precheck", always_clear)

    exit_code = evaluation.main(["--fail-on-mismatch"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert '"status": "not_for_clinical_signoff"' in output
    assert '"failed_case_ids"' in output
    assert all(case.text not in output for case in _default_package().cases)


def test_invalid_package_cli_error_is_privacy_minimal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset_path, manifest_path = _copy_package(tmp_path)
    lines = dataset_path.read_text(encoding="utf-8").splitlines()
    first: dict[str, Any] = json.loads(lines[0])
    first["label_status"] = "clinically_adjudicated"
    first["text"] = "SYNTHETIC-TEXT-MUST-NOT-BE-PRINTED"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    dataset_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    exit_code = evaluation.main(["--dataset", str(dataset_path), "--manifest", str(manifest_path)])
    output = capsys.readouterr().out

    assert exit_code == 2
    assert '"error_code": "clinical_label_status_not_permitted"' in output
    assert "SYNTHETIC-TEXT-MUST-NOT-BE-PRINTED" not in output
