"""Focused contract tests for the frozen RAG silver evaluator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import evaluate_rag_silver as evaluator


def _record(
    query_id: str,
    arm: str,
    *,
    target: str = "target-1",
    source_ids: list[str] | None = None,
    dataset_sha: str = "d" * 64,
    config_sha: str = "c" * 64,
) -> dict[str, Any]:
    source_ids = source_ids or []
    results = [
        {
            "rank": index,
            "source_type": "case",
            "source_id": source_id,
            "chunk_id": f"chunk-{index}",
            "title": "test",
            "score": 0.5,
            "score_type": "vector_score",
            "metadata": {},
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    rank, hit, reciprocal_rank = evaluator.score_results(results, target)
    components = evaluator.baseline_component_templates()
    components["vector"].update({"status": "succeeded", "attempted": True, "candidate_count": len(results)})
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": "run",
        "dataset_version": "rag-silver-v1",
        "dataset_sha256": dataset_sha,
        "config_sha256": config_sha,
        "split": "test",
        "query_id": query_id,
        "arm": arm,
        "query": "测试查询",
        "query_sha256": evaluator.sha256_text("测试查询"),
        "target_record_key": "a" * 64,
        "target_source_id": target,
        "source_types": ["case"],
        "top_k": 8,
        "status": "success",
        "attempt_count": 1,
        "started_at": "2026-08-08T00:00:00Z",
        "completed_at": "2026-08-08T00:00:01Z",
        "latency_ms": 1.0,
        "effective_query": "测试查询",
        "effective_query_sha256": evaluator.sha256_text("测试查询"),
        "components": components,
        "degradations": [],
        "results": results,
        "first_relevant_rank": rank,
        "hit_at_8": hit,
        "reciprocal_rank": reciprocal_rank,
    }
    record["record_sha256"] = evaluator._record_hash(record)
    return record


def _full_record(
    query_id: str,
    *,
    source_ids: list[str] | None = None,
    reranker_status: str = "succeeded",
) -> dict[str, Any]:
    record = _record(query_id, "full", source_ids=source_ids)
    components = evaluator.full_component_templates()
    components["rewrite"].update({"status": "succeeded", "attempted": True, "model": "rewrite"})
    components["vector"].update({"status": "succeeded", "attempted": True, "embedding_model": "embed"})
    components["fulltext"].update({"status": "succeeded", "attempted": True})
    if reranker_status == "succeeded":
        components["reranker"].update({"status": "succeeded", "attempted": True, "model": "reranker"})
        for result in record["results"]:
            result["score_type"] = "reranker_score"
            result["metadata"] = {
                "reranker_provider": "cross_encoder",
                "reranker_model": "reranker",
                "reranker_score": 0.8,
            }
    else:
        components["reranker"].update({"status": reranker_status, "attempted": False})
    record["components"] = components
    record["degradations"] = evaluator._default_degradations(components)
    record["record_sha256"] = evaluator._record_hash(record)
    return record


def test_target_rows_reject_zero_and_multiple_mappings() -> None:
    with pytest.raises(evaluator.TargetResolutionError, match="missing"):
        evaluator.validate_target_rows(["a" * 64], [])
    with pytest.raises(evaluator.TargetResolutionError, match="non_unique"):
        evaluator.validate_target_rows(["a" * 64], [("one", "a" * 64), ("two", "a" * 64)])
    assert evaluator.validate_target_rows(["a" * 64], [("one", "a" * 64)]) == {"a" * 64: "one"}


def test_first_relevant_rank_deduplicates_target_chunks() -> None:
    results = [
        {"rank": 1, "source_type": "case", "source_id": "other"},
        {"rank": 2, "source_type": "case", "source_id": "target"},
        {"rank": 3, "source_type": "case", "source_id": "target"},
    ]
    assert evaluator.score_results(results, "target") == (2, 1, 0.5)


def test_first_relevant_rank_rejects_non_contiguous_ranks() -> None:
    with pytest.raises(evaluator.EvaluationError, match="contiguous"):
        evaluator.first_relevant_rank([{"rank": 2, "source_type": "case", "source_id": "target"}], "target")


@pytest.mark.asyncio
async def test_pure_vector_adapter_uses_only_private_vector_signature() -> None:
    class Retriever:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[str], int, object]] = []

        async def _vector_search(
            self, query: str, sources: list[str], *, top_k: int, filters: dict[str, Any] | None
        ) -> list[Any]:
            self.calls.append((query, sources, top_k, filters))
            return [SimpleNamespace(source_id="target")]

    retriever = Retriever()
    event = evaluator.component_template("not_attempted")
    adapter = evaluator.PureVectorAdapter(retriever, embedding_model="actual-model")
    hits = await adapter.search("q", event=event)
    assert len(hits) == 1
    assert retriever.calls == [("q", ["case"], 8, None)]
    assert event["status"] == "succeeded"
    assert event["embedding_model"] == "actual-model"


@pytest.mark.asyncio
async def test_pure_vector_adapter_exception_is_arm_failure_not_empty_hit() -> None:
    class Retriever:
        async def _vector_search(
            self, query: str, sources: list[str], *, top_k: int, filters: dict[str, Any] | None
        ) -> list[Any]:
            raise TimeoutError("transient")

    event = evaluator.component_template("not_attempted")
    adapter = evaluator.PureVectorAdapter(Retriever(), embedding_model="actual-model")
    with pytest.raises(evaluator.ArmTechnicalFailure):
        await adapter.search("q", event=event)
    assert event["status"] == "failed"
    assert event["error_type"] == "TimeoutError"


def test_pure_vector_adapter_fails_closed_on_signature_drift() -> None:
    class DriftedRetriever:
        async def _vector_search(self, query: str, sources: list[str], top_k: int) -> list[Any]:
            return []

    with pytest.raises(evaluator.EvaluationError, match="signature"):
        evaluator.PureVectorAdapter(DriftedRetriever(), embedding_model="m")


@pytest.mark.asyncio
async def test_observed_gateway_marks_success_empty_and_exception() -> None:
    class Gateway:
        async def chat(self, *args: Any, **kwargs: Any) -> str:
            return "改写结果"

    event = evaluator.component_template("not_attempted")
    gateway = evaluator.ObservedGateway(Gateway(), component="rewrite", event_getter=lambda: event, default_model="m")
    assert await gateway.chat(model="actual") == "改写结果"
    assert event["status"] == "succeeded"
    assert event["model"] == "actual"

    class EmptyGateway:
        async def chat(self, *args: Any, **kwargs: Any) -> str:
            return "  "

    empty_event = evaluator.component_template("not_attempted")
    empty = evaluator.ObservedGateway(
        EmptyGateway(), component="rewrite", event_getter=lambda: empty_event, default_model="m"
    )
    assert await empty.chat() == "  "
    assert empty_event["status"] == "fallback"
    assert empty_event["error_type"] == "ValueError"


def test_resume_recovers_only_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "baseline-results.jsonl"
    record = _record("test-1", "baseline")
    path.write_bytes(evaluator.compact_json_bytes(record) + b'\n{"partial"')
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "execution.log").touch()
    (run_dir / "failures.jsonl").touch()
    evaluator.write_json_atomic(run_dir / "state.json", evaluator.initial_state("run"))
    resumed = evaluator.read_resume_records(
        path,
        arm="baseline",
        split="test",
        dataset_sha256="d" * 64,
        config_sha256="c" * 64,
        run_dir=run_dir,
    )
    assert set(resumed) == {"test-1"}
    assert path.read_bytes().endswith(b"\n")


def test_resume_rejects_middle_corruption(tmp_path: Path) -> None:
    path = tmp_path / "baseline-results.jsonl"
    record = _record("test-1", "baseline")
    path.write_bytes(
        evaluator.compact_json_bytes(record) + b"\nnot-json\n" + evaluator.compact_json_bytes(record) + b"\n"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "execution.log").touch()
    (run_dir / "failures.jsonl").touch()
    evaluator.write_json_atomic(run_dir / "state.json", evaluator.initial_state("run"))
    with pytest.raises(evaluator.ResumeIntegrityError, match="middle"):
        evaluator.read_resume_records(
            path,
            arm="baseline",
            split="test",
            dataset_sha256="d" * 64,
            config_sha256="c" * 64,
            run_dir=run_dir,
        )


def test_result_component_contract_requires_case_only_and_cross_encoder_metadata() -> None:
    record = _full_record("q1", source_ids=["target-1"])
    evaluator.validate_result_record(
        record,
        arm="full",
        split="test",
        run_id="run",
        dataset_sha256="d" * 64,
        config_sha256="c" * 64,
    )
    record["results"][0]["source_type"] = "formula"
    record["record_sha256"] = evaluator._record_hash(record)
    with pytest.raises(evaluator.ResumeIntegrityError, match="non-case"):
        evaluator.validate_result_record(record)

    missing_metadata = _full_record("q2", source_ids=["target-1"])
    missing_metadata["results"][0]["metadata"] = {}
    missing_metadata["record_sha256"] = evaluator._record_hash(missing_metadata)
    with pytest.raises(evaluator.ResumeIntegrityError, match="cross-encoder"):
        evaluator.validate_result_record(missing_metadata)


def test_result_component_contract_rejects_unobserved_fulltext_and_degradation_drift() -> None:
    record = _full_record("q1", source_ids=[], reranker_status="not_applied_insufficient_candidates")
    evaluator.validate_result_record(record)
    record["components"]["fulltext"]["status"] = "not_attempted"
    record["record_sha256"] = evaluator._record_hash(record)
    with pytest.raises(evaluator.ResumeIntegrityError, match="PostgreSQL"):
        evaluator.validate_result_record(record)

    degraded = _full_record("q2", source_ids=[], reranker_status="not_applied_insufficient_candidates")
    degraded["degradations"] = []
    degraded["record_sha256"] = evaluator._record_hash(degraded)
    with pytest.raises(evaluator.ResumeIntegrityError, match="degradations"):
        evaluator.validate_result_record(degraded)


def test_records_are_bound_to_frozen_query_and_resolved_target() -> None:
    frozen = [{"query_id": "q1", "query": "冻结查询", "target_record_key": "a" * 64}]
    record = _record("q1", "baseline", target="source-1")
    record["query"] = "冻结查询"
    record["query_sha256"] = evaluator.sha256_text("冻结查询")
    record["target_record_key"] = "a" * 64
    record["record_sha256"] = evaluator._record_hash(record)
    evaluator.validate_records_bound_to_frozen_split(record and [record], frozen, {"a" * 64: "source-1"})
    record["target_source_id"] = "other"
    with pytest.raises(evaluator.EvaluationError, match="target_source_id"):
        evaluator.validate_records_bound_to_frozen_split([record], frozen, {"a" * 64: "source-1"})


def test_preflight_target_mapping_selects_test_bindings_from_smoke_and_test_evidence(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    smoke_key = "s" * 64
    test_key = "t" * 64
    evaluator.write_json_atomic(
        run_dir / "preflight.json",
        {
            "checks": [
                {
                    "name": "frozen_target_resolution",
                    "status": "passed",
                    "evidence": {"target_mapping": {smoke_key: "smoke-source", test_key: "test-source"}},
                }
            ]
        },
    )
    assert evaluator.preflight_target_mapping(run_dir, [{"query_id": "test", "target_record_key": test_key}]) == {
        test_key: "test-source"
    }


def test_paired_metrics_and_fixed_bootstrap_golden_values() -> None:
    baseline = [_record("q1", "baseline", source_ids=[]), _record("q2", "baseline", source_ids=["target-1"])]
    full = [_record("q1", "full", source_ids=["target-1"]), _record("q2", "full", source_ids=["other", "target-1"])]
    metrics = evaluator.compute_metrics(
        baseline,
        full,
        run_id="run",
        dataset_sha256="d" * 64,
        config_sha256="c" * 64,
        bootstrap_samples=10_000,
        seed=20260807,
    )
    assert metrics["paired_hits"] == {"0_0": 0, "0_1": 1, "1_0": 0, "1_1": 1}
    assert metrics["arms"]["baseline"] == {"target_recall_at_8": 0.5, "mrr": 0.5}
    assert metrics["arms"]["full"] == {"target_recall_at_8": 1.0, "mrr": 0.75}
    assert metrics["deltas"]["target_recall_at_8"]["point"] == 0.5
    assert metrics["deltas"]["target_recall_at_8"]["ci95"] == {"low": 0.0, "high": 1.0}
    assert metrics["deltas"]["mrr"]["ci95"] == {"low": -0.5, "high": 1.0}


@pytest.mark.parametrize(
    ("values", "probability", "expected"),
    [([1.0], 0.025, 1.0), ([0.0, 1.0], 0.5, 0.5), ([0.0, 1.0, 2.0], 0.25, 0.5)],
)
def test_type7_quantile_boundaries(values: list[float], probability: float, expected: float) -> None:
    assert evaluator.type7_quantile(values, probability) == expected


def test_redacted_config_hash_is_stable_when_capture_time_changes() -> None:
    settings = SimpleNamespace(
        milvus_host="localhost",
        milvus_port=19530,
        milvus_collection="xuanhu_knowledge_v4",
        embedding_dim=4096,
        embedding_model="Qwen/Qwen3-Embedding-8B",
        rag_query_rewrite_model="Qwen3.5-2B-free",
        chat_model="chat",
        rag_reranker_model="jina-reranker-m0",
        rag_top_k_vector=12,
        rag_top_k_fulltext=12,
        rag_reranker_provider="cross_encoder",
        rag_reranker_top_k=20,
        rag_reranker_final_top_k=8,
        rag_query_rewrite_enabled=True,
        rag_query_rewrite_model_temperature=0.1,
        rag_query_rewrite_model_max_tokens=400,
        embedding_gateway_timeout_seconds=0,
        embedding_cache_ttl_seconds=0,
        model_gateway_timeout_seconds=60,
        rag_query_rewrite_gateway_timeout_seconds=0,
        rag_query_rewrite_timeout_seconds=3,
        reranker_gateway_timeout_seconds=0,
        rag_reranker_timeout_seconds=5,
    )
    first = evaluator.redacted_config(settings)
    second = evaluator.redacted_config(settings)
    assert first["config_sha256"] == second["config_sha256"]
    assert "api_key" not in json.dumps(first).lower()


def test_resume_r1_requires_bge_m3_even_with_positive_confidence_intervals() -> None:
    metrics = {
        "arms": {
            "baseline": {"target_recall_at_8": 0.2, "mrr": 0.2},
            "full": {"target_recall_at_8": 0.3, "mrr": 0.3},
        },
        "deltas": {
            "target_recall_at_8": {"point": 0.1, "ci95": {"low": 0.01, "high": 0.2}},
            "mrr": {"point": 0.1, "ci95": {"low": 0.01, "high": 0.2}},
        },
        "components": {
            "full_rewrite": {"coverage": 1.0},
            "full_cross_encoder": {"coverage": 1.0},
        },
    }
    environment = {"corpus": {"prepared_total_entries": 3808}}
    qwen = evaluator.make_resume_bullet(
        run_id="run",
        metrics=metrics,
        config={"models": {"embedding": "Qwen/Qwen3-Embedding-8B"}},
        environment=environment,
        acceptance="PASS",
    )
    bge = evaluator.make_resume_bullet(
        run_id="run",
        metrics=metrics,
        config={"models": {"embedding": "BAAI/bge-m3"}},
        environment=environment,
        acceptance="PASS",
    )
    assert "selection_rule: R2" in qwen
    assert "selection_rule: R1" in bge


def test_contract_environment_disables_unprovenanced_embedding_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_keys = {
        "MILVUS_COLLECTION",
        "RAG_QUERY_REWRITE_ENABLED",
        "RAG_QUERY_REWRITE_MODEL_TEMPERATURE",
        "RAG_TOP_K_VECTOR",
        "RAG_TOP_K_FULLTEXT",
        "RAG_RERANKER_ENABLED",
        "RAG_RERANKER_PROVIDER",
        "RAG_RERANKER_TOP_K",
        "RAG_RERANKER_FINAL_TOP_K",
        "RAG_TOP_N_FINAL",
        "EMBEDDING_CACHE_TTL_SECONDS",
    }
    original = {key: os.environ.get(key) for key in contract_keys}
    missing = {key for key in contract_keys if key not in os.environ}
    with monkeypatch.context() as env_patch:
        for key in contract_keys:
            env_patch.setenv(key, os.environ.get(key, "__pytest_contract_env_placeholder__"))
        evaluator.set_contract_environment("xuanhu_knowledge_v4")
        assert os.environ["EMBEDDING_CACHE_TTL_SECONDS"] == "0"
        assert os.environ["RAG_QUERY_REWRITE_ENABLED"] == "true"
    for key in contract_keys:
        if key in missing:
            assert key not in os.environ
        else:
            assert os.environ[key] == original[key]


def _preflight_for_report() -> dict[str, Any]:
    keys = {f"{index:064x}": f"source-{index}" for index in range(220)}
    evidence = {
        "local_quality_gates": {},
        "frozen_dataset": {"smoke_count": 20, "test_count": 200, "test_sha256": "a" * 64},
        "source_file": {"sha256": "b" * 64},
        "effective_contract_configuration": {"collection": "xuanhu_knowledge_v4", "config_sha256": "c" * 64},
        "postgres_connectivity": {"active_case_rows": 3254, "active_case_chunks": 3799},
        "milvus_collection": {"collection": "xuanhu_knowledge_v4", "embedding_dim": 4096},
        "model_gateways": {
            "embedding": {"model": "embedding"},
            "rewrite": {"model": "rewrite"},
            "reranker": {"model": "reranker"},
        },
        "frozen_target_resolution": {
            "resolved": 220,
            "unique_source_ids": 220,
            "vector_ready": 220,
            "target_mapping": keys,
            "expected_target_chunks": 223,
            "matched_target_chunks": 223,
        },
        "prepared_corpus_snapshot": {"prepared_total_entries": 3808, "prepared_case_entries": 3254},
    }
    return {
        "overall_status": "passed",
        "checks": [
            {"name": name, "required": True, "status": "passed", "evidence": value} for name, value in evidence.items()
        ],
    }


def _formal_metrics_for_report() -> dict[str, Any]:
    return {
        "n_pairs": 200,
        "dataset_sha256": "a" * 64,
        "arms": {
            "baseline": {"target_recall_at_8": 0.4, "mrr": 0.3},
            "full": {"target_recall_at_8": 0.5, "mrr": 0.4},
        },
        "deltas": {
            "target_recall_at_8": {"point": 0.1, "ci95": {"low": 0.01, "high": 0.2}},
            "mrr": {"point": 0.1, "ci95": {"low": 0.01, "high": 0.2}},
        },
        "paired_hits": {"0_0": 80, "0_1": 40, "1_0": 20, "1_1": 60},
        "components": {
            name: {"success": 200, "denominator": 200, "coverage": 1.0}
            for name in (
                "baseline_vector",
                "full_rewrite",
                "full_vector",
                "full_fulltext",
                "full_cross_encoder",
            )
        },
        "degradation_counts": {"reranker_not_applied_insufficient_candidates": 2},
    }


def test_report_is_complete_template_and_suppresses_partial_metric_claims() -> None:
    report = evaluator.make_report(
        run_id="run",
        acceptance="INCOMPLETE",
        metrics={"n_pairs": 199, "dataset_sha256": "a" * 64},
        config={"models": {"embedding": "embed"}, "milvus": {"collection": "collection", "embedding_dim": 4096}},
        environment={"corpus": {"prepared_total_entries": 3808, "prepared_case_entries": 3254}},
        reasons=["formal_result_count_not_200_per_arm"],
        dataset_sha256="a" * 64,
        preflight=_preflight_for_report(),
        validation=evaluator.make_validation_payload("INCOMPLETE", ["formal_result_count_not_200_per_arm"]),
    )
    assert "test.jsonl SHA-256 = " + "a" * 64 in report
    assert "正式指标不可用" in report
    assert "| Target Recall@8 |" not in report
    for heading in ("## 失败与恢复", "## 有效性判定", "## 简历文案", "Test target 映射", "泄漏、重复和哈希验证"):
        assert heading in report


def test_report_renders_formal_component_degradation_and_must_decisions() -> None:
    report = evaluator.make_report(
        run_id="run",
        acceptance="PASS",
        metrics=_formal_metrics_for_report(),
        config={
            "models": {"embedding": "BAAI/bge-m3", "rewrite": "rewrite", "reranker": "reranker"},
            "milvus": {"collection": "collection", "embedding_dim": 4096},
            "config_sha256": "c" * 64,
        },
        environment={"corpus": {"prepared_total_entries": 3808, "prepared_case_entries": 3254}},
        reasons=[],
        dataset_sha256="a" * 64,
        preflight=_preflight_for_report(),
        failure_summary={
            "arm_technical_failures": 1,
            "unresolved_arm_technical_failures": 0,
            "component_fallbacks": {"reranker": 2},
            "real_zero_hits": 100,
            "full_test_success_rows": 200,
            "resume_events": 1,
            "resume_summaries": ["recovered torn trailing line"],
        },
        validation=evaluator.make_validation_payload("PASS", []),
        resume_bullet="source_run: run\nselection_rule: R4\n",
    )
    assert "| Target Recall@8 | 40.0% | 50.0% | +10.0 pp" in report
    assert "fallback / 未应用" in report
    assert "reranker_not_applied_insufficient_candidates=2" in report
    assert "实现和本地质量（06 §3）" in report
    assert "recovered torn trailing line" in report


def test_quality_gate_evidence_requires_exact_argv_and_log(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    evaluator.ensure_run_directory(run_dir)
    commands = []
    for name, argv in evaluator._QUALITY_GATE_COMMANDS:
        commands.append({"name": name, "argv": list(argv), "exit_code": 0})
        evaluator.log_execution(run_dir, "implement", " ".join(argv), 0, f"{name}: passed")
    assert evaluator.quality_gate_evidence_errors(run_dir, {"commands": commands}) == []
    commands[0]["argv"] = ["uv", "run", "ruff", "check"]
    assert evaluator.quality_gate_evidence_errors(run_dir, {"commands": commands})


def test_metrics_validation_status_must_match_terminal_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    evaluator.ensure_run_directory(run_dir)
    assert evaluator.validate_metrics_validation_state(run_dir, {"validation": {"status": "INCOMPLETE"}}) is None
    evaluator.update_state(run_dir, conclusion="PASS", status="completed")
    assert (
        evaluator.validate_metrics_validation_state(run_dir, {"validation": {"status": "INCOMPLETE"}})
        == "metrics_validation_state_conclusion_mismatch"
    )
    assert evaluator.validate_metrics_validation_state(run_dir, {"validation": {"status": "PASS"}}) is None
    assert evaluator.reporting_acceptance("PASS", evaluator.read_json(run_dir / "state.json")) == "PASS"


@pytest.mark.asyncio
async def test_run_report_keeps_partial_run_incomplete_without_formal_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    dataset_dir = tmp_path / "dataset"
    smoke = evaluator.FrozenSplit("smoke", [], "s" * 64, {})
    test = evaluator.FrozenSplit("test", [], "t" * 64, {})
    evaluator.ensure_run_directory(run_dir)
    evaluator.write_json_atomic(
        run_dir / "config.redacted.json",
        {
            "config_sha256": "c" * 64,
            "models": {"embedding": "embed", "rewrite": "rewrite", "reranker": "reranker"},
            "milvus": {"collection": "collection", "embedding_dim": 4096},
        },
    )
    monkeypatch.setattr(evaluator, "validate_dataset_pair", lambda _path: (smoke, test))
    monkeypatch.setattr(
        evaluator,
        "validate_run",
        lambda **_kwargs: ("INVALID", ["formal_result_count_not_200_per_arm"], None),
    )
    assert await evaluator.run_report(dataset_dir, run_dir, 10_000, 20260807) == 1
    metrics = evaluator.read_json(run_dir / "metrics.json")
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    state = evaluator.read_json(run_dir / "state.json")
    assert metrics["formal_metrics_available"] is False
    assert metrics["validation"]["status"] == "INCOMPLETE"
    assert "正式指标不可用" in report
    assert state["conclusion"] is None
