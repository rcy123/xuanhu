"""Unit tests for the frozen weak-supervision RAG evaluation dataset builder.

All gateway calls below are in-memory fakes.  These tests never read a real
credential, call a model, or write outside pytest's temporary directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.build_rag_silver_eval as builder


def _record_key(index: int) -> str:
    return hashlib.sha256(f"record-{index}".encode()).hexdigest()


def _case(index: int, *, category: str = "内科", content: str | None = None) -> dict[str, Any]:
    source_char = chr(0x4E00 + index)
    return {
        "entry_type": "case",
        "title": f"病例标题{index}",
        "content": content if content is not None else source_char * 50,
        "disease_category": category,
        "syndrome": "气血不足",
        "treatment_principle": "益气养血",
        "formula_summary": "归脾汤：人参、白术",
        "metadata": {"record_key": _record_key(index)},
    }


def _valid_query(index: int) -> str:
    query_char = chr(0x6000 + index)
    return f"患者近来反复出现不适伴随睡眠波动和食欲下降{query_char * 30}"


def _candidate(index: int, *, stratum: str) -> builder.Candidate:
    symptom_char = chr(0x4E00 + index)
    symptom_text = symptom_char * 50
    return builder.Candidate(
        record_key=_record_key(index),
        title=f"病例标题{index}",
        stratum=stratum,
        symptom_text=symptom_text,
        source_symptom_sha256=builder.sha256_text(symptom_text),
        syndrome=None,
        treatment_principle=None,
        formula_summary=None,
        content=symptom_text,
        forbidden_terms=[],
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_bundle(tmp_path: Path, count: int = 220) -> tuple[Path, list[dict[str, Any]]]:
    bundle = tmp_path / "staging"
    cases = [_case(index) for index in range(count)]
    cases_path = bundle / "prepared" / "cases.json"
    _write_json(cases_path, cases)
    cases_sha256 = builder.sha256_file(cases_path)
    manifest = {
        "outputs": [
            {
                "kind": "cases",
                "disposition": "prepared",
                "relative_path": "prepared/cases.json",
                "sha256": cases_sha256,
                "record_count": len(cases),
            }
        ],
        "source_files": [
            {
                "kind": "cases",
                "path": "C:/read-only/theory_cases_converted.json",
                "bytes": 1234,
                "sha256": "a" * 64,
                "record_count": len(cases),
            }
        ],
    }
    _write_json(bundle / "manifest.json", manifest)
    return bundle, cases


def _write_valid_frozen_dataset(tmp_path: Path) -> tuple[Path, Path]:
    bundle, cases = _make_bundle(tmp_path)
    candidates, rejected = builder.load_structurally_valid_candidates(cases)
    assert not rejected
    builder.apply_low_frequency_merge(candidates)
    candidates_by_key = {candidate.record_key: candidate for candidate in candidates}

    dataset_dir = tmp_path / "dataset"
    test_records: list[dict[str, Any]] = []
    smoke_records: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        record_key = case["metadata"]["record_key"]
        candidate = candidates_by_key[record_key]
        split = "smoke" if index < builder.FIXED_SMOKE_SIZE else "test"
        query = _valid_query(index)
        accepted = builder.AcceptedQuery(
            query_id=builder.stable_query_id(split, record_key, builder.normalize_query_for_dedup(query)),
            query=query,
            normalized_query=builder.normalize_query_for_dedup(query),
            target_record_key=record_key,
            stratum=candidate.stratum,
            source_symptom_sha256=candidate.source_symptom_sha256,
            query_sha256=builder.sha256_text(query),
        )
        target = smoke_records if split == "smoke" else test_records
        target.append(builder.accepted_to_jsonl_record(accepted, split=split))

    builder.write_jsonl_atomic(dataset_dir / "smoke.jsonl", smoke_records)
    builder.write_jsonl_atomic(dataset_dir / "test.jsonl", test_records)
    builder.write_jsonl_atomic(dataset_dir / "rejected.jsonl", [])
    source_cases = bundle / "prepared" / "cases.json"
    source_manifest = bundle / "manifest.json"
    manifest = {
        "schema_version": builder.SCHEMA_VERSION,
        "dataset_version": builder.DATASET_VERSION,
        "seed": builder.FIXED_SEED,
        "frozen": True,
        "source": {
            "raw_cases": {"path": "C:/read-only/theory_cases_converted.json", "bytes": 1234, "sha256": "a" * 64},
            "prepared_cases_sha256": builder.sha256_file(source_cases),
            "prepared_cases_record_count": len(cases),
            "staging_manifest_sha256": builder.sha256_file(source_manifest),
        },
        "counts": {"test": len(test_records), "smoke": len(smoke_records), "rejected": 0},
        "artifact_sha256": {
            "smoke.jsonl": builder.sha256_file(dataset_dir / "smoke.jsonl"),
            "test.jsonl": builder.sha256_file(dataset_dir / "test.jsonl"),
            "rejected.jsonl": builder.sha256_file(dataset_dir / "rejected.jsonl"),
        },
    }
    builder.write_json_atomic(dataset_dir / "manifest.json", manifest)
    return bundle, dataset_dir


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ａ　Ｂ\r\n\r\n第三段\t\t内容\r末尾  ", "A B\n第三段 内容\n末尾"),
        ("首段\n\n\n次段\u3000\u3000尾部", "首段\n次段 尾部"),
    ],
)
def test_normalize_content_handles_newlines_nfkc_whitespace_and_paragraphs(raw: str, expected: str) -> None:
    assert builder.normalize_content(raw) == expected


@pytest.mark.parametrize("marker", builder.ANSWER_BOUNDARY_MARKERS)
def test_extract_symptom_fragment_stops_at_every_answer_boundary_marker(marker: str) -> None:
    prefix = "患者反复头晕乏力心悸纳差睡眠不安活动后加重晨起口干咽燥小便偏黄大便正常" * 2
    spaced_marker = " ".join(marker)
    result = builder.extract_symptom_fragment(f"{prefix}{spaced_marker} ： 后续答案字段")

    assert result.reason is None
    assert result.symptom_text == prefix


def test_extract_symptom_fragment_removes_normalized_label_lines() -> None:
    prefix = "患者反复头晕乏力心悸纳差睡眠不安活动后加重晨起口干咽燥小便偏黄大便正常" * 2
    result = builder.extract_symptom_fragment(f"{prefix}\n证型：气血不足\n治 法 : 益气养血")

    assert result.reason is None
    assert result.symptom_text == prefix


def test_chinese_length_query_length_and_minimum_symptom_boundaries() -> None:
    assert builder.count_chinese_chars("甲A乙1") == 2
    assert builder.check_query_length("甲" * 25)
    assert builder.check_query_length("甲" * 180)
    assert not builder.check_query_length("甲" * 24)
    assert not builder.check_query_length("甲" * 181)
    assert builder.extract_symptom_fragment("甲" * 39).reason == "insufficient_symptom_text"
    assert builder.extract_symptom_fragment("甲" * 40).symptom_text == "甲" * 40


def test_structural_strata_handle_unclassified_and_low_frequency_categories() -> None:
    cases = [_case(0, category="")]
    cases.extend(_case(index, category="常见类") for index in range(1, 6))
    cases.extend(_case(index, category="稀有类") for index in range(6, 10))

    candidates, rejected = builder.load_structurally_valid_candidates(cases)
    assert not rejected
    builder.apply_low_frequency_merge(candidates)
    strata = {candidate.record_key: candidate.stratum for candidate in candidates}

    assert strata[_record_key(0)] == builder.UNCLASSIFIED_STRATUM
    assert {strata[_record_key(index)] for index in range(1, 6)} == {"常见类"}
    assert {strata[_record_key(index)] for index in range(6, 10)} == {builder.LOW_FREQUENCY_STRATUM}


@pytest.mark.parametrize(
    "query",
    [
        "患者出现病例标题明显加重伴睡眠不佳和食欲下降",
        "患者出现气血不足表现伴睡眠不佳和食欲下降",
        "患者出现益气养血需求伴睡眠不佳和食欲下降",
        "患者服用归脾汤后仍有睡眠不佳和食欲下降",
        "患者辨证为某证后仍有睡眠不佳和食欲下降",
    ],
)
def test_answer_and_conclusion_leakage_is_rejected(query: str) -> None:
    case = _case(
        0,
        content=("患者反复头晕乏力心悸纳差睡眠不安活动后加重晨起口干咽燥小便偏黄大便正常" * 2) + "辨证为气血不足",
    )
    case["title"] = "001、病例标题（医案）"
    candidate, rejected = builder.load_structurally_valid_candidates([case])
    assert not rejected
    assert builder.check_answer_leakage(query, candidate[0].forbidden_terms) or builder.check_conclusion_style_leakage(
        query
    )


def test_jaccard_thresholds_are_strictly_greater_than_contract_boundaries() -> None:
    assert builder.jaccard_similarity("abcdef", "abcdefgh") == pytest.approx(0.60)
    assert not builder.check_excessive_source_overlap("abcdef", "abcdefgh")
    assert builder.check_excessive_source_overlap("abcdefg", "abcdefgh")

    assert builder.jaccard_similarity("abcdefghijkl", "abcdefghijklm") == pytest.approx(0.90)
    assert not builder.check_near_duplicate("abcdefghijkl", ["abcdefghijklm"])
    assert builder.check_near_duplicate("abcdefghijklm", ["abcdefghijklmn"])


def test_largest_remainder_uses_utf8_order_for_ties() -> None:
    assert builder.largest_remainder_allocation({"乙": 5, "甲": 5}, 5) == {"乙": 3, "甲": 2}
    assert builder.largest_remainder_allocation({"乙": 5, "甲": 5}, 1) == {"乙": 1, "甲": 0}


class _FakeGenerator:
    async def generate(self, symptom_text: str, *, trace_id: str) -> builder.QueryGenerationResult:
        del trace_id
        source_char = symptom_text[0]
        query_char = chr(ord(source_char) + 0x1000)
        return builder.QueryGenerationResult(
            raw_response=json.dumps({"query": f"患者反复不适伴随睡眠波动和食欲下降{query_char * 30}"}),
            model="fake-rewrite",
            attempt_count=1,
            latency_ms=0.0,
            error_type=None,
        )


@pytest.mark.asyncio
async def test_exhausted_stratum_redistributes_quota_to_remaining_candidates() -> None:
    grouped = {
        "甲": [_candidate(0, stratum="甲")],
        "乙": [_candidate(index, stratum="乙") for index in range(1, 4)],
    }
    result, cursors = await builder.build_split(
        grouped,
        {"甲": 2, "乙": 1},
        generator=_FakeGenerator(),  # type: ignore[arg-type]
        split="test",
        target_size=3,
        accepted_normalized_queries=[],
        accepted_target_keys=set(),
    )

    assert len(result.accepted) == 3
    assert cursors["甲"] == 1
    assert result.redistributions == [
        builder.QuotaRedistribution(exhausted_stratum="甲", shortfall=1, redistributed_to={"乙": 1})
    ]


def test_stable_query_id_and_split_exclusion_checks() -> None:
    normalized = builder.normalize_query_for_dedup("患者近来反复不适伴有睡眠波动和食欲下降甲" * 2)
    target = _record_key(0)
    assert builder.stable_query_id("test", target, normalized).startswith("test-")
    assert builder.stable_query_id("smoke", target, normalized).startswith("smoke-")
    assert builder.stable_query_id("test", target, normalized) != builder.stable_query_id("smoke", target, normalized)

    accepted = builder.AcceptedQuery(
        query_id="test-a",
        query="患者近来反复不适伴有睡眠波动和食欲下降甲" * 2,
        normalized_query=normalized,
        target_record_key=target,
        stratum="内科",
        source_symptom_sha256="a" * 64,
        query_sha256="b" * 64,
    )
    duplicate_target = builder.AcceptedQuery(
        query_id="smoke-b",
        query="另一条足够长的患者症状描述且包含不同的文字内容用于验证互斥规则甲乙丙丁",
        normalized_query="另一条足够长的患者症状描述且包含不同的文字内容用于验证互斥规则甲乙丙丁",
        target_record_key=target,
        stratum="内科",
        source_symptom_sha256="c" * 64,
        query_sha256="d" * 64,
    )
    assert builder.check_split_mutual_exclusion([duplicate_target], [accepted]) == ["shared_target_record_key: 1"]


class _RecordingGateway:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append((messages, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_query_generator_payload_contains_only_prompts_and_symptom_text() -> None:
    gateway = _RecordingGateway('{"query":"患者近来反复头晕乏力伴睡眠不佳食欲下降活动后加重晨起口干舌燥"}')
    generator = builder.QueryGenerator(gateway, model="rewrite-model", max_tokens=321)
    symptom_text = "患者反复头晕乏力心悸纳差睡眠不安活动后加重晨起口干咽燥小便偏黄大便正常" * 2

    result = await generator.generate(symptom_text, trace_id="test-trace")

    assert result.error_type is None
    assert len(gateway.calls) == 1
    messages, kwargs = gateway.calls[0]
    assert messages == [
        {"role": "system", "content": builder.SYSTEM_PROMPT},
        {"role": "user", "content": builder.USER_PROMPT_TEMPLATE.format(symptom_text=symptom_text)},
    ]
    payload_text = json.dumps(messages, ensure_ascii=False)
    assert "病例标题" not in payload_text
    assert "气血不足" not in payload_text
    assert "益气养血" not in payload_text
    assert "归脾汤" not in payload_text
    assert kwargs["model"] == "rewrite-model"
    assert kwargs["temperature"] == builder.QUERY_MODEL_TEMPERATURE
    assert kwargs["max_tokens"] == 321


def test_prompt_hashes_are_stable_and_separate() -> None:
    assert builder.sha256_text(builder.SYSTEM_PROMPT) == builder.sha256_text(builder.SYSTEM_PROMPT)
    assert builder.sha256_text(builder.USER_PROMPT_TEMPLATE) == builder.sha256_text(builder.USER_PROMPT_TEMPLATE)
    assert builder.sha256_text(builder.SYSTEM_PROMPT) != builder.sha256_text(builder.USER_PROMPT_TEMPLATE)


def test_verify_frozen_dataset_is_read_only_and_checks_full_schema(tmp_path: Path) -> None:
    bundle, dataset_dir = _write_valid_frozen_dataset(tmp_path)
    before = {path: path.read_bytes() for path in dataset_dir.iterdir()}

    assert builder.verify_frozen_dataset(dataset_dir, bundle) == []

    assert {path: path.read_bytes() for path in dataset_dir.iterdir()} == before


@pytest.mark.asyncio
async def test_build_rejects_an_existing_frozen_directory_without_writing(tmp_path: Path) -> None:
    output_dir = tmp_path / "frozen"
    output_dir.mkdir()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text('{"frozen": true}\n', encoding="utf-8")
    before = manifest_path.read_bytes()
    config = builder.BuildConfig(
        prepared_bundle=tmp_path / "unused",
        output_dir=output_dir,
        seed=builder.FIXED_SEED,
        smoke_size=builder.FIXED_SMOKE_SIZE,
        test_size=builder.FIXED_TEST_SIZE,
        query_model="",
    )

    assert await builder.run_build(config) == 1
    assert manifest_path.read_bytes() == before


@pytest.mark.asyncio
async def test_build_uses_configured_dedicated_rewrite_gateway_and_freezes_a_complete_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.core.config as config_module
    import app.core.gateway as gateway_module
    import app.core.rewrite_gateway as rewrite_gateway_module

    bundle, _ = _make_bundle(tmp_path)
    dedicated_gateway_settings = SimpleNamespace(name="dedicated-rewrite-settings")
    settings = SimpleNamespace(
        rag_query_rewrite_model="Qwen3.5-2B-free",
        chat_model="generic-chat-model",
        rag_query_rewrite_model_temperature=builder.QUERY_MODEL_TEMPERATURE,
        rag_query_rewrite_model_max_tokens=321,
    )

    class FakeModelGateway:
        instances: list[FakeModelGateway] = []

        def __init__(self, *, settings: Any) -> None:
            self.settings = settings
            self.calls: list[dict[str, Any]] = []
            self.__class__.instances.append(self)

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
            self.calls.append({"messages": messages, "kwargs": kwargs})
            source_char = str(messages[1]["content"])[-1]
            query_char = chr(ord(source_char) + 0x1000)
            return json.dumps({"query": f"患者反复不适伴随睡眠波动和食欲下降{query_char * 30}"})

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(gateway_module, "ModelGatewayClient", FakeModelGateway)
    monkeypatch.setattr(
        rewrite_gateway_module,
        "build_rewrite_gateway_settings",
        lambda configured_settings: dedicated_gateway_settings if configured_settings is settings else None,
    )
    output_dir = tmp_path / "frozen-output"
    config = builder.BuildConfig(
        prepared_bundle=bundle,
        output_dir=output_dir,
        seed=builder.FIXED_SEED,
        smoke_size=builder.FIXED_SMOKE_SIZE,
        test_size=builder.FIXED_TEST_SIZE,
        query_model="",
    )

    assert await builder.run_build(config) == 0
    assert builder.verify_frozen_dataset(output_dir, bundle) == []
    assert len(FakeModelGateway.instances) == 1
    fake_gateway = FakeModelGateway.instances[0]
    assert fake_gateway.settings is dedicated_gateway_settings
    assert len(fake_gateway.calls) == builder.FIXED_SMOKE_SIZE + builder.FIXED_TEST_SIZE
    assert all(call["kwargs"]["model"] == "Qwen3.5-2B-free" for call in fake_gateway.calls)
    assert all(call["kwargs"]["temperature"] == builder.QUERY_MODEL_TEMPERATURE for call in fake_gateway.calls)
    assert all(call["kwargs"]["max_tokens"] == 321 for call in fake_gateway.calls)
    manifest = builder.read_json_file(output_dir / "manifest.json")
    assert manifest["query_generator"]["model"] == "Qwen3.5-2B-free"
    assert manifest["query_generator"]["gateway_mode"] == "dedicated_rewrite_gateway"
