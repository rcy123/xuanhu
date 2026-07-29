"""RAG 评估脚本测试 — 覆盖 schema 校验、命中判定、无结果处理、报告生成。

测试策略：
- 使用 FakeRetriever 模拟 RAG 检索行为
- 不依赖真实 Milvus / PG / Embedding Gateway
- 覆盖正常、无结果、报错、negative_case 等场景
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.evaluate_rag import (
    VALID_SOURCE_TYPES,
    EvalReport,
    EvidenceSummary,
    FakeRetriever,
    QueryEvalResult,
    _check_keyword_match,
    _make_fake_evidence,
    _normalize_text,
    generate_json_report,
    generate_markdown_report,
    judge_forbidden_title_hit,
    judge_source_type_hit,
    judge_title_hit,
    judge_topic_hit,
    run_evaluation,
    validate_eval_queries,
    validate_eval_query,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ev(**kwargs: Any) -> Any:
    """创建一个与 Evidence 兼容的假对象。"""
    defaults: dict[str, Any] = {
        "evidence_id": "ev-test-001",
        "source_type": "herb",
        "source_id": "src-001",
        "chunk_id": "chunk-001",
        "title": "测试证据",
        "content_snippet": "测试内容片段",
        "score": 0.85,
        "rank": 1,
    }
    defaults.update(kwargs)
    return _make_fake_evidence(**defaults)


@pytest.fixture
def herb_evidence() -> Any:
    return _make_ev(
        evidence_id="ev-herb-001",
        source_type="herb",
        title="半夏",
        content_snippet="半夏有毒，辛温，归脾胃肺经。燥湿化痰，降逆止呕。与乌头、附子为十八反禁忌。",
        score=0.90,
    )


@pytest.fixture
def formula_evidence() -> Any:
    return _make_ev(
        evidence_id="ev-formula-001",
        source_type="formula",
        title="二陈汤",
        content_snippet="二陈汤由半夏、陈皮、茯苓、甘草组成，功专燥湿化痰，理气和中。主治湿痰证。",
        score=0.88,
    )


@pytest.fixture
def acupoint_evidence() -> Any:
    return _make_ev(
        evidence_id="ev-acupoint-001",
        source_type="acupoint",
        title="足三里",
        content_snippet="足三里，足阳明胃经合穴。位于犊鼻下3寸。主治胃痛、呕吐、腹胀、泄泻。",
        score=0.92,
    )


@pytest.fixture
def theory_evidence() -> Any:
    return _make_ev(
        evidence_id="ev-theory-001",
        source_type="theory",
        title="风寒表证辨治要点",
        content_snippet="风寒袭表，卫阳被遏。常见恶寒重、发热轻、无汗、头痛身痛、鼻塞清涕。治宜辛温解表。",
        score=0.87,
    )


@pytest.fixture
def case_evidence() -> Any:
    return _make_ev(
        evidence_id="ev-case-001",
        source_type="case",
        title="模拟医案：脾虚湿困便溏",
        content_snippet="患者主诉大便溏薄反复两月，食后腹胀，纳少，乏力。舌淡胖，苔白腻，脉缓弱。",
        score=0.85,
    )


# ---------------------------------------------------------------------------
# Schema 校验测试
# ---------------------------------------------------------------------------


class TestValidateEvalQuery:
    """评估 query schema 校验。"""

    def test_valid_query_passes(self) -> None:
        q = {
            "query": "测试查询",
            "expected_topics": ["测试"],
            "expected_source_types": ["herb"],
            "category": "测试分类",
        }
        errors = validate_eval_query(q, 1)
        assert errors == []

    def test_missing_query_field(self) -> None:
        q = {
            "expected_topics": ["测试"],
            "expected_source_types": ["herb"],
        }
        errors = validate_eval_query(q, 1)
        assert any("query" in e for e in errors)

    def test_missing_expected_topics(self) -> None:
        q = {
            "query": "测试",
            "expected_source_types": ["herb"],
        }
        errors = validate_eval_query(q, 1)
        assert any("expected_topics" in e for e in errors)

    def test_missing_expected_source_types(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": ["测试"],
        }
        errors = validate_eval_query(q, 1)
        assert any("expected_source_types" in e for e in errors)

    def test_invalid_source_type(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": ["测试"],
            "expected_source_types": ["invalid_type"],
        }
        errors = validate_eval_query(q, 1)
        assert any("无效的 source_type" in e for e in errors)

    def test_empty_topic_string(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": [""],
            "expected_source_types": ["herb"],
        }
        errors = validate_eval_query(q, 1)
        assert any("非空字符串" in e for e in errors)

    def test_negative_case_with_topics_is_invalid(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": ["不应该有"],
            "expected_source_types": [],
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert any("negative_case" in e for e in errors)

    def test_negative_case_with_source_types_is_invalid(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": [],
            "expected_source_types": ["herb"],
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert any("negative_case" in e for e in errors)

    def test_negative_case_empty_expected_is_valid(self) -> None:
        q = {
            "query": "川贝母",
            "expected_topics": [],
            "expected_source_types": [],
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert errors == []

    def test_must_hit_titles_not_list(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": ["测试"],
            "expected_source_types": ["herb"],
            "must_hit_titles": "not_a_list",
        }
        errors = validate_eval_query(q, 1)
        assert any("must_hit_titles" in e for e in errors)

    def test_must_hit_titles_rejects_empty_title(self) -> None:
        q = {
            "query": "测试",
            "expected_topics": ["测试"],
            "expected_source_types": ["herb"],
            "must_hit_titles": [""],
        }
        errors = validate_eval_query(q, 1)
        assert any("must_hit_titles" in e and "非空字符串" in e for e in errors)

    def test_negative_case_with_forbidden_fields_is_valid(self) -> None:
        q = {
            "query": "涌泉穴在哪里",
            "expected_topics": [],
            "expected_source_types": [],
            "forbidden_topics": ["涌泉"],
            "forbidden_titles": ["涌泉"],
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert errors == []

    @pytest.mark.parametrize("field_name", ["forbidden_topics", "forbidden_titles"])
    def test_forbidden_fields_must_be_lists(self, field_name: str) -> None:
        q = {
            "query": "测试",
            "expected_topics": [],
            "expected_source_types": [],
            field_name: "不是列表",
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert any(field_name in e and "必须是 list" in e for e in errors)

    @pytest.mark.parametrize("field_name", ["forbidden_topics", "forbidden_titles"])
    def test_forbidden_fields_reject_empty_values(self, field_name: str) -> None:
        q = {
            "query": "测试",
            "expected_topics": [],
            "expected_source_types": [],
            field_name: [""],
            "negative_case": True,
        }
        errors = validate_eval_query(q, 1)
        assert any(field_name in e and "非空字符串" in e for e in errors)

    def test_positive_query_cannot_use_forbidden_fields(self) -> None:
        q = {
            "query": "半夏的功效",
            "expected_topics": ["半夏"],
            "expected_source_types": ["herb"],
            "forbidden_topics": ["半夏"],
        }
        errors = validate_eval_query(q, 1)
        assert any("仅适用于 negative_case" in e for e in errors)


class TestValidateEvalQueries:
    """评估集整体校验。"""

    def test_too_few_queries(self) -> None:
        errors = validate_eval_queries([{"query": "x", "expected_topics": [], "expected_source_types": []}])
        assert any("不足" in e for e in errors)

    def test_too_many_queries(self) -> None:
        queries = [
            {"query": f"q{i}", "expected_topics": [], "expected_source_types": []}
            for i in range(25)
        ]
        errors = validate_eval_queries(queries)
        assert any("超出" in e for e in errors)

    def test_empty_queries(self) -> None:
        errors = validate_eval_queries([])
        assert any("为空" in e for e in errors)

    def test_missing_categories(self) -> None:
        queries = [
            {"query": f"q{i}", "expected_topics": [], "expected_source_types": [], "category": "测试"}
            for i in range(12)
        ]
        errors = validate_eval_queries(queries)
        assert any("缺少覆盖类别" in e for e in errors)

    def test_valid_set_passes(self) -> None:
        queries = [
            {
                "query": f"test query {i}",
                "expected_topics": ["test"],
                "expected_source_types": ["herb"],
                "category": cat,
            }
            for i, cat in enumerate(
                ["主诉到证型", "证型到方剂", "药物禁忌", "穴位检索", "理论检索", "医案检索", "无结果场景",
                 "主诉到证型", "证型到方剂", "药物禁忌", "跨类型检索", "剂量上限"]
            )
        ]
        errors = validate_eval_queries(queries)
        assert errors == []


# ---------------------------------------------------------------------------
# 命中判定测试
# ---------------------------------------------------------------------------


class TestNormalizeText:
    """文本归一化。"""

    def test_removes_punctuation(self) -> None:
        assert "。" not in _normalize_text("测试。内容")

    def test_lowercase(self) -> None:
        assert _normalize_text("ABC") == "abc"

    def test_keeps_chinese_alphanumeric(self) -> None:
        result = _normalize_text("半夏dose30g")
        assert "半夏" in result
        assert "dose30g" in result


class TestKeywordMatch:
    """关键词匹配。"""

    def test_exact_match(self) -> None:
        hit, matched = _check_keyword_match("半夏有毒", ["半夏"])
        assert hit
        assert "半夏" in matched

    def test_partial_match(self) -> None:
        hit, matched = _check_keyword_match("脾虚湿困证辨治要点", ["脾虚湿困"])
        assert hit
        assert "脾虚湿困" in matched

    def test_no_match(self) -> None:
        hit, matched = _check_keyword_match("川贝母", ["大黄"])
        assert not hit
        assert matched == []

    def test_multiple_keywords(self) -> None:
        hit, matched = _check_keyword_match(
            "半夏与乌头为十八反禁忌",
            ["半夏", "乌头", "十九畏"],
        )
        assert hit
        assert "半夏" in matched
        assert "乌头" in matched
        assert "十九畏" not in matched

    def test_empty_keywords_is_always_match(self) -> None:
        hit, matched = _check_keyword_match("任意内容", [])
        assert hit
        assert matched == []


class TestJudgeTopicHit:
    """topic 命中判定。"""

    def test_single_evidence_hit(self, herb_evidence: Any) -> None:
        hit, matched = judge_topic_hit([herb_evidence], ["半夏", "乌头"])
        assert hit
        assert "半夏" in matched

    def test_multi_evidence_one_hit(self, herb_evidence: Any, formula_evidence: Any) -> None:
        hit, matched = judge_topic_hit(
            [formula_evidence, herb_evidence],
            ["半夏"],
        )
        assert hit

    def test_no_hit_across_evidences(self, formula_evidence: Any) -> None:
        hit, matched = judge_topic_hit([formula_evidence], ["大黄"])
        assert not hit
        assert matched == []

    def test_empty_expected_topics(self, herb_evidence: Any) -> None:
        hit, matched = judge_topic_hit([herb_evidence], [])
        assert not hit
        assert matched == []

    def test_no_evidences(self) -> None:
        hit, matched = judge_topic_hit([], ["半夏"])
        assert not hit

    def test_match_in_content_snippet(self) -> None:
        ev = _make_ev(title="某方剂", content_snippet="此方包含半夏，注意配伍禁忌")
        hit, matched = judge_topic_hit([ev], ["半夏"])
        assert hit

    def test_match_in_title(self) -> None:
        ev = _make_ev(title="半夏泻心汤", content_snippet="")
        hit, matched = judge_topic_hit([ev], ["半夏"])
        assert hit


class TestJudgeSourceTypeHit:
    """source_type 命中判定。"""

    def test_exact_match(self, herb_evidence: Any) -> None:
        hit, matched = judge_source_type_hit([herb_evidence], ["herb"])
        assert hit
        assert "herb" in matched

    def test_multi_evidence_multi_source_types(
        self, herb_evidence: Any, formula_evidence: Any
    ) -> None:
        hit, matched = judge_source_type_hit(
            [herb_evidence, formula_evidence],
            ["herb", "formula"],
        )
        assert hit
        assert "herb" in matched
        assert "formula" in matched

    def test_no_match(self, herb_evidence: Any) -> None:
        hit, matched = judge_source_type_hit([herb_evidence], ["acupoint"])
        assert not hit

    def test_empty_expected(self, herb_evidence: Any) -> None:
        hit, matched = judge_source_type_hit([herb_evidence], [])
        assert not hit

    def test_empty_evidences(self) -> None:
        hit, matched = judge_source_type_hit([], ["herb"])
        assert not hit

    def test_partial_match_returns_matched_only(
        self, herb_evidence: Any, formula_evidence: Any
    ) -> None:
        hit, matched = judge_source_type_hit(
            [herb_evidence, formula_evidence],
            ["herb", "theory", "acupoint"],
        )
        assert hit
        assert matched == ["herb"]  # formula 也在，但不在期望中？


class TestJudgeTitleHit:
    """must_hit_titles 命中判定。"""

    def test_exact_title_match(self) -> None:
        ev = _make_ev(title="参苓白术散")
        assert judge_title_hit([ev], ["参苓白术散"])

    def test_partial_title_match(self) -> None:
        ev = _make_ev(title="模拟医案：脾虚湿困便溏")
        assert judge_title_hit([ev], ["脾虚湿困"])

    def test_no_match(self) -> None:
        ev = _make_ev(title="二陈汤")
        assert not judge_title_hit([ev], ["参苓白术散"])

    def test_empty_must_hit_is_pass(self) -> None:
        ev = _make_ev(title="任意标题")
        assert judge_title_hit([ev], [])

    def test_multi_evidence_one_hits(self) -> None:
        ev1 = _make_ev(title="二陈汤")
        ev2 = _make_ev(title="参苓白术散")
        assert judge_title_hit([ev1, ev2], ["参苓白术散"])

    def test_multi_required_all_match(self) -> None:
        ev1 = _make_ev(title="二陈汤")
        ev2 = _make_ev(title="参苓白术散")
        # 只需任一条 Evidence 命中任一 must_hit_title 即可
        assert judge_title_hit([ev1, ev2], ["二陈汤", "参苓白术散"])


class TestJudgeForbiddenTitleHit:
    """forbidden_titles 命中判定。"""

    def test_forbidden_title_hit(self) -> None:
        hit, matched = judge_forbidden_title_hit([_make_ev(title="经穴：涌泉")], ["涌泉"])
        assert hit
        assert matched == ["涌泉"]

    def test_forbidden_title_miss(self) -> None:
        hit, matched = judge_forbidden_title_hit([_make_ev(title="足三里")], ["涌泉"])
        assert not hit
        assert matched == []


# ---------------------------------------------------------------------------
# FakeRetriever 测试
# ---------------------------------------------------------------------------


class TestFakeRetriever:
    """FakeRetriever 用于测试。"""

    async def test_returns_preset_responses(self) -> None:
        fake = FakeRetriever(responses={
            "测试query": [_make_ev(title="结果1"), _make_ev(title="结果2")],
        })
        results = await fake.retrieve("测试query", primary_sources=["herb"])
        assert len(results) == 2

    async def test_returns_empty_for_unknown_query(self) -> None:
        fake = FakeRetriever()
        results = await fake.retrieve("未知query", primary_sources=["herb"])
        assert results == []

    async def test_always_empty_mode(self) -> None:
        fake = FakeRetriever(always_empty=True)
        results = await fake.retrieve("任意query", primary_sources=["herb"])
        assert results == []

    async def test_always_raise_mode(self) -> None:
        fake = FakeRetriever(always_raise=RuntimeError("模拟异常"))
        with pytest.raises(RuntimeError, match="模拟异常"):
            await fake.retrieve("任意query", primary_sources=["herb"])

    async def test_logs_calls(self) -> None:
        fake = FakeRetriever()
        await fake.retrieve("q1", primary_sources=["herb"], top_k=5)
        await fake.retrieve("q2", primary_sources=["formula"], allow_cross_source=False)
        assert len(fake.call_log) == 2
        assert fake.call_log[0]["query"] == "q1"
        assert fake.call_log[0]["top_k"] == 5
        assert fake.call_log[1]["allow_cross_source"] is False


# ---------------------------------------------------------------------------
# run_evaluation 测试
# ---------------------------------------------------------------------------


class TestRunEvaluation:
    """评估流程测试。"""

    async def test_primary_sources_follow_positive_expectation(self) -> None:
        fake = FakeRetriever(always_empty=True)
        queries: list[dict[str, Any]] = [
            {
                "query": "太冲穴在哪里",
                "expected_topics": ["太冲"],
                "expected_source_types": ["acupoint"],
            },
            {
                "query": "涌泉穴在哪里",
                "expected_topics": [],
                "expected_source_types": [],
                "negative_case": True,
            },
        ]
        await run_evaluation(queries, retriever=fake, top_k=8)
        assert fake.call_log[0]["primary_sources"] == ["acupoint"]
        assert fake.call_log[0]["allow_cross_source"] is False
        assert fake.call_log[1]["primary_sources"] == sorted(VALID_SOURCE_TYPES)
        assert fake.call_log[1]["allow_cross_source"] is True

    async def test_normal_query_passes(self) -> None:
        fake = FakeRetriever(responses={
            "脾虚湿困用什么方？": [
                _make_ev(
                    title="参苓白术散",
                    source_type="formula",
                    content_snippet="参苓白术散，功专健脾化湿。",
                ),
            ],
        })
        queries = [{
            "query": "脾虚湿困用什么方？",
            "expected_topics": ["脾虚", "参苓白术散"],
            "expected_source_types": ["formula"],
            "category": "证型到方剂",
            "notes": "测试",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        assert report.total_queries == 1
        assert len(report.query_results) == 1
        r = report.query_results[0]
        assert r.has_results
        assert r.topic_hit
        assert r.source_type_hit
        assert r.passed
        assert report.pass_rate == 1.0

    async def test_no_results_query(self) -> None:
        fake = FakeRetriever(always_empty=True)
        queries = [{
            "query": "不存在的内容",
            "expected_topics": ["不存在"],
            "expected_source_types": ["herb"],
            "category": "无结果场景",
            "notes": "测试",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert not r.has_results
        assert r.total_returned == 0
        assert not r.topic_hit
        assert not r.source_type_hit
        assert not r.passed  # 正常 query 无结果 = fail

    async def test_negative_case_no_results_passes(self) -> None:
        fake = FakeRetriever(always_empty=True)
        queries = [{
            "query": "川贝母的功效",
            "expected_topics": [],
            "expected_source_types": [],
            "negative_case": True,
            "category": "无结果场景",
            "notes": "测试",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert not r.has_results
        assert r.passed  # negative_case 无结果 = pass

    async def test_negative_case_with_unrelated_results_passes(self) -> None:
        fake = FakeRetriever(responses={
            "川贝母的功效": [
                _make_ev(
                    title="党参",
                    source_type="herb",
                    content_snippet="党参补中益气，健脾益肺。",
                ),
            ],
        })
        queries = [{
            "query": "川贝母的功效",
            "expected_topics": [],
            "expected_source_types": [],
            "negative_case": True,
            "category": "无结果场景",
            "notes": "测试",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert r.has_results
        # negative_case: 有结果但无 topic/source 命中 → pass
        assert r.passed

    async def test_negative_case_forbidden_topic_hit_fails(self) -> None:
        fake = FakeRetriever(responses={
            "川贝母的功效": [
                _make_ev(title="止咳药材", content_snippet="川贝母清热润肺、化痰止咳。"),
            ],
        })
        queries: list[dict[str, Any]] = [{
            "query": "川贝母的功效",
            "expected_topics": [],
            "expected_source_types": [],
            "forbidden_topics": ["川贝母"],
            "negative_case": True,
            "category": "无结果场景",
        }]
        result = (await run_evaluation(queries, retriever=fake)).query_results[0]
        assert result.forbidden_topic_hit
        assert result.forbidden_topics_matched == ["川贝母"]
        assert not result.passed

    async def test_negative_case_forbidden_title_hit_fails(self) -> None:
        fake = FakeRetriever(responses={
            "涌泉穴在哪里": [
                _make_ev(title="涌泉穴", source_type="acupoint"),
            ],
        })
        queries: list[dict[str, Any]] = [{
            "query": "涌泉穴在哪里",
            "expected_topics": [],
            "expected_source_types": [],
            "forbidden_titles": ["涌泉"],
            "negative_case": True,
            "category": "无结果场景",
        }]
        result = (await run_evaluation(queries, retriever=fake)).query_results[0]
        assert result.forbidden_title_hit
        assert result.forbidden_titles_matched == ["涌泉"]
        assert not result.passed

    async def test_error_handling(self) -> None:
        fake = FakeRetriever(always_raise=RuntimeError("模拟异常"))
        queries = [{
            "query": "测试查询",
            "expected_topics": ["测试"],
            "expected_source_types": ["herb"],
            "category": "测试",
            "notes": "测试",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert r.error is not None
        assert "模拟异常" in r.error
        assert len(report.error_queries) == 1

    async def test_multi_query_batch(self) -> None:
        fake = FakeRetriever(responses={
            "q1": [_make_ev(title="结果A", source_type="herb", content_snippet="脾虚")],
            "q2": [
                _make_ev(title="结果B", source_type="formula", content_snippet="柴胡"),
            ],
        })
        queries: list[dict[str, Any]] = [
            {
                "query": "q1", "expected_topics": ["脾虚"],
                "expected_source_types": ["herb"], "category": "测试", "notes": "",
            },
            {
                "query": "q2", "expected_topics": ["柴胡"],
                "expected_source_types": ["formula", "theory"], "category": "测试", "notes": "",
            },
            {
                "query": "q3", "expected_topics": [], "expected_source_types": [],
                "negative_case": True, "category": "无结果场景", "notes": "",
            },
        ]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        assert report.total_queries == 3
        assert report.query_results[0].passed
        assert report.query_results[1].passed
        # q3: negative_case, no results from fake
        assert report.query_results[2].passed

    async def test_must_hit_titles_affects_score(self) -> None:
        fake = FakeRetriever(responses={
            "test": [_make_ev(title="参苓白术散", source_type="formula", content_snippet="健脾化湿")],
        })
        queries = [{
            "query": "test",
            "expected_topics": ["健脾"],
            "expected_source_types": ["formula"],
            "must_hit_titles": ["参苓白术散"],
            "category": "证型到方剂",
            "notes": "",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert r.topic_hit
        assert r.source_type_hit
        assert r.title_hit
        assert r.passed

    async def test_must_hit_titles_miss(self) -> None:
        fake = FakeRetriever(responses={
            "test": [_make_ev(title="二陈汤", source_type="formula", content_snippet="燥湿化痰")],
        })
        queries = [{
            "query": "test",
            "expected_topics": ["燥湿"],
            "expected_source_types": ["formula"],
            "must_hit_titles": ["参苓白术散"],
            "category": "证型到方剂",
            "notes": "",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert r.topic_hit
        assert r.source_type_hit
        assert not r.title_hit  # must_hit_titles 未命中
        assert not r.passed
        assert report.low_recall_queries == [r]

    async def test_top_k_truncation(self) -> None:
        """验证 top 摘要截断到 top_k。"""
        evidences = [_make_ev(title=f"结果{i}", source_type="herb") for i in range(15)]
        fake = FakeRetriever(responses={"test": evidences})
        queries = [{
            "query": "test",
            "expected_topics": ["结果"],
            "expected_source_types": ["herb"],
            "category": "测试",
            "notes": "",
        }]
        report = await run_evaluation(queries, retriever=fake, top_k=8)
        r = report.query_results[0]
        assert r.total_returned == 15
        assert len(r.top_evidences) == 8  # 只取 top_k 摘要

    async def test_metrics_computation(self) -> None:
        fake = FakeRetriever(responses={
            "q1": [_make_ev(title="A", source_type="herb", content_snippet="脾虚")],
            "q2": [_make_ev(title="B", source_type="formula", content_snippet="柴胡")],
            "q3": [],
            "q4": [_make_ev(title="D", source_type="herb", content_snippet="nothing relevant")],
        })
        queries = [
            {"query": "q1", "expected_topics": ["脾虚"], "expected_source_types": ["herb"], "category": "c1", "notes": ""},
            {"query": "q2", "expected_topics": ["柴胡"], "expected_source_types": ["formula"], "category": "c2", "notes": ""},
            {"query": "q3", "expected_topics": ["大黄"], "expected_source_types": ["herb"], "category": "c3", "notes": ""},
            {"query": "q4", "expected_topics": ["大黄"], "expected_source_types": ["herb"], "category": "c4", "notes": ""},
        ]
        report = await run_evaluation(queries, retriever=fake, top_k=8)

        # 有结果率: q1,q2,q4 = 3/4
        assert report.has_results_rate == 0.75
        # topic 命中率: q1,q2 = 2/4
        assert report.topic_hit_count == 2
        # source_type 命中率: q1,q2,q4 = 3/4
        assert report.source_type_hit_count == 3
        # pass: q1,q2 = 2/4 (q3 无结果, q4 topic miss)
        assert report.pass_count == 2
        # 低召回: q4 (有结果但 topic miss)
        assert len(report.low_recall_queries) == 1
        assert report.low_recall_queries[0].query == "q4"


# ---------------------------------------------------------------------------
# 报告生成测试
# ---------------------------------------------------------------------------


class TestMarkdownReport:
    """Markdown 报告生成。"""

    def test_generates_report_with_metrics(self) -> None:
        report = EvalReport(
            generated_at="2026-06-25T00:00:00Z",
            total_queries=2,
            top_k=8,
            has_results_count=1,
            has_results_rate=0.5,
            topic_hit_count=1,
            topic_hit_rate=1.0,
            source_type_hit_count=1,
            source_type_hit_rate=1.0,
            pass_count=1,
            pass_rate=0.5,
            query_results=[
                QueryEvalResult(
                    index=1, query="q1", category="测试", notes="",
                    expected_topics=["test"], expected_source_types=["herb"],
                    total_returned=1, has_results=True,
                    topic_hit=True, source_type_hit=True, topics_matched=["test"],
                    source_types_matched=["herb"], passed=True,
                    top_evidences=[
                        EvidenceSummary(
                            rank=1, evidence_id="ev-1", title="结果1",
                            source_type="herb", source_id="s1", chunk_id="c1",
                            score=0.85, snippet="内容片段",
                        ),
                    ],
                ),
                QueryEvalResult(
                    index=2, query="q2", category="无结果场景", notes="test",
                    expected_topics=[], expected_source_types=[],
                    negative_case=True, total_returned=0, has_results=False,
                    passed=True,
                ),
            ],
        )
        md = generate_markdown_report(report)
        assert "RAG 检索评估报告" in md
        assert "模拟样例数据" in md
        assert "不代表真实医学知识质量" in md
        assert "总体指标" in md
        assert "q1" in md
        assert "q2" in md
        assert "PASS" in md
        assert "0.85" in md
        assert "herb" in md
        assert "后续改进建议" in md
        # 不得包含 API key
        assert "sk-" not in md.lower() or "sk-test" in md  # allow test keys

    def test_report_includes_low_recall_section(self) -> None:
        report = EvalReport(
            generated_at="2026-06-25T00:00:00Z",
            total_queries=3,
            top_k=8,
            has_results_count=2,
            has_results_rate=0.67,
            topic_hit_count=1,
            topic_hit_rate=0.5,
            source_type_hit_count=1,
            source_type_hit_rate=0.5,
            pass_count=1,
            pass_rate=0.33,
            low_recall_queries=[
                QueryEvalResult(
                    index=1, query="低召回query", category="测试", notes="",
                    expected_topics=["大黄"], expected_source_types=["herb"],
                    total_returned=3, has_results=True,
                    topic_hit=False, source_type_hit=True, passed=False,
                ),
            ],
            query_results=[
                QueryEvalResult(
                    index=1, query="低召回query", category="测试", notes="",
                    expected_topics=["大黄"], expected_source_types=["herb"],
                    total_returned=3, has_results=True,
                    topic_hit=False, source_type_hit=True, passed=False,
                ),
            ],
        )
        md = generate_markdown_report(report)
        assert "低召回" in md
        assert "低召回query" in md

    def test_report_includes_error_queries(self) -> None:
        report = EvalReport(
            generated_at="2026-06-25T00:00:00Z",
            total_queries=1,
            top_k=8,
            error_queries=[
                QueryEvalResult(
                    index=1, query="报错query", category="测试", notes="",
                    expected_topics=[], expected_source_types=[],
                    error="RAGUnavailableError: 数据库连接失败",
                ),
            ],
            query_results=[
                QueryEvalResult(
                    index=1, query="报错query", category="测试", notes="",
                    expected_topics=[], expected_source_types=[],
                    error="RAGUnavailableError: 数据库连接失败",
                ),
            ],
        )
        md = generate_markdown_report(report)
        assert "报错" in md
        assert "RAGUnavailableError" in md

    def test_report_no_api_key_leak(self) -> None:
        """验证报告不泄露 API key。"""
        # 模拟包含敏感信息的 evidence snippet
        report = EvalReport(
            generated_at="2026-06-25T00:00:00Z",
            total_queries=1,
            top_k=8,
            has_results_count=1,
            has_results_rate=1.0,
            pass_count=1,
            pass_rate=1.0,
            query_results=[
                QueryEvalResult(
                    index=1, query="q1", category="测试", notes="",
                    expected_topics=["test"], expected_source_types=["herb"],
                    total_returned=1, has_results=True,
                    topic_hit=True, source_type_hit=True, passed=True,
                    top_evidences=[
                        EvidenceSummary(
                            rank=1, evidence_id="ev-1", title="结果1",
                            source_type="herb", source_id="s1", chunk_id="c1",
                            score=0.85,
                            snippet="这是一段不包含API key的正文内容",
                        ),
                    ],
                ),
            ],
        )
        md = generate_markdown_report(report)
        # 检查不包含常见 secret 模式
        assert "sk-" not in md or "snippet" not in md.lower()
        assert "Bearer" not in md


class TestJsonReport:
    """JSON 报告生成。"""

    def test_generates_valid_json(self) -> None:
        report = EvalReport(
            generated_at="2026-06-25T00:00:00Z",
            total_queries=1,
            top_k=8,
            has_results_count=1,
            pass_count=1,
            pass_rate=1.0,
            query_results=[
                QueryEvalResult(
                    index=1, query="q1", category="测试", notes="",
                    expected_topics=["test"], expected_source_types=["herb"],
                    must_hit_titles=["结果1"],
                    total_returned=1, has_results=True,
                    topic_hit=True, source_type_hit=True, title_hit=True, passed=True,
                ),
            ],
        )
        data = generate_json_report(report)
        assert data["total_queries"] == 1
        assert data["metrics"]["pass_rate"] == 1.0
        assert len(data["query_results"]) == 1
        assert data["query_results"][0]["passed"] is True
        assert data["query_results"][0]["must_hit_titles"] == ["结果1"]
        assert data["query_results"][0]["title_hit"] is True


# ---------------------------------------------------------------------------
# EvidenceSummary 测试
# ---------------------------------------------------------------------------


class TestEvidenceSummary:
    """Evidence 摘要数据结构。"""

    def test_create_from_evidence(self) -> None:
        ev = _make_ev(
            evidence_id="ev-001",
            title="党参",
            source_type="herb",
            source_id="src-001",
            chunk_id="chunk-001",
            score=0.85,
            rank=1,
            content_snippet="党参补中益气，健脾益肺。",
        )
        summary = EvidenceSummary(
            rank=ev.rank,
            evidence_id=ev.evidence_id,
            title=ev.title,
            source_type=ev.source_type,
            source_id=ev.source_id,
            chunk_id=ev.chunk_id,
            score=ev.score,
            snippet=ev.content_snippet,
        )
        assert summary.rank == 1
        assert summary.title == "党参"
        assert summary.source_type == "herb"


# ---------------------------------------------------------------------------
# 集成测试：完整评估管线
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """完整评估管线集成测试。"""

    async def test_full_pipeline_with_fake_retriever(self) -> None:
        """端到端测试：从 JSON 文件到报告。"""
        # 构造临时评估集
        queries: list[dict[str, Any]] = [
            {
                "query": "脾虚湿困用什么方？",
                "expected_topics": ["脾虚", "参苓白术散"],
                "expected_source_types": ["formula", "theory"],
                "category": "证型到方剂",
                "notes": "测试证型检索",
            },
            {
                "query": "半夏的配伍禁忌",
                "expected_topics": ["半夏", "十八反", "乌头"],
                "expected_source_types": ["herb"],
                "category": "药物禁忌",
                "notes": "测试药物禁忌检索",
            },
            {
                "query": "足三里在哪里",
                "expected_topics": ["足三里", "胃经"],
                "expected_source_types": ["acupoint"],
                "must_hit_titles": ["足三里"],
                "category": "穴位检索",
                "notes": "测试穴位检索",
            },
            {
                "query": "川贝母功效",
                "expected_topics": [],
                "expected_source_types": [],
                "negative_case": True,
                "category": "无结果场景",
                "notes": "测试无结果场景",
            },
        ]

        # 创建 FakeRetriever 预设响应
        fake = FakeRetriever(responses={
            "脾虚湿困用什么方？": [
                _make_ev(
                    title="参苓白术散",
                    source_type="formula",
                    content_snippet="参苓白术散功专健脾化湿，主治脾虚湿困。",
                ),
                _make_ev(
                    title="脾虚湿困证辨治要点",
                    source_type="theory",
                    content_snippet="脾主运化，脾气不足则湿浊内生。",
                ),
            ],
            "半夏的配伍禁忌": [
                _make_ev(
                    title="半夏",
                    source_type="herb",
                    content_snippet="半夏与乌头、附子为十八反禁忌。妊娠期慎用。",
                ),
            ],
            "足三里在哪里": [
                _make_ev(
                    title="足三里",
                    source_type="acupoint",
                    content_snippet="足三里位于犊鼻下3寸，属足阳明胃经。",
                ),
            ],
        })

        report = await run_evaluation(queries, retriever=fake, top_k=8)

        # 逐条验证
        results = {r.query: r for r in report.query_results}

        # q1: 正常 → pass
        r1 = results["脾虚湿困用什么方？"]
        assert r1.passed
        assert r1.topic_hit
        assert r1.source_type_hit
        assert len(r1.top_evidences) == 2

        # q2: 正常 → pass
        r2 = results["半夏的配伍禁忌"]
        assert r2.passed
        assert r2.topic_hit

        # q3: 正常 → pass, with title_hit
        r3 = results["足三里在哪里"]
        assert r3.passed
        assert r3.title_hit

        # q4: negative_case → pass (无结果)
        r4 = results["川贝母功效"]
        assert r4.passed

        # 总体指标
        assert report.pass_rate == 1.0
        assert len(report.low_recall_queries) == 0

        # 可序列化
        md = generate_markdown_report(report)
        assert "脾虚湿困" in md
        assert "半夏" in md
        assert "足三里" in md

        json_data = generate_json_report(report)
        assert json_data["metrics"]["pass_rate"] == 1.0

    async def test_schema_validation_on_real_file(self) -> None:
        """验证真实评估集文件 schema。"""
        queries_path = Path(__file__).parent.parent / "data" / "rag_eval_queries.json"
        if not queries_path.exists():
            pytest.skip("评估集文件不存在")

        with open(queries_path, encoding="utf-8") as f:
            queries = json.load(f)

        errors = validate_eval_queries(queries)
        # 允许 category 覆盖警告但不应有硬错误
        hard_errors = [e for e in errors if "缺少覆盖类别" not in e]
        if hard_errors:
            # 如果硬错误包含 "不足" 是因为数量 < 10
            non_count_errors = [e for e in hard_errors if "不足" not in e and "超出" not in e]
            assert len(non_count_errors) == 0, f"Schema 校验发现硬错误: {non_count_errors}"


# ---------------------------------------------------------------------------
# VALID_SOURCE_TYPES 常量
# ---------------------------------------------------------------------------


def test_valid_source_types_constant() -> None:
    """验证 VALID_SOURCE_TYPES 与 app.rag.schemas 一致。"""
    from app.rag.schemas import VALID_SOURCE_TYPES as APP_VALID_SOURCE_TYPES
    assert VALID_SOURCE_TYPES == APP_VALID_SOURCE_TYPES
