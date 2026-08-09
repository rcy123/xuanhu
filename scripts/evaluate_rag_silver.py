"""Run the frozen ``rag-silver-v1`` retrieval evaluation.

This module deliberately contains evaluation plumbing only.  The baseline calls
``RAGRetriever._vector_search`` and the full arm calls the existing
``RAGRetriever.retrieve`` implementation; it must never grow a second copy of
the production hybrid-ranking algorithm.

The command line intentionally has no mock, fixture, ranking, or metric input.
Fakes belong in unit tests only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCHEMA_VERSION = "1.0"
DATASET_VERSION = "rag-silver-v1"
FIXED_SEED = 20260807
FIXED_SMOKE_SIZE = 20
FIXED_TEST_SIZE = 200
FIXED_TOP_K = 8
RECALL_CUTOFFS = (1, 5, FIXED_TOP_K)
FIXED_BOOTSTRAP_SAMPLES = 10_000
SOURCE_TYPES = ["case"]
RESULT_SCHEMA_VERSION = "1.0"
RETRY_BACKOFF_SECONDS = (1.0, 3.0)
QUERY_STYLE_NATURAL_LANGUAGE_V1 = "natural_language_v1"
QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1 = "structured_fact_key_value_v1"
_SUPPORTED_QUERY_STYLES = frozenset(
    {
        QUERY_STYLE_NATURAL_LANGUAGE_V1,
        QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1,
    }
)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_SOURCE_FILES: dict[str, Path] = {
    "evaluator": Path(__file__).resolve(),
    "builder": _PROJECT_ROOT / "scripts" / "build_rag_silver_eval.py",
    "hard_patient_builder": _PROJECT_ROOT / "scripts" / "build_rag_hard_patient_eval.py",
    "config": _PROJECT_ROOT / "app" / "core" / "config.py",
    "gateway": _PROJECT_ROOT / "app" / "core" / "gateway.py",
    "embedding_gateway": _PROJECT_ROOT / "app" / "core" / "embedding_gateway.py",
    "reranker_gateway": _PROJECT_ROOT / "app" / "core" / "reranker_gateway.py",
    "rewrite_gateway": _PROJECT_ROOT / "app" / "core" / "rewrite_gateway.py",
    "retriever": _PROJECT_ROOT / "app" / "rag" / "retriever.py",
    "reranker": _PROJECT_ROOT / "app" / "rag" / "reranker.py",
    "reasoning_retrieval": _PROJECT_ROOT / "app" / "rag" / "reasoning_retrieval.py",
}
_RECORD_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURED_FACT_KEY_VALUE_PART_RE = re.compile(
    r"(?P<fact_key>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)=(?P<value>[^=；\r\n]+)"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+)[^\s,]+|(sk-[A-Za-z0-9_\-]+)|((?:password|token|api[_-]?key)\s*[=:]\s*)[^\s,]+"
)
_ARTIFACT_SECRET_RE = re.compile(
    r"(?i)bearer\s+(?!\*\*\*)\S+|sk-[A-Za-z0-9_\-]+|(?:api[_-]?key|password|token|authorization)\s*[\"']?\s*[:=]\s*[\"']?(?!\*\*\*)[^\s,}\]]+"
)
_FAILURE_CLASSIFICATIONS = {
    "arm_technical_failure",
    "component_fallback",
    "dataset_rejection",
    "preflight_failure",
    "operator_error",
}
_REQUIRED_PREFLIGHT_CHECKS = (
    "local_quality_gates",
    "frozen_dataset",
    "source_file",
    "effective_contract_configuration",
    "postgres_connectivity",
    "milvus_collection",
    "model_gateways",
    "frozen_target_resolution",
    "prepared_corpus_snapshot",
)

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class RetrievalProfile:
    """Explicit full-arm retrieval parameters for a reproducible comparison."""

    name: str
    vector_top_k: int
    fulltext_top_k: int
    reranker_top_k: int
    fulltext_quota: int
    fulltext_lexical_enabled: bool
    reranker_max_chunks_per_source: int = 0
    dual_query_enabled: bool = False


_RETRIEVAL_PROFILES: dict[str, RetrievalProfile] = {
    "v12-lexical-off": RetrievalProfile("v12-lexical-off", 12, 12, 20, 0, False),
    "current-v12": RetrievalProfile("current-v12", 12, 12, 20, 0, True),
    "current-v12-source-diverse": RetrievalProfile(
        "current-v12-source-diverse", 12, 12, 20, 0, True, reranker_max_chunks_per_source=1
    ),
    "current-v12-dual-rrf": RetrievalProfile("current-v12-dual-rrf", 12, 12, 20, 0, True, dual_query_enabled=True),
    "current-v12-dual-rrf-source-diverse": RetrievalProfile(
        "current-v12-dual-rrf-source-diverse",
        12,
        12,
        20,
        0,
        True,
        reranker_max_chunks_per_source=1,
        dual_query_enabled=True,
    ),
    "current-v12-expanded20": RetrievalProfile("current-v12-expanded20", 20, 20, 28, 0, True),
    "current-v12-dual-full": RetrievalProfile("current-v12-dual-full", 12, 12, 48, 0, True, dual_query_enabled=True),
    # ``reranker_top_k`` is the complete candidate pool, not just the vector
    # prefix.  Reserve eight lexical-only places *in addition to* the 20
    # widened vector candidates so the experiment actually tests recall@20.
    "expanded-v20-f8": RetrievalProfile("expanded-v20-f8", 20, 20, 28, 8, True),
    "expanded-v32-f12": RetrievalProfile("expanded-v32-f12", 32, 32, 44, 12, True),
}
_DEFAULT_RETRIEVAL_PROFILE = "current-v12"


def resolve_retrieval_profile(name: str) -> RetrievalProfile:
    try:
        return _RETRIEVAL_PROFILES[name]
    except KeyError as exc:
        raise EvaluationError(f"unknown retrieval profile: {name}") from exc


class EvaluationError(RuntimeError):
    """A fail-closed evaluation contract error."""


class DatasetError(EvaluationError):
    """The frozen dataset does not satisfy the evaluation contract."""


class ResumeIntegrityError(EvaluationError):
    """An existing result JSONL cannot safely be resumed."""


class TargetResolutionError(EvaluationError):
    """A frozen target cannot uniquely resolve to an active case."""


class ArmTechnicalFailure(EvaluationError):
    """An arm did not produce a scoreable ranking and must be retried."""


def _require_query_style(query_style: Any, *, error_type: type[EvaluationError] = EvaluationError) -> str:
    if not isinstance(query_style, str) or query_style not in _SUPPORTED_QUERY_STYLES:
        raise error_type(f"unknown query_style: {query_style!r}")
    return query_style


def query_style_from_manifest(manifest: Mapping[str, Any]) -> str:
    """Return the frozen input contract, preserving legacy v1's default."""
    return _require_query_style(
        manifest.get("query_style", QUERY_STYLE_NATURAL_LANGUAGE_V1),
        error_type=DatasetError,
    )


def query_style_from_config(config: Mapping[str, Any]) -> str:
    """Read the mode captured with a run, defaulting only legacy v1 artifacts."""
    return _require_query_style(config.get("query_style", QUERY_STYLE_NATURAL_LANGUAGE_V1))


def query_style_from_result(record: Mapping[str, Any]) -> str:
    """Read the mode persisted on a result row, defaulting only legacy v1 rows."""
    return _require_query_style(
        record.get("query_style", QUERY_STYLE_NATURAL_LANGUAGE_V1), error_type=ResumeIntegrityError
    )


def validate_structured_fact_key_value_query(query: str) -> None:
    """Fail closed unless a production-style direct fact query is unambiguous."""
    try:
        from scripts.build_rag_silver_eval import STRUCTURED_QUERY_CANONICAL_FACT_KEYS
    except Exception as exc:  # pragma: no cover - a missing shared contract is an operator failure
        raise DatasetError("structured query canonical fact-key contract is unavailable") from exc
    canonical_fact_keys = set(STRUCTURED_QUERY_CANONICAL_FACT_KEYS)
    if not canonical_fact_keys or not all(isinstance(key, str) for key in canonical_fact_keys):
        raise DatasetError("structured query canonical fact-key contract is invalid")
    if query != query.strip() or "\n" in query or "\r" in query:
        raise DatasetError("structured query has leading/trailing whitespace or a line break")
    parts = query.split("；")
    if not 2 <= len(parts) <= 8 or any(not part for part in parts):
        raise DatasetError("structured query must contain two to eight fact_key=value parts")
    fact_keys: set[str] = set()
    for part in parts:
        match = _STRUCTURED_FACT_KEY_VALUE_PART_RE.fullmatch(part)
        if match is None:
            raise DatasetError("structured query is not fact_key=value；... format")
        fact_key = match.group("fact_key")
        value = match.group("value")
        if fact_key not in canonical_fact_keys:
            raise DatasetError("structured query contains an unknown canonical fact_key")
        if value != value.strip() or not value or ";" in value:
            raise DatasetError("structured query fact value is empty or padded")
        if fact_key in fact_keys:
            raise DatasetError("structured query repeats a fact_key")
        fact_keys.add(fact_key)


def utc_now() -> str:
    """Return an unambiguous UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compact_json_bytes(value: Any) -> bytes:
    """Canonical bytes used by every content hash in this module."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    """Human-readable stable JSON bytes for artifact files."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Write via a sibling temporary file, preserving a prior valid artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def write_json_atomic(path: Path, value: Any) -> None:
    write_bytes_atomic(path, pretty_json_bytes(value))


def read_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"invalid JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON artifact must be an object: {path.name}")
    return cast(JsonObject, value)


def append_jsonl(path: Path, value: JsonObject) -> None:
    """Append one independently durable JSONL row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = compact_json_bytes(value) + b"\n"
    with path.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def redacted_message(exc: BaseException | str) -> str:
    """Keep failure artifacts useful without serialising secrets or responses."""
    raw = str(exc)
    raw = _SECRET_VALUE_RE.sub(lambda match: match.group(1) or match.group(3) or "***", raw)
    raw = raw.replace("\n", " ").replace("\r", " ")
    return raw[:180] or type(exc).__name__ if not isinstance(exc, str) else raw[:180]


def safe_exception_type(exc: BaseException) -> str:
    return type(exc).__name__


def sanitize_host(value: str) -> str:
    """Persist only scheme/host/port, never a path, query, or userinfo."""
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname:
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    return "***"


def _git_value(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(args, cwd=_PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_environment() -> tuple[str, str, bool]:
    commit = _git_value(["git", "rev-parse", "HEAD"]) or "nogit"
    branch = _git_value(["git", "branch", "--show-current"]) or "detached"
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=_PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL
        )
        dirty = bool(status.strip())
    except (OSError, subprocess.CalledProcessError):
        dirty = False
    return commit, branch, dirty


def _phase_map() -> JsonObject:
    return {
        name: {"status": "pending", "started_at": None, "completed_at": None}
        for name in ("discover", "preflight", "implement", "dataset", "corpus", "smoke", "final", "report", "accept")
    }


def initial_state(run_id: str) -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "conclusion": None,
        "dataset_sha256": None,
        "config_sha256": None,
        "collection": None,
        "current_phase": "discover",
        "phases": _phase_map(),
        "last_error": None,
        "updated_at": utc_now(),
    }


def ensure_run_directory(run_dir: Path) -> JsonObject:
    """Create only missing run artifacts; existing audit history is preserved."""
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    if state_path.exists():
        state = read_json(state_path)
    else:
        state = initial_state(run_dir.name)
        write_json_atomic(state_path, state)
    for name in ("execution.log", "failures.jsonl"):
        (run_dir / name).touch(exist_ok=True)
    return state


def update_state(
    run_dir: Path,
    *,
    current_phase: str | None = None,
    status: str | None = None,
    conclusion: str | None | object = ...,  # ellipsis means preserve
    dataset_sha256: str | None | object = ...,
    config_sha256: str | None | object = ...,
    collection: str | None | object = ...,
    last_error: str | None | object = ...,
    phase_status: str | None = None,
) -> JsonObject:
    """Atomically update lifecycle state without conflating status and conclusion."""
    state = ensure_run_directory(run_dir)
    if current_phase is not None:
        state["current_phase"] = current_phase
        phases = state.setdefault("phases", _phase_map())
        phase = phases.setdefault(current_phase, {"status": "pending", "started_at": None, "completed_at": None})
        if phase_status is not None:
            phase["status"] = phase_status
            if phase_status == "running" and not phase.get("started_at"):
                phase["started_at"] = utc_now()
            if phase_status in {"completed", "failed", "blocked"}:
                phase["completed_at"] = utc_now()
    if status is not None:
        if status not in {"pending", "running", "completed", "failed", "blocked"}:
            raise EvaluationError(f"invalid lifecycle status: {status}")
        state["status"] = status
    if conclusion is not ...:
        if conclusion not in {None, "PASS", "INVALID", "BLOCKED"}:
            raise EvaluationError(f"invalid conclusion: {conclusion}")
        state["conclusion"] = conclusion
    for key, value in (
        ("dataset_sha256", dataset_sha256),
        ("config_sha256", config_sha256),
        ("collection", collection),
        ("last_error", last_error),
    ):
        if value is not ...:
            state[key] = value
    state["updated_at"] = utc_now()
    write_json_atomic(run_dir / "state.json", state)
    return state


def log_execution(run_dir: Path, phase: str, command: str, exit_code: int | None, summary: str) -> None:
    """Append an auditable, redacted command summary."""
    row = {
        "at": utc_now(),
        "phase": phase,
        "command": command,
        "exit_code": exit_code,
        "summary": redacted_message(summary),
    }
    append_jsonl(run_dir / "execution.log", row)


def write_failure(
    run_dir: Path,
    *,
    phase: str,
    split: str | None,
    query_id: str | None,
    arm: str | None,
    component: str,
    attempt_number: int,
    retryable: bool,
    backoff_seconds: float,
    will_retry: bool,
    classification: str,
    error: BaseException | str,
    dataset_sha256: str | None,
    config_sha256: str | None,
    error_type: str | None = None,
) -> None:
    append_jsonl(
        run_dir / "failures.jsonl",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_dir.name,
            "occurred_at": utc_now(),
            "phase": phase,
            "split": split,
            "query_id": query_id,
            "arm": arm,
            "component": component,
            "attempt_number": attempt_number,
            "retryable": retryable,
            "backoff_seconds": backoff_seconds,
            "will_retry": will_retry,
            "classification": classification,
            "error_type": error_type or (type(error).__name__ if not isinstance(error, str) else "OperatorError"),
            "message_redacted": redacted_message(error),
            "dataset_sha256": dataset_sha256,
            "config_sha256": config_sha256,
        },
    )


def read_jsonl(path: Path) -> list[JsonObject]:
    if not path.exists():
        raise EvaluationError(f"missing JSONL artifact: {path.name}")
    records: list[JsonObject] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationError(f"cannot read JSONL artifact: {path.name}") from exc
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"invalid JSONL row {number} in {path.name}") from exc
        if not isinstance(row, dict):
            raise EvaluationError(f"non-object JSONL row {number} in {path.name}")
        records.append(cast(JsonObject, row))
    return records


@dataclass(frozen=True, slots=True)
class FrozenSplit:
    split: str
    records: list[JsonObject]
    sha256: str
    manifest: JsonObject


FROZEN_REWRITE_CACHE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class FrozenRewriteCache:
    """Immutable actual Rewrite outputs shared by every ablation run.

    A cache is deliberately bound to the two frozen split hashes, the model
    configuration and every original query hash.  It prevents sampling drift
    in the Rewrite model from being attributed to a retrieval-only ablation.
    """

    sha256: str
    model: str
    temperature: float
    entries: Mapping[str, Mapping[str, Any]]

    def effective_query_for(self, query_id: str, original_query: str) -> str:
        entry = self.entries.get(query_id)
        if entry is None or entry.get("query_sha256") != sha256_text(original_query):
            raise EvaluationError("frozen rewrite cache entry does not bind to the current query")
        effective_query = entry.get("effective_query")
        if not isinstance(effective_query, str) or not effective_query.strip():
            raise EvaluationError("frozen rewrite cache has an empty effective query")
        if entry.get("effective_query_sha256") != sha256_text(effective_query):
            raise EvaluationError("frozen rewrite cache effective query hash is invalid")
        return effective_query


def _validate_dataset_record(
    record: Mapping[str, Any],
    split: str,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> None:
    query_style = _require_query_style(query_style, error_type=DatasetError)
    if record.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError("frozen query schema_version mismatch")
    if record.get("dataset_version") != DATASET_VERSION:
        raise DatasetError("frozen query dataset_version mismatch")
    if record.get("split") != split:
        raise DatasetError("frozen query split mismatch")
    query_id = record.get("query_id")
    query = record.get("query")
    target = record.get("target_record_key")
    if not isinstance(query_id, str) or not query_id:
        raise DatasetError("frozen query has no query_id")
    if not isinstance(query, str) or not query:
        raise DatasetError("frozen query has no query")
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        validate_structured_fact_key_value_query(query)
    if not isinstance(target, str) or _RECORD_KEY_RE.fullmatch(target) is None:
        raise DatasetError("frozen query target_record_key must be a SHA-256")


def load_frozen_split(dataset_dir: Path, split: str) -> FrozenSplit:
    """Load a split and fail closed on manifest/hash/count/identity drift."""
    if split not in {"smoke", "test"}:
        raise DatasetError("split must be smoke or test")
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise DatasetError("frozen manifest.json is missing")
    manifest = read_json(manifest_path)
    if manifest.get("dataset_version") != DATASET_VERSION or manifest.get("frozen") is not True:
        raise DatasetError("dataset is not frozen rag-silver-v1")
    query_style = query_style_from_manifest(manifest)
    path = dataset_dir / f"{split}.jsonl"
    actual_sha = sha256_file(path) if path.exists() else ""
    expected_sha = (manifest.get("artifact_sha256") or {}).get(f"{split}.jsonl")
    if not isinstance(expected_sha, str) or expected_sha != actual_sha:
        raise DatasetError(f"{split}.jsonl SHA-256 differs from frozen manifest")
    records = read_jsonl(path)
    expected_size = FIXED_SMOKE_SIZE if split == "smoke" else FIXED_TEST_SIZE
    if len(records) != expected_size:
        raise DatasetError(f"{split}.jsonl must contain exactly {expected_size} rows")
    query_ids: set[str] = set()
    targets: set[str] = set()
    for row in records:
        _validate_dataset_record(row, split, query_style=query_style)
        query_id = cast(str, row["query_id"])
        target = cast(str, row["target_record_key"])
        if query_id in query_ids or target in targets:
            raise DatasetError(f"duplicate frozen identity in {split}.jsonl")
        query_ids.add(query_id)
        targets.add(target)
    return FrozenSplit(split=split, records=records, sha256=actual_sha, manifest=manifest)


def validate_dataset_pair(dataset_dir: Path) -> tuple[FrozenSplit, FrozenSplit]:
    smoke = load_frozen_split(dataset_dir, "smoke")
    test = load_frozen_split(dataset_dir, "test")
    if query_style_from_manifest(smoke.manifest) != query_style_from_manifest(test.manifest):
        raise DatasetError("smoke/test query_style mismatch")
    smoke_targets = {cast(str, row["target_record_key"]) for row in smoke.records}
    test_targets = {cast(str, row["target_record_key"]) for row in test.records}
    if smoke_targets & test_targets:
        raise DatasetError("smoke/test target_record_key overlap")
    smoke_queries = {cast(str, row["query"]).strip() for row in smoke.records}
    test_queries = {cast(str, row["query"]).strip() for row in test.records}
    if smoke_queries & test_queries:
        raise DatasetError("smoke/test query overlap")
    _verify_frozen_dataset_source(dataset_dir, test.manifest)
    return smoke, test


def _path_from_manifest(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise DatasetError("frozen manifest has a missing source path")
    path = Path(value)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _verify_frozen_dataset_source(dataset_dir: Path, manifest: Mapping[str, Any]) -> None:
    """Run the builder's read-only verifier plus raw/prepared hash binding."""
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise DatasetError("frozen manifest source is missing")
    staging_manifest_path = _path_from_manifest(source.get("staging_manifest_path"))
    prepared_cases_path = _path_from_manifest(source.get("prepared_cases_path"))
    if staging_manifest_path.name != "manifest.json" or prepared_cases_path.name != "cases.json":
        raise DatasetError("frozen manifest source paths are malformed")
    prepared_bundle = staging_manifest_path.parent
    try:
        hardening = manifest.get("hardening")
        if isinstance(hardening, Mapping) and hardening.get("variant") == "rag-hard-patient-v1":
            from scripts.build_rag_hard_patient_eval import verify_hard_patient_dataset

            problems = verify_hard_patient_dataset(dataset_dir, prepared_bundle)
        else:
            from scripts.build_rag_silver_eval import verify_frozen_dataset

            problems = verify_frozen_dataset(dataset_dir, prepared_bundle)
    except Exception as exc:
        raise DatasetError("builder frozen-dataset verifier failed") from exc
    if problems:
        raise DatasetError(f"builder frozen-dataset verifier rejected dataset: {problems[0].check}")
    if source.get("staging_manifest_sha256") != sha256_file(staging_manifest_path):
        raise DatasetError("staging manifest hash differs from frozen manifest")
    if source.get("prepared_cases_sha256") != sha256_file(prepared_cases_path):
        raise DatasetError("prepared cases hash differs from frozen manifest")
    raw = source.get("raw_cases")
    if not isinstance(raw, Mapping):
        raise DatasetError("frozen manifest lacks raw case source hash")
    raw_path = _path_from_manifest(raw.get("path"))
    if not raw_path.exists() or raw.get("sha256") != sha256_file(raw_path):
        raise DatasetError("raw source hash differs from frozen manifest")


def prepared_corpus_counts(dataset_dir: Path) -> dict[str, int]:
    """Read prepared totals from the frozen dataset's original staging report."""
    manifest = read_json(dataset_dir / "manifest.json")
    source = manifest.get("source", {})
    if not isinstance(source, Mapping):
        raise DatasetError("frozen manifest source is missing")
    prepared_case_entries = int(source.get("prepared_cases_record_count", 0) or 0)
    staging_manifest_path = _path_from_manifest(source.get("staging_manifest_path"))
    report_path = staging_manifest_path.parent / "prepare-report.json"
    if not report_path.exists():
        raise DatasetError("frozen staging prepare-report.json is missing")
    report = read_json(report_path)
    summary = report.get("summary", {})
    if not isinstance(summary, Mapping):
        raise DatasetError("staging prepare-report summary is missing")
    total = int(summary.get("prepared_records", 0) or 0)
    if total <= 0 or prepared_case_entries <= 0:
        raise DatasetError("frozen prepared corpus counts are invalid")
    return {"prepared_total_entries": total, "prepared_case_entries": prepared_case_entries}


def copy_dataset_manifest(dataset_dir: Path, run_dir: Path, smoke: FrozenSplit, test: FrozenSplit) -> None:
    manifest_path = dataset_dir / "manifest.json"
    source_manifest = read_json(manifest_path)
    copied = dict(source_manifest)
    copied.update(
        {
            "dataset_path": str(dataset_dir).replace("\\", "/"),
            "copied_at": utc_now(),
            "manifest_sha256": sha256_file(manifest_path),
            "test_jsonl_sha256": test.sha256,
            "smoke_jsonl_sha256": smoke.sha256,
            "frozen": True,
        }
    )
    destination = run_dir / "dataset-manifest.json"
    if destination.exists():
        prior = read_json(destination)
        if prior.get("test_jsonl_sha256") != test.sha256 or prior.get("manifest_sha256") != copied["manifest_sha256"]:
            raise EvaluationError("run directory is bound to a different frozen dataset")
    write_json_atomic(destination, copied)


def _frozen_rewrite_cache_body(payload: Mapping[str, Any]) -> JsonObject:
    body = dict(payload)
    body.pop("cache_sha256", None)
    return body


def load_frozen_rewrite_cache(
    path: Path,
    *,
    smoke: FrozenSplit,
    test: FrozenSplit,
    settings: Any,
) -> FrozenRewriteCache:
    """Load a cache only when it exactly matches frozen inputs and Rewrite config."""
    payload = read_json(path)
    if payload.get("schema_version") != FROZEN_REWRITE_CACHE_SCHEMA_VERSION:
        raise EvaluationError("frozen rewrite cache schema_version is invalid")
    observed_sha = payload.get("cache_sha256")
    expected_sha = sha256_bytes(compact_json_bytes(_frozen_rewrite_cache_body(payload)))
    if not isinstance(observed_sha, str) or observed_sha != expected_sha:
        raise EvaluationError("frozen rewrite cache SHA-256 is invalid")
    dataset = payload.get("dataset")
    rewrite = payload.get("rewrite")
    if not isinstance(dataset, Mapping) or not isinstance(rewrite, Mapping):
        raise EvaluationError("frozen rewrite cache lacks dataset or rewrite contract")
    if dataset.get("smoke_sha256") != smoke.sha256 or dataset.get("test_sha256") != test.sha256:
        raise EvaluationError("frozen rewrite cache binds to a different frozen split")
    if rewrite.get("model") != _actual_rewrite_model(settings):
        raise EvaluationError("frozen rewrite cache uses a different Rewrite model")
    try:
        cache_temperature = float(cast(Any, rewrite.get("temperature")))
    except (TypeError, ValueError) as exc:
        raise EvaluationError("frozen rewrite cache has an invalid Rewrite temperature") from exc
    if not math.isclose(cache_temperature, float(settings.rag_query_rewrite_model_temperature), abs_tol=1e-12):
        raise EvaluationError("frozen rewrite cache uses a different Rewrite temperature")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise EvaluationError("frozen rewrite cache entries must be a list")

    expected_rows = {
        str(row["query_id"]): (split.split, str(row["query"])) for split in (smoke, test) for row in split.records
    }
    entries: dict[str, Mapping[str, Any]] = {}
    for item in raw_entries:
        if not isinstance(item, Mapping):
            raise EvaluationError("frozen rewrite cache contains a non-object entry")
        query_id = item.get("query_id")
        if not isinstance(query_id, str) or query_id in entries or query_id not in expected_rows:
            raise EvaluationError("frozen rewrite cache query identities are invalid")
        expected_split, original_query = expected_rows[query_id]
        if item.get("split") != expected_split or item.get("query_sha256") != sha256_text(original_query):
            raise EvaluationError("frozen rewrite cache entry does not bind to frozen input")
        effective_query = item.get("effective_query")
        if not isinstance(effective_query, str) or not effective_query.strip():
            raise EvaluationError("frozen rewrite cache contains an empty rewrite")
        if item.get("effective_query_sha256") != sha256_text(effective_query):
            raise EvaluationError("frozen rewrite cache entry effective hash is invalid")
        if item.get("gateway_status") != "succeeded":
            raise EvaluationError("frozen rewrite cache may not replay a failed or fallback rewrite")
        entries[query_id] = item
    if set(entries) != set(expected_rows):
        raise EvaluationError("frozen rewrite cache does not cover every Smoke/Test query")
    return FrozenRewriteCache(
        sha256=observed_sha,
        model=str(rewrite["model"]),
        temperature=cache_temperature,
        entries=entries,
    )


async def freeze_rewrites(
    dataset_dir: Path,
    output_path: Path,
    collection: str,
    *,
    profile_name: str = _DEFAULT_RETRIEVAL_PROFILE,
) -> int:
    """Make one audited Rewrite snapshot for fair, replayable profile ablations."""
    if output_path.exists():
        raise EvaluationError("refusing to overwrite an existing frozen rewrite cache")
    profile = resolve_retrieval_profile(profile_name)
    if profile.name != _DEFAULT_RETRIEVAL_PROFILE:
        raise EvaluationError("frozen rewrite cache must use the current-v12 Rewrite contract")
    smoke, test = validate_dataset_pair(dataset_dir)
    if query_style_from_manifest(test.manifest) != QUERY_STYLE_NATURAL_LANGUAGE_V1:
        raise EvaluationError("frozen Rewrite replay is only applicable to natural-language evaluation inputs")
    settings = load_contract_settings(collection, profile, query_style=QUERY_STYLE_NATURAL_LANGUAGE_V1)
    from app.core.gateway import ModelGatewayClient
    from app.core.rewrite_gateway import build_rewrite_gateway_settings
    from app.rag.reasoning_retrieval import rewrite_syndrome_query

    raw_gateway = ModelGatewayClient(settings=build_rewrite_gateway_settings(settings) or settings)
    entries: list[JsonObject] = []
    try:
        for split in (smoke, test):
            for row in split.records:
                event = component_template("not_attempted")

                def event_getter(event: JsonObject = event) -> JsonObject:
                    return event

                observed_gateway = ObservedGateway(
                    raw_gateway,
                    component="rewrite",
                    event_getter=event_getter,
                    default_model=_actual_rewrite_model(settings),
                )
                original_query = str(row["query"])
                rewritten = await rewrite_syndrome_query(
                    [SimpleNamespace(fact_key="present_illness", value=original_query)],
                    gateway=observed_gateway,
                    trace_id=f"rag-silver-freeze-{row['query_id']}",
                )
                if event.get("status") != "succeeded" or not rewritten.strip():
                    raise EvaluationError("a frozen Rewrite cache entry did not complete an observed success")
                entries.append(
                    {
                        "split": split.split,
                        "query_id": str(row["query_id"]),
                        "query_sha256": sha256_text(original_query),
                        "effective_query": rewritten,
                        "effective_query_sha256": sha256_text(rewritten),
                        "gateway_status": "succeeded",
                        "gateway_latency_ms": event.get("latency_ms"),
                    }
                )
    finally:
        closer = getattr(raw_gateway, "aclose", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                await closer()
    entries.sort(key=lambda item: (str(item["split"]), str(item["query_id"])))
    payload: JsonObject = {
        "schema_version": FROZEN_REWRITE_CACHE_SCHEMA_VERSION,
        "created_at": utc_now(),
        "dataset": {"smoke_sha256": smoke.sha256, "test_sha256": test.sha256},
        "rewrite": {
            "model": _actual_rewrite_model(settings),
            "temperature": float(settings.rag_query_rewrite_model_temperature),
        },
        "entries": entries,
    }
    payload["cache_sha256"] = sha256_bytes(compact_json_bytes(payload))
    write_json_atomic(output_path, payload)
    print(f"wrote frozen Rewrite cache: {output_path}")
    return 0


def set_contract_environment(
    collection: str,
    profile: RetrievalProfile | None = None,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> None:
    """Set the documented full-arm configuration before Settings is first read."""
    if not collection or not re.fullmatch(r"[A-Za-z0-9_\-]+", collection):
        raise EvaluationError("collection must be a non-empty safe collection name")
    query_style = _require_query_style(query_style)
    profile = profile or resolve_retrieval_profile(_DEFAULT_RETRIEVAL_PROFILE)
    values = {
        "MILVUS_COLLECTION": collection,
        "RAG_QUERY_REWRITE_ENABLED": str(query_style == QUERY_STYLE_NATURAL_LANGUAGE_V1).lower(),
        "RAG_QUERY_REWRITE_MODEL_TEMPERATURE": "0.1",
        "RAG_TOP_K_VECTOR": str(profile.vector_top_k),
        "RAG_TOP_K_FULLTEXT": str(profile.fulltext_top_k),
        "RAG_FULLTEXT_LEXICAL_ENABLED": str(profile.fulltext_lexical_enabled).lower(),
        "RAG_FULLTEXT_LEXICAL_MAX_TERMS": "12",
        "RAG_DUAL_QUERY_ENABLED": str(profile.dual_query_enabled).lower(),
        "RAG_DUAL_QUERY_RRF_K": "60",
        "RAG_RERANKER_ENABLED": "true",
        "RAG_RERANKER_PROVIDER": "cross_encoder",
        "RAG_RERANKER_TOP_K": str(profile.reranker_top_k),
        "RAG_RERANKER_FULLTEXT_QUOTA": str(profile.fulltext_quota),
        "RAG_RERANKER_MAX_CHUNKS_PER_SOURCE": str(profile.reranker_max_chunks_per_source),
        "RAG_RERANKER_FINAL_TOP_K": "8",
        "RAG_TOP_N_FINAL": "8",
        # The production cache key is only query text, with no model or
        # collection provenance.  Disable it for an auditable evaluation.
        "EMBEDDING_CACHE_TTL_SECONDS": "0",
    }
    os.environ.update(values)


def load_contract_settings(
    collection: str,
    profile: RetrievalProfile | None = None,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> Any:
    """Load a fresh global Settings singleton after applying contract overrides."""
    query_style = _require_query_style(query_style)
    profile = profile or resolve_retrieval_profile(_DEFAULT_RETRIEVAL_PROFILE)
    set_contract_environment(collection, profile, query_style=query_style)
    from app.core.config import get_settings
    from app.rag.retriever import reset_shared_rag_retriever

    get_settings.cache_clear()
    reset_shared_rag_retriever()
    settings = get_settings()
    expected: dict[str, Any] = {
        "milvus_collection": collection,
        "rag_query_rewrite_enabled": query_style == QUERY_STYLE_NATURAL_LANGUAGE_V1,
        "rag_query_rewrite_model_temperature": 0.1,
        "rag_top_k_vector": profile.vector_top_k,
        "rag_top_k_fulltext": profile.fulltext_top_k,
        "rag_fulltext_lexical_enabled": profile.fulltext_lexical_enabled,
        "rag_fulltext_lexical_max_terms": 12,
        "rag_dual_query_enabled": profile.dual_query_enabled,
        "rag_dual_query_rrf_k": 60,
        "rag_reranker_enabled": True,
        "rag_reranker_provider": "cross_encoder",
        "rag_reranker_top_k": profile.reranker_top_k,
        "rag_reranker_fulltext_quota": profile.fulltext_quota,
        "rag_reranker_max_chunks_per_source": profile.reranker_max_chunks_per_source,
        "rag_reranker_final_top_k": 8,
        "embedding_cache_ttl_seconds": 0,
    }
    for name, value in expected.items():
        observed = getattr(settings, name)
        if observed != value:
            raise EvaluationError(f"effective contract setting mismatch: {name}")
    return settings


def _timeout_or_fallback(settings: Any, specific: str, fallback: str) -> int | float:
    value = getattr(settings, specific, 0)
    return value if value else getattr(settings, fallback)


def _actual_rewrite_model(settings: Any) -> str:
    return str(getattr(settings, "rag_query_rewrite_model", "") or settings.chat_model)


def _actual_reranker_model(settings: Any) -> str:
    return str(getattr(settings, "rag_reranker_model", "") or "jina-reranker-m0")


def redacted_config(
    settings: Any,
    profile: RetrievalProfile | None = None,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
    frozen_rewrite_cache: FrozenRewriteCache | None = None,
) -> JsonObject:
    """Create the whitelist-only config snapshot used by results and resume keys."""
    query_style = _require_query_style(query_style)
    profile = profile or resolve_retrieval_profile(_DEFAULT_RETRIEVAL_PROFILE)
    payload: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "milvus": {
            "host": sanitize_host(f"http://{settings.milvus_host}:{settings.milvus_port}"),
            "port": int(settings.milvus_port),
            "collection": str(settings.milvus_collection),
            "embedding_dim": int(settings.embedding_dim),
        },
        "models": {
            "embedding": str(settings.embedding_model),
            "rewrite": _actual_rewrite_model(settings),
            "reranker": _actual_reranker_model(settings),
        },
        "retrieval": {
            "profile": profile.name,
            "source_types": SOURCE_TYPES,
            "final_top_k": FIXED_TOP_K,
            "vector_top_k": int(settings.rag_top_k_vector),
            "fulltext_top_k": int(settings.rag_top_k_fulltext),
            "fulltext_lexical_enabled": bool(getattr(settings, "rag_fulltext_lexical_enabled", True)),
            "fulltext_lexical_max_terms": int(getattr(settings, "rag_fulltext_lexical_max_terms", 12)),
            "dual_query_enabled": bool(getattr(settings, "rag_dual_query_enabled", False)),
            "dual_query_rrf_k": int(getattr(settings, "rag_dual_query_rrf_k", 60)),
            "candidate_trace": True,
            "vector_weight": 0.65,
            "fulltext_weight": 0.25,
            "source_priority_weight": 0.10,
            "reranker_provider": str(settings.rag_reranker_provider),
            "reranker_enabled": bool(getattr(settings, "rag_reranker_enabled", False)),
            "reranker_top_k": int(settings.rag_reranker_top_k),
            "reranker_fulltext_quota": int(getattr(settings, "rag_reranker_fulltext_quota", 0)),
            "reranker_max_chunks_per_source": int(getattr(settings, "rag_reranker_max_chunks_per_source", 0)),
            "reranker_final_top_k": int(settings.rag_reranker_final_top_k),
            "embedding_cache_ttl_seconds": int(settings.embedding_cache_ttl_seconds),
        },
        "rewrite": {
            "enabled": bool(settings.rag_query_rewrite_enabled),
            "temperature": float(settings.rag_query_rewrite_model_temperature),
            "max_tokens": int(settings.rag_query_rewrite_model_max_tokens),
        },
        "timeouts_seconds": {
            "embedding": _timeout_or_fallback(
                settings, "embedding_gateway_timeout_seconds", "model_gateway_timeout_seconds"
            ),
            "rewrite": _timeout_or_fallback(
                settings, "rag_query_rewrite_gateway_timeout_seconds", "rag_query_rewrite_timeout_seconds"
            ),
            "reranker": _timeout_or_fallback(
                settings, "reranker_gateway_timeout_seconds", "rag_reranker_timeout_seconds"
            ),
        },
        "secrets_redacted": True,
    }
    if frozen_rewrite_cache is not None:
        payload["rewrite"] = {
            **cast(JsonObject, payload["rewrite"]),
            "execution_mode": "frozen_replay",
            "frozen_cache_sha256": frozen_rewrite_cache.sha256,
            "frozen_cache_entry_count": len(frozen_rewrite_cache.entries),
        }
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        # This is intentionally an opt-in addition: legacy v1 config payloads
        # (and therefore their hashes) remain byte-for-byte unchanged.
        payload["query_style"] = query_style
        models = cast(JsonObject, payload["models"])
        models.pop("rewrite", None)
        payload["rewrite"] = {
            "enabled": False,
            "applicable": False,
            "gateway_call": "not_applicable",
        }
    config_sha = sha256_bytes(compact_json_bytes(payload))
    payload["captured_at"] = utc_now()
    payload["config_sha256"] = config_sha
    return payload


def write_or_validate_config(
    run_dir: Path,
    settings: Any,
    profile: RetrievalProfile | None = None,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
    frozen_rewrite_cache: FrozenRewriteCache | None = None,
) -> JsonObject:
    snapshot = redacted_config(
        settings,
        profile,
        query_style=query_style,
        frozen_rewrite_cache=frozen_rewrite_cache,
    )
    path = run_dir / "config.redacted.json"
    if path.exists():
        prior = read_json(path)
        if prior.get("config_sha256") != snapshot["config_sha256"]:
            raise EvaluationError("effective configuration differs from the run's frozen config")
        return prior
    write_json_atomic(path, snapshot)
    return snapshot


def write_or_update_environment(
    run_dir: Path,
    *,
    corpus: Mapping[str, int] | None = None,
    commands: Sequence[Mapping[str, Any]] | None = None,
) -> JsonObject:
    """Capture reproducibility data, never connection strings or environment variables."""
    path = run_dir / "environment.json"
    prior = read_json(path) if path.exists() else {}
    # Identity is frozen at preflight.  Later phases must never silently
    # relabel a result with a different commit or evaluator implementation.
    if prior and corpus is None and commands is None:
        return prior
    commit, branch, dirty = _git_environment()
    runtime_source_sha256 = {name: sha256_file(path) for name, path in _RUNTIME_SOURCE_FILES.items()}
    payload: JsonObject = {
        "captured_at": prior.get("captured_at", utc_now()),
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "project_package_version": "0.1.0",
        "evaluator_sha256": sha256_file(Path(__file__).resolve()),
        "builder_sha256": sha256_file(_PROJECT_ROOT / "scripts" / "build_rag_silver_eval.py"),
        "runtime_source_sha256": runtime_source_sha256,
        "commands": list(commands) if commands is not None else prior.get("commands", []),
        "corpus": dict(prior.get("corpus", {})),
    }
    if corpus is not None:
        payload["corpus"].update({key: int(value) for key, value in corpus.items()})
    write_json_atomic(path, payload)
    return payload


def ensure_frozen_code_identity(run_dir: Path) -> None:
    """Reject a run if its evaluation implementation changed after preflight."""
    environment_path = run_dir / "environment.json"
    if not environment_path.exists():
        raise EvaluationError("environment identity has not been captured")
    environment = read_json(environment_path)
    expected_evaluator = environment.get("evaluator_sha256")
    expected_builder = environment.get("builder_sha256")
    if expected_evaluator != sha256_file(Path(__file__).resolve()):
        raise EvaluationError("evaluator source changed after preflight")
    if expected_builder != sha256_file(_PROJECT_ROOT / "scripts" / "build_rag_silver_eval.py"):
        raise EvaluationError("builder source changed after preflight")
    expected_runtime = environment.get("runtime_source_sha256")
    if expected_runtime is None:
        # Legacy v1 artifacts predate runtime dependency identity capture.
        # Their evaluator/builder checks above remain valid and no new report
        # is ever written by a tuning run without the stronger map.
        return
    current_runtime = {name: sha256_file(path) for name, path in _RUNTIME_SOURCE_FILES.items()}
    if expected_runtime != current_runtime:
        raise EvaluationError("runtime retrieval source changed after preflight")


# ---------------------------------------------------------------------------
# Read-only corpus resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetMapping:
    """The only accepted relevance identity for a frozen query."""

    record_key: str
    source_id: str
    chunk_ids: tuple[str, ...]
    vector_ids: tuple[str, ...]


def _result_rows(result: Any) -> list[Any]:
    """Normalise SQLAlchemy/fake result objects for the resolver test seam."""
    if hasattr(result, "all"):
        return list(result.all())
    return list(result)


def _row_pair(row: Any) -> tuple[Any, Any]:
    if isinstance(row, tuple):
        return row[0], row[1]
    try:
        return row[0], row[1]
    except (IndexError, KeyError, TypeError):
        return row.id, row.record_key


def validate_target_rows(
    record_keys: Iterable[str],
    rows: Iterable[tuple[Any, Any]],
) -> dict[str, str]:
    """Validate one and only one active case row per requested record key.

    Kept pure so zero/multiple/deleted/case filtering behaviours can be tested
    without a live PostgreSQL instance.  The database query itself applies the
    deleted and entry-type predicates before it reaches this function.
    """
    requested = list(record_keys)
    if len(set(requested)) != len(requested):
        raise TargetResolutionError("frozen split contains duplicate record_key")
    grouped: dict[str, list[str]] = defaultdict(list)
    for source_id, record_key in rows:
        grouped[str(record_key)].append(str(source_id))
    mapping: dict[str, str] = {}
    for key in requested:
        values = grouped.get(key, [])
        if len(values) != 1:
            state = "missing" if not values else "non_unique"
            raise TargetResolutionError(f"target {state} for frozen record_key")
        mapping[key] = values[0]
    return mapping


class TargetResolver:
    """Resolve frozen ``metadata.record_key`` values through active case rows.

    It is intentionally read-only and validates chunk/vector readiness as part
    of resolution.  No UUID is ever persisted into the frozen dataset.
    """

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory

    def _get_session_factory(self) -> Any:
        if self._session_factory is None:
            from app.db.session import get_session_factory

            self._session_factory = get_session_factory()
        return self._session_factory

    async def resolve(self, record_keys: Sequence[str]) -> dict[str, TargetMapping]:
        if not record_keys:
            return {}
        if any(_RECORD_KEY_RE.fullmatch(key) is None for key in record_keys):
            raise TargetResolutionError("target_record_key must be a SHA-256")

        from sqlalchemy import select

        from app.models.knowledge import KnowledgeChunk, TheoryCase

        key_expr = TheoryCase.extra_meta["record_key"].as_string()
        case_stmt = (
            select(TheoryCase.id, key_expr.label("record_key"))
            .where(
                TheoryCase.entry_type == "case",
                TheoryCase.deleted_at.is_(None),
                key_expr.in_(list(record_keys)),
            )
            .order_by(TheoryCase.id)
        )
        factory = self._get_session_factory()
        async with factory() as session:
            case_rows = _result_rows(await session.execute(case_stmt))
            pairs = [_row_pair(row) for row in case_rows]
            source_ids = validate_target_rows(record_keys, pairs)
            try:
                source_id_values = [uuid.UUID(source_id) for source_id in source_ids.values()]
            except ValueError as exc:
                raise TargetResolutionError("TheoryCase.id is not a UUID") from exc
            chunk_stmt = (
                select(KnowledgeChunk.id, KnowledgeChunk.source_id, KnowledgeChunk.vector_id)
                .where(
                    KnowledgeChunk.source_type == "case",
                    KnowledgeChunk.source_id.in_(source_id_values),
                    KnowledgeChunk.deleted_at.is_(None),
                    KnowledgeChunk.embedding_status == "done",
                    KnowledgeChunk.vector_id.is_not(None),
                )
                .distinct()
            )
            chunk_rows = _result_rows(await session.execute(chunk_stmt))

        ready_by_source: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in chunk_rows:
            try:
                chunk_id, source_id, vector_id = row[0], row[1], row[2]
            except (IndexError, KeyError, TypeError):
                chunk_id, source_id, vector_id = row.id, row.source_id, row.vector_id
            ready_by_source[str(source_id)].append((str(chunk_id), str(vector_id)))
        missing_chunks = [str(source_id) for source_id in source_id_values if str(source_id) not in ready_by_source]
        if missing_chunks:
            raise TargetResolutionError("one or more resolved case targets lack an active vector-ready chunk")
        return {
            key: TargetMapping(
                record_key=key,
                source_id=source_ids[key],
                chunk_ids=tuple(chunk_id for chunk_id, _ in ready_by_source[source_ids[key]]),
                vector_ids=tuple(vector_id for _, vector_id in ready_by_source[source_ids[key]]),
            )
            for key in record_keys
        }


# ---------------------------------------------------------------------------
# Observability adapters.  They delegate to production code rather than
# rebuilding any of its retrieval or ranking behaviour.
# ---------------------------------------------------------------------------


def component_template(status: str = "not_applicable") -> JsonObject:
    return {
        "status": status,
        "attempted": False,
        "model": None,
        "embedding_model": None,
        "embedding_source": None,
        "latency_ms": None,
        "candidate_count": None,
        "error_type": None,
    }


def full_component_templates() -> JsonObject:
    return {
        "rewrite": component_template("not_attempted"),
        "vector": component_template("not_attempted"),
        "fulltext": component_template("not_attempted"),
        "reranker": component_template("not_attempted"),
    }


def baseline_component_templates() -> JsonObject:
    return {
        "rewrite": component_template("not_applicable"),
        "vector": component_template("not_attempted"),
        "fulltext": component_template("not_applicable"),
        "reranker": component_template("not_applicable"),
    }


def _event_start(event: JsonObject, *, model: str | None = None, embedding_model: str | None = None) -> float:
    event["attempted"] = True
    event["status"] = "running"
    if model is not None:
        event["model"] = model
    if embedding_model is not None:
        event["embedding_model"] = embedding_model
    return time.perf_counter()


def _event_success(event: JsonObject, started: float, candidate_count: int | None = None) -> None:
    event["status"] = "succeeded"
    event["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    if candidate_count is not None:
        event["candidate_count"] = candidate_count


def _event_failure(event: JsonObject, started: float, exc: BaseException, *, status: str = "fallback") -> None:
    event["status"] = status
    event["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    event["error_type"] = safe_exception_type(exc)


def assert_vector_search_signature(retriever: Any) -> None:
    """Fail closed if the private baseline API changes shape.

    A silent switch to another public retrieval path would invalidate the A/B
    comparison, so the intentionally private call is guarded explicitly.
    """
    method = getattr(retriever, "_vector_search", None)
    if method is None or not callable(method):
        raise EvaluationError("RAGRetriever._vector_search is unavailable")
    signature = inspect.signature(method)
    parameters = list(signature.parameters.values())
    names = [parameter.name for parameter in parameters]
    if names != ["query", "sources", "top_k", "filters"]:
        raise EvaluationError("RAGRetriever._vector_search signature changed")
    if parameters[0].kind not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
        raise EvaluationError("RAGRetriever._vector_search query parameter changed")
    if parameters[1].kind not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
        raise EvaluationError("RAGRetriever._vector_search sources parameter changed")
    if any(parameter.kind is not inspect.Parameter.KEYWORD_ONLY for parameter in parameters[2:]):
        raise EvaluationError("RAGRetriever._vector_search keyword-only contract changed")


class PureVectorAdapter:
    """The sole allowed baseline invocation path."""

    def __init__(self, retriever: Any, *, embedding_model: str) -> None:
        self._retriever = retriever
        self._embedding_model = embedding_model
        assert_vector_search_signature(retriever)

    async def search(self, query: str, *, top_k: int = FIXED_TOP_K, event: JsonObject | None = None) -> list[Any]:
        if top_k != FIXED_TOP_K:
            raise EvaluationError("baseline top_k must be 8")
        if event is None:
            event = component_template("not_attempted")
        started = _event_start(event, embedding_model=self._embedding_model)
        try:
            # Do not add fulltext, merge, query rewrite, or reranking here.
            hits = await self._retriever._vector_search(query, ["case"], top_k=FIXED_TOP_K, filters=None)
        except Exception as exc:
            _event_failure(event, started, exc, status="failed")
            raise ArmTechnicalFailure("baseline vector search failed") from exc
        _event_success(event, started, len(hits))
        event["embedding_source"] = "gateway"
        return list(hits)


class ObservedGateway:
    """Transparent gateway proxy used to observe actual rewrite/rerank calls."""

    def __init__(
        self, delegate: Any, *, component: str, event_getter: Callable[[], JsonObject], default_model: str
    ) -> None:
        self._delegate = delegate
        self._component = component
        self._event_getter = event_getter
        self._default_model = default_model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        """Observe a rewrite chat call and preserve its exact behaviour."""
        event = self._event_getter()
        started = _event_start(event, model=str(kwargs.get("model") or self._default_model))
        try:
            response = await self._delegate.chat(*args, **kwargs)
        except Exception as exc:
            _event_failure(event, started, exc)
            raise
        if not isinstance(response, str) or not response.strip():
            empty = ValueError("empty gateway output")
            _event_failure(event, started, empty)
        else:
            _event_success(event, started)
        return response

    async def _request_with_retry(self, *args: Any, **kwargs: Any) -> Any:
        """Observe the real Cross-Encoder HTTP request used by production code."""
        event = self._event_getter()
        payload = kwargs.get("payload") or {}
        model = payload.get("model") if isinstance(payload, dict) else None
        started = _event_start(event, model=str(model or self._default_model))
        try:
            response = await self._delegate._request_with_retry(*args, **kwargs)
        except Exception as exc:
            _event_failure(event, started, exc)
            raise
        _event_success(event, started)
        return response


class ObservedRAGRetrieverMixin:
    """Mixin layered on the production retriever; it only adds observations."""

    _component_records: JsonObject
    _embedding_model_for_eval: str
    _reranker_model_for_eval: str
    _observed_reranker_gateway: ObservedGateway | None
    _vector_candidate_hits: list[Any]
    _fulltext_candidate_hits: list[Any]
    _vector_candidate_batches: list[list[Any]]
    _fulltext_candidate_batches: list[list[Any]]
    _vector_search_call_count: int
    _vector_search_failure_count: int
    _fulltext_search_call_count: int

    def set_component_records(self, records: JsonObject) -> None:
        self._component_records = records
        self._vector_candidate_hits = []
        self._fulltext_candidate_hits = []
        self._vector_candidate_batches = []
        self._fulltext_candidate_batches = []
        self._vector_search_call_count = 0
        self._vector_search_failure_count = 0
        self._fulltext_search_call_count = 0

    def _evaluation_event(self, component: str) -> JsonObject:
        return cast(JsonObject, self._component_records[component])

    async def _vector_search(
        self,
        query: str,
        sources: list[str],
        *,
        top_k: int = 12,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        event = self._evaluation_event("vector")
        self._vector_search_call_count += 1
        prior_latency = float(event.get("latency_ms") or 0.0)
        prior_count = int(event.get("candidate_count") or 0)
        if not event.get("attempted"):
            started = _event_start(event, embedding_model=self._embedding_model_for_eval)
        else:
            event["status"] = "running"
            started = time.perf_counter()
        try:
            hits = await super()._vector_search(query, sources, top_k=top_k, filters=filters)  # type: ignore[misc]
        except Exception as exc:
            self._vector_search_failure_count += 1
            event["status"] = "fallback"
            event["latency_ms"] = round(prior_latency + (time.perf_counter() - started) * 1000.0, 3)
            event["error_type"] = safe_exception_type(exc)
            event["embedding_source"] = "gateway"
            raise
        event["status"] = "succeeded"
        if self._vector_search_failure_count:
            # Both query views ran.  A later success must not erase an earlier
            # vector fallback from component coverage or degradation evidence.
            event["status"] = "fallback"
        event["latency_ms"] = round(prior_latency + (time.perf_counter() - started) * 1000.0, 3)
        event["candidate_count"] = prior_count + len(hits)
        event["embedding_source"] = "gateway"
        self._vector_candidate_hits = list(hits)
        self._vector_candidate_batches.append(list(hits))
        return self._vector_candidate_hits

    async def _fulltext_search(
        self,
        query: str,
        sources: list[str],
        *,
        top_k: int = 12,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        event = self._evaluation_event("fulltext")
        self._fulltext_search_call_count += 1
        prior_latency = float(event.get("latency_ms") or 0.0)
        prior_count = int(event.get("candidate_count") or 0)
        if not event.get("attempted"):
            started = _event_start(event)
        else:
            event["status"] = "running"
            started = time.perf_counter()
        try:
            hits = await super()._fulltext_search(query, sources, top_k=top_k, filters=filters)  # type: ignore[misc]
        except Exception as exc:
            event["status"] = "failed"
            event["latency_ms"] = round(prior_latency + (time.perf_counter() - started) * 1000.0, 3)
            event["error_type"] = safe_exception_type(exc)
            raise
        event["status"] = "succeeded"
        event["latency_ms"] = round(prior_latency + (time.perf_counter() - started) * 1000.0, 3)
        event["candidate_count"] = prior_count + len(hits)
        self._fulltext_candidate_hits = list(hits)
        self._fulltext_candidate_batches.append(list(hits))
        return self._fulltext_candidate_hits

    def candidate_trace(self, target_source_id: str) -> JsonObject:
        """Return numeric candidate-stage evidence without retaining source text or IDs."""
        from app.rag.retriever import merge_deduplicate, reciprocal_rank_fuse_hits, select_reranker_candidates
        from app.rag.schemas import FulltextHit, VectorHit

        def rank_for(hits: Sequence[Any]) -> int | None:
            for rank, hit in enumerate(hits, start=1):
                if str(getattr(hit, "source_id", "")) == target_source_id:
                    return rank
            return None

        settings = cast(Any, self)._settings
        query_view_count = max(self._vector_search_call_count, self._fulltext_search_call_count)
        if bool(getattr(settings, "rag_dual_query_enabled", False)) and query_view_count > 1:
            vector_hits = [
                hit
                for hit in reciprocal_rank_fuse_hits(
                    self._vector_candidate_batches,
                    score_field="vector_score",
                    rrf_k=int(getattr(settings, "rag_dual_query_rrf_k", 60)),
                )
                if isinstance(hit, VectorHit)
            ]
            fulltext_hits = [
                hit
                for hit in reciprocal_rank_fuse_hits(
                    self._fulltext_candidate_batches,
                    score_field="fulltext_score",
                    rrf_k=int(getattr(settings, "rag_dual_query_rrf_k", 60)),
                )
                if isinstance(hit, FulltextHit)
            ]
        else:
            vector_hits = self._vector_candidate_hits
            fulltext_hits = self._fulltext_candidate_hits
        merged = merge_deduplicate(vector_hits, fulltext_hits, primary_sources={"case"})
        reranker_candidates = select_reranker_candidates(
            merged,
            fulltext_quota=int(getattr(settings, "rag_reranker_fulltext_quota", 0)),
            limit=int(getattr(settings, "rag_reranker_top_k", len(merged))),
            max_chunks_per_source=int(getattr(settings, "rag_reranker_max_chunks_per_source", 0)),
        )
        reranker_event = self._evaluation_event("reranker")
        return {
            # For dual-view retrieval, candidate-layer diagnostics must reflect
            # the fused pool, rather than the second leg that happened to run
            # last.  The single-view values stay byte-for-byte equivalent.
            "vector_candidate_count": len(vector_hits),
            "vector_target_rank": rank_for(vector_hits),
            "fulltext_candidate_count": len(fulltext_hits),
            "fulltext_target_rank": rank_for(fulltext_hits),
            "merged_candidate_count": len(merged),
            "merged_target_rank": rank_for(merged),
            "reranker_candidate_count": len(reranker_candidates),
            "reranker_candidate_target_rank": rank_for(reranker_candidates),
            "reranker_candidate_unique_source_count": len(
                {(hit.source_type, str(hit.source_id)) for hit in reranker_candidates}
            ),
            "query_view_count": query_view_count,
            "dual_rrf_applied": bool(getattr(settings, "rag_dual_query_enabled", False)) and query_view_count == 2,
            "reranker_attempted": bool(reranker_event.get("attempted")),
        }

    def _get_reranker_gateway(self) -> Any:
        if self._observed_reranker_gateway is None:
            raw = super()._get_reranker_gateway()  # type: ignore[misc]
            self._observed_reranker_gateway = ObservedGateway(
                raw,
                component="reranker",
                event_getter=lambda: self._evaluation_event("reranker"),
                default_model=self._reranker_model_for_eval,
            )
        return self._observed_reranker_gateway

    def finalise_reranker_observation(self, evidences: Sequence[Any]) -> None:
        event = self._evaluation_event("reranker")
        if not event.get("attempted"):
            event["status"] = "not_applied_insufficient_candidates"
            return
        if event.get("status") != "succeeded":
            return
        valid = bool(evidences) and all(
            isinstance(getattr(evidence, "metadata", None), dict)
            and getattr(evidence, "metadata", {}).get("reranker_provider") == "cross_encoder"
            and bool(getattr(evidence, "metadata", {}).get("reranker_model"))
            and "reranker_score" in getattr(evidence, "metadata", {})
            for evidence in evidences
        )
        if not valid:
            event["status"] = "fallback"
            event["error_type"] = "RerankerMetadataMissing"


def build_observed_retriever(settings: Any) -> Any:
    """Construct a production RAGRetriever subclass without copying its algorithm."""
    from app.rag.retriever import RAGRetriever

    class ObservedRAGRetriever(ObservedRAGRetrieverMixin, RAGRetriever):
        def __init__(self) -> None:
            super().__init__(settings=settings)
            self._component_records = full_component_templates()
            self._embedding_model_for_eval = str(settings.embedding_model)
            self._reranker_model_for_eval = _actual_reranker_model(settings)
            self._observed_reranker_gateway = None
            self._vector_candidate_hits = []
            self._fulltext_candidate_hits = []
            self._vector_candidate_batches = []
            self._fulltext_candidate_batches = []
            self._vector_search_call_count = 0
            self._vector_search_failure_count = 0
            self._fulltext_search_call_count = 0

    return ObservedRAGRetriever()


# ---------------------------------------------------------------------------
# Per-query execution and JSONL checkpointing
# ---------------------------------------------------------------------------


def _safe_metadata(metadata: Any) -> JsonObject:
    """Persist score provenance only, never document bodies or gateway output."""
    if not isinstance(metadata, Mapping):
        return {}
    allowed = {
        "vector_score",
        "fulltext_score",
        "source_priority",
        "reranker_score",
        "reranker_model",
        "reranker_provider",
        "content_hash",
    }
    result: JsonObject = {}
    for key in allowed:
        value = metadata.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


def project_vector_hits(hits: Sequence[Any]) -> list[JsonObject]:
    results: list[JsonObject] = []
    for rank, hit in enumerate(hits[:FIXED_TOP_K], start=1):
        metadata = _safe_metadata({"content_hash": getattr(hit, "content_hash", "")})
        results.append(
            {
                "rank": rank,
                "source_type": str(getattr(hit, "source_type", "")),
                "source_id": str(getattr(hit, "source_id", "")),
                "chunk_id": str(getattr(hit, "chunk_id", "")) or None,
                "title": str(getattr(hit, "title", "")),
                "score": float(getattr(hit, "vector_score", 0.0)),
                "score_type": "vector_score",
                "metadata": metadata,
            }
        )
    return results


def project_evidences(evidences: Sequence[Any]) -> list[JsonObject]:
    results: list[JsonObject] = []
    for rank, evidence in enumerate(evidences[:FIXED_TOP_K], start=1):
        metadata = _safe_metadata(getattr(evidence, "metadata", {}))
        score_type = "reranker_score" if metadata.get("reranker_provider") == "cross_encoder" else "hybrid_score"
        results.append(
            {
                "rank": rank,
                "source_type": str(getattr(evidence, "source_type", "")),
                "source_id": str(getattr(evidence, "source_id", "")),
                "chunk_id": str(getattr(evidence, "chunk_id", "")) or None,
                "title": str(getattr(evidence, "title", "")),
                "score": float(getattr(evidence, "score", 0.0)),
                "score_type": score_type,
                "metadata": metadata,
            }
        )
    return results


def first_relevant_rank(
    results: Sequence[Mapping[str, Any]], target_source_id: str, *, top_k: int = FIXED_TOP_K
) -> int | None:
    """Return the first relevant source rank; duplicate target chunks score once."""
    for expected_rank, result in enumerate(results[:top_k], start=1):
        if int(result.get("rank", -1)) != expected_rank:
            raise EvaluationError("result ranks must be contiguous from one")
        if result.get("source_type") == "case" and str(result.get("source_id")) == str(target_source_id):
            return expected_rank
    return None


def score_results(results: Sequence[Mapping[str, Any]], target_source_id: str) -> tuple[int | None, int, float]:
    rank = first_relevant_rank(results, target_source_id)
    if rank is None:
        return None, 0, 0.0
    return rank, 1, 1.0 / rank


def _default_degradations(components: Mapping[str, Any]) -> list[str]:
    degradations: list[str] = []
    vector = components.get("vector", {})
    fulltext = components.get("fulltext", {})
    reranker = components.get("reranker", {})
    rewrite = components.get("rewrite", {})
    if vector.get("status") == "fallback" and fulltext.get("status") == "succeeded":
        degradations.append("vector_fallback_to_fulltext")
    if rewrite.get("status") == "fallback":
        degradations.append("rewrite_fallback")
    if reranker.get("status") == "fallback":
        degradations.append("reranker_fallback")
    if reranker.get("status") == "not_applied_insufficient_candidates":
        degradations.append("reranker_not_applied_insufficient_candidates")
    return degradations


def _record_hash(record: Mapping[str, Any]) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    return sha256_bytes(compact_json_bytes(body))


def _require_component_shape(component: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(component, Mapping):
        raise ResumeIntegrityError(f"missing component observation: {name}")
    if not isinstance(component.get("status"), str) or not isinstance(component.get("attempted"), bool):
        raise ResumeIntegrityError(f"invalid component observation: {name}")
    return component


def validate_component_contract(
    record: Mapping[str, Any],
    *,
    query_style: str | None = None,
) -> None:
    """Validate observable component semantics against the scoreable arm rules."""
    observed_query_style = query_style_from_result(record)
    if query_style is not None and observed_query_style != _require_query_style(query_style):
        raise ResumeIntegrityError("result query_style mismatch")
    components = record.get("components")
    if not isinstance(components, Mapping):
        raise ResumeIntegrityError("result components must be an object")
    rewrite = _require_component_shape(components.get("rewrite"), "rewrite")
    vector = _require_component_shape(components.get("vector"), "vector")
    fulltext = _require_component_shape(components.get("fulltext"), "fulltext")
    reranker = _require_component_shape(components.get("reranker"), "reranker")
    arm = record.get("arm")
    if arm == "baseline":
        if vector.get("status") != "succeeded" or not vector.get("attempted"):
            raise ResumeIntegrityError("baseline vector must be an observed success")
        if any(component.get("status") != "not_applicable" for component in (rewrite, fulltext, reranker)):
            raise ResumeIntegrityError("baseline must not use rewrite, fulltext, or reranker")
    elif arm == "full":
        if observed_query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            if rewrite.get("status") != "not_applicable" or rewrite.get("attempted"):
                raise ResumeIntegrityError("structured full must mark rewrite disabled and not applicable")
        elif rewrite.get("status") not in {"succeeded", "fallback"} or not rewrite.get("attempted"):
            raise ResumeIntegrityError("full rewrite observation is not scoreable")
        if vector.get("status") not in {"succeeded", "fallback"} or not vector.get("attempted"):
            raise ResumeIntegrityError("full vector observation is not scoreable")
        if fulltext.get("status") != "succeeded" or not fulltext.get("attempted"):
            raise ResumeIntegrityError("full PostgreSQL observation is not an observed success")
        rerank_status = reranker.get("status")
        if rerank_status == "succeeded":
            if not reranker.get("attempted"):
                raise ResumeIntegrityError("successful reranker was not attempted")
            results = record.get("results", [])
            if (
                not isinstance(results, list)
                or not results
                or not all(
                    isinstance(result.get("metadata"), Mapping)
                    and result["metadata"].get("reranker_provider") == "cross_encoder"
                    and bool(result["metadata"].get("reranker_model"))
                    and "reranker_score" in result["metadata"]
                    for result in results
                    if isinstance(result, Mapping)
                )
            ):
                raise ResumeIntegrityError("successful reranker lacks cross-encoder evidence metadata")
        elif rerank_status == "fallback":
            if not reranker.get("attempted"):
                raise ResumeIntegrityError("reranker fallback was not attempted")
        elif rerank_status == "not_applied_insufficient_candidates":
            if reranker.get("attempted"):
                raise ResumeIntegrityError("unapplied reranker unexpectedly attempted")
        else:
            raise ResumeIntegrityError("full reranker observation is not scoreable")
    else:
        raise ResumeIntegrityError("unknown result arm")
    expected_degradations = _default_degradations(components)
    if record.get("degradations") != expected_degradations:
        raise ResumeIntegrityError("result degradations do not match component observations")


def validate_candidate_trace(record: Mapping[str, Any]) -> None:
    """Validate optional numeric-only candidate diagnostics when a run records them."""
    trace = record.get("candidate_trace")
    if trace is None:
        return
    if not isinstance(trace, Mapping):
        raise ResumeIntegrityError("candidate_trace must be an object")
    count_names = (
        "vector_candidate_count",
        "fulltext_candidate_count",
        "merged_candidate_count",
        "reranker_candidate_count",
    )
    rank_names = (
        "vector_target_rank",
        "fulltext_target_rank",
        "merged_target_rank",
        "reranker_candidate_target_rank",
    )
    if not isinstance(trace.get("reranker_attempted"), bool):
        raise ResumeIntegrityError("candidate_trace reranker_attempted must be boolean")
    for name in count_names:
        value = trace.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ResumeIntegrityError(f"candidate_trace invalid count: {name}")
    unique_source_count = trace.get("reranker_candidate_unique_source_count")
    if unique_source_count is not None and (
        not isinstance(unique_source_count, int)
        or isinstance(unique_source_count, bool)
        or unique_source_count < 0
        or unique_source_count > trace["reranker_candidate_count"]
    ):
        raise ResumeIntegrityError("candidate_trace invalid reranker_candidate_unique_source_count")
    query_view_count = trace.get("query_view_count")
    dual_rrf_applied = trace.get("dual_rrf_applied")
    if query_view_count is not None:
        if (
            not isinstance(query_view_count, int)
            or isinstance(query_view_count, bool)
            or query_view_count not in {1, 2}
        ):
            raise ResumeIntegrityError("candidate_trace invalid query_view_count")
        if not isinstance(dual_rrf_applied, bool):
            raise ResumeIntegrityError("candidate_trace dual_rrf_applied must be boolean")
        if dual_rrf_applied != (query_view_count == 2):
            raise ResumeIntegrityError("candidate_trace dual_rrf_applied disagrees with query_view_count")
    elif dual_rrf_applied is not None:
        raise ResumeIntegrityError("candidate_trace dual_rrf_applied requires query_view_count")
    for name in rank_names:
        value = trace.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise ResumeIntegrityError(f"candidate_trace invalid rank: {name}")
    pairs = (
        ("vector_target_rank", "vector_candidate_count"),
        ("fulltext_target_rank", "fulltext_candidate_count"),
        ("merged_target_rank", "merged_candidate_count"),
        ("reranker_candidate_target_rank", "reranker_candidate_count"),
    )
    for rank_name, count_name in pairs:
        rank = trace.get(rank_name)
        if rank is not None and rank > trace[count_name]:
            raise ResumeIntegrityError(f"candidate_trace rank exceeds count: {rank_name}")


def validate_result_record(
    record: Mapping[str, Any],
    *,
    arm: str | None = None,
    split: str | None = None,
    run_id: str | None = None,
    dataset_sha256: str | None = None,
    config_sha256: str | None = None,
    query_style: str | None = None,
) -> None:
    """Validate a successful result row before it may contribute to a metric."""
    if record.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ResumeIntegrityError("result schema_version mismatch")
    if record.get("dataset_version") != DATASET_VERSION or record.get("status") != "success":
        raise ResumeIntegrityError("result is not a successful rag-silver-v1 record")
    if arm is not None and record.get("arm") != arm:
        raise ResumeIntegrityError("result arm mismatch")
    if split is not None and record.get("split") != split:
        raise ResumeIntegrityError("result split mismatch")
    if run_id is not None and record.get("run_id") != run_id:
        raise ResumeIntegrityError("result run_id mismatch")
    if dataset_sha256 is not None and record.get("dataset_sha256") != dataset_sha256:
        raise ResumeIntegrityError("result dataset SHA-256 mismatch")
    if config_sha256 is not None and record.get("config_sha256") != config_sha256:
        raise ResumeIntegrityError("result config SHA-256 mismatch")
    observed_query_style = query_style_from_result(record)
    if query_style is not None and observed_query_style != _require_query_style(query_style):
        raise ResumeIntegrityError("result query_style mismatch")
    if record.get("source_types") != SOURCE_TYPES or record.get("top_k") != FIXED_TOP_K:
        raise ResumeIntegrityError("result retrieval contract mismatch")
    if not isinstance(record.get("query_id"), str) or not record.get("query_id"):
        raise ResumeIntegrityError("result query_id missing")
    if not isinstance(record.get("target_source_id"), str) or not record.get("target_source_id"):
        raise ResumeIntegrityError("result target_source_id missing")
    query = record.get("query")
    effective_query = record.get("effective_query")
    if not isinstance(query, str) or record.get("query_sha256") != sha256_text(query):
        raise ResumeIntegrityError("result query_sha256 mismatch")
    if observed_query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        try:
            validate_structured_fact_key_value_query(query)
        except DatasetError as exc:
            raise ResumeIntegrityError("structured result query is malformed") from exc
    if not isinstance(effective_query, str) or record.get("effective_query_sha256") != sha256_text(effective_query):
        raise ResumeIntegrityError("result effective_query_sha256 mismatch")
    if observed_query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1 and effective_query != query:
        raise ResumeIntegrityError("structured full result effective_query must equal direct query")
    results = record.get("results")
    if not isinstance(results, list) or len(results) > FIXED_TOP_K:
        raise ResumeIntegrityError("result ranking is invalid")
    if any(not isinstance(result, Mapping) or result.get("source_type") != "case" for result in results):
        raise ResumeIntegrityError("result ranking contains a non-case source")
    try:
        rank, hit, reciprocal_rank = score_results(
            cast(list[Mapping[str, Any]], results), str(record["target_source_id"])
        )
    except (TypeError, ValueError, EvaluationError) as exc:
        raise ResumeIntegrityError("result ranking cannot be recomputed") from exc
    if record.get("first_relevant_rank") != rank or record.get("hit_at_8") != hit:
        raise ResumeIntegrityError("stored result relevance fields mismatch")
    stored_rr = record.get("reciprocal_rank")
    if not isinstance(stored_rr, (float, int)) or not math.isclose(float(stored_rr), reciprocal_rank, abs_tol=1e-12):
        raise ResumeIntegrityError("stored result reciprocal_rank mismatch")
    expected_hash = _record_hash(record)
    if record.get("record_sha256") != expected_hash:
        raise ResumeIntegrityError("result record_sha256 mismatch")
    validate_component_contract(record, query_style=observed_query_style)
    validate_candidate_trace(record)


def result_path(run_dir: Path, split: str, arm: str) -> Path:
    if split == "test":
        return run_dir / f"{arm}-results.jsonl"
    return run_dir / f"smoke-{arm}-results.jsonl"


def resume_key(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("query_id")),
        str(record.get("arm")),
        str(record.get("dataset_sha256")),
        str(record.get("config_sha256")),
    )


def read_resume_records(
    path: Path,
    *,
    arm: str,
    split: str,
    dataset_sha256: str,
    config_sha256: str,
    run_dir: Path,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> dict[str, JsonObject]:
    """Read a checkpoint, recovering only a torn final line (never mid-file)."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    recovered = False
    completed_without_newline = False
    valid_bytes = 0
    records: dict[str, JsonObject] = {}
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        if not line.endswith((b"\n", b"\r")) and is_last:
            try:
                decoded_last = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                recovered = True
                break
            if not isinstance(decoded_last, dict):
                raise ResumeIntegrityError("non-object final result line")
            row = cast(JsonObject, decoded_last)
            completed_without_newline = True
        else:
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ResumeIntegrityError("middle result JSONL corruption") from exc
            if not isinstance(decoded, dict):
                raise ResumeIntegrityError("non-object result JSONL row")
            row = cast(JsonObject, decoded)
        validate_result_record(
            row,
            arm=arm,
            split=split,
            run_id=run_dir.name,
            dataset_sha256=dataset_sha256,
            config_sha256=config_sha256,
            query_style=query_style,
        )
        query_id = str(row["query_id"])
        if query_id in records:
            raise ResumeIntegrityError("duplicate successful result row")
        records[query_id] = row
        valid_bytes += len(line)
    if recovered:
        write_bytes_atomic(path, raw[:valid_bytes])
        log_execution(run_dir, split, "resume", 0, f"recovered torn trailing line from {path.name}")
    elif completed_without_newline:
        write_bytes_atomic(path, raw[:valid_bytes] + b"\n")
        log_execution(run_dir, split, "resume", 0, f"normalised final newline in {path.name}")
    return records


def make_result_record(
    *,
    run_dir: Path,
    split: FrozenSplit,
    query_row: Mapping[str, Any],
    arm: str,
    config_sha256: str,
    target: TargetMapping,
    effective_query: str,
    components: JsonObject,
    results: list[JsonObject],
    attempt_count: int,
    started_at: str,
    latency_ms: float,
    candidate_trace: JsonObject | None = None,
) -> JsonObject:
    query_style = query_style_from_manifest(split.manifest)
    rank, hit, reciprocal_rank = score_results(results, target.source_id)
    record: JsonObject = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": split.sha256,
        "config_sha256": config_sha256,
        "split": split.split,
        "query_id": str(query_row["query_id"]),
        "arm": arm,
        "query": str(query_row["query"]),
        "query_sha256": sha256_text(str(query_row["query"])),
        "target_record_key": str(query_row["target_record_key"]),
        "target_source_id": target.source_id,
        "source_types": SOURCE_TYPES,
        "top_k": FIXED_TOP_K,
        "status": "success",
        "attempt_count": attempt_count,
        "started_at": started_at,
        "completed_at": utc_now(),
        "latency_ms": round(latency_ms, 3),
        "effective_query": effective_query,
        "effective_query_sha256": sha256_text(effective_query),
        "components": components,
        "degradations": _default_degradations(components),
        "results": results,
        "first_relevant_rank": rank,
        "hit_at_8": hit,
        "reciprocal_rank": reciprocal_rank,
    }
    if candidate_trace is not None:
        record["candidate_trace"] = candidate_trace
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        record["query_style"] = query_style
    record["record_sha256"] = _record_hash(record)
    return record


@dataclass(slots=True)
class EvaluationRuntime:
    settings: Any
    baseline: PureVectorAdapter
    full_retriever: Any
    rewrite_gateway: ObservedGateway | None
    frozen_rewrite_cache: FrozenRewriteCache | None
    closeables: list[Any]

    async def aclose(self) -> None:
        seen: set[int] = set()
        for client in self.closeables:
            if id(client) in seen:
                continue
            seen.add(id(client))
            closer = getattr(client, "aclose", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    await closer()


def build_runtime(
    settings: Any,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
    frozen_rewrite_cache: FrozenRewriteCache | None = None,
) -> EvaluationRuntime:
    """Build real gateways/retrievers after the global contract is asserted."""
    query_style = _require_query_style(query_style)
    if query_style == QUERY_STYLE_NATURAL_LANGUAGE_V1:
        # Preserve the legacy v1 construction/import sequence.
        from app.core.gateway import ModelGatewayClient
        from app.core.rewrite_gateway import build_rewrite_gateway_settings
    from app.rag.retriever import RAGRetriever

    baseline_retriever = RAGRetriever(settings=settings)
    baseline = PureVectorAdapter(baseline_retriever, embedding_model=str(settings.embedding_model))
    full_retriever = build_observed_retriever(settings)
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        if bool(getattr(settings, "rag_query_rewrite_enabled", True)):
            raise EvaluationError("structured runtime requires rag_query_rewrite_enabled=false")
        return EvaluationRuntime(
            settings=settings,
            baseline=baseline,
            full_retriever=full_retriever,
            rewrite_gateway=None,
            frozen_rewrite_cache=None,
            closeables=[getattr(baseline_retriever, "_gateway", None)],
        )

    if frozen_rewrite_cache is not None:
        return EvaluationRuntime(
            settings=settings,
            baseline=baseline,
            full_retriever=full_retriever,
            rewrite_gateway=None,
            frozen_rewrite_cache=frozen_rewrite_cache,
            closeables=[getattr(baseline_retriever, "_gateway", None)],
        )

    rewrite_settings = build_rewrite_gateway_settings(settings) or settings
    raw_rewrite_gateway = ModelGatewayClient(settings=rewrite_settings)
    rewrite_event: JsonObject = component_template("not_attempted")
    observed_rewrite_gateway = ObservedGateway(
        raw_rewrite_gateway,
        component="rewrite",
        event_getter=lambda: rewrite_event,
        default_model=_actual_rewrite_model(settings),
    )
    # The event pointer is replaced per query by ``set_rewrite_event``.
    observed_rewrite_gateway.set_event = lambda event: rewrite_event.update(event)  # type: ignore[attr-defined]
    return EvaluationRuntime(
        settings=settings,
        baseline=baseline,
        full_retriever=full_retriever,
        rewrite_gateway=observed_rewrite_gateway,
        frozen_rewrite_cache=None,
        closeables=[getattr(baseline_retriever, "_gateway", None), raw_rewrite_gateway],
    )


def _bind_rewrite_event(gateway: ObservedGateway, event: JsonObject) -> None:
    """Retarget an ObservedGateway's event getter without changing delegated IO."""
    gateway._event_getter = lambda: event  # noqa: SLF001 - private on local evaluation wrapper


async def evaluate_baseline_query(
    runtime: EvaluationRuntime,
    *,
    run_dir: Path,
    split: FrozenSplit,
    query_row: Mapping[str, Any],
    target: TargetMapping,
    config_sha256: str,
    attempt_count: int,
) -> JsonObject:
    started_at = utc_now()
    started = time.perf_counter()
    components = baseline_component_templates()
    try:
        hits = await runtime.baseline.search(str(query_row["query"]), event=cast(JsonObject, components["vector"]))
    except ArmTechnicalFailure:
        raise
    results = project_vector_hits(hits)
    return make_result_record(
        run_dir=run_dir,
        split=split,
        query_row=query_row,
        arm="baseline",
        config_sha256=config_sha256,
        target=target,
        effective_query=str(query_row["query"]),
        components=components,
        results=results,
        attempt_count=attempt_count,
        started_at=started_at,
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )


async def evaluate_full_query(
    runtime: EvaluationRuntime,
    *,
    run_dir: Path,
    split: FrozenSplit,
    query_row: Mapping[str, Any],
    target: TargetMapping,
    config_sha256: str,
    attempt_count: int,
) -> JsonObject:
    """Run the frozen input contract through the production full retriever."""
    query_style = query_style_from_manifest(split.manifest)
    original_query = str(query_row["query"])
    started_at = utc_now()
    started = time.perf_counter()
    components = full_component_templates()
    rewrite_event = cast(JsonObject, components["rewrite"])
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        if bool(getattr(runtime.settings, "rag_query_rewrite_enabled", True)):
            raise EvaluationError("structured full arm requires rag_query_rewrite_enabled=false")
        # The direct production input is already ``fact_key=value；...``.
        # Do not instantiate, bind, or call a rewrite gateway for this arm.
        components["rewrite"] = component_template("not_applicable")
        effective_query = original_query
        try:
            runtime.full_retriever.set_component_records(components)
            evidences = await runtime.full_retriever.retrieve(
                effective_query,
                ["case"],
                allow_cross_source=False,
                top_k=FIXED_TOP_K,
            )
        except Exception as exc:
            fulltext = cast(JsonObject, components["fulltext"])
            if fulltext.get("status") == "failed":
                raise ArmTechnicalFailure("full PostgreSQL retrieval failed") from exc
            raise ArmTechnicalFailure("full retrieval failed before scoreable ranking") from exc
    else:
        # Keep the legacy v1 natural-language arm's call sequence unchanged.
        from app.rag.reasoning_retrieval import build_syndrome_query, rewrite_syndrome_query

        observation = SimpleNamespace(fact_key="present_illness", value=original_query)
        intake_key_query = build_syndrome_query([observation])
        try:
            frozen_rewrite_cache = getattr(runtime, "frozen_rewrite_cache", None)
            if frozen_rewrite_cache is not None:
                if not isinstance(frozen_rewrite_cache, FrozenRewriteCache):
                    raise EvaluationError("runtime frozen Rewrite cache is invalid")
                effective_query = frozen_rewrite_cache.effective_query_for(str(query_row["query_id"]), original_query)
                rewrite_event.update(
                    {
                        "status": "succeeded",
                        "attempted": True,
                        "model": frozen_rewrite_cache.model,
                        "latency_ms": 0.0,
                        "execution_mode": "frozen_replay",
                        "frozen_cache_sha256": frozen_rewrite_cache.sha256,
                    }
                )
            else:
                if runtime.rewrite_gateway is None:
                    raise EvaluationError("natural-language full arm has no rewrite gateway")
                _bind_rewrite_event(runtime.rewrite_gateway, rewrite_event)
                effective_query = await rewrite_syndrome_query([observation], gateway=runtime.rewrite_gateway)
                # Settings is asserted enabled and the observation is nonempty,
                # so a missing gateway call is observable rather than inferred.
                if not rewrite_event.get("attempted"):
                    rewrite_event["status"] = "not_attempted"
            runtime.full_retriever.set_component_records(components)
            if bool(getattr(runtime.settings, "rag_dual_query_enabled", False)):
                evidences = await runtime.full_retriever.retrieve_dual_query(
                    intake_key_query,
                    effective_query,
                    ["case"],
                    allow_cross_source=False,
                    top_k=FIXED_TOP_K,
                )
            else:
                evidences = await runtime.full_retriever.retrieve(
                    effective_query,
                    ["case"],
                    allow_cross_source=False,
                    top_k=FIXED_TOP_K,
                )
        except Exception as exc:
            # Fulltext failures propagate from production; rewrite/vector/rerank
            # component fallbacks are handled by production and remain scoreable.
            fulltext = cast(JsonObject, components["fulltext"])
            if fulltext.get("status") == "failed":
                raise ArmTechnicalFailure("full PostgreSQL retrieval failed") from exc
            if rewrite_event.get("status") == "not_attempted" or rewrite_event.get("status") == "running":
                _event_failure(rewrite_event, started, exc)
            raise ArmTechnicalFailure("full retrieval failed before scoreable ranking") from exc
    runtime.full_retriever.finalise_reranker_observation(evidences)
    results = project_evidences(evidences)
    candidate_trace = runtime.full_retriever.candidate_trace(target.source_id)
    return make_result_record(
        run_dir=run_dir,
        split=split,
        query_row=query_row,
        arm="full",
        config_sha256=config_sha256,
        target=target,
        effective_query=effective_query,
        components=components,
        results=results,
        attempt_count=attempt_count,
        started_at=started_at,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        candidate_trace=candidate_trace,
    )


async def evaluate_with_retries(
    runtime: EvaluationRuntime,
    *,
    run_dir: Path,
    split: FrozenSplit,
    query_row: Mapping[str, Any],
    arm: str,
    target: TargetMapping,
    config_sha256: str,
) -> JsonObject:
    """Retry only arm-level technical failures, never a legitimate miss."""
    worker = evaluate_baseline_query if arm == "baseline" else evaluate_full_query
    for attempt in range(1, len(RETRY_BACKOFF_SECONDS) + 2):
        try:
            return await worker(
                runtime,
                run_dir=run_dir,
                split=split,
                query_row=query_row,
                target=target,
                config_sha256=config_sha256,
                attempt_count=attempt,
            )
        except ArmTechnicalFailure as exc:
            retry_index = attempt - 1
            will_retry = retry_index < len(RETRY_BACKOFF_SECONDS)
            backoff = RETRY_BACKOFF_SECONDS[retry_index] if will_retry else 0.0
            write_failure(
                run_dir,
                phase="final" if split.split == "test" else "smoke",
                split=split.split,
                query_id=str(query_row["query_id"]),
                arm=arm,
                component="retrieval",
                attempt_number=attempt,
                retryable=True,
                backoff_seconds=backoff,
                will_retry=will_retry,
                classification="arm_technical_failure",
                error=exc,
                dataset_sha256=split.sha256,
                config_sha256=config_sha256,
            )
            if will_retry:
                await asyncio.sleep(backoff)
                continue
            raise
    raise AssertionError("unreachable retry loop")


def record_component_fallbacks(run_dir: Path, record: Mapping[str, Any]) -> None:
    """Retain scoreable component degradations in the failure audit stream."""
    components = record.get("components", {})
    if not isinstance(components, Mapping):
        return
    for component, value in components.items():
        if not isinstance(value, Mapping):
            continue
        status = value.get("status")
        if status not in {"fallback", "not_applied_insufficient_candidates"}:
            continue
        write_failure(
            run_dir,
            phase="final" if record.get("split") == "test" else "smoke",
            split=str(record.get("split")),
            query_id=str(record.get("query_id")),
            arm=str(record.get("arm")),
            component=str(component),
            attempt_number=int(record.get("attempt_count", 1)),
            retryable=False,
            backoff_seconds=0.0,
            will_retry=False,
            classification="component_fallback",
            error=f"{component} {status}",
            dataset_sha256=str(record.get("dataset_sha256")),
            config_sha256=str(record.get("config_sha256")),
            error_type=str(value.get("error_type") or "ComponentFallback"),
        )


# ---------------------------------------------------------------------------
# Metric computation.  These functions only read result records and can be
# used independently by verification and unit tests.
# ---------------------------------------------------------------------------


def type7_quantile(values: Sequence[float], probability: float) -> float:
    """R type-7 / linear quantile, specified by the experiment contract."""
    if not values:
        raise EvaluationError("cannot calculate a quantile of an empty sequence")
    if not 0.0 <= probability <= 1.0:
        raise EvaluationError("quantile probability must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    h = (len(ordered) - 1) * probability
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def paired_bootstrap(
    baseline_values: Sequence[float],
    full_values: Sequence[float],
    *,
    samples: int = FIXED_BOOTSTRAP_SAMPLES,
    seed: int = FIXED_SEED,
) -> tuple[float, float]:
    """Paired query-level bootstrap using shared sampled indices for A/B."""
    if len(baseline_values) != len(full_values) or not baseline_values:
        raise EvaluationError("paired bootstrap requires non-empty equal-sized arms")
    if samples < 1:
        raise EvaluationError("bootstrap sample count must be positive")
    count = len(baseline_values)
    differences = [float(full_values[index]) - float(baseline_values[index]) for index in range(count)]
    random_source = random.Random(seed)
    samples_out: list[float] = []
    for _ in range(samples):
        samples_out.append(sum(differences[random_source.randrange(count)] for _ in range(count)) / count)
    return type7_quantile(samples_out, 0.025), type7_quantile(samples_out, 0.975)


def _component_coverage(records: Sequence[Mapping[str, Any]], component: str) -> JsonObject:
    success = sum(
        1
        for record in records
        if isinstance(record.get("components"), Mapping)
        and isinstance(record["components"].get(component), Mapping)
        and record["components"][component].get("status") == "succeeded"
    )
    denominator = len(records)
    return {"success": success, "denominator": denominator, "coverage": success / denominator if denominator else 0.0}


def _degradation_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        degradations = record.get("degradations", [])
        if not isinstance(degradations, list):
            raise EvaluationError("degradations must be a list")
        for degradation in degradations:
            if isinstance(degradation, str):
                counter[degradation] += 1
    return dict(sorted(counter.items()))


def _candidate_audit(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    """Aggregate numeric-only full-arm candidate diagnostics.

    The final top-8 ranking cannot tell whether a miss was absent from recall
    or present and rejected by the Cross-Encoder.  This aggregate deliberately
    stores only counts/ranks, never source ids, query texts, or snippets.
    """
    traces: list[Mapping[str, Any]] = []
    traced_records: list[Mapping[str, Any]] = []
    for record in records:
        trace = record.get("candidate_trace")
        if trace is None:
            continue
        validate_candidate_trace(record)
        traces.append(cast(Mapping[str, Any], trace))
        traced_records.append(record)

    denominator = len(records)
    trace_count = len(traces)
    payload: JsonObject = {
        "trace_records": trace_count,
        "denominator": denominator,
        "coverage": trace_count / denominator if denominator else 0.0,
    }
    if not traces:
        return payload

    stage_fields = (
        ("vector", "vector_candidate_count", "vector_target_rank"),
        ("fulltext", "fulltext_candidate_count", "fulltext_target_rank"),
        ("merged", "merged_candidate_count", "merged_target_rank"),
        ("reranker_pool", "reranker_candidate_count", "reranker_candidate_target_rank"),
    )
    stages: JsonObject = {}
    for stage, count_name, rank_name in stage_fields:
        counts = sorted(int(trace[count_name]) for trace in traces)
        middle = trace_count // 2
        median: float = float(counts[middle])
        if trace_count % 2 == 0:
            median = (counts[middle - 1] + counts[middle]) / 2.0
        target_present = sum(1 for trace in traces if trace[rank_name] is not None)
        stages[stage] = {
            "target_present": target_present,
            "denominator": trace_count,
            "coverage": target_present / trace_count,
            "candidate_count": {"min": counts[0], "median": median, "max": counts[-1]},
        }
    payload["stages"] = stages
    unique_source_counts = sorted(
        int(trace["reranker_candidate_unique_source_count"])
        for trace in traces
        if trace.get("reranker_candidate_unique_source_count") is not None
    )
    if unique_source_counts:
        middle = len(unique_source_counts) // 2
        unique_median: float = float(unique_source_counts[middle])
        if len(unique_source_counts) % 2 == 0:
            unique_median = (unique_source_counts[middle - 1] + unique_source_counts[middle]) / 2.0
        payload["reranker_pool_unique_source_count"] = {
            "min": unique_source_counts[0],
            "median": unique_median,
            "max": unique_source_counts[-1],
        }
    query_view_counts = sorted(
        int(trace["query_view_count"]) for trace in traces if trace.get("query_view_count") is not None
    )
    if query_view_counts:
        middle = len(query_view_counts) // 2
        view_median: float = float(query_view_counts[middle])
        if len(query_view_counts) % 2 == 0:
            view_median = (query_view_counts[middle - 1] + query_view_counts[middle]) / 2.0
        payload["query_views"] = {
            "dual_rrf_applied": sum(1 for trace in traces if trace.get("dual_rrf_applied") is True),
            "denominator": len(query_view_counts),
            "count": {"min": query_view_counts[0], "median": view_median, "max": query_view_counts[-1]},
        }
    reranker_attempted = sum(1 for trace in traces if trace["reranker_attempted"])
    payload["reranker_attempted"] = reranker_attempted
    payload["reranker_pool_target_present_final_miss"] = sum(
        1
        for record, trace in zip(traced_records, traces, strict=True)
        if trace["reranker_attempted"]
        and trace["reranker_candidate_target_rank"] is not None
        and int(record.get("hit_at_8", 0)) == 0
    )
    return payload


def compute_metrics(
    baseline_records: Sequence[Mapping[str, Any]],
    full_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    dataset_sha256: str,
    config_sha256: str,
    bootstrap_samples: int = FIXED_BOOTSTRAP_SAMPLES,
    seed: int = FIXED_SEED,
) -> JsonObject:
    """Recompute all formal metrics from immutable JSONL records."""
    query_styles = {query_style_from_result(record) for record in (*baseline_records, *full_records)}
    if len(query_styles) != 1:
        raise EvaluationError("baseline/full records do not share one query_style")
    query_style = next(iter(query_styles))
    baseline_by_id = {str(record.get("query_id")): record for record in baseline_records}
    full_by_id = {str(record.get("query_id")): record for record in full_records}
    if len(baseline_by_id) != len(baseline_records) or len(full_by_id) != len(full_records):
        raise EvaluationError("duplicate query_id in successful result records")
    if set(baseline_by_id) != set(full_by_id):
        raise EvaluationError("baseline/full query_id sets are not paired")
    ordered_ids = sorted(baseline_by_id)
    if not ordered_ids:
        raise EvaluationError("no paired records")
    baseline_hits_by_cutoff: dict[int, list[float]] = {cutoff: [] for cutoff in RECALL_CUTOFFS}
    full_hits_by_cutoff: dict[int, list[float]] = {cutoff: [] for cutoff in RECALL_CUTOFFS}
    baseline_rrs: list[float] = []
    full_rrs: list[float] = []
    paired_hits_by_cutoff: dict[int, dict[str, int]] = {
        cutoff: {"0_0": 0, "0_1": 0, "1_0": 0, "1_1": 0} for cutoff in RECALL_CUTOFFS
    }
    for query_id in ordered_ids:
        baseline = baseline_by_id[query_id]
        full = full_by_id[query_id]
        scored: dict[str, tuple[int | None, int, float]] = {}
        for arm, record in (("baseline", baseline), ("full", full)):
            results = cast(list[Mapping[str, Any]], record.get("results", []))
            rank, hit, reciprocal_rank = score_results(results, str(record.get("target_source_id", "")))
            if record.get("first_relevant_rank") != rank or int(record.get("hit_at_8", -1)) != hit:
                raise EvaluationError("result relevance fields do not recompute")
            if not math.isclose(float(record.get("reciprocal_rank", -1.0)), reciprocal_rank, abs_tol=1e-12):
                raise EvaluationError("result reciprocal_rank does not recompute")
            scored[arm] = (rank, hit, reciprocal_rank)
        baseline_rank, _baseline_hit_at_8, baseline_rr = scored["baseline"]
        full_rank, _full_hit_at_8, full_rr = scored["full"]
        for cutoff in RECALL_CUTOFFS:
            b_hit = int(baseline_rank is not None and baseline_rank <= cutoff)
            f_hit = int(full_rank is not None and full_rank <= cutoff)
            baseline_hits_by_cutoff[cutoff].append(float(b_hit))
            full_hits_by_cutoff[cutoff].append(float(f_hit))
            paired_hits_by_cutoff[cutoff][f"{b_hit}_{f_hit}"] += 1
        baseline_rrs.append(float(baseline_rr))
        full_rrs.append(float(full_rr))
    recall_ci_by_cutoff = {
        cutoff: paired_bootstrap(
            baseline_hits_by_cutoff[cutoff], full_hits_by_cutoff[cutoff], samples=bootstrap_samples, seed=seed
        )
        for cutoff in RECALL_CUTOFFS
    }
    mrr_ci = paired_bootstrap(baseline_rrs, full_rrs, samples=bootstrap_samples, seed=seed)
    count = len(ordered_ids)
    baseline_arm = {
        f"target_recall_at_{cutoff}": sum(baseline_hits_by_cutoff[cutoff]) / count for cutoff in RECALL_CUTOFFS
    }
    full_arm = {f"target_recall_at_{cutoff}": sum(full_hits_by_cutoff[cutoff]) / count for cutoff in RECALL_CUTOFFS}
    baseline_arm["mrr"] = sum(baseline_rrs) / count
    full_arm["mrr"] = sum(full_rrs) / count
    recall_deltas: JsonObject = {
        f"target_recall_at_{cutoff}": {
            "point": sum(
                full_hits_by_cutoff[cutoff][index] - baseline_hits_by_cutoff[cutoff][index] for index in range(count)
            )
            / count,
            "ci95": {"low": recall_ci_by_cutoff[cutoff][0], "high": recall_ci_by_cutoff[cutoff][1]},
        }
        for cutoff in RECALL_CUTOFFS
    }
    metrics: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": dataset_sha256,
        "config_sha256": config_sha256,
        "n_pairs": count,
        "top_k": FIXED_TOP_K,
        "arms": {
            "baseline": baseline_arm,
            "full": full_arm,
        },
        "deltas": {
            **recall_deltas,
            "mrr": {
                "point": sum(full_rrs[index] - baseline_rrs[index] for index in range(count)) / count,
                "ci95": {"low": mrr_ci[0], "high": mrr_ci[1]},
            },
        },
        # Retain this legacy @8 field for existing run readers; the structured
        # object makes the stricter @1/@5 paired diagnostics explicit.
        "paired_hits": paired_hits_by_cutoff[FIXED_TOP_K],
        "paired_hits_by_cutoff": {f"at_{cutoff}": paired_hits_by_cutoff[cutoff] for cutoff in RECALL_CUTOFFS},
        "components": {
            "baseline_vector": _component_coverage(baseline_records, "vector"),
            "full_rewrite": _component_coverage(full_records, "rewrite"),
            "full_vector": _component_coverage(full_records, "vector"),
            "full_fulltext": _component_coverage(full_records, "fulltext"),
            "full_cross_encoder": _component_coverage(full_records, "reranker"),
        },
        "degradation_counts": _degradation_counts(full_records),
        "bootstrap": {
            "method": "paired_query_bootstrap",
            "samples": bootstrap_samples,
            "seed": seed,
            "confidence_level": 0.95,
            "quantile_method": "linear_type7",
        },
        "artifact_sha256": {},
        "validation": {"status": "INCOMPLETE", "reasons": []},
    }
    # Keep legacy v1 metric contracts byte-for-byte recomputable.  New tuning
    # runs opt in by recording candidate traces on the full arm.
    if any(record.get("candidate_trace") is not None for record in full_records):
        metrics["candidate_audit"] = _candidate_audit(full_records)
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        metrics["query_style"] = query_style
    return metrics


def _require_metric_equal(observed: Any, expected: Any, label: str, reasons: list[str]) -> None:
    if isinstance(observed, (float, int)) and isinstance(expected, (float, int)):
        if not math.isclose(float(observed), float(expected), abs_tol=1e-12):
            reasons.append(label)
    elif observed != expected:
        reasons.append(label)


def _result_records_for_verify(
    path: Path,
    *,
    arm: str,
    split: str = "test",
    dataset_sha256: str,
    config_sha256: str,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> list[JsonObject]:
    records = read_jsonl(path)
    for record in records:
        validate_result_record(
            record,
            arm=arm,
            split=split,
            run_id=path.parent.name,
            dataset_sha256=dataset_sha256,
            config_sha256=config_sha256,
            query_style=query_style,
        )
    return records


def quality_gate_evidence_errors(run_dir: Path, environment: Mapping[str, Any]) -> list[str]:
    """Validate the exact four audited quality commands and their log evidence.

    Successful command *names* alone are not enough: a stale or hand-edited
    environment file could otherwise claim a quality gate that did not execute
    the mandated argv in this run directory.
    """
    expected = [{"name": name, "argv": command, "exit_code": 0} for name, command in _QUALITY_GATE_COMMANDS]
    commands = environment.get("commands")
    if not isinstance(commands, list) or len(commands) != len(expected):
        return ["environment.commands is not the required four-command record"]
    normalized: list[JsonObject] = []
    for item in commands:
        if not isinstance(item, Mapping):
            return ["environment.commands contains a non-object entry"]
        argv = item.get("argv")
        if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
            return ["environment.commands lacks exact argv"]
        normalized.append({"name": item.get("name"), "argv": list(argv), "exit_code": item.get("exit_code")})
    if normalized != expected:
        return ["environment.commands does not equal the mandated quality argv"]
    try:
        log_rows = read_jsonl(run_dir / "execution.log")
    except EvaluationError as exc:
        return [f"execution.log is unreadable: {redacted_message(exc)}"]
    missing_logs: list[str] = []
    for item in expected:
        command_text = " ".join(cast(list[str], item["argv"]))
        expected_summary = f"{item['name']}: passed"
        if not any(
            row.get("phase") == "implement"
            and row.get("command") == command_text
            and row.get("exit_code") == 0
            and row.get("summary") == expected_summary
            for row in log_rows
        ):
            missing_logs.append(str(item["name"]))
    if missing_logs:
        return [f"execution.log lacks exact successful evidence for {','.join(missing_logs)}"]
    return []


def validate_metrics_validation_state(
    run_dir: Path,
    persisted_metrics: Mapping[str, Any],
) -> str | None:
    """Ensure the persisted validation outcome and lifecycle conclusion agree.

    Before final acceptance, ``state.conclusion`` remains ``null`` and the
    only coherent metrics state is ``INCOMPLETE``.  Once a terminal conclusion
    exists, the two strings must match exactly.
    """
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return "missing_state"
    try:
        state = read_json(state_path)
    except EvaluationError:
        return "invalid_state"
    validation = persisted_metrics.get("validation")
    if not isinstance(validation, Mapping) or not isinstance(validation.get("status"), str):
        return "metrics_validation_missing_or_invalid"
    metric_status = str(validation["status"])
    conclusion = state.get("conclusion")
    if conclusion is None:
        return None if metric_status == "INCOMPLETE" else "metrics_validation_state_conclusion_mismatch"
    if conclusion not in {"PASS", "INVALID", "BLOCKED"}:
        return "state_conclusion_invalid"
    return None if metric_status == conclusion else "metrics_validation_state_conclusion_mismatch"


def preflight_target_mapping(run_dir: Path, frozen_records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Read the read-only resolver mapping captured before Smoke/Test started."""
    preflight_path = run_dir / "preflight.json"
    if not preflight_path.exists():
        raise EvaluationError("missing preflight")
    preflight = read_json(preflight_path)
    target_check = next(
        (
            check
            for check in preflight.get("checks", [])
            if isinstance(check, Mapping) and check.get("name") == "frozen_target_resolution"
        ),
        None,
    )
    if not isinstance(target_check, Mapping) or target_check.get("status") != "passed":
        raise EvaluationError("frozen target resolver preflight is not passed")
    evidence = target_check.get("evidence")
    mapping = evidence.get("target_mapping") if isinstance(evidence, Mapping) else None
    if not isinstance(mapping, Mapping):
        raise EvaluationError("preflight has no target mapping evidence")
    expected_keys = {str(record["target_record_key"]) for record in frozen_records}
    # A preflight resolves both frozen Smoke and Test so that neither run can
    # silently choose a random database target.  A formal Test verifier must
    # select its own required subset rather than rejecting those valid Smoke
    # bindings as unexpected extras.
    normalized = {str(key): str(value) for key, value in mapping.items()}
    missing = expected_keys - set(normalized)
    if missing or any(
        not isinstance(mapping.get(key), str) or not str(mapping.get(key)).strip() for key in expected_keys
    ):
        raise EvaluationError("preflight target mapping does not bind all frozen queries")
    return {key: normalized[key] for key in expected_keys}


def validate_records_bound_to_frozen_split(
    records: Sequence[Mapping[str, Any]],
    frozen_records: Sequence[Mapping[str, Any]],
    target_mapping: Mapping[str, str],
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> None:
    """Bind every scoreable result to its immutable query and resolver target."""
    query_style = _require_query_style(query_style)
    frozen_by_id = {str(row["query_id"]): row for row in frozen_records}
    for record in records:
        query_id = str(record.get("query_id"))
        frozen = frozen_by_id.get(query_id)
        if frozen is None:
            raise EvaluationError("result query_id is absent from frozen Test")
        expected_query = str(frozen["query"])
        target_key = str(frozen["target_record_key"])
        if record.get("query") != expected_query or record.get("query_sha256") != sha256_text(expected_query):
            raise EvaluationError("result query is not bound to frozen Test text")
        if query_style_from_result(record) != query_style:
            raise EvaluationError("result query_style is not bound to frozen Test")
        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            try:
                validate_structured_fact_key_value_query(expected_query)
            except DatasetError as exc:
                raise EvaluationError("frozen structured query is malformed") from exc
            if record.get("effective_query") != expected_query:
                raise EvaluationError("structured result did not use the frozen direct query")
        if record.get("target_record_key") != target_key:
            raise EvaluationError("result target_record_key is not bound to frozen Test")
        if record.get("target_source_id") != target_mapping.get(target_key):
            raise EvaluationError("result target_source_id is not bound to resolved frozen target")


def preflight_contract_errors(preflight: Mapping[str, Any]) -> list[str]:
    """Check the complete required preflight inventory and its key evidence."""
    errors: list[str] = []
    if preflight.get("overall_status") != "passed":
        errors.append("preflight overall_status is not passed")
    checks = preflight.get("checks")
    if not isinstance(checks, list):
        return errors + ["preflight checks is not a list"]
    by_name: dict[str, Mapping[str, Any]] = {}
    for check in checks:
        if not isinstance(check, Mapping) or not isinstance(check.get("name"), str):
            errors.append("preflight contains an invalid check")
            continue
        name = str(check["name"])
        if name in by_name:
            errors.append(f"preflight duplicates required check {name}")
        else:
            by_name[name] = check
    for name in _REQUIRED_PREFLIGHT_CHECKS:
        check = by_name.get(name)
        if check is None:
            errors.append(f"preflight missing required check {name}")
        elif check.get("required") is not True or check.get("status") != "passed":
            errors.append(f"preflight required check is not passed: {name}")

    def evidence(name: str) -> Mapping[str, Any]:
        check = by_name.get(name, {})
        value = check.get("evidence") if isinstance(check, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    frozen = evidence("frozen_dataset")
    if frozen.get("smoke_count") != FIXED_SMOKE_SIZE or frozen.get("test_count") != FIXED_TEST_SIZE:
        errors.append("preflight frozen_dataset count evidence is invalid")
    if (
        not isinstance(frozen.get("test_sha256"), str)
        or _RECORD_KEY_RE.fullmatch(str(frozen.get("test_sha256"))) is None
    ):
        errors.append("preflight frozen_dataset test hash evidence is invalid")
    try:
        query_style = _require_query_style(frozen.get("query_style", QUERY_STYLE_NATURAL_LANGUAGE_V1))
    except EvaluationError:
        errors.append("preflight frozen_dataset query_style is invalid")
        query_style = QUERY_STYLE_NATURAL_LANGUAGE_V1
    source = evidence("source_file")
    if not isinstance(source.get("sha256"), str) or _RECORD_KEY_RE.fullmatch(str(source.get("sha256"))) is None:
        errors.append("preflight source_file hash evidence is invalid")
    config = evidence("effective_contract_configuration")
    if not isinstance(config.get("collection"), str) or not config.get("collection"):
        errors.append("preflight effective configuration has no collection evidence")
    if (
        not isinstance(config.get("config_sha256"), str)
        or _RECORD_KEY_RE.fullmatch(str(config.get("config_sha256"))) is None
    ):
        errors.append("preflight effective configuration has no config hash evidence")
    try:
        configured_query_style = _require_query_style(config.get("query_style", QUERY_STYLE_NATURAL_LANGUAGE_V1))
    except EvaluationError:
        errors.append("preflight effective configuration query_style is invalid")
    else:
        if configured_query_style != query_style:
            errors.append("preflight frozen/configuration query_style mismatch")
    postgres = evidence("postgres_connectivity")
    if not all(
        isinstance(postgres.get(key), int) and int(postgres[key]) >= 0
        for key in ("active_case_rows", "active_case_chunks")
    ):
        errors.append("preflight PostgreSQL corpus evidence is invalid")
    milvus = evidence("milvus_collection")
    if not isinstance(milvus.get("collection"), str) or not isinstance(milvus.get("embedding_dim"), int):
        errors.append("preflight Milvus evidence is invalid")
    gateways = evidence("model_gateways")
    for name in ("embedding", "reranker"):
        component = gateways.get(name)
        if (
            not isinstance(component, Mapping)
            or not isinstance(component.get("model"), str)
            or not component.get("model")
        ):
            errors.append(f"preflight gateway evidence is invalid: {name}")
    rewrite_gateway = gateways.get("rewrite")
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        if (
            not isinstance(rewrite_gateway, Mapping)
            or rewrite_gateway.get("status") != "not_applicable"
            or rewrite_gateway.get("enabled") is not False
            or (rewrite_gateway.get("model") is not None and rewrite_gateway.get("model") != "")
        ):
            errors.append("preflight structured rewrite evidence is not explicitly disabled")
    elif (
        not isinstance(rewrite_gateway, Mapping)
        or not isinstance(rewrite_gateway.get("model"), str)
        or not rewrite_gateway.get("model")
    ):
        errors.append("preflight gateway evidence is invalid: rewrite")
    targets = evidence("frozen_target_resolution")
    target_mapping = targets.get("target_mapping")
    if (
        targets.get("resolved") != FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        or targets.get("unique_source_ids") != FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        or targets.get("vector_ready") != FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        or not isinstance(target_mapping, Mapping)
        or len(target_mapping) != FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
    ):
        errors.append("preflight frozen target-resolution evidence is invalid")
    expected_chunks = targets.get("expected_target_chunks")
    matched_chunks = targets.get("matched_target_chunks")
    if (
        not isinstance(expected_chunks, int)
        or expected_chunks < FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        or matched_chunks != expected_chunks
    ):
        errors.append("preflight Milvus target-membership evidence is invalid")
    prepared = evidence("prepared_corpus_snapshot")
    if not all(
        isinstance(prepared.get(key), int) and int(prepared[key]) > 0
        for key in ("prepared_total_entries", "prepared_case_entries")
    ):
        errors.append("preflight prepared corpus evidence is invalid")
    return errors


def artifact_secret_audit_errors(run_dir: Path) -> list[str]:
    """Flag credential-shaped values without echoing them into a report/log."""
    artifact_names = (
        "config.redacted.json",
        "execution.log",
        "failures.jsonl",
        "baseline-results.jsonl",
        "full-results.jsonl",
        "smoke-baseline-results.jsonl",
        "smoke-full-results.jsonl",
        "metrics.json",
        "report.md",
        "resume-bullet.md",
    )
    errors: list[str] = []
    for name in artifact_names:
        path = run_dir / name
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"cannot read artifact for secret audit: {name}")
            continue
        if _ARTIFACT_SECRET_RE.search(content):
            errors.append(f"credential-shaped value detected in {name}")
    return errors


def audit_failure_log(
    run_dir: Path,
    *,
    test_dataset_sha256: str,
    smoke_dataset_sha256: str,
    config_sha256: str,
    successful_records: Sequence[Mapping[str, Any]],
) -> tuple[list[str], JsonObject]:
    """Audit recovery evidence and identify unresolved arm-level failures."""
    path = run_dir / "failures.jsonl"
    summary: JsonObject = {
        "total": 0,
        "by_classification": {},
        "arm_technical_failures": 0,
        "unresolved_arm_technical_failures": 0,
        "component_fallbacks": {},
    }
    try:
        rows = read_jsonl(path)
    except EvaluationError as exc:
        return [f"failures.jsonl is unreadable: {redacted_message(exc)}"], summary
    errors: list[str] = []
    successful_keys = {
        (str(record.get("split")), str(record.get("arm")), str(record.get("query_id"))) for record in successful_records
    }
    terminal_failures: set[tuple[str, str, str]] = set()
    classifications: Counter[str] = Counter()
    fallback_components: Counter[str] = Counter()
    for number, row in enumerate(rows, start=1):
        if row.get("schema_version") != SCHEMA_VERSION or row.get("run_id") != run_dir.name:
            errors.append(f"failure row {number} has inconsistent schema or run_id")
        classification = row.get("classification")
        if not isinstance(classification, str) or classification not in _FAILURE_CLASSIFICATIONS:
            errors.append(f"failure row {number} has an invalid classification")
            continue
        classifications[classification] += 1
        message = row.get("message_redacted")
        if not isinstance(message, str) or _ARTIFACT_SECRET_RE.search(message):
            errors.append(f"failure row {number} has an unsafe redacted message")
        split = row.get("split")
        arm = row.get("arm")
        if split not in {None, "smoke", "test"} or arm not in {None, "baseline", "full"}:
            errors.append(f"failure row {number} has an invalid split or arm")
        if split in {"smoke", "test"}:
            expected_dataset = smoke_dataset_sha256 if split == "smoke" else test_dataset_sha256
            if row.get("dataset_sha256") != expected_dataset or row.get("config_sha256") != config_sha256:
                errors.append(f"failure row {number} has inconsistent dataset or config hash")
        if classification == "component_fallback":
            fallback_components[str(row.get("component") or "unknown")] += 1
        if classification == "arm_technical_failure":
            summary["arm_technical_failures"] = int(summary["arm_technical_failures"]) + 1
            if row.get("will_retry") is False and isinstance(split, str) and isinstance(arm, str):
                query_id = row.get("query_id")
                if isinstance(query_id, str) and query_id:
                    terminal_failures.add((split, arm, query_id))
    unresolved = terminal_failures - successful_keys
    summary["total"] = len(rows)
    summary["by_classification"] = dict(sorted(classifications.items()))
    summary["component_fallbacks"] = dict(sorted(fallback_components.items()))
    summary["unresolved_arm_technical_failures"] = len(unresolved)
    if unresolved:
        errors.append("unresolved arm technical failure remains in failures.jsonl")
    return errors, summary


def validate_run(
    *,
    dataset_dir: Path,
    run_dir: Path,
    require_final_artifacts: bool = True,
) -> tuple[str, list[str], JsonObject | None]:
    """Machine-check the formal must-gates and return PASS/INVALID evidence."""
    reasons: list[str] = []
    metrics: JsonObject | None = None
    candidate_trace_required = False
    try:
        smoke, test = validate_dataset_pair(dataset_dir)
    except EvaluationError as exc:
        return "INVALID", [f"dataset:{redacted_message(exc)}"], None
    query_style = query_style_from_manifest(test.manifest)
    expected_sha = test.sha256
    manifest_path = run_dir / "dataset-manifest.json"
    config_path = run_dir / "config.redacted.json"
    if not manifest_path.exists():
        reasons.append("missing_dataset_manifest")
    else:
        run_manifest = read_json(manifest_path)
        if run_manifest.get("test_jsonl_sha256") != expected_sha:
            reasons.append("run_dataset_hash_mismatch")
        try:
            run_query_style = query_style_from_manifest(run_manifest)
        except EvaluationError:
            reasons.append("run_dataset_query_style_invalid")
        else:
            if run_query_style != query_style:
                reasons.append("run_dataset_query_style_mismatch")
    if not config_path.exists():
        reasons.append("missing_config_redacted")
        config_sha = ""
    else:
        config = read_json(config_path)
        config_sha = str(config.get("config_sha256", ""))
        try:
            config_query_style = query_style_from_config(config)
        except EvaluationError:
            reasons.append("config_query_style_invalid")
        else:
            if config_query_style != query_style:
                reasons.append("config_query_style_mismatch")
        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            rewrite = config.get("rewrite")
            models = config.get("models")
            if (
                not isinstance(rewrite, Mapping)
                or not isinstance(models, Mapping)
                or rewrite.get("enabled") is not False
                or rewrite.get("applicable") is not False
                or rewrite.get("gateway_call") != "not_applicable"
                or (isinstance(models, Mapping) and models.get("rewrite") is not None and models.get("rewrite") != "")
            ):
                reasons.append("structured_rewrite_not_explicitly_disabled")
        retrieval = config.get("retrieval")
        candidate_trace_required = bool(isinstance(retrieval, Mapping) and retrieval.get("candidate_trace") is True)
        config_body = dict(config)
        config_body.pop("config_sha256", None)
        config_body.pop("captured_at", None)
        if not config_sha or sha256_bytes(compact_json_bytes(config_body)) != config_sha:
            reasons.append("config_hash_invalid")
    baseline_path = run_dir / "baseline-results.jsonl"
    full_path = run_dir / "full-results.jsonl"
    if not baseline_path.exists() or not full_path.exists():
        reasons.append("missing_formal_result_jsonl")
        baseline_records: list[JsonObject] = []
        full_records: list[JsonObject] = []
    else:
        try:
            baseline_records = _result_records_for_verify(
                baseline_path,
                arm="baseline",
                dataset_sha256=expected_sha,
                config_sha256=config_sha,
                query_style=query_style,
            )
            full_records = _result_records_for_verify(
                full_path,
                arm="full",
                dataset_sha256=expected_sha,
                config_sha256=config_sha,
                query_style=query_style,
            )
        except EvaluationError as exc:
            reasons.append(f"result_integrity:{redacted_message(exc)}")
            baseline_records, full_records = [], []
    smoke_baseline_records: list[JsonObject] = []
    smoke_full_records: list[JsonObject] = []
    test_ids = {str(record["query_id"]) for record in test.records}
    if len(baseline_records) != FIXED_TEST_SIZE or len(full_records) != FIXED_TEST_SIZE:
        reasons.append("formal_result_count_not_200_per_arm")
    if {str(record.get("query_id")) for record in baseline_records} != test_ids:
        reasons.append("baseline_query_set_not_frozen_test")
    if {str(record.get("query_id")) for record in full_records} != test_ids:
        reasons.append("full_query_set_not_frozen_test")
    if baseline_records and full_records:
        try:
            mapping = preflight_target_mapping(run_dir, test.records)
            validate_records_bound_to_frozen_split(baseline_records, test.records, mapping, query_style=query_style)
            validate_records_bound_to_frozen_split(full_records, test.records, mapping, query_style=query_style)
        except EvaluationError as exc:
            reasons.append(f"frozen_result_binding:{redacted_message(exc)}")
    if candidate_trace_required and full_records and any("candidate_trace" not in record for record in full_records):
        reasons.append("candidate_trace_not_complete_for_full_arm")
    if baseline_records and full_records:
        try:
            metrics = compute_metrics(
                baseline_records,
                full_records,
                run_id=run_dir.name,
                dataset_sha256=expected_sha,
                config_sha256=config_sha,
            )
            metrics["artifact_sha256"] = {
                "baseline_results": sha256_file(baseline_path),
                "full_results": sha256_file(full_path),
            }
        except EvaluationError as exc:
            reasons.append(f"metrics_recompute:{redacted_message(exc)}")
    smoke_baseline_path = result_path(run_dir, "smoke", "baseline")
    smoke_full_path = result_path(run_dir, "smoke", "full")
    if not smoke_baseline_path.exists() or not smoke_full_path.exists():
        reasons.append("missing_smoke_result_jsonl")
    else:
        try:
            smoke_baseline_records = _result_records_for_verify(
                smoke_baseline_path,
                arm="baseline",
                split="smoke",
                dataset_sha256=smoke.sha256,
                config_sha256=config_sha,
                query_style=query_style,
            )
            smoke_full_records = _result_records_for_verify(
                smoke_full_path,
                arm="full",
                split="smoke",
                dataset_sha256=smoke.sha256,
                config_sha256=config_sha,
                query_style=query_style,
            )
            smoke_ids = {str(record["query_id"]) for record in smoke.records}
            if len(smoke_baseline_records) != FIXED_SMOKE_SIZE or len(smoke_full_records) != FIXED_SMOKE_SIZE:
                raise EvaluationError("Smoke result count is not 20 per arm")
            if {str(record.get("query_id")) for record in smoke_baseline_records} != smoke_ids:
                raise EvaluationError("Smoke baseline query set does not equal frozen Smoke")
            if {str(record.get("query_id")) for record in smoke_full_records} != smoke_ids:
                raise EvaluationError("Smoke full query set does not equal frozen Smoke")
            smoke_mapping = preflight_target_mapping(run_dir, smoke.records)
            validate_records_bound_to_frozen_split(
                smoke_baseline_records, smoke.records, smoke_mapping, query_style=query_style
            )
            validate_records_bound_to_frozen_split(
                smoke_full_records, smoke.records, smoke_mapping, query_style=query_style
            )
        except EvaluationError as exc:
            reasons.append(f"smoke_result_integrity:{redacted_message(exc)}")
            smoke_baseline_records, smoke_full_records = [], []
    if (
        candidate_trace_required
        and smoke_full_records
        and any("candidate_trace" not in record for record in smoke_full_records)
    ):
        reasons.append("candidate_trace_not_complete_for_smoke_full_arm")
    failure_errors, _failure_summary = audit_failure_log(
        run_dir,
        test_dataset_sha256=expected_sha,
        smoke_dataset_sha256=smoke.sha256,
        config_sha256=config_sha,
        successful_records=baseline_records + full_records + smoke_baseline_records + smoke_full_records,
    )
    reasons.extend(f"failure_audit:{error}" for error in failure_errors)
    reasons.extend(f"artifact_security:{error}" for error in artifact_secret_audit_errors(run_dir))
    preflight_path = run_dir / "preflight.json"
    if not preflight_path.exists():
        reasons.append("missing_preflight")
    else:
        preflight = read_json(preflight_path)
        reasons.extend(f"preflight_contract:{error}" for error in preflight_contract_errors(preflight))
    if metrics is not None:
        components = cast(JsonObject, metrics["components"])
        if components["baseline_vector"]["success"] != FIXED_TEST_SIZE:
            reasons.append("baseline_vector_coverage_not_100_percent")
        if query_style == QUERY_STYLE_NATURAL_LANGUAGE_V1 and components["full_rewrite"]["coverage"] < 0.95:
            reasons.append("rewrite_coverage_below_95_percent")
        if components["full_cross_encoder"]["coverage"] < 0.95:
            reasons.append("cross_encoder_coverage_below_95_percent")
    if require_final_artifacts:
        required_names = (
            "preflight.json",
            "state.json",
            "dataset-manifest.json",
            "config.redacted.json",
            "environment.json",
            "baseline-results.jsonl",
            "full-results.jsonl",
            "metrics.json",
            "failures.jsonl",
            "execution.log",
            "report.md",
            "resume-bullet.md",
        )
        for name in required_names:
            if not (run_dir / name).exists():
                reasons.append(f"missing_artifact:{name}")
    metrics_path = run_dir / "metrics.json"
    if metrics_path.exists():
        try:
            persisted = read_json(metrics_path)
        except EvaluationError as exc:
            reasons.append(f"invalid_metrics_json:{redacted_message(exc)}")
        else:
            if metrics is not None:
                persisted_contract = dict(persisted)
                computed_contract = dict(metrics)
                # Validation is verifier output, not a value derived from JSONL.
                persisted_contract.pop("validation", None)
                computed_contract.pop("validation", None)
                if compact_json_bytes(persisted_contract) != compact_json_bytes(computed_contract):
                    reasons.append("persisted_metrics_contract_mismatch")
            validation_error = validate_metrics_validation_state(run_dir, persisted)
            if validation_error is not None:
                reasons.append(validation_error)
    elif metrics is not None:
        reasons.append("missing_metrics_json")
    environment_path = run_dir / "environment.json"
    if not environment_path.exists():
        reasons.append("missing_environment")
    else:
        environment = read_json(environment_path)
        try:
            ensure_frozen_code_identity(run_dir)
        except EvaluationError as exc:
            reasons.append(f"code_identity:{redacted_message(exc)}")
        if quality_gate_evidence_errors(run_dir, environment):
            reasons.append("quality_gate_evidence_mismatch")
    return ("PASS" if not reasons else "INVALID"), reasons, metrics


# ---------------------------------------------------------------------------
# Real dependency preflight
# ---------------------------------------------------------------------------


def _preflight_check(
    name: str, required: bool, status: str, evidence: Mapping[str, Any] | None = None, error: Any = None
) -> JsonObject:
    return {
        "name": name,
        "required": required,
        "status": status,
        "observed_at": utc_now(),
        "evidence": dict(evidence or {}),
        "error_type": safe_exception_type(error) if isinstance(error, BaseException) else None,
    }


async def _corpus_counts(session_factory: Any) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.models.knowledge import KnowledgeChunk, TheoryCase

    async with session_factory() as session:
        active_cases = await session.execute(
            select(func.count())
            .select_from(TheoryCase)
            .where(TheoryCase.entry_type == "case", TheoryCase.deleted_at.is_(None))
        )
        active_chunks = await session.execute(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.source_type == "case", KnowledgeChunk.deleted_at.is_(None))
        )
    return {
        "active_case_rows": int(active_cases.scalar_one()),
        "active_case_chunks": int(active_chunks.scalar_one()),
    }


def _milvus_dimension(description: Any) -> int | None:
    if not isinstance(description, Mapping):
        return None
    fields = description.get("fields", [])
    if not isinstance(fields, list):
        return None
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        params = field.get("params")
        if isinstance(params, Mapping) and "dim" in params:
            try:
                return int(params["dim"])
            except (TypeError, ValueError):
                continue
    return None


async def _milvus_preflight(settings: Any) -> dict[str, Any]:
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=f"http://{settings.milvus_host}:{settings.milvus_port}", timeout=settings.milvus_timeout_seconds
    )
    collection = str(settings.milvus_collection)
    try:
        exists = await asyncio.to_thread(client.has_collection, collection_name=collection)
    except TypeError:
        exists = await asyncio.to_thread(client.has_collection, collection)
    if not exists:
        raise EvaluationError("configured Milvus collection does not exist")
    description = await asyncio.to_thread(client.describe_collection, collection)
    dimension = _milvus_dimension(description)
    if dimension != int(settings.embedding_dim):
        raise EvaluationError("Milvus collection dimension does not equal embedding_dim")
    row_count = 0
    try:
        stats = await asyncio.to_thread(client.get_collection_stats, collection)
        if isinstance(stats, Mapping):
            row_count = int(stats.get("row_count", 0))
    except Exception:
        # A read-only describe succeeded; stats remains useful diagnostic data,
        # not a reason to invoke any mutating alternative API.
        row_count = 0
    return {"collection": collection, "embedding_dim": dimension, "row_count": row_count}


def _milvus_in_filter(field: str, values: Sequence[str]) -> str:
    quoted = ", ".join(json.dumps(value, ensure_ascii=False) for value in values)
    return f"{field} in [{quoted}]"


async def verify_target_collection_membership(settings: Any, mappings: Mapping[str, TargetMapping]) -> dict[str, int]:
    """Prove every frozen target has its current active chunk in this Collection.

    A matching collection dimension alone does not establish corpus identity.
    The query is read-only and compares both Milvus chunk IDs and source IDs to
    the freshly resolved PostgreSQL targets.
    """
    from pymilvus import MilvusClient

    expected: dict[str, str] = {}
    for mapping in mappings.values():
        for chunk_id in mapping.chunk_ids:
            expected[chunk_id] = mapping.source_id
    if not expected:
        raise TargetResolutionError("no active chunks for frozen targets")
    client = MilvusClient(
        uri=f"http://{settings.milvus_host}:{settings.milvus_port}", timeout=settings.milvus_timeout_seconds
    )
    observed: dict[str, str] = {}
    chunk_ids = sorted(expected)
    for offset in range(0, len(chunk_ids), 200):
        batch = chunk_ids[offset : offset + 200]
        rows = await asyncio.to_thread(
            client.query,
            collection_name=str(settings.milvus_collection),
            filter=_milvus_in_filter("chunk_id", batch),
            output_fields=["chunk_id", "source_id", "source_type"],
            limit=len(batch),
        )
        if not isinstance(rows, list):
            raise EvaluationError("Milvus target identity query returned an invalid response")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if row.get("source_type") == "case":
                observed[str(row.get("chunk_id", ""))] = str(row.get("source_id", ""))
    missing = [chunk_id for chunk_id, source_id in expected.items() if observed.get(chunk_id) != source_id]
    if missing:
        raise TargetResolutionError("selected Milvus collection lacks one or more resolved target chunks")
    return {"expected_target_chunks": len(expected), "matched_target_chunks": len(observed)}


async def _gateway_preflight(
    settings: Any,
    *,
    query_style: str = QUERY_STYLE_NATURAL_LANGUAGE_V1,
) -> dict[str, Any]:
    """Perform small real requests to each required model capability."""
    query_style = _require_query_style(query_style)
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1 and bool(
        getattr(settings, "rag_query_rewrite_enabled", True)
    ):
        raise EvaluationError("structured preflight requires rag_query_rewrite_enabled=false")
    from app.core.embedding_gateway import build_embedding_gateway_settings
    from app.core.gateway import ModelGatewayClient
    from app.core.reranker_gateway import build_reranker_gateway_settings
    from app.rag.reranker import cross_encoder_rerank
    from app.rag.schemas import MergedHit

    evidence: dict[str, Any] = {}
    embedding_client = ModelGatewayClient(settings=build_embedding_gateway_settings(settings))
    rewrite_client: Any = None
    reranker_client: Any = None
    try:
        vectors = await embedding_client.embed(["评测连通性检查"], trace_id="rag-silver-preflight-embedding")
        if len(vectors) != 1 or len(vectors[0]) != int(settings.embedding_dim):
            raise EvaluationError("embedding gateway returned an unexpected vector dimension")
        evidence["embedding"] = {"model": str(settings.embedding_model), "dimension": len(vectors[0])}

        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            # A disabled capability must have no client construction or probe.
            evidence["rewrite"] = {"status": "not_applicable", "enabled": False}
        else:
            from app.core.rewrite_gateway import build_rewrite_gateway_settings
            from app.rag.reasoning_retrieval import rewrite_syndrome_query

            rewrite_client = ModelGatewayClient(settings=build_rewrite_gateway_settings(settings) or settings)
            rewrite_event = component_template("not_attempted")
            observed_rewrite = ObservedGateway(
                rewrite_client,
                component="rewrite",
                event_getter=lambda: rewrite_event,
                default_model=_actual_rewrite_model(settings),
            )
            rewritten = await rewrite_syndrome_query(
                [SimpleNamespace(fact_key="present_illness", value="近日咳嗽咽痒，夜间较明显，伴少量白痰")],
                gateway=observed_rewrite,
                trace_id="rag-silver-preflight-rewrite",
            )
            if rewrite_event.get("status") != "succeeded" or not rewritten:
                raise EvaluationError("rewrite gateway did not complete a successful observed request")
            evidence["rewrite"] = {"model": _actual_rewrite_model(settings), "status": "succeeded"}

        # The production reranker normally receives merged hits from retrieve.
        # A two-item harmless synthetic probe validates the real endpoint and
        # response parser without persisting fake retrieval results.
        reranker_client = ModelGatewayClient(settings=build_reranker_gateway_settings(settings))
        reranker_event = component_template("not_attempted")
        observed_reranker = ObservedGateway(
            reranker_client,
            component="reranker",
            event_getter=lambda: reranker_event,
            default_model=_actual_reranker_model(settings),
        )
        probe_hits = [
            MergedHit(
                chunk_id="preflight-1",
                source_type="case",
                source_id="preflight-source-1",
                title="检索连通性样本一",
                content_snippet="咳嗽咽痒，夜间较明显。",
                vector_score=0.5,
                fulltext_score=0.5,
                is_primary=True,
            ),
            MergedHit(
                chunk_id="preflight-2",
                source_type="case",
                source_id="preflight-source-2",
                title="检索连通性样本二",
                content_snippet="纳差乏力，睡眠不安。",
                vector_score=0.4,
                fulltext_score=0.4,
                is_primary=True,
            ),
        ]
        reranked = await cross_encoder_rerank(
            "咳嗽咽痒夜间明显", probe_hits, gateway=observed_reranker, model=_actual_reranker_model(settings), top_k=2
        )
        if reranker_event.get("status") != "succeeded" or len(reranked) != 2:
            raise EvaluationError("reranker gateway did not return a scoreable response")
        evidence["reranker"] = {"model": _actual_reranker_model(settings), "status": "succeeded"}
        return evidence
    finally:
        for client in (embedding_client, rewrite_client, reranker_client):
            closer = getattr(client, "aclose", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    await closer()


_QUALITY_GATE_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    (
        "ruff_format",
        [
            "uv",
            "run",
            "ruff",
            "format",
            "--check",
            "scripts/build_rag_silver_eval.py",
            "scripts/evaluate_rag_silver.py",
            "scripts/compare_rag_profiles.py",
            "app/core/config.py",
            "app/rag/retriever.py",
            "tests/test_build_rag_silver_eval.py",
            "tests/test_evaluate_rag_silver.py",
            "tests/test_rag_retriever.py",
        ],
    ),
    (
        "ruff_check",
        [
            "uv",
            "run",
            "ruff",
            "check",
            "scripts/build_rag_silver_eval.py",
            "scripts/evaluate_rag_silver.py",
            "scripts/compare_rag_profiles.py",
            "app/core/config.py",
            "app/rag/retriever.py",
            "tests/test_build_rag_silver_eval.py",
            "tests/test_evaluate_rag_silver.py",
            "tests/test_rag_retriever.py",
        ],
    ),
    (
        "mypy",
        [
            "uv",
            "run",
            "mypy",
            "scripts/build_rag_silver_eval.py",
            "scripts/evaluate_rag_silver.py",
            "scripts/compare_rag_profiles.py",
            "app/core/config.py",
            "app/rag/retriever.py",
        ],
    ),
    (
        "target_tests",
        [
            "uv",
            "run",
            "pytest",
            "tests/test_build_rag_silver_eval.py",
            "tests/test_evaluate_rag_silver.py",
            "tests/test_rag_retriever.py",
            "-q",
        ],
    ),
)


def run_local_quality_gates(run_dir: Path) -> list[JsonObject]:
    """Execute and audit the four mandatory local quality commands."""
    outcomes: list[JsonObject] = []
    for name, command in _QUALITY_GATE_COMMANDS:
        try:
            completed = subprocess.run(
                command,
                cwd=_PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            exit_code = completed.returncode
            summary = "passed" if exit_code == 0 else "failed"
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_code = 1
            summary = safe_exception_type(exc)
        outcome = {"name": name, "argv": list(command), "exit_code": exit_code}
        outcomes.append(outcome)
        log_execution(run_dir, "implement", " ".join(command), exit_code, f"{name}: {summary}")
    return outcomes


async def run_preflight(
    dataset_dir: Path,
    run_dir: Path,
    collection: str,
    *,
    profile_name: str = _DEFAULT_RETRIEVAL_PROFILE,
    rewrite_cache_path: Path | None = None,
) -> int:
    """Write structured non-mutating readiness evidence for the frozen run."""
    profile = resolve_retrieval_profile(profile_name)
    ensure_run_directory(run_dir)
    update_state(run_dir, current_phase="preflight", status="running", phase_status="running", collection=collection)
    checks: list[JsonObject] = []
    corpus: dict[str, int] = {}
    quality_commands = run_local_quality_gates(run_dir)
    checks.append(
        _preflight_check(
            "local_quality_gates",
            True,
            "passed" if all(command["exit_code"] == 0 for command in quality_commands) else "failed",
            {"commands": quality_commands},
        )
    )
    smoke: FrozenSplit | None = None
    test: FrozenSplit | None = None
    query_style = QUERY_STYLE_NATURAL_LANGUAGE_V1
    try:
        smoke, test = validate_dataset_pair(dataset_dir)
        query_style = query_style_from_manifest(test.manifest)
        copy_dataset_manifest(dataset_dir, run_dir, smoke, test)
        frozen_evidence: JsonObject = {
            "smoke_count": len(smoke.records),
            "test_count": len(test.records),
            "test_sha256": test.sha256,
        }
        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            frozen_evidence["query_style"] = query_style
        checks.append(
            _preflight_check(
                "frozen_dataset",
                True,
                "passed",
                frozen_evidence,
            )
        )
    except EvaluationError as exc:
        checks.append(_preflight_check("frozen_dataset", True, "failed", error=exc))
    if test is not None:
        source = test.manifest.get("source", {})
        raw = source.get("raw_cases", {}) if isinstance(source, Mapping) else {}
        try:
            raw_source = _path_from_manifest(raw.get("path") if isinstance(raw, Mapping) else None)
            actual_raw_sha = sha256_file(raw_source)
            if actual_raw_sha != (raw.get("sha256") if isinstance(raw, Mapping) else None):
                raise DatasetError("raw source SHA-256 differs from frozen manifest")
            checks.append(_preflight_check("source_file", True, "passed", {"sha256": actual_raw_sha}))
        except (EvaluationError, OSError) as exc:
            checks.append(_preflight_check("source_file", True, "failed", error=exc))
    else:
        checks.append(
            _preflight_check("source_file", True, "failed", error=EvaluationError("frozen dataset unavailable"))
        )

    settings: Any | None = None
    config: JsonObject | None = None
    frozen_rewrite_cache: FrozenRewriteCache | None = None
    try:
        settings = load_contract_settings(collection, profile, query_style=query_style)
        if rewrite_cache_path is not None:
            if smoke is None or test is None:
                raise EvaluationError("cannot bind a Rewrite cache before frozen splits validate")
            if query_style != QUERY_STYLE_NATURAL_LANGUAGE_V1:
                raise EvaluationError("structured evaluation must not bind a Rewrite cache")
            frozen_rewrite_cache = load_frozen_rewrite_cache(
                rewrite_cache_path,
                smoke=smoke,
                test=test,
                settings=settings,
            )
        config = write_or_validate_config(
            run_dir,
            settings,
            profile,
            query_style=query_style,
            frozen_rewrite_cache=frozen_rewrite_cache,
        )
        config_evidence: JsonObject = {
            "collection": collection,
            "profile": profile.name,
            "config_sha256": config["config_sha256"],
        }
        if frozen_rewrite_cache is not None:
            config_evidence["frozen_rewrite_cache_sha256"] = frozen_rewrite_cache.sha256
        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            config_evidence["query_style"] = query_style
        checks.append(
            _preflight_check(
                "effective_contract_configuration",
                True,
                "passed",
                config_evidence,
            )
        )
    except Exception as exc:
        checks.append(_preflight_check("effective_contract_configuration", True, "failed", error=exc))

    if settings is not None:
        try:
            from app.db.session import get_session_factory

            counts = await _corpus_counts(get_session_factory())
            corpus.update(counts)
            checks.append(_preflight_check("postgres_connectivity", True, "passed", counts))
        except Exception as exc:
            checks.append(_preflight_check("postgres_connectivity", True, "failed", error=exc))
        try:
            milvus = await _milvus_preflight(settings)
            corpus["milvus_collection_rows"] = int(milvus["row_count"])
            checks.append(_preflight_check("milvus_collection", True, "passed", milvus))
        except Exception as exc:
            checks.append(_preflight_check("milvus_collection", True, "failed", error=exc))
        try:
            gateway_evidence = await _gateway_preflight(settings, query_style=query_style)
            checks.append(_preflight_check("model_gateways", True, "passed", gateway_evidence))
        except Exception as exc:
            checks.append(_preflight_check("model_gateways", True, "failed", error=exc))
        if smoke is not None and test is not None:
            try:
                resolver = TargetResolver()
                all_keys = [str(row["target_record_key"]) for row in smoke.records + test.records]
                mappings = await resolver.resolve(all_keys)
                unique_sources = {mapping.source_id for mapping in mappings.values()}
                if len(mappings) != FIXED_SMOKE_SIZE + FIXED_TEST_SIZE or len(unique_sources) != len(mappings):
                    raise TargetResolutionError("all frozen targets must map to distinct active cases")
                membership = await verify_target_collection_membership(settings, mappings)
                checks.append(
                    _preflight_check(
                        "frozen_target_resolution",
                        True,
                        "passed",
                        {
                            "resolved": len(mappings),
                            "unique_source_ids": len(unique_sources),
                            "vector_ready": len(mappings),
                            "target_mapping": {key: mapping.source_id for key, mapping in sorted(mappings.items())},
                            **membership,
                        },
                    )
                )
            except Exception as exc:
                checks.append(_preflight_check("frozen_target_resolution", True, "failed", error=exc))
    else:
        for name in ("postgres_connectivity", "milvus_collection", "model_gateways", "frozen_target_resolution"):
            checks.append(_preflight_check(name, True, "failed", error=EvaluationError("settings unavailable")))

    if smoke is not None and test is not None:
        try:
            corpus.update(prepared_corpus_counts(dataset_dir))
        except EvaluationError as exc:
            checks.append(_preflight_check("prepared_corpus_snapshot", True, "failed", error=exc))
        else:
            checks.append(
                _preflight_check(
                    "prepared_corpus_snapshot",
                    True,
                    "passed",
                    {
                        "prepared_total_entries": corpus["prepared_total_entries"],
                        "prepared_case_entries": corpus["prepared_case_entries"],
                    },
                )
            )
    write_or_update_environment(run_dir, corpus=corpus, commands=quality_commands)
    overall = (
        "passed" if checks and all(check["status"] == "passed" for check in checks if check["required"]) else "failed"
    )
    payload: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "updated_at": utc_now(),
        "overall_status": overall,
        "checks": checks,
    }
    write_json_atomic(run_dir / "preflight.json", payload)
    config_sha = config.get("config_sha256") if config else None
    update_state(
        run_dir,
        current_phase="preflight",
        status="running" if overall == "passed" else "failed",
        dataset_sha256=test.sha256 if test is not None else None,
        config_sha256=config_sha if isinstance(config_sha, str) else None,
        collection=collection,
        last_error=None if overall == "passed" else "preflight required check failed",
        phase_status="completed" if overall == "passed" else "failed",
    )
    log_execution(run_dir, "preflight", "evaluate_rag_silver preflight", 0 if overall == "passed" else 1, overall)
    return 0 if overall == "passed" else 1


# ---------------------------------------------------------------------------
# CLI phases: run, report, and verify
# ---------------------------------------------------------------------------


def _preflight_is_passed(run_dir: Path) -> bool:
    path = run_dir / "preflight.json"
    if not path.exists():
        return False
    try:
        preflight = read_json(path)
    except EvaluationError:
        return False
    return preflight.get("overall_status") == "passed" and all(
        check.get("status") == "passed"
        for check in preflight.get("checks", [])
        if isinstance(check, Mapping) and check.get("required")
    )


def parse_arms(value: str) -> list[str]:
    arms = [item.strip() for item in value.split(",") if item.strip()]
    if not arms or any(arm not in {"baseline", "full"} for arm in arms) or len(set(arms)) != len(arms):
        raise EvaluationError("--arms must be a unique comma-separated subset of baseline,full")
    return arms


async def run_split(
    dataset_dir: Path,
    run_dir: Path,
    collection: str,
    *,
    split_name: str,
    arms: Sequence[str],
    top_k: int,
    resume: bool,
    profile_name: str = _DEFAULT_RETRIEVAL_PROFILE,
    rewrite_cache_path: Path | None = None,
) -> int:
    """Execute one frozen split with durable per-arm checkpoints."""
    if split_name not in {"smoke", "test"}:
        raise EvaluationError("run split must be smoke or test")
    if top_k != FIXED_TOP_K:
        raise EvaluationError("formal evaluation top_k is fixed at 8")
    if not _preflight_is_passed(run_dir):
        raise EvaluationError("required preflight has not passed; refusing to score results")
    ensure_run_directory(run_dir)
    smoke_split, test_split = validate_dataset_pair(dataset_dir)
    split = smoke_split if split_name == "smoke" else test_split
    query_style = query_style_from_manifest(split.manifest)
    formal_test_sha = test_split.sha256
    ensure_frozen_code_identity(run_dir)
    profile = resolve_retrieval_profile(profile_name)
    settings = load_contract_settings(collection, profile, query_style=query_style)
    frozen_rewrite_cache: FrozenRewriteCache | None = None
    if rewrite_cache_path is not None:
        if query_style != QUERY_STYLE_NATURAL_LANGUAGE_V1:
            raise EvaluationError("structured evaluation must not bind a Rewrite cache")
        frozen_rewrite_cache = load_frozen_rewrite_cache(
            rewrite_cache_path,
            smoke=smoke_split,
            test=test_split,
            settings=settings,
        )
    config = write_or_validate_config(
        run_dir,
        settings,
        profile,
        query_style=query_style,
        frozen_rewrite_cache=frozen_rewrite_cache,
    )
    config_sha = str(config["config_sha256"])
    state = ensure_run_directory(run_dir)
    expected_dataset_sha = state.get("dataset_sha256")
    expected_config_sha = state.get("config_sha256")
    if expected_dataset_sha not in {None, formal_test_sha}:
        raise EvaluationError("run state is bound to another frozen Test hash")
    if expected_config_sha not in {None, config_sha}:
        raise EvaluationError("run state is bound to another configuration hash")

    phase = "final" if split_name == "test" else "smoke"
    update_state(
        run_dir,
        current_phase=phase,
        status="running",
        dataset_sha256=formal_test_sha,
        config_sha256=config_sha,
        collection=collection,
        phase_status="running",
    )
    resolver = TargetResolver()
    mappings = await resolver.resolve([str(row["target_record_key"]) for row in split.records])
    existing: dict[str, dict[str, JsonObject]] = {}
    for arm in arms:
        path = result_path(run_dir, split_name, arm)
        if path.exists() and path.stat().st_size and not resume:
            raise EvaluationError(f"{path.name} exists; use --resume after integrity validation")
        existing[arm] = read_resume_records(
            path,
            arm=arm,
            split=split_name,
            dataset_sha256=split.sha256,
            config_sha256=config_sha,
            run_dir=run_dir,
            query_style=query_style,
        )
    runtime = build_runtime(settings, query_style=query_style, frozen_rewrite_cache=frozen_rewrite_cache)
    unresolved = 0
    try:
        for row in split.records:
            query_id = str(row["query_id"])
            target = mappings[str(row["target_record_key"])]
            for arm in arms:
                if query_id in existing[arm]:
                    continue
                try:
                    record = await evaluate_with_retries(
                        runtime,
                        run_dir=run_dir,
                        split=split,
                        query_row=row,
                        arm=arm,
                        target=target,
                        config_sha256=config_sha,
                    )
                except ArmTechnicalFailure as exc:
                    unresolved += 1
                    log_execution(run_dir, phase, f"{arm}:{query_id}", 1, redacted_message(exc))
                    continue
                append_jsonl(result_path(run_dir, split_name, arm), record)
                record_component_fallbacks(run_dir, record)
                existing[arm][query_id] = record
    finally:
        await runtime.aclose()
    expected_count = FIXED_SMOKE_SIZE if split_name == "smoke" else FIXED_TEST_SIZE
    complete = unresolved == 0 and all(len(existing[arm]) == expected_count for arm in arms)
    if complete:
        update_state(run_dir, current_phase=phase, status="running", phase_status="completed", last_error=None)
        log_execution(run_dir, phase, "evaluate_rag_silver run", 0, f"{split_name} completed for {','.join(arms)}")
        return 0
    update_state(
        run_dir,
        current_phase=phase,
        status="failed",
        phase_status="failed",
        last_error="unresolved arm technical failure; resume after repair",
    )
    log_execution(run_dir, phase, "evaluate_rag_silver run", 1, "run incomplete due to arm technical failure")
    return 1


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_pp(value: float) -> str:
    return f"{value * 100:+.1f} pp"


def _formal_metric_table(arms: Mapping[str, Any], deltas: Mapping[str, Any]) -> str:
    """Render available Recall cutoffs plus MRR; legacy artifacts may have only @8."""
    baseline = cast(Mapping[str, Any], arms["baseline"])
    full = cast(Mapping[str, Any], arms["full"])
    rows = ["| 指标 | baseline | full | full - baseline | 95% CI |", "|---|---:|---:|---:|---:|"]
    for cutoff in RECALL_CUTOFFS:
        key = f"target_recall_at_{cutoff}"
        delta = deltas.get(key)
        if key not in baseline or key not in full or not isinstance(delta, Mapping):
            continue
        ci = cast(Mapping[str, Any], delta["ci95"])
        rows.append(
            f"| Target Recall@{cutoff} | {_format_pct(float(baseline[key]))} | "
            f"{_format_pct(float(full[key]))} | {_format_pp(float(delta['point']))} | "
            f"[{_format_pp(float(ci['low']))}, {_format_pp(float(ci['high']))}] |"
        )
    mrr = cast(Mapping[str, Any], deltas["mrr"])
    mrr_ci = cast(Mapping[str, Any], mrr["ci95"])
    rows.append(
        f"| MRR | {float(baseline['mrr']):.3f} | {float(full['mrr']):.3f} | {float(mrr['point']):+.3f} | "
        f"[{float(mrr_ci['low']):+.3f}, {float(mrr_ci['high']):+.3f}] |"
    )
    return "\n".join(rows)


def _actual_components_for_resume(config: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    query_style = query_style_from_config(config)
    models = cast(Mapping[str, Any], config.get("models", {}))
    components = cast(Mapping[str, Any], metrics.get("components", {}))
    parts = [str(models.get("embedding", "Embedding")), "Milvus/PostgreSQL 混合检索"]
    rewrite = components.get("full_rewrite", {})
    reranker = components.get("full_cross_encoder", {})
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        parts.insert(0, "fact_key=value 结构化 Query（Rewrite 禁用）")
    elif isinstance(rewrite, Mapping) and float(rewrite.get("coverage", 0.0)) >= 0.95:
        parts.insert(0, "Query Rewrite")
    if isinstance(reranker, Mapping) and float(reranker.get("coverage", 0.0)) >= 0.95:
        parts.append("Cross-Encoder 重排")
    return "、".join(parts)


def is_bge_m3_model(model: str) -> bool:
    normalized = model.lower().replace("_", "-")
    return "bge" in normalized and "m3" in normalized


def make_resume_bullet(
    *,
    run_id: str,
    metrics: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    environment: Mapping[str, Any],
    acceptance: str,
) -> str:
    """Choose the conservative resume language directly from raw metrics."""
    metric_sha = sha256_bytes(compact_json_bytes(metrics)) if metrics is not None else ""
    header = f"source_run: {run_id}\nsource_metrics_sha256: {metric_sha}\nacceptance: {acceptance}\n"
    try:
        query_style = query_style_from_config(config)
    except EvaluationError:
        return header + "selection_rule: R5\n评测输入合同无效，不能用于简历指标。\n"
    if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
        header += "query_style: structured_fact_key_value_v1\nrewrite: disabled_not_applicable\n"
    if acceptance != "PASS" or metrics is None:
        return header + "selection_rule: R5\n实验未完成或未通过验收，不能用于简历指标。\n"
    arms = cast(Mapping[str, Any], metrics["arms"])
    deltas = cast(Mapping[str, Any], metrics["deltas"])
    baseline = cast(Mapping[str, Any], arms["baseline"])
    full = cast(Mapping[str, Any], arms["full"])
    recall_delta = cast(Mapping[str, Any], deltas["target_recall_at_8"])
    mrr_delta = cast(Mapping[str, Any], deltas["mrr"])
    prepared_total = int(cast(Mapping[str, Any], environment.get("corpus", {})).get("prepared_total_entries", 0) or 0)
    corpus_text = "近 3,800 条" if 3600 <= prepared_total <= 3999 else f"{prepared_total} 条"
    components_text = _actual_components_for_resume(config, metrics)
    model = str(cast(Mapping[str, Any], config.get("models", {})).get("embedding", "Embedding"))
    r_point = float(recall_delta["point"])
    m_point = float(mrr_delta["point"])
    r_ci_low = float(cast(Mapping[str, Any], recall_delta["ci95"])["low"])
    m_ci_low = float(cast(Mapping[str, Any], mrr_delta["ci95"])["low"])
    components = cast(Mapping[str, Any], metrics.get("components", {}))
    rewrite_coverage = float(cast(Mapping[str, Any], components.get("full_rewrite", {})).get("coverage", 0.0))
    reranker_coverage = float(cast(Mapping[str, Any], components.get("full_cross_encoder", {})).get("coverage", 0.0))
    if (
        query_style == QUERY_STYLE_NATURAL_LANGUAGE_V1
        and is_bge_m3_model(model)
        and rewrite_coverage >= 0.95
        and reranker_coverage >= 0.95
        and r_point > 0
        and m_point > 0
        and r_ci_low > 0
        and m_ci_low > 0
    ):
        return (
            header
            + "selection_rule: R1\n"
            + f"面向{corpus_text}中医药知识条目构建 Query Rewrite、{model} Embedding、Milvus/PostgreSQL 混合检索与 Cross-Encoder 重排组成的 RAG 管线；"
            + "基于 200 条由医案症状自动构建并经答案泄漏校验的弱监督冻结测试集开展对照实验，"
            + f"相较纯向量基线将目标医案 Recall@8 从 {_format_pct(float(baseline['target_recall_at_8']))} 提升至 {_format_pct(float(full['target_recall_at_8']))}、"
            + f"MRR 从 {float(baseline['mrr']):.3f} 提升至 {float(full['mrr']):.3f}。\n"
        )
    if r_point > 0 and m_point > 0:
        return (
            header
            + "selection_rule: R2\n"
            + f"面向{corpus_text}中医药知识条目构建 {components_text} 组成的 RAG 管线；基于 200 条由医案症状自动构建并经答案泄漏校验的弱监督冻结测试集完成纯向量/混合检索对照评测，"
            + f"Target Recall@8 为 {_format_pct(float(full['target_recall_at_8']))}（基线 {_format_pct(float(baseline['target_recall_at_8']))}，变化 {_format_pp(r_point)}），"
            + f"MRR 为 {float(full['mrr']):.3f}（基线 {float(baseline['mrr']):.3f}，变化 {m_point:+.3f}）。\n"
        )
    if r_point > 0 or m_point > 0:
        return (
            header
            + "selection_rule: R3\n"
            + f"构建 {components_text} RAG 管线，并在 200 条弱监督冻结测试集上完成纯向量/混合检索对照评测；"
            + f"Target Recall@8 为 {_format_pct(float(full['target_recall_at_8']))}（基线 {_format_pct(float(baseline['target_recall_at_8']))}），"
            + f"MRR 为 {float(full['mrr']):.3f}（基线 {float(baseline['mrr']):.3f}）。\n"
        )
    return (
        header
        + "selection_rule: R4\n"
        + f"构建 {components_text} RAG 管线，并基于 200 条由医案症状自动构建、经答案泄漏校验的弱监督冻结测试集完成纯向量与混合检索对照评测，"
        + "记录 Target Recall@8、MRR、组件降级和配对置信区间。\n"
    )


def reporting_acceptance(machine_acceptance: str, state: Mapping[str, Any]) -> str:
    """Translate formal validation into the documented terminal/nonterminal state.

    ``validate_run`` is deliberately binary for Must gates.  A run which has
    not finished its final Test remains ``INCOMPLETE`` rather than being
    relabelled ``INVALID`` merely because its 200 rows do not exist yet.
    """
    if machine_acceptance == "PASS":
        return "PASS"
    if state.get("conclusion") == "BLOCKED" or state.get("status") == "blocked":
        return "BLOCKED"
    phases = state.get("phases")
    final_phase = phases.get("final") if isinstance(phases, Mapping) else {}
    final_status = final_phase.get("status") if isinstance(final_phase, Mapping) else "pending"
    if state.get("conclusion") is None and final_status != "completed":
        return "INCOMPLETE"
    return "INVALID"


def validation_must_decisions(acceptance: str, reasons: Sequence[str]) -> list[JsonObject]:
    """Render explicit section-level Must decisions from fail-closed reasons."""
    groups: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "implementation_and_quality",
            "实现和本地质量（06 §3）",
            ("quality_gate_", "code_identity:"),
        ),
        (
            "frozen_dataset",
            "冻结数据集（06 §4）",
            (
                "dataset:",
                "run_dataset_hash_mismatch",
                "preflight_contract:preflight frozen_dataset",
                "preflight_contract:preflight source_file",
            ),
        ),
        (
            "dependencies_and_corpus",
            "真实依赖与语料一致性（06 §5）",
            ("missing_preflight", "preflight_contract:", "frozen_result_binding:"),
        ),
        (
            "formal_paired_run",
            "正式配对运行及 Smoke 留存（06 §6）",
            (
                "missing_formal_result_jsonl",
                "formal_result_count_",
                "baseline_query_set_",
                "full_query_set_",
                "result_integrity:",
                "missing_smoke_result_jsonl",
                "smoke_result_integrity:",
                "failure_audit:",
            ),
        ),
        (
            "components",
            "组件可观测性和覆盖率（06 §7）",
            ("baseline_vector_", "rewrite_coverage_", "cross_encoder_coverage_"),
        ),
        (
            "metrics_and_statistics",
            "指标、统计与可复算性（06 §8）",
            (
                "metrics_recompute:",
                "persisted_metrics_",
                "missing_metrics_json",
                "invalid_metrics_json:",
                "metrics_validation_",
                "state_conclusion_",
            ),
        ),
        (
            "audit_and_artifacts",
            "审计、保密和产物完整性（06 §9）",
            ("missing_artifact:", "artifact_security:", "missing_environment", "missing_state"),
        ),
        (
            "latest_and_resume",
            "LATEST 与简历声明（06 §10）",
            (),
        ),
    )
    decisions: list[JsonObject] = []
    for identifier, label, prefixes in groups:
        matched = [reason for reason in reasons if any(reason.startswith(prefix) for prefix in prefixes)]
        if identifier == "latest_and_resume" and acceptance != "PASS":
            status = acceptance if acceptance in {"INCOMPLETE", "BLOCKED"} else "FAILED"
        elif matched:
            status = "INCOMPLETE" if acceptance == "INCOMPLETE" else "FAILED"
        elif acceptance == "INCOMPLETE" and identifier in {"formal_paired_run", "metrics_and_statistics"}:
            status = "INCOMPLETE"
        elif acceptance == "BLOCKED":
            status = "BLOCKED"
        else:
            status = "PASSED"
        decisions.append({"id": identifier, "label": label, "status": status, "reasons": matched})
    return decisions


def make_validation_payload(acceptance: str, reasons: Sequence[str]) -> JsonObject:
    return {
        "status": acceptance,
        "reasons": list(reasons),
        "must": validation_must_decisions(acceptance, reasons),
    }


def _report_preflight_evidence(preflight: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if not isinstance(preflight, Mapping):
        return {}
    checks = preflight.get("checks")
    if not isinstance(checks, list):
        return {}
    for check in checks:
        if isinstance(check, Mapping) and check.get("name") == name and isinstance(check.get("evidence"), Mapping):
            return cast(Mapping[str, Any], check["evidence"])
    return {}


def _format_counter(values: Mapping[str, Any] | None) -> str:
    if not isinstance(values, Mapping) or not values:
        return "无"
    return "；".join(f"{key}={value}" for key, value in sorted(values.items(), key=lambda item: str(item[0])))


def run_failure_recovery_summary(
    run_dir: Path,
    *,
    test_dataset_sha256: str,
    smoke_dataset_sha256: str,
    config_sha256: str,
) -> JsonObject:
    """Collect report-only recovery facts without turning partial rows into metrics."""
    records: list[JsonObject] = []
    read_errors: list[str] = []
    for split, arm in (("test", "baseline"), ("test", "full"), ("smoke", "baseline"), ("smoke", "full")):
        path = result_path(run_dir, split, arm)
        if not path.exists():
            continue
        try:
            records.extend(row for row in read_jsonl(path) if row.get("status") == "success")
        except EvaluationError as exc:
            read_errors.append(redacted_message(exc))
    errors, summary = audit_failure_log(
        run_dir,
        test_dataset_sha256=test_dataset_sha256,
        smoke_dataset_sha256=smoke_dataset_sha256,
        config_sha256=config_sha256,
        successful_records=records,
    )
    full_test = [row for row in records if row.get("split") == "test" and row.get("arm") == "full"]
    summary["real_zero_hits"] = sum(1 for row in full_test if row.get("hit_at_8") == 0)
    summary["full_test_success_rows"] = len(full_test)
    try:
        execution_rows = read_jsonl(run_dir / "execution.log")
    except EvaluationError as exc:
        execution_rows = []
        read_errors.append(redacted_message(exc))
    resume_rows = [
        row
        for row in execution_rows
        if row.get("command") == "resume" and row.get("exit_code") == 0 and isinstance(row.get("summary"), str)
    ]
    summary["resume_events"] = len(resume_rows)
    summary["resume_summaries"] = [str(row["summary"]) for row in resume_rows[:3]]
    summary["audit_errors"] = errors + read_errors
    return summary


def _legacy_make_report(
    *,
    run_id: str,
    acceptance: str,
    metrics: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    environment: Mapping[str, Any],
    reasons: Sequence[str],
) -> str:
    """Produce a compact report with the engineering-evaluation limitation explicit."""
    models = cast(Mapping[str, Any], config.get("models", {}))
    milvus = cast(Mapping[str, Any], config.get("milvus", {}))
    corpus = cast(Mapping[str, Any], environment.get("corpus", {}))
    if metrics is None:
        metric_table = "正式指标不可用：未完成 200 条完整配对。"
        paired_table = "配对命中变化不可用。"
        component_table = "组件覆盖率不可用。"
    else:
        arms = cast(Mapping[str, Any], metrics["arms"])
        deltas = cast(Mapping[str, Any], metrics["deltas"])
        metric_table = _formal_metric_table(arms, deltas)
        paired = cast(Mapping[str, Any], metrics["paired_hits"])
        paired_table = (
            "| baseline Hit@8 | full Hit@8 | Query 数 |\n|---:|---:|---:|\n"
            f"| 0 | 0 | {paired['0_0']} |\n| 0 | 1 | {paired['0_1']} |\n| 1 | 0 | {paired['1_0']} |\n| 1 | 1 | {paired['1_1']} |"
        )
        components = cast(Mapping[str, Any], metrics["components"])
        rows = []
        for label, key in (
            ("baseline 向量检索", "baseline_vector"),
            ("Query Rewrite", "full_rewrite"),
            ("full 向量检索", "full_vector"),
            ("PostgreSQL 全文检索", "full_fulltext"),
            ("Cross-Encoder", "full_cross_encoder"),
        ):
            value = cast(Mapping[str, Any], components[key])
            rows.append(
                f"| {label} | {value['success']} / {value['denominator']} | {_format_pct(float(value['coverage']))} |"
            )
        component_table = "| 组件 | 成功 / 分母 | 覆盖率 |\n|---|---:|---:|\n" + "\n".join(rows)
    reason_text = "；".join(reasons) if reasons else "所有机器验收项通过。"
    return f"""# RAG 检索对照实验报告

## 结论

- 运行：{run_id}
- 结论：{acceptance}
- 正式数据：200 条基于医案症状自动构建、经过答案泄漏校验的弱监督工程评测 Query
- 范围：仅 source_type=case，最终 top_k=8
- 限制：本实验衡量是否找回产生 Query 的来源医案；不评价辨证、处方或临床结论正确性。

## 可复现性

| 项目 | 值 |
|---|---|
| Git commit / dirty | {environment.get("git_commit", "unknown")} / {environment.get("git_dirty", "unknown")} |
| Milvus Collection | {milvus.get("collection", "unknown")} |
| Embedding 模型 / 维度 | {models.get("embedding", "unknown")} / {milvus.get("embedding_dim", "unknown")} |
| Rewrite 模型 | {models.get("rewrite", "unknown")} |
| Reranker 模型 | {models.get("reranker", "unknown")} |
| 配置哈希 | {config.get("config_sha256", "unknown")} |
| Bootstrap | 10,000 次配对 Query 级，seed=20260807，95% CI |

## 语料与冻结数据

| 统计项 | 实际值 |
|---|---:|
| prepared 总知识条目 | {corpus.get("prepared_total_entries", 0)} |
| prepared case 条目 | {corpus.get("prepared_case_entries", 0)} |
| active case 主表记录 | {corpus.get("active_case_rows", 0)} |
| active case chunks | {corpus.get("active_case_chunks", 0)} |
| Smoke / Test Query | 20 / 200 |

## A/B 合同

| 组别 | 实际链路 |
|---|---|
| baseline | 原始 Query -> {models.get("embedding", "embedding")} -> Milvus 纯向量检索 |
| full | Query Rewrite -> Milvus + PostgreSQL 混合检索 -> Cross-Encoder -> top 8 |

## 正式指标

{metric_table}

Target Recall@8 对单目标数据等价于 Hit@8；MRR 取目标医案来源首次出现 rank 的倒数。

## 配对命中变化

{paired_table}

## 组件执行与降级

{component_table}

## 有效性判定

{reason_text}
"""


def make_report(
    *,
    run_id: str,
    acceptance: str,
    metrics: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    environment: Mapping[str, Any],
    reasons: Sequence[str],
    dataset_sha256: str | None = None,
    preflight: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    failure_summary: Mapping[str, Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    resume_bullet: str | None = None,
) -> str:
    """Render the complete report contract without promoting partial results.

    A nonempty but incomplete pair set is useful recovery evidence, not a
    formal metric.  Consequently, every numeric A/B table below is gated on
    exactly 200 immutable Test pairs.
    """
    models = cast(Mapping[str, Any], config.get("models", {}))
    milvus = cast(Mapping[str, Any], config.get("milvus", {}))
    corpus = cast(Mapping[str, Any], environment.get("corpus", {}))
    rewrite_config = cast(Mapping[str, Any], config.get("rewrite", {}))
    frozen_rewrite_cache_sha = rewrite_config.get("frozen_cache_sha256")
    frozen_rewrite_replay = rewrite_config.get("execution_mode") == "frozen_replay" and isinstance(
        frozen_rewrite_cache_sha, str
    )
    try:
        query_style = query_style_from_config(config)
    except EvaluationError:
        query_style = "invalid"
    structured_query_style = query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1
    query_contract_line = (
        "- 输入合同：`fact_key=value；…` 结构化事实键；Rewrite 已禁用且不适用（未调用网关）。\n"
        if structured_query_style
        else ""
    )
    rewrite_model_display = (
        "disabled / not applicable（未调用 Rewrite gateway）"
        if structured_query_style
        else models.get("rewrite", "unknown")
    )
    full_arm_contract = (
        "原始 fact_key=value；… Query（Rewrite 已禁用 / 不适用） -> Milvus + PostgreSQL 混合检索 -> Cross-Encoder -> top 8"
        if structured_query_style
        else (
            f"冻结回放同一 Rewrite（cache={frozen_rewrite_cache_sha}） -> Milvus + PostgreSQL 混合检索 -> "
            "Cross-Encoder -> top 8"
            if frozen_rewrite_replay
            else "Query Rewrite -> Milvus + PostgreSQL 混合检索 -> Cross-Encoder -> top 8"
        )
    )
    state = state or {}
    failure_summary = failure_summary or {}
    if validation is None:
        from_metrics = metrics.get("validation") if isinstance(metrics, Mapping) else None
        validation = from_metrics if isinstance(from_metrics, Mapping) else make_validation_payload(acceptance, reasons)
    validation_reasons = validation.get("reasons") if isinstance(validation, Mapping) else reasons
    if not isinstance(validation_reasons, list):
        validation_reasons = list(reasons)
    decisions = validation.get("must") if isinstance(validation, Mapping) else None
    if not isinstance(decisions, list):
        decisions = validation_must_decisions(acceptance, [str(reason) for reason in validation_reasons])

    formal_metrics: Mapping[str, Any] | None = None
    if isinstance(metrics, Mapping) and metrics.get("n_pairs") == FIXED_TEST_SIZE:
        arms = metrics.get("arms")
        deltas = metrics.get("deltas")
        paired = metrics.get("paired_hits")
        components = metrics.get("components")
        if all(isinstance(value, Mapping) for value in (arms, deltas, paired, components)):
            formal_metrics = metrics
    test_sha = dataset_sha256 or (
        str(formal_metrics.get("dataset_sha256")) if isinstance(formal_metrics, Mapping) else "不可用"
    )

    frozen_evidence = _report_preflight_evidence(preflight, "frozen_dataset")
    target_evidence = _report_preflight_evidence(preflight, "frozen_target_resolution")
    prepared_evidence = _report_preflight_evidence(preflight, "prepared_corpus_snapshot")
    source_evidence = _report_preflight_evidence(preflight, "source_file")
    preflight_errors = (
        preflight_contract_errors(preflight) if isinstance(preflight, Mapping) else ["缺少 preflight.json"]
    )
    leakage_hash_summary = (
        "通过：冻结构建器只读校验已覆盖长度、答案泄漏、重复/近重复与 raw/prepared/manifest 哈希。"
        if not preflight_errors
        else "未通过或待完成：" + "；".join(preflight_errors[:3])
    )
    mapping_count = target_evidence.get("resolved")
    unique_count = target_evidence.get("unique_source_ids")
    vector_ready = target_evidence.get("vector_ready")
    if (
        mapping_count == FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        and unique_count == FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
        and vector_ready == FIXED_SMOKE_SIZE + FIXED_TEST_SIZE
    ):
        target_mapping_text = "200 / 200 Test（预检同时解析 Smoke+Test 共 220 / 220 个唯一 active case target）"
    else:
        target_mapping_text = "未完成或无效（预检 target 映射证据不足）"

    if formal_metrics is None:
        metric_table = "正式指标不可用：未完成 200 条 Test 的完整配对；不会展示部分行或 Smoke 的数值。"
        paired_table = "配对命中变化不可用：正式 Test 尚未形成 200 对。"
        component_table = "正式组件覆盖率不可用：部分执行记录不作为正式覆盖率或指标呈现。"
        formal_data_line = "正式结果：尚未完成 200 条 Test 完整配对；本报告不展示或推断正式指标。"
    else:
        arms = cast(Mapping[str, Any], formal_metrics["arms"])
        deltas = cast(Mapping[str, Any], formal_metrics["deltas"])
        metric_table = _formal_metric_table(arms, deltas)
        paired = cast(Mapping[str, Any], formal_metrics["paired_hits"])
        paired_table = (
            "| baseline Hit@8 | full Hit@8 | Query 数 |\n|---:|---:|---:|\n"
            f"| 0 | 0 | {paired['0_0']} |\n| 0 | 1 | {paired['0_1']} |\n"
            f"| 1 | 0 | {paired['1_0']} |\n| 1 | 1 | {paired['1_1']} |"
        )
        components = cast(Mapping[str, Any], formal_metrics["components"])
        degradations = cast(Mapping[str, Any], formal_metrics.get("degradation_counts", {}))
        fallbacks = cast(Mapping[str, Any], failure_summary.get("component_fallbacks", {}))
        component_rows: list[str] = []
        for label, key, fallback_key, threshold in (
            ("baseline 向量检索", "baseline_vector", None, 1.0),
            ("Query Rewrite", "full_rewrite", "rewrite_fallback", 0.95),
            ("full 向量检索", "full_vector", "vector_fallback_to_fulltext", None),
            ("PostgreSQL 全文检索", "full_fulltext", None, None),
            ("Cross-Encoder", "full_cross_encoder", "reranker", 0.95),
        ):
            component = cast(Mapping[str, Any], components[key])
            if structured_query_style and key == "full_rewrite":
                component_rows.append("| Query Rewrite（禁用 / 不适用） | N/A | N/A | 0 | 未调用 |")
                continue
            fallback_count = 0
            if fallback_key == "reranker":
                fallback_count = int(degradations.get("reranker_fallback", 0)) + int(
                    degradations.get("reranker_not_applied_insufficient_candidates", 0)
                )
                failure_component = "reranker"
            elif fallback_key is not None:
                fallback_count = int(degradations.get(fallback_key, 0))
                failure_component = key.replace("full_", "")
            else:
                failure_component = "vector" if key == "baseline_vector" else key.replace("full_", "")
            fallback_count += int(fallbacks.get(failure_component, 0))
            coverage = float(component["coverage"])
            if threshold is None:
                conclusion = "已观测" if int(component["denominator"]) == FIXED_TEST_SIZE else "无效"
            else:
                conclusion = "通过" if coverage >= threshold else "未通过"
            component_rows.append(
                f"| {label} | {component['success']} / {component['denominator']} | {_format_pct(coverage)} | {fallback_count} | {conclusion} |"
            )
        component_table = (
            "| 组件 | 成功 / 分母 | 覆盖率 | fallback / 未应用 | 结论 |\n|---|---:|---:|---:|---|\n"
            + "\n".join(component_rows)
        )
        formal_data_line = "正式结果：已完成 200 条 Test 完整配对；以下数值由两份正式 JSONL 重算。"
        if acceptance != "PASS":
            formal_data_line += "该数值仅为验收预览，verify 前或 INVALID 状态下均不能用于简历指标。"

    candidate_audit_table = "候选追踪未记录；不能区分初召回遗漏与重排淘汰。"
    if formal_metrics is not None:
        candidate_audit = formal_metrics.get("candidate_audit")
        if isinstance(candidate_audit, Mapping) and int(candidate_audit.get("trace_records", 0) or 0) > 0:
            stages = candidate_audit.get("stages")
            if isinstance(stages, Mapping):
                rows: list[str] = []
                labels = {
                    "vector": "向量候选",
                    "fulltext": "全文候选",
                    "merged": "去重并集",
                    "reranker_pool": "Cross-Encoder 候选池",
                }
                for key in ("vector", "fulltext", "merged", "reranker_pool"):
                    stage = stages.get(key)
                    if not isinstance(stage, Mapping):
                        continue
                    counts = stage.get("candidate_count")
                    if not isinstance(counts, Mapping):
                        continue
                    rows.append(
                        f"| {labels[key]} | {stage.get('target_present', 0)} / {stage.get('denominator', 0)} | "
                        f"{_format_pct(float(stage.get('coverage', 0.0)))} | "
                        f"{counts.get('min', 0)} / {counts.get('median', 0)} / {counts.get('max', 0)} |"
                    )
                if rows:
                    source_diversity_line = ""
                    unique_sources = candidate_audit.get("reranker_pool_unique_source_count")
                    if isinstance(unique_sources, Mapping):
                        source_diversity_line = (
                            "\n\nCross-Encoder 候选池中的不同来源数（min / median / max）："
                            f"{unique_sources.get('min', 0)} / {unique_sources.get('median', 0)} / "
                            f"{unique_sources.get('max', 0)}。"
                        )
                    query_view_line = ""
                    query_views = candidate_audit.get("query_views")
                    if isinstance(query_views, Mapping):
                        counts = query_views.get("count")
                        if isinstance(counts, Mapping):
                            query_view_line = (
                                "\n\nQuery 视图数（min / median / max）："
                                f"{counts.get('min', 0)} / {counts.get('median', 0)} / {counts.get('max', 0)}；"
                                "实际执行双视图 RRF："
                                f"{query_views.get('dual_rrf_applied', 0)} / {query_views.get('denominator', 0)}。"
                            )
                    candidate_audit_table = (
                        "trace 覆盖："
                        f"{candidate_audit.get('trace_records', 0)} / {candidate_audit.get('denominator', 0)}；"
                        "候选数列为 min / median / max。\n\n"
                        "| 阶段 | target 已进入候选 | 覆盖率 | 候选数 |\n|---|---:|---:|---:|\n"
                        + "\n".join(rows)
                        + "\n\n"
                        "实际调用 Cross-Encoder："
                        f"{candidate_audit.get('reranker_attempted', 0)} / {candidate_audit.get('trace_records', 0)}。\n\n"
                        "目标进入 Cross-Encoder 候选池但未进最终 top8："
                        f"{candidate_audit.get('reranker_pool_target_present_final_miss', 0)} 条。"
                        + source_diversity_line
                        + query_view_line
                    )

    arm_failures = int(failure_summary.get("arm_technical_failures", 0) or 0)
    unresolved = int(failure_summary.get("unresolved_arm_technical_failures", 0) or 0)
    fallback_text = _format_counter(cast(Mapping[str, Any] | None, failure_summary.get("component_fallbacks")))
    fallback_text = f"degradation_counts：{_format_counter(cast(Mapping[str, Any] | None, (formal_metrics or {}).get('degradation_counts')))}；failure log：{fallback_text}"
    if formal_metrics is None or int(failure_summary.get("full_test_success_rows", 0) or 0) != FIXED_TEST_SIZE:
        zero_hits = "不可用（正式 full Test 未完成 200 条）"
    else:
        zero_hits = str(int(failure_summary.get("real_zero_hits", 0) or 0))
    resume_events = int(failure_summary.get("resume_events", 0) or 0)
    resume_details = failure_summary.get("resume_summaries", [])
    if resume_events:
        resume_text = f"发生 {resume_events} 次；" + "；".join(str(item) for item in resume_details)
    else:
        resume_text = "未观察到断点恢复事件"
    must_rows: list[str] = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        decision_reasons = decision.get("reasons")
        detail = (
            "；".join(str(item) for item in decision_reasons)
            if isinstance(decision_reasons, list) and decision_reasons
            else "证据已通过"
        )
        must_rows.append(
            f"| {decision.get('label', decision.get('id', 'Must'))} | {decision.get('status', 'UNKNOWN')} | {detail} |"
        )
    must_table = "| Must | 判定 | 证据或原因 |\n|---|---|---|\n" + "\n".join(must_rows)
    blocked_detail = ""
    if acceptance == "BLOCKED":
        blocked_detail = "\n- 唯一待外部输入：" + str(state.get("last_error") or (reasons[0] if reasons else "未记录"))
    bullet_text = (
        resume_bullet
        if resume_bullet is not None
        else make_resume_bullet(
            run_id=run_id,
            metrics=formal_metrics,
            config=config,
            environment=environment,
            acceptance=acceptance,
        )
    )
    audit_errors = failure_summary.get("audit_errors", [])
    audit_note = "；".join(str(item) for item in audit_errors) if audit_errors else "无额外失败日志审计错误"
    return f"""# RAG 检索对照实验报告

## 结论

- 运行：{run_id}
- 结论：{acceptance}
- {formal_data_line}
- 数据集：rag-silver-v1，test.jsonl SHA-256 = {test_sha}
{query_contract_line}- 范围：仅 source_type=case，最终 top_k=8
- 限制：本实验衡量“是否找回产生 Query 的来源医案”，不评价中医辨证、处方或临床结论正确性。{blocked_detail}

## 可复现性

| 项目 | 值 |
|---|---|
| Git commit / dirty | {environment.get("git_commit", "unknown")} / {environment.get("git_dirty", "unknown")} |
| PostgreSQL / prepared 语料快照 | prepared={prepared_evidence.get("prepared_total_entries", corpus.get("prepared_total_entries", 0))}；case={prepared_evidence.get("prepared_case_entries", corpus.get("prepared_case_entries", 0))}；active_case={corpus.get("active_case_rows", 0)}；active_chunks={corpus.get("active_case_chunks", 0)} |
| Milvus Collection | {milvus.get("collection", "unknown")} |
| Embedding 模型 / 维度 | {models.get("embedding", "unknown")} / {milvus.get("embedding_dim", "unknown")} |
| Rewrite 模型 | {rewrite_model_display} |
| Rewrite 执行 | {f"冻结回放 cache SHA-256={frozen_rewrite_cache_sha}" if frozen_rewrite_replay else "每条 Query 实时调用"} |
| Reranker 模型 | {models.get("reranker", "unknown")} |
| 配置哈希 | {config.get("config_sha256", "unknown")} |
| Bootstrap | 10,000 次配对 Query 级，seed=20260807，95% CI |

## 语料与冻结数据

| 统计项 | 实际值 |
|---|---:|
| prepared 总知识条目 | {corpus.get("prepared_total_entries", prepared_evidence.get("prepared_total_entries", 0))} |
| prepared case 条目 | {corpus.get("prepared_case_entries", prepared_evidence.get("prepared_case_entries", 0))} |
| active case 主表记录 | {corpus.get("active_case_rows", 0)} |
| active case chunks | {corpus.get("active_case_chunks", 0)} |
| Smoke / Test Query | {frozen_evidence.get("smoke_count", "未验证")} / {frozen_evidence.get("test_count", "未验证")} |
| Test target 映射 | {target_mapping_text} |
| 泄漏、重复和哈希验证 | {leakage_hash_summary} |
| 原始 source SHA-256 | {source_evidence.get("sha256", "未验证")} |

## A/B 合同

| 组别 | 实际链路 |
|---|---|
| baseline | 原始 Query -> {models.get("embedding", "embedding")} -> Milvus 纯向量检索 |
| full | {full_arm_contract} |

## 正式指标

{metric_table}

Target Recall@8 对单目标数据等价于 Hit@8；MRR 取目标医案来源首次出现 rank 的倒数。

## 配对命中变化

{paired_table}

## 候选层诊断

{candidate_audit_table}

## 组件执行与降级

{component_table}

## 失败与恢复

- arm 级技术失败：{arm_failures}；最终未解决：{unresolved}
- 组件降级：{fallback_text}
- 真实零命中：{zero_hits}
- 断点恢复：{resume_text}
- 失败日志审计：{audit_note}

## 有效性判定

{must_table}

## 简历文案

```text
{bullet_text.rstrip()}
```
"""


async def run_report(dataset_dir: Path, run_dir: Path, bootstrap_samples: int, seed: int) -> int:
    if bootstrap_samples != FIXED_BOOTSTRAP_SAMPLES or seed != FIXED_SEED:
        raise EvaluationError("bootstrap samples and seed are fixed by the experiment contract")
    state = ensure_run_directory(run_dir)
    config = read_json(run_dir / "config.redacted.json") if (run_dir / "config.redacted.json").exists() else {}
    config_sha = str(config.get("config_sha256", ""))
    smoke: FrozenSplit | None = None
    test: FrozenSplit | None = None
    query_style = QUERY_STYLE_NATURAL_LANGUAGE_V1
    with contextlib.suppress(EvaluationError):
        smoke, test = validate_dataset_pair(dataset_dir)
        if test is not None:
            query_style = query_style_from_manifest(test.manifest)
    metrics: JsonObject | None = None
    if smoke is not None and test is not None and config_sha:
        baseline_path = result_path(run_dir, "test", "baseline")
        full_path = result_path(run_dir, "test", "full")
        try:
            baseline = _result_records_for_verify(
                baseline_path,
                arm="baseline",
                dataset_sha256=test.sha256,
                config_sha256=config_sha,
                query_style=query_style,
            )
            full = _result_records_for_verify(
                full_path,
                arm="full",
                dataset_sha256=test.sha256,
                config_sha256=config_sha,
                query_style=query_style,
            )
            expected_ids = {str(row["query_id"]) for row in test.records}
            if (
                len(baseline) == FIXED_TEST_SIZE
                and len(full) == FIXED_TEST_SIZE
                and {str(row.get("query_id")) for row in baseline} == expected_ids
                and {str(row.get("query_id")) for row in full} == expected_ids
            ):
                metrics = compute_metrics(
                    baseline,
                    full,
                    run_id=run_dir.name,
                    dataset_sha256=test.sha256,
                    config_sha256=config_sha,
                    bootstrap_samples=bootstrap_samples,
                    seed=seed,
                )
                metrics["artifact_sha256"] = {
                    "baseline_results": sha256_file(baseline_path),
                    "full_results": sha256_file(full_path),
                }
        except EvaluationError:
            # validate_run records the precise integrity reason.  Do not turn
            # malformed or partial rows into a reportable formal metric here.
            metrics = None
    staged_status = (
        str(state.get("conclusion")) if state.get("conclusion") in {"PASS", "INVALID", "BLOCKED"} else "INCOMPLETE"
    )
    if metrics is None:
        metrics = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_dir.name,
            "dataset_version": DATASET_VERSION,
            "dataset_sha256": test.sha256 if test is not None else None,
            "config_sha256": config_sha or None,
            "formal_metrics_available": False,
        }
        if query_style == QUERY_STYLE_STRUCTURED_FACT_KEY_VALUE_V1:
            metrics["query_style"] = query_style
    metrics["validation"] = make_validation_payload(staged_status, ["report_recomputation_in_progress"])
    write_json_atomic(run_dir / "metrics.json", metrics)
    machine_acceptance, reasons, recomputed = validate_run(
        dataset_dir=dataset_dir, run_dir=run_dir, require_final_artifacts=False
    )
    if recomputed is not None:
        metrics = recomputed
    acceptance = reporting_acceptance(machine_acceptance, state)
    # Only verify may emit a terminal PASS.  Report remains a transparent
    # preview even when every recomputable Must is ready for final acceptance.
    if machine_acceptance == "PASS" and state.get("conclusion") != "PASS":
        acceptance = "INCOMPLETE"
        reasons = ["awaiting_final_verify"]
    validation = make_validation_payload(acceptance, reasons)
    metrics["validation"] = validation
    write_json_atomic(run_dir / "metrics.json", metrics)
    environment = write_or_update_environment(run_dir)
    preflight = read_json(run_dir / "preflight.json") if (run_dir / "preflight.json").exists() else {}
    failure_summary = run_failure_recovery_summary(
        run_dir,
        test_dataset_sha256=test.sha256 if test is not None else "",
        smoke_dataset_sha256=smoke.sha256 if smoke is not None else "",
        config_sha256=config_sha,
    )
    bullet = make_resume_bullet(
        run_id=run_dir.name,
        metrics=metrics if metrics.get("n_pairs") == FIXED_TEST_SIZE else None,
        config=config,
        environment=environment,
        acceptance=acceptance,
    )
    write_bytes_atomic(run_dir / "resume-bullet.md", bullet.encode("utf-8"))
    report = make_report(
        run_id=run_dir.name,
        acceptance=acceptance,
        metrics=metrics,
        config=config,
        environment=environment,
        reasons=reasons,
        dataset_sha256=test.sha256 if test is not None else None,
        preflight=preflight,
        state=state,
        failure_summary=failure_summary,
        validation=validation,
        resume_bullet=bullet,
    )
    write_bytes_atomic(run_dir / "report.md", report.encode("utf-8"))
    if acceptance == "PASS":
        update_state(
            run_dir,
            current_phase="report",
            status="completed",
            conclusion="PASS",
            phase_status="completed",
            last_error=None,
        )
    elif acceptance == "INCOMPLETE":
        update_state(
            run_dir,
            current_phase="report",
            status="running",
            conclusion=None,
            phase_status="completed",
            last_error="awaiting final verify" if machine_acceptance == "PASS" else "; ".join(reasons)[:180],
        )
    elif acceptance == "BLOCKED":
        update_state(
            run_dir,
            current_phase="report",
            status="blocked",
            conclusion="BLOCKED",
            phase_status="blocked",
            last_error="; ".join(reasons)[:180],
        )
    else:
        update_state(
            run_dir,
            current_phase="report",
            status="failed",
            conclusion="INVALID",
            phase_status="failed",
            last_error="; ".join(reasons)[:180],
        )
    log_execution(run_dir, "report", "evaluate_rag_silver report", 0 if machine_acceptance == "PASS" else 1, acceptance)
    return 0 if machine_acceptance == "PASS" else 1


def write_latest_success(run_dir: Path, dataset_sha: str) -> None:
    latest = _PROJECT_ROOT / "docs" / "05_RAG效果评测" / "LATEST.md"
    relative = f"runs/{run_dir.name}"
    content = (
        "# Latest successful RAG evaluation\n"
        f"run_id: {run_dir.name}\n"
        f"path: {relative}\n"
        "conclusion: PASS\n"
        f"dataset_sha256: {dataset_sha}\n"
        f"verified_at: {utc_now()}\n"
    )
    write_bytes_atomic(latest, content.encode("utf-8"))


def run_verify(dataset_dir: Path, run_dir: Path) -> int:
    state = ensure_run_directory(run_dir)
    machine_acceptance, reasons, metrics = validate_run(
        dataset_dir=dataset_dir, run_dir=run_dir, require_final_artifacts=True
    )
    acceptance = reporting_acceptance(machine_acceptance, state)
    metrics_path = run_dir / "metrics.json"
    if metrics is None:
        metrics = (
            read_json(metrics_path)
            if metrics_path.exists()
            else {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_dir.name,
                "dataset_version": DATASET_VERSION,
                "formal_metrics_available": False,
            }
        )
    validation = make_validation_payload(acceptance, reasons)
    metrics["validation"] = validation
    write_json_atomic(metrics_path, metrics)
    config = read_json(run_dir / "config.redacted.json") if (run_dir / "config.redacted.json").exists() else {}
    environment = read_json(run_dir / "environment.json") if (run_dir / "environment.json").exists() else {}
    preflight = read_json(run_dir / "preflight.json") if (run_dir / "preflight.json").exists() else {}
    smoke: FrozenSplit | None = None
    test: FrozenSplit | None = None
    with contextlib.suppress(EvaluationError):
        smoke, test = validate_dataset_pair(dataset_dir)
    config_sha = str(config.get("config_sha256", ""))
    failure_summary = run_failure_recovery_summary(
        run_dir,
        test_dataset_sha256=test.sha256 if test is not None else "",
        smoke_dataset_sha256=smoke.sha256 if smoke is not None else "",
        config_sha256=config_sha,
    )
    formal_metrics = metrics if metrics.get("n_pairs") == FIXED_TEST_SIZE else None
    bullet = make_resume_bullet(
        run_id=run_dir.name,
        metrics=formal_metrics,
        config=config,
        environment=environment,
        acceptance=acceptance,
    )
    write_bytes_atomic(run_dir / "resume-bullet.md", bullet.encode("utf-8"))
    report = make_report(
        run_id=run_dir.name,
        acceptance=acceptance,
        metrics=formal_metrics,
        config=config,
        environment=environment,
        reasons=reasons,
        dataset_sha256=test.sha256 if test is not None else None,
        preflight=preflight,
        state=state,
        failure_summary=failure_summary,
        validation=validation,
        resume_bullet=bullet,
    )
    write_bytes_atomic(run_dir / "report.md", report.encode("utf-8"))
    if acceptance == "PASS":
        update_state(
            run_dir,
            current_phase="accept",
            status="completed",
            conclusion="PASS",
            phase_status="completed",
            last_error=None,
        )
    elif acceptance == "BLOCKED":
        update_state(
            run_dir,
            current_phase="accept",
            status="blocked",
            conclusion="BLOCKED",
            phase_status="blocked",
            last_error="; ".join(reasons)[:180],
        )
    elif acceptance == "INCOMPLETE":
        update_state(
            run_dir,
            current_phase="accept",
            status="running",
            conclusion=None,
            phase_status="completed",
            last_error="; ".join(reasons)[:180],
        )
    else:
        update_state(
            run_dir,
            current_phase="accept",
            status="failed",
            conclusion="INVALID",
            phase_status="failed",
            last_error="; ".join(reasons)[:180],
        )
    if acceptance == "PASS":
        write_latest_success(run_dir, test.sha256 if test is not None else "")
    log_execution(
        run_dir,
        "accept",
        "evaluate_rag_silver verify",
        0 if acceptance == "PASS" else 1,
        acceptance,
    )
    return 0 if acceptance == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evaluate_rag_silver", description="Run the frozen rag-silver-v1 evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze-rewrites", help="freeze actual Rewrite outputs for fair profile ablations")
    freeze.add_argument("--dataset-dir", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--collection", type=str, required=True)
    freeze.add_argument("--profile", choices=sorted(_RETRIEVAL_PROFILES), default=_DEFAULT_RETRIEVAL_PROFILE)

    preflight = subparsers.add_parser("preflight", help="read-only real service and corpus preflight")
    preflight.add_argument("--dataset-dir", type=Path, required=True)
    preflight.add_argument("--run-dir", type=Path, required=True)
    preflight.add_argument("--collection", type=str, required=True)
    preflight.add_argument("--profile", choices=sorted(_RETRIEVAL_PROFILES), default=_DEFAULT_RETRIEVAL_PROFILE)
    preflight.add_argument("--rewrite-cache", type=Path)

    run = subparsers.add_parser("run", help="run one frozen split")
    run.add_argument("--dataset-dir", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--collection", type=str, required=True)
    run.add_argument("--profile", choices=sorted(_RETRIEVAL_PROFILES), default=_DEFAULT_RETRIEVAL_PROFILE)
    run.add_argument("--split", choices=("smoke", "test"), required=True)
    run.add_argument("--arms", type=str, required=True)
    run.add_argument("--top-k", type=int, required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--rewrite-cache", type=Path)

    report = subparsers.add_parser("report", help="recompute formal metrics and report")
    report.add_argument("--dataset-dir", type=Path, required=True)
    report.add_argument("--run-dir", type=Path, required=True)
    report.add_argument("--bootstrap-samples", type=int, required=True)
    report.add_argument("--seed", type=int, required=True)

    verify = subparsers.add_parser("verify", help="machine-verify formal artifacts")
    verify.add_argument("--dataset-dir", type=Path, required=True)
    verify.add_argument("--run-dir", type=Path, required=True)
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "freeze-rewrites":
        return await freeze_rewrites(args.dataset_dir, args.output, args.collection, profile_name=args.profile)
    if args.command == "preflight":
        return await run_preflight(
            args.dataset_dir,
            args.run_dir,
            args.collection,
            profile_name=args.profile,
            rewrite_cache_path=args.rewrite_cache,
        )
    if args.command == "run":
        return await run_split(
            args.dataset_dir,
            args.run_dir,
            args.collection,
            split_name=args.split,
            arms=parse_arms(args.arms),
            top_k=args.top_k,
            resume=bool(args.resume),
            profile_name=args.profile,
            rewrite_cache_path=args.rewrite_cache,
        )
    if args.command == "report":
        return await run_report(args.dataset_dir, args.run_dir, args.bootstrap_samples, args.seed)
    if args.command == "verify":
        return run_verify(args.dataset_dir, args.run_dir)
    raise EvaluationError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except EvaluationError as exc:
        print(f"evaluation failed: {redacted_message(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("evaluation interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - CLI last-resort protection
        print(f"evaluation unexpected failure: {safe_exception_type(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
