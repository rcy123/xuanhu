"""Evaluate the deterministic triage precheck against an engineering seed set.

This tool intentionally cannot issue, import, or imitate a clinical sign-off.
Its bundled data is synthetic and has not been clinically adjudicated.  The
only persisted failure detail is a ``case_id``; source text is never copied to
the report.

Usage::

    uv run python -m scripts.evaluate_triage_precheck
    uv run python -m scripts.evaluate_triage_precheck --fail-on-mismatch
    uv run python -m scripts.evaluate_triage_precheck --output triage-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from app.agent_runtime.triage_precheck import (
    TRIAGE_PRECHECK_VERSION,
    PrecheckContext,
    PrecheckDisposition,
    evaluate_raw_text_triage_precheck,
)
from app.schemas.intake import RedFlagCategory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "triage_precheck_engineering_seed.v1.jsonl"
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "triage_precheck_engineering_seed.v1.manifest.json"
RULE_PATH = PROJECT_ROOT / "app" / "agent_runtime" / "triage_precheck.py"

CASE_SCHEMA_VERSION = "triage-precheck-evaluation-case.v1"
MANIFEST_SCHEMA_VERSION = "triage-precheck-evaluation-manifest.v1"
REPORT_SCHEMA_VERSION = "triage-precheck-engineering-report.v1"
DATASET_NAME = "triage-precheck-engineering-seed"
ENGINEERING_LABEL_STATUS = "engineering_seed_not_clinically_adjudicated"
ENGINEERING_PROVENANCE = "synthetic_deidentified_engineering_seed"
REPORT_STATUS = "not_for_clinical_signoff"

_CASE_ID_PATTERN = re.compile(r"^seed-[a-z0-9][a-z0-9-]{2,63}$")
_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
_COVERAGE_TAG_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,95}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CASE_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_version",
        "case_id",
        "label_status",
        "provenance",
        "text",
        "expected_disposition",
        "expected_categories",
        "expected_contexts",
        "coverage_tags",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_name",
        "dataset_version",
        "label_status",
        "provenance",
        "case_count",
        "canonical_sha256",
    }
)
_HIGH_RISK_CATEGORIES = frozenset(
    category.value for category in RedFlagCategory if category is not RedFlagCategory.OTHER
)
_VALID_DISPOSITIONS = frozenset(item.value for item in PrecheckDisposition)
_VALID_CATEGORIES = frozenset(item.value for item in RedFlagCategory)
_VALID_CONTEXTS = frozenset(item.value for item in PrecheckContext)
_REQUIRED_COVERAGE_TAGS = frozenset(
    {
        "language.zh",
        "language.en",
        "lexical.synonym",
        "numeric.temperature.celsius_boundary",
        "numeric.temperature.fahrenheit_conversion",
        "numeric.oxygen.boundary",
        "context.negation",
        "context.historical",
        "context.resolved",
        "context.hypothetical",
        "context.third_person",
        "context.uncertain",
        "multi_red_flag",
    }
)
_CASE_NAMESPACE = UUID("4e723923-a513-5aa5-a152-c17ac647b31d")


class PackageValidationError(ValueError):
    """Privacy-safe package validation error.

    The exception deliberately retains only a stable error code and optional
    line number.  It never includes source text or a rejected field value.
    """

    def __init__(self, code: str, *, line_number: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.line_number = line_number


@dataclass(frozen=True)
class EvaluationCase:
    schema_version: str
    dataset_version: str
    case_id: str
    label_status: str
    provenance: str
    text: str
    expected_disposition: str
    expected_categories: tuple[str, ...]
    expected_contexts: tuple[str, ...]
    coverage_tags: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_version": self.dataset_version,
            "case_id": self.case_id,
            "label_status": self.label_status,
            "provenance": self.provenance,
            "text": self.text,
            "expected_disposition": self.expected_disposition,
            "expected_categories": list(self.expected_categories),
            "expected_contexts": list(self.expected_contexts),
            "coverage_tags": list(self.coverage_tags),
        }


@dataclass(frozen=True)
class EvaluationManifest:
    schema_version: str
    dataset_name: str
    dataset_version: str
    label_status: str
    provenance: str
    case_count: int
    canonical_sha256: str


@dataclass(frozen=True)
class EvaluationPackage:
    manifest: EvaluationManifest
    cases: tuple[EvaluationCase, ...]


def _require_string(payload: Mapping[str, object], key: str, *, line_number: int | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PackageValidationError(f"{key}_must_be_string", line_number=line_number)
    return value


def _require_sorted_string_tuple(
    payload: Mapping[str, object],
    key: str,
    *,
    line_number: int,
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise PackageValidationError(f"{key}_must_be_array", line_number=line_number)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PackageValidationError(f"{key}_items_must_be_strings", line_number=line_number)
        items.append(item)
    result = tuple(items)
    if len(result) != len(set(result)) or result != tuple(sorted(result)):
        raise PackageValidationError(f"{key}_must_be_unique_and_sorted", line_number=line_number)
    return result


def _object_payload(raw: object, *, code: str, line_number: int | None = None) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise PackageValidationError(code, line_number=line_number)
    return {str(key): value for key, value in raw.items()}


def _parse_case(raw: object, *, line_number: int, dataset_version: str) -> EvaluationCase:
    payload = _object_payload(raw, code="case_must_be_object", line_number=line_number)
    if frozenset(payload) != _CASE_FIELDS:
        raise PackageValidationError("case_schema_fields_invalid", line_number=line_number)

    schema_version = _require_string(payload, "schema_version", line_number=line_number)
    case_dataset_version = _require_string(payload, "dataset_version", line_number=line_number)
    case_id = _require_string(payload, "case_id", line_number=line_number)
    label_status = _require_string(payload, "label_status", line_number=line_number)
    provenance = _require_string(payload, "provenance", line_number=line_number)
    text = _require_string(payload, "text", line_number=line_number)
    expected_disposition = _require_string(payload, "expected_disposition", line_number=line_number)
    expected_categories = _require_sorted_string_tuple(payload, "expected_categories", line_number=line_number)
    expected_contexts = _require_sorted_string_tuple(payload, "expected_contexts", line_number=line_number)
    coverage_tags = _require_sorted_string_tuple(payload, "coverage_tags", line_number=line_number)

    if schema_version != CASE_SCHEMA_VERSION:
        raise PackageValidationError("case_schema_version_invalid", line_number=line_number)
    if case_dataset_version != dataset_version:
        raise PackageValidationError("case_dataset_version_mismatch", line_number=line_number)
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        raise PackageValidationError("case_id_invalid", line_number=line_number)
    if label_status != ENGINEERING_LABEL_STATUS:
        raise PackageValidationError("clinical_label_status_not_permitted", line_number=line_number)
    if provenance != ENGINEERING_PROVENANCE:
        raise PackageValidationError("case_provenance_invalid", line_number=line_number)
    if not text.strip() or len(text) > 1000 or any(character in text for character in ("\r", "\n", "\x00")):
        raise PackageValidationError("case_text_invalid", line_number=line_number)
    if expected_disposition not in _VALID_DISPOSITIONS:
        raise PackageValidationError("expected_disposition_invalid", line_number=line_number)
    if not set(expected_categories).issubset(_VALID_CATEGORIES):
        raise PackageValidationError("expected_categories_invalid", line_number=line_number)
    if not set(expected_contexts).issubset(_VALID_CONTEXTS):
        raise PackageValidationError("expected_contexts_invalid", line_number=line_number)
    if not coverage_tags or any(not _COVERAGE_TAG_PATTERN.fullmatch(tag) for tag in coverage_tags):
        raise PackageValidationError("coverage_tags_invalid", line_number=line_number)

    if expected_disposition == PrecheckDisposition.CLEAR.value and (expected_categories or expected_contexts):
        raise PackageValidationError("clear_expectation_must_have_no_evidence", line_number=line_number)
    if expected_disposition == PrecheckDisposition.RED_FLAG.value and (
        not expected_categories
        or RedFlagCategory.OTHER.value in expected_categories
        or expected_contexts != (PrecheckContext.CURRENT.value,)
    ):
        raise PackageValidationError("red_flag_expectation_invalid", line_number=line_number)
    if expected_disposition == PrecheckDisposition.MANUAL_REVIEW.value and (
        expected_categories != (RedFlagCategory.OTHER.value,)
        or not expected_contexts
        or PrecheckContext.CURRENT.value in expected_contexts
        or PrecheckContext.SYSTEM_ERROR.value in expected_contexts
    ):
        raise PackageValidationError("manual_review_expectation_invalid", line_number=line_number)

    return EvaluationCase(
        schema_version=schema_version,
        dataset_version=case_dataset_version,
        case_id=case_id,
        label_status=label_status,
        provenance=provenance,
        text=text,
        expected_disposition=expected_disposition,
        expected_categories=expected_categories,
        expected_contexts=expected_contexts,
        coverage_tags=coverage_tags,
    )


def _parse_manifest(raw: object) -> EvaluationManifest:
    payload = _object_payload(raw, code="manifest_must_be_object")
    if frozenset(payload) != _MANIFEST_FIELDS:
        raise PackageValidationError("manifest_schema_fields_invalid")

    schema_version = _require_string(payload, "schema_version")
    dataset_name = _require_string(payload, "dataset_name")
    dataset_version = _require_string(payload, "dataset_version")
    label_status = _require_string(payload, "label_status")
    provenance = _require_string(payload, "provenance")
    canonical_sha256 = _require_string(payload, "canonical_sha256")
    case_count = payload.get("case_count")

    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise PackageValidationError("manifest_schema_version_invalid")
    if dataset_name != DATASET_NAME:
        raise PackageValidationError("manifest_dataset_name_invalid")
    if not _VERSION_PATTERN.fullmatch(dataset_version):
        raise PackageValidationError("manifest_dataset_version_invalid")
    if label_status != ENGINEERING_LABEL_STATUS:
        raise PackageValidationError("clinical_label_status_not_permitted")
    if provenance != ENGINEERING_PROVENANCE:
        raise PackageValidationError("manifest_provenance_invalid")
    if not isinstance(case_count, int) or isinstance(case_count, bool) or case_count < 1:
        raise PackageValidationError("manifest_case_count_invalid")
    if not _SHA256_PATTERN.fullmatch(canonical_sha256):
        raise PackageValidationError("manifest_canonical_sha256_invalid")

    return EvaluationManifest(
        schema_version=schema_version,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        label_status=label_status,
        provenance=provenance,
        case_count=case_count,
        canonical_sha256=canonical_sha256,
    )


def canonical_dataset_sha256(cases: Sequence[EvaluationCase]) -> str:
    """Return the SHA-256 of canonical, ordered JSONL case payloads."""

    digest = hashlib.sha256()
    for case in cases:
        canonical_line = json.dumps(
            case.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(canonical_line)
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_dataset_coverage(cases: Sequence[EvaluationCase]) -> None:
    if not cases:
        raise PackageValidationError("dataset_must_not_be_empty")
    case_ids = tuple(case.case_id for case in cases)
    if len(case_ids) != len(set(case_ids)):
        raise PackageValidationError("dataset_case_ids_must_be_unique")
    if case_ids != tuple(sorted(case_ids)):
        raise PackageValidationError("dataset_cases_must_be_sorted")

    covered_categories = {
        category
        for case in cases
        if case.expected_disposition == PrecheckDisposition.RED_FLAG.value
        for category in case.expected_categories
    }
    if not _HIGH_RISK_CATEGORIES.issubset(covered_categories):
        raise PackageValidationError("dataset_high_risk_category_coverage_incomplete")
    coverage_tags = {tag for case in cases for tag in case.coverage_tags}
    if not _REQUIRED_COVERAGE_TAGS.issubset(coverage_tags):
        raise PackageValidationError("dataset_required_coverage_tags_incomplete")


def load_evaluation_package(dataset_path: Path, manifest_path: Path) -> EvaluationPackage:
    """Load, strictly validate, and hash-check an engineering evaluation package."""

    try:
        manifest_raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageValidationError("manifest_json_invalid", line_number=exc.lineno) from None
    manifest = _parse_manifest(manifest_raw)

    cases: list[EvaluationCase] = []
    for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise PackageValidationError("dataset_blank_line_not_permitted", line_number=line_number)
        try:
            raw: object = json.loads(line)
        except json.JSONDecodeError:
            raise PackageValidationError("case_json_invalid", line_number=line_number) from None
        cases.append(_parse_case(raw, line_number=line_number, dataset_version=manifest.dataset_version))

    frozen_cases = tuple(cases)
    _validate_dataset_coverage(frozen_cases)
    if len(frozen_cases) != manifest.case_count:
        raise PackageValidationError("manifest_case_count_mismatch")
    if canonical_dataset_sha256(frozen_cases) != manifest.canonical_sha256:
        raise PackageValidationError("dataset_canonical_sha256_mismatch")
    return EvaluationPackage(manifest=manifest, cases=frozen_cases)


def evaluate_package(package: EvaluationPackage) -> dict[str, object]:
    """Evaluate cases and return a privacy-minimal engineering-only report."""

    failed_case_ids: list[str] = []
    disposition_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()

    for case in package.cases:
        disposition_counts[case.expected_disposition] += 1
        category_counts.update(case.expected_categories)
        coverage_counts.update(case.coverage_tags)

        result = evaluate_raw_text_triage_precheck(uuid5(_CASE_NAMESPACE, case.case_id), case.text)
        actual_categories = tuple(sorted(candidate.category.value for candidate in result.candidates))
        actual_contexts = tuple(sorted({match.context.value for match in result.matches}))
        if (
            result.disposition.value != case.expected_disposition
            or actual_categories != case.expected_categories
            or actual_contexts != case.expected_contexts
        ):
            failed_case_ids.append(case.case_id)

    total_cases = len(package.cases)
    matched_cases = total_cases - len(failed_case_ids)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": REPORT_STATUS,
        "label_status": ENGINEERING_LABEL_STATUS,
        "dataset_name": package.manifest.dataset_name,
        "dataset_version": package.manifest.dataset_version,
        "dataset_canonical_sha256": package.manifest.canonical_sha256,
        "rule_version": TRIAGE_PRECHECK_VERSION,
        "rule_file_sha256": hashlib.sha256(RULE_PATH.read_bytes()).hexdigest(),
        "total_cases": total_cases,
        "engineering_expectation_matched_cases": matched_cases,
        "engineering_expectation_mismatched_cases": len(failed_case_ids),
        "engineering_expectation_match_rate": round(matched_cases / total_cases, 6),
        "expected_disposition_counts": dict(sorted(disposition_counts.items())),
        "expected_category_counts": dict(sorted(category_counts.items())),
        "coverage_tag_counts": dict(sorted(coverage_counts.items())),
        "failed_case_ids": failed_case_ids,
        "limitations": [
            "synthetic_engineering_seed",
            "not_clinically_adjudicated",
            "must_not_be_used_for_clinical_signoff",
        ],
    }


def _safe_error_payload(exc: PackageValidationError) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "invalid_evaluation_package",
        "label_status": ENGINEERING_LABEL_STATUS,
        "error_code": exc.code,
    }
    if exc.line_number is not None:
        payload["line_number"] = exc.line_number
    return payload


def _write_report(payload: Mapping[str, object], output_path: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is None:
        print(rendered)
    else:
        output_path.write_text(f"{rendered}\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate synthetic engineering triage seeds; never produces clinical sign-off evidence."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Return exit code 1 when an engineering expectation does not match.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        package = load_evaluation_package(args.dataset, args.manifest)
        report = evaluate_package(package)
        _write_report(report, args.output)
    except PackageValidationError as exc:
        _write_report(_safe_error_payload(exc), None)
        return 2
    except OSError:
        _write_report(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "status": "invalid_evaluation_package",
                "label_status": ENGINEERING_LABEL_STATUS,
                "error_code": "evaluation_package_io_error",
            },
            None,
        )
        return 2
    except Exception:
        _write_report(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "status": "evaluation_internal_error",
                "label_status": ENGINEERING_LABEL_STATUS,
                "error_code": "evaluation_internal_error",
            },
            None,
        )
        return 2

    if args.fail_on_mismatch and report["engineering_expectation_mismatched_cases"] != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
