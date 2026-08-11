"""Compare two completed full RAG profiles on exactly paired frozen Queries.

This utility deliberately compares the two ``full`` arms directly.  Comparing
each profile's uplift against its own pure-vector arm would combine unrelated
run-to-run effects and is not a valid ablation comparison.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts import evaluate_rag_silver as evaluator

JsonObject = dict[str, Any]
_REFERENCE_PROFILE = "current-v12"
_CANDIDATE_PROFILES = frozenset(
    {
        "current-v12-source-diverse",
        "current-v12-dual-rrf",
        "current-v12-dual-rrf-source-diverse",
        "current-v12-expanded20",
        "current-v12-dual-full",
    }
)
_SHARED_RETRIEVAL_FIELDS = (
    "source_types",
    "final_top_k",
    "fulltext_lexical_enabled",
    "fulltext_lexical_max_terms",
    "vector_weight",
    "fulltext_weight",
    "source_priority_weight",
    "reranker_provider",
    "reranker_enabled",
    "reranker_fulltext_quota",
    "reranker_final_top_k",
    "embedding_cache_ttl_seconds",
)


@dataclass(frozen=True)
class LoadedRun:
    config: JsonObject
    manifest: JsonObject
    environment: JsonObject
    metrics: JsonObject
    records: list[JsonObject]


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise evaluator.EvaluationError(f"{label} must be an object")
    return value


def _latency_summary(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    values = sorted(float(record["latency_ms"]) for record in records)
    if not values:
        raise evaluator.EvaluationError("cannot summarize empty result records")
    return {
        "p50_ms": round(evaluator.type7_quantile(values, 0.5), 3),
        "p95_ms": round(evaluator.type7_quantile(values, 0.95), 3),
        "mean_ms": round(sum(values) / len(values), 3),
        "max_ms": round(values[-1], 3),
    }


def _component_coverages(records: Sequence[Mapping[str, Any]]) -> JsonObject:
    return {
        component: evaluator._component_coverage(records, component)  # noqa: SLF001 - shared evaluation definition
        for component in ("vector", "rewrite", "fulltext", "reranker")
    }


def _load_run(run_dir: Path) -> LoadedRun:
    config = evaluator.read_json(run_dir / "config.redacted.json")
    manifest = evaluator.read_json(run_dir / "dataset-manifest.json")
    environment = evaluator.read_json(run_dir / "environment.json")
    config_body = dict(config)
    config_body.pop("captured_at", None)
    config_body.pop("config_sha256", None)
    config_sha = config.get("config_sha256")
    dataset_sha = manifest.get("test_jsonl_sha256")
    if not isinstance(config_sha, str) or not isinstance(dataset_sha, str):
        raise evaluator.EvaluationError("run lacks config or frozen test SHA-256")
    if evaluator.sha256_bytes(evaluator.compact_json_bytes(config_body)) != config_sha:
        raise evaluator.EvaluationError("run config_sha256 does not recompute")
    dataset_path = manifest.get("dataset_path")
    if not isinstance(dataset_path, str) or not dataset_path:
        raise evaluator.EvaluationError("run lacks a local frozen dataset path")
    frozen_dataset_dir = Path(dataset_path)
    if not frozen_dataset_dir.is_absolute():
        frozen_dataset_dir = Path.cwd() / frozen_dataset_dir
    acceptance, reasons, metrics = evaluator.validate_run(
        dataset_dir=frozen_dataset_dir,
        run_dir=run_dir,
        require_final_artifacts=True,
    )
    if acceptance != "PASS" or metrics is None:
        raise evaluator.EvaluationError(f"run did not pass full verification: {','.join(reasons)}")
    state = evaluator.read_json(run_dir / "state.json")
    persisted_metrics = evaluator.read_json(run_dir / "metrics.json")
    persisted_validation = _require_mapping(persisted_metrics.get("validation"), "persisted metrics validation")
    if state.get("conclusion") != "PASS" or persisted_validation.get("status") != "PASS":
        raise evaluator.EvaluationError("run artifacts are not in terminal PASS state")
    query_style = evaluator.query_style_from_config(config)
    records = evaluator._result_records_for_verify(  # noqa: SLF001 - same strict row contract as final verify
        run_dir / "full-results.jsonl",
        arm="full",
        dataset_sha256=dataset_sha,
        config_sha256=config_sha,
        query_style=query_style,
    )
    if len(records) != evaluator.FIXED_TEST_SIZE:
        raise evaluator.EvaluationError("profile comparison requires exactly 200 full-arm Test rows")
    candidate_audit = metrics.get("candidate_audit")
    if not isinstance(candidate_audit, Mapping) or candidate_audit.get("coverage") != 1.0:
        raise evaluator.EvaluationError("profile comparison requires complete full-arm candidate traces")
    return LoadedRun(config=config, manifest=manifest, environment=environment, metrics=metrics, records=records)


def _profile(config: Mapping[str, Any]) -> str:
    retrieval = _require_mapping(config.get("retrieval"), "config.retrieval")
    profile = retrieval.get("profile")
    if not isinstance(profile, str):
        raise evaluator.EvaluationError("config.retrieval.profile is missing")
    return profile


def _assert_fair_pair(
    reference: LoadedRun,
    candidate: LoadedRun,
) -> None:
    ref_config, ref_manifest, ref_environment, ref_records = (
        reference.config,
        reference.manifest,
        reference.environment,
        reference.records,
    )
    cand_config, cand_manifest, cand_environment, cand_records = (
        candidate.config,
        candidate.manifest,
        candidate.environment,
        candidate.records,
    )
    if ref_manifest.get("test_jsonl_sha256") != cand_manifest.get("test_jsonl_sha256"):
        raise evaluator.EvaluationError("runs do not bind to the same frozen Test JSONL")
    if evaluator.query_style_from_config(ref_config) != evaluator.query_style_from_config(cand_config):
        raise evaluator.EvaluationError("runs do not use one query input contract")
    ref_milvus = _require_mapping(ref_config.get("milvus"), "reference milvus")
    cand_milvus = _require_mapping(cand_config.get("milvus"), "candidate milvus")
    if ref_milvus != cand_milvus:
        raise evaluator.EvaluationError("runs do not use the same Milvus collection/model dimension")
    ref_models = _require_mapping(ref_config.get("models"), "reference models")
    cand_models = _require_mapping(cand_config.get("models"), "candidate models")
    if ref_models != cand_models:
        raise evaluator.EvaluationError("runs do not use the same embedding/rewrite/reranker models")
    if ref_config.get("rewrite") != cand_config.get("rewrite"):
        raise evaluator.EvaluationError("runs do not use the same Rewrite execution contract/cache")
    if ref_config.get("timeouts_seconds") != cand_config.get("timeouts_seconds"):
        raise evaluator.EvaluationError("runs do not use the same timeout contract")
    ref_retrieval = _require_mapping(ref_config.get("retrieval"), "reference retrieval")
    cand_retrieval = _require_mapping(cand_config.get("retrieval"), "candidate retrieval")
    for field in _SHARED_RETRIEVAL_FIELDS:
        if ref_retrieval.get(field) != cand_retrieval.get(field):
            raise evaluator.EvaluationError(f"runs differ on fixed retrieval field: {field}")
    if ref_retrieval.get("reranker_enabled") is not True or ref_retrieval.get("reranker_provider") != "cross_encoder":
        raise evaluator.EvaluationError("reference run does not prove Cross-Encoder reranking is enabled")
    candidate_profile = _profile(cand_config)
    if candidate_profile == "current-v12-source-diverse":
        expected = {
            "reference_dual_query_enabled": False,
            "candidate_dual_query_enabled": False,
            "reference_reranker_max_chunks_per_source": 0,
            "candidate_reranker_max_chunks_per_source": 1,
        }
        expected_widening = {
            "reference_vector_top_k": 12,
            "reference_fulltext_top_k": 12,
            "reference_reranker_top_k": 20,
            "candidate_vector_top_k": 12,
            "candidate_fulltext_top_k": 12,
            "candidate_reranker_top_k": 20,
        }
    elif candidate_profile == "current-v12-dual-rrf":
        expected = {
            "reference_dual_query_enabled": False,
            "candidate_dual_query_enabled": True,
            "reference_reranker_max_chunks_per_source": 0,
            "candidate_reranker_max_chunks_per_source": 0,
        }
        expected_widening = {
            "reference_vector_top_k": 12,
            "reference_fulltext_top_k": 12,
            "reference_reranker_top_k": 20,
            "candidate_vector_top_k": 12,
            "candidate_fulltext_top_k": 12,
            "candidate_reranker_top_k": 20,
        }
    elif candidate_profile == "current-v12-dual-rrf-source-diverse":
        expected = {
            "reference_dual_query_enabled": False,
            "candidate_dual_query_enabled": True,
            "reference_reranker_max_chunks_per_source": 0,
            "candidate_reranker_max_chunks_per_source": 1,
        }
        expected_widening = {
            "reference_vector_top_k": 12,
            "reference_fulltext_top_k": 12,
            "reference_reranker_top_k": 20,
            "candidate_vector_top_k": 12,
            "candidate_fulltext_top_k": 12,
            "candidate_reranker_top_k": 20,
        }
    elif candidate_profile == "current-v12-expanded20":
        expected = {
            "reference_dual_query_enabled": False,
            "candidate_dual_query_enabled": False,
            "reference_reranker_max_chunks_per_source": 0,
            "candidate_reranker_max_chunks_per_source": 0,
        }
        expected_widening = {
            "reference_vector_top_k": 12,
            "reference_fulltext_top_k": 12,
            "reference_reranker_top_k": 20,
            "candidate_vector_top_k": 20,
            "candidate_fulltext_top_k": 20,
            "candidate_reranker_top_k": 28,
        }
    elif candidate_profile == "current-v12-dual-full":
        expected = {
            "reference_dual_query_enabled": False,
            "candidate_dual_query_enabled": True,
            "reference_reranker_max_chunks_per_source": 0,
            "candidate_reranker_max_chunks_per_source": 0,
        }
        expected_widening = {
            "reference_vector_top_k": 12,
            "reference_fulltext_top_k": 12,
            "reference_reranker_top_k": 20,
            "candidate_vector_top_k": 12,
            "candidate_fulltext_top_k": 12,
            "candidate_reranker_top_k": 48,
        }
    else:
        raise evaluator.EvaluationError("candidate profile is outside the pre-registered ablation contract")
    observed = {
        "reference_dual_query_enabled": ref_retrieval.get("dual_query_enabled"),
        "candidate_dual_query_enabled": cand_retrieval.get("dual_query_enabled"),
        "reference_reranker_max_chunks_per_source": ref_retrieval.get("reranker_max_chunks_per_source"),
        "candidate_reranker_max_chunks_per_source": cand_retrieval.get("reranker_max_chunks_per_source"),
    }
    if observed != expected:
        raise evaluator.EvaluationError("profile differs from its pre-registered permitted configuration delta")
    observed_widening = {
        **{
            f"reference_{field}": ref_retrieval.get(field)
            for field in ("vector_top_k", "fulltext_top_k", "reranker_top_k")
        },
        **{
            f"candidate_{field}": cand_retrieval.get(field)
            for field in ("vector_top_k", "fulltext_top_k", "reranker_top_k")
        },
    }
    if observed_widening != expected_widening:
        raise evaluator.EvaluationError("profile differs from its pre-registered candidate widening contract")
    if ref_retrieval.get("dual_query_rrf_k") != 60 or cand_retrieval.get("dual_query_rrf_k") != 60:
        raise evaluator.EvaluationError("dual RRF constant is not fixed at 60")
    if ref_environment.get("runtime_source_sha256") != cand_environment.get("runtime_source_sha256"):
        raise evaluator.EvaluationError("runtime retrieval source identity differs between runs")
    if ref_environment.get("corpus") != cand_environment.get("corpus"):
        raise evaluator.EvaluationError("runtime corpus snapshot differs between runs")
    ref_by_id = {str(record["query_id"]): record for record in ref_records}
    cand_by_id = {str(record["query_id"]): record for record in cand_records}
    ref_ids = set(ref_by_id)
    cand_ids = set(cand_by_id)
    if len(ref_ids) != len(ref_records) or len(cand_ids) != len(cand_records) or ref_ids != cand_ids:
        raise evaluator.EvaluationError("full-arm result rows are not one-to-one paired")
    for query_id in sorted(ref_ids):
        ref_record = ref_by_id[query_id]
        cand_record = cand_by_id[query_id]
        for field in ("query_sha256", "target_record_key", "target_source_id", "effective_query_sha256"):
            if ref_record.get(field) != cand_record.get(field):
                raise evaluator.EvaluationError(f"paired rows disagree on {field}: {query_id}")


def compare_runs(reference_run: Path, candidate_run: Path) -> JsonObject:
    """Return a fail-closed direct profile comparison suitable for selection."""
    reference = _load_run(reference_run)
    candidate = _load_run(candidate_run)
    ref_config, ref_manifest, ref_records = reference.config, reference.manifest, reference.records
    cand_config, cand_records = candidate.config, candidate.records
    if _profile(ref_config) != _REFERENCE_PROFILE:
        raise evaluator.EvaluationError(f"reference profile must be {_REFERENCE_PROFILE}")
    candidate_profile = _profile(cand_config)
    if candidate_profile not in _CANDIDATE_PROFILES:
        raise evaluator.EvaluationError("candidate profile is outside the pre-registered ablation contract")
    _assert_fair_pair(reference, candidate)

    dataset_sha = str(ref_manifest["test_jsonl_sha256"])
    combined_config_sha = evaluator.sha256_bytes(
        evaluator.compact_json_bytes(
            {
                "reference": ref_config["config_sha256"],
                "candidate": cand_config["config_sha256"],
            }
        )
    )
    raw = evaluator.compute_metrics(
        ref_records,
        cand_records,
        run_id=f"{reference_run.name}__vs__{candidate_run.name}",
        dataset_sha256=dataset_sha,
        config_sha256=combined_config_sha,
        bootstrap_samples=evaluator.FIXED_BOOTSTRAP_SAMPLES,
        seed=evaluator.FIXED_SEED,
    )
    deltas = cast(Mapping[str, Any], raw["deltas"])
    recall_at_1_delta = _require_mapping(deltas.get("target_recall_at_1"), "Recall@1 delta")
    recall_at_5_delta = _require_mapping(deltas.get("target_recall_at_5"), "Recall@5 delta")
    recall_at_8_delta = _require_mapping(deltas.get("target_recall_at_8"), "Recall@8 delta")
    mrr_delta = _require_mapping(deltas.get("mrr"), "MRR delta")
    recall_at_1_ci = _require_mapping(recall_at_1_delta.get("ci95"), "Recall@1 CI")
    quality_eligible = (
        float(recall_at_1_delta["point"]) > 0
        and float(recall_at_1_ci["low"]) > 0
        and float(recall_at_5_delta["point"]) >= 0
        and float(recall_at_8_delta["point"]) >= 0
        and float(mrr_delta["point"]) >= 0
    )

    return {
        "schema_version": "1.0",
        "comparison": {
            "reference_run_id": reference_run.name,
            "candidate_run_id": candidate_run.name,
            "reference_profile": _profile(ref_config),
            "candidate_profile": candidate_profile,
            "dataset_sha256": dataset_sha,
            "query_style": evaluator.query_style_from_config(ref_config),
            "bootstrap_samples": evaluator.FIXED_BOOTSTRAP_SAMPLES,
            "seed": evaluator.FIXED_SEED,
            "runtime_corpus_snapshot_equal": reference.environment.get("corpus") == candidate.environment.get("corpus"),
        },
        "arms": {
            "reference": {
                "target_recall_at_1": raw["arms"]["baseline"]["target_recall_at_1"],
                "target_recall_at_5": raw["arms"]["baseline"]["target_recall_at_5"],
                "target_recall_at_8": raw["arms"]["baseline"]["target_recall_at_8"],
                "mrr": raw["arms"]["baseline"]["mrr"],
                "latency": _latency_summary(ref_records),
                "components": _component_coverages(ref_records),
                "candidate_audit": evaluator._candidate_audit(ref_records),  # noqa: SLF001
            },
            "candidate": {
                "target_recall_at_1": raw["arms"]["full"]["target_recall_at_1"],
                "target_recall_at_5": raw["arms"]["full"]["target_recall_at_5"],
                "target_recall_at_8": raw["arms"]["full"]["target_recall_at_8"],
                "mrr": raw["arms"]["full"]["mrr"],
                "latency": _latency_summary(cand_records),
                "components": _component_coverages(cand_records),
                "candidate_audit": evaluator._candidate_audit(cand_records),  # noqa: SLF001
            },
        },
        "deltas_candidate_minus_reference": {
            "target_recall_at_1": deltas["target_recall_at_1"],
            "target_recall_at_5": recall_at_5_delta,
            "target_recall_at_8": recall_at_8_delta,
            "mrr": mrr_delta,
            "paired_hits": raw["paired_hits"],
            "paired_hits_by_cutoff": raw["paired_hits_by_cutoff"],
        },
        "selection_rule": {
            "primary_metric": "target_recall_at_1",
            "recall_at_1_point_gt_zero": float(recall_at_1_delta["point"]) > 0,
            "recall_at_1_ci95_lower_gt_zero": float(recall_at_1_ci["low"]) > 0,
            "recall_at_5_point_gte_zero": float(recall_at_5_delta["point"]) >= 0,
            "recall_at_8_point_gte_zero": float(recall_at_8_delta["point"]) >= 0,
            "mrr_point_gte_zero": float(mrr_delta["point"]) >= 0,
            "quality_eligible": quality_eligible,
        },
        "artifact_sha256": {
            "reference_full_results": evaluator.sha256_file(reference_run / "full-results.jsonl"),
            "candidate_full_results": evaluator.sha256_file(candidate_run / "full-results.jsonl"),
            "comparison_script": evaluator.sha256_file(Path(__file__).resolve()),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare two paired RAG full-arm profiles")
    parser.add_argument("--reference-run", type=Path, required=True)
    parser.add_argument("--candidate-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = compare_runs(args.reference_run, args.candidate_run)
    evaluator.write_json_atomic(args.output, payload)
    print(f"wrote paired profile comparison: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
