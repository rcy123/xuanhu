"""RAG 推理检索策略层测试：query 构造、降级封装、阶段开关。"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.reasoning_retrieval import (
    FORMULA_PRIMARY_SOURCES,
    SYNDROME_PRIMARY_SOURCES,
    build_formula_query,
    build_syndrome_query,
    evidence_context_items,
    retrieve_formula_evidence,
    retrieve_syndrome_evidence,
    stage_rag_enabled,
)
from app.rag.schemas import Evidence


class _Obs:
    def __init__(self, fact_key: str, value: Any) -> None:
        self.fact_key = fact_key
        self.value = value


class FakeRetriever:
    def __init__(self, evidence: list[Evidence] | None = None, error: Exception | None = None) -> None:
        self.evidence = evidence
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, query: str, primary_sources: list[str], **kwargs: Any) -> list[Evidence]:
        self.calls.append({"query": query, "primary_sources": primary_sources, **kwargs})
        if self.error is not None:
            raise self.error
        return list(self.evidence or ())


class DualFakeRetriever(FakeRetriever):
    def __init__(self, evidence: list[Evidence] | None = None, error: Exception | None = None) -> None:
        super().__init__(evidence=evidence, error=error)
        self.dual_calls: list[dict[str, Any]] = []

    async def retrieve_dual_query(
        self,
        original_query: str,
        rewritten_query: str,
        primary_sources: list[str],
        **kwargs: Any,
    ) -> list[Evidence]:
        self.dual_calls.append(
            {
                "original_query": original_query,
                "rewritten_query": rewritten_query,
                "primary_sources": primary_sources,
                **kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        return list(self.evidence or ())


def _evidence(evidence_id: str, *, source_type: str = "case", rank: int = 1) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id="00000000-0000-0000-0000-000000000001",
        title=f"t-{evidence_id}",
        content_snippet="x" * 300,
        score=0.8,
        rank=rank,
    )


# ---------------------------------------------------------------------------
# query 构造
# ---------------------------------------------------------------------------


def test_build_syndrome_query_prefers_white_listed_keys() -> None:
    obs = (
        _Obs("chief_complaint.symptom", "咳嗽三天"),
        _Obs("ten_questions.cold_heat", "怕冷，微热"),
        _Obs("patient.age", 45),  # 非白名单，放后面
        _Obs("present_illness.cough", "咳声重浊"),
    )
    query = build_syndrome_query(obs, max_chars=2000)
    assert query.startswith("chief_complaint.symptom=")
    # 白名单键在前
    assert query.index("chief_complaint.symptom=") < query.index("patient.age=")
    assert "咳嗽三天" in query
    assert "怕冷" in query
    assert "咳声重浊" in query


def test_build_syndrome_query_skips_empty_and_dedups() -> None:
    obs = (
        _Obs("chief_complaint.symptom", "头痛"),
        _Obs("chief_complaint.symptom", None),  # 空值跳过
        _Obs("ten_questions.sweat", ""),  # 空字符串跳过
        _Obs("chief_complaint.symptom", "头痛"),  # 同 key 去重
    )
    query = build_syndrome_query(obs, max_chars=2000)
    assert "头痛" in query
    assert query.count("chief_complaint.symptom=") == 1


def test_build_syndrome_query_truncates() -> None:
    obs = (_Obs("chief_complaint.symptom", "咳" * 1000),)
    query = build_syndrome_query(obs, max_chars=100)
    assert len(query) <= 100


def test_build_formula_query_uses_syndrome_and_symptoms() -> None:
    class _Syndrome:
        syndrome = "风寒束肺证"
        treatment_principle = "疏风散寒，宣肺止咳"

    obs = (_Obs("chief_complaint.symptom", "咳嗽三天"), _Obs("ten_questions.cold_heat", "怕冷"))
    query = build_formula_query(_Syndrome(), obs, max_chars=2000)
    assert "证型=风寒束肺证" in query
    assert "治法=疏风散寒，宣肺止咳" in query
    assert "症状=" in query
    assert "咳嗽三天" in query


def test_build_formula_query_falls_back_to_symptoms_without_syndrome() -> None:
    class _EmptySyndrome:
        syndrome = None
        treatment_principle = None

    obs = (_Obs("chief_complaint.symptom", "腹泻"),)
    query = build_formula_query(_EmptySyndrome(), obs, max_chars=2000)
    assert query == "症状=chief_complaint.symptom=腹泻"


def test_build_syndrome_query_empty_returns_empty_string() -> None:
    assert build_syndrome_query([], max_chars=2000) == ""
    assert build_syndrome_query([_Obs("k", None)], max_chars=2000) == ""


def test_build_syndrome_query_expands_slot_snapshot_to_real_fact_keys() -> None:
    slot_snapshot = {
        "dimension": "ten_questions.cold_heat",
        "slots": [
            {
                "slot_name": "present_illness.chills",
                "value": "怕冷不明显",
                "source_message_id": "opaque-id",
                "confidence": 0.9,
            },
            {
                "slot_name": "present_illness.fever",
                "value": "不发烧",
                "source_message_id": "opaque-id-2",
                "confidence": 0.9,
            },
        ],
        "completeness": "complete",
        "missing_slots": [],
    }

    query = build_syndrome_query((_Obs("ten_questions.cold_heat", slot_snapshot),), max_chars=2000)

    assert query == "present_illness.chills=怕冷不明显；present_illness.fever=不发烧"
    assert "complete" not in query
    assert "opaque-id" not in query


def test_build_syndrome_query_slot_snapshot_uses_real_keys_for_priority_order() -> None:
    slot_snapshot = {
        "dimension": "ten_questions.cold_heat",
        "slots": [{"slot_name": "present_illness.fever", "value": "午后低热"}],
        "completeness": "complete",
        "missing_slots": [],
    }
    observations = (
        _Obs("ten_questions.cold_heat", slot_snapshot),
        _Obs("patient.age", 45),
        _Obs("chief_complaint.symptom", "咳嗽三天"),
    )

    query = build_syndrome_query(observations, max_chars=2000)

    assert query.index("chief_complaint.symptom=") < query.index("patient.age=")
    assert query.index("present_illness.fever=") < query.index("patient.age=")


# ---------------------------------------------------------------------------
# 检索与降级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_syndrome_evidence_passes_query_and_sources() -> None:
    retriever = FakeRetriever([_evidence("ev-1")])
    obs = (_Obs("chief_complaint.symptom", "咳嗽"),)
    results = await retrieve_syndrome_evidence(retriever, obs, top_k=4)
    assert [ev.evidence_id for ev in results] == ["ev-1"]
    call = retriever.calls[0]
    assert call["primary_sources"] == list(SYNDROME_PRIMARY_SOURCES)
    assert call["top_k"] == 4


@pytest.mark.asyncio
async def test_retrieve_syndrome_evidence_uses_dual_query_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_dual_query_enabled", True)
    retriever = DualFakeRetriever([_evidence("ev-1")])
    observations = (
        _Obs("chief_complaint.symptom", "咳嗽"),
        _Obs("ten_questions.cold_heat", "恶寒"),
    )

    results = await retrieve_syndrome_evidence(
        retriever,
        observations,
        query="咳嗽恶寒，白痰，夜间加重",
        top_k=4,
    )

    assert [ev.evidence_id for ev in results] == ["ev-1"]
    assert retriever.calls == []
    assert len(retriever.dual_calls) == 1
    call = retriever.dual_calls[0]
    assert "chief_complaint.symptom=咳嗽" in call["original_query"]
    assert call["rewritten_query"] == "咳嗽恶寒，白痰，夜间加重"
    assert call["primary_sources"] == list(SYNDROME_PRIMARY_SOURCES)
    assert call["top_k"] == 4


@pytest.mark.asyncio
async def test_retrieve_formula_evidence_uses_formula_sources() -> None:
    retriever = FakeRetriever([_evidence("ev-1", source_type="formula")])

    class _Syndrome:
        syndrome = "肝郁气滞证"
        treatment_principle = "疏肝理气"

    results = await retrieve_formula_evidence(
        retriever, _Syndrome(), (_Obs("chief_complaint.symptom", "胁痛"),), top_k=6
    )
    assert [ev.evidence_id for ev in results] == ["ev-1"]
    call = retriever.calls[0]
    assert call["primary_sources"] == list(FORMULA_PRIMARY_SOURCES)
    assert call["top_k"] == 6


@pytest.mark.asyncio
async def test_retrieve_syndrome_evidence_degrades_on_error() -> None:
    retriever = FakeRetriever(error=RuntimeError("milvus down"))
    results = await retrieve_syndrome_evidence(retriever, (_Obs("chief_complaint.symptom", "咳嗽"),), top_k=8)
    assert results == []


@pytest.mark.asyncio
async def test_retrieve_syndrome_evidence_skips_when_no_query() -> None:
    retriever = FakeRetriever([_evidence("ev-1")])
    results = await retrieve_syndrome_evidence(retriever, (), top_k=8)
    assert results == []
    assert retriever.calls == []


# ---------------------------------------------------------------------------
# 阶段开关
# ---------------------------------------------------------------------------


def test_stage_rag_enabled_obeys_master_and_stage_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_syndrome_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_formula_enabled", False)
    assert stage_rag_enabled("syndrome") is True
    assert stage_rag_enabled("formula") is False
    assert stage_rag_enabled("unknown") is False

    monkeypatch.setattr(get_settings(), "rag_enabled", False)
    assert stage_rag_enabled("syndrome") is False
    assert stage_rag_enabled("formula") is False


# ---------------------------------------------------------------------------
# 证据投影
# ---------------------------------------------------------------------------


def test_evidence_context_items_truncates_snippet_and_count() -> None:
    evidence = tuple(_evidence(f"ev-{i}", rank=i + 1) for i in range(10))
    items = evidence_context_items(evidence)
    assert len(items) <= 8  # EVIDENCE_CONTEXT_MAX_ITEMS
    assert items[0]["evidence_id"] == "ev-0"
    assert items[0]["rank"] == 1
    assert len(items[0]["content_snippet"]) <= 200  # EVIDENCE_SNIPPET_MAX_CHARS
    assert "source_type" in items[0]
    assert "score" in items[0]


def test_evidence_context_items_empty() -> None:
    assert evidence_context_items(()) == []
