"""验证 TP3.2 进程级共享 RAGRetriever 与 TP3.5/M1 output_fields content 的 plumbing。

- shared singleton：同进程 ``get_shared_rag_retriever()`` 返回同一实例，避免
  每个 stage 重建 Milvus gRPC channel。
- M1 content merge：VectorHit.content（来自 Milvus output_fields）经
  ``merge_deduplicate`` 进入 MergedHit.content_snippet，省去 PG 回填。
- M1 fallback：VectorHit.content 为空时，merged snippet 仍为空，由下游
  ``_backfill_content_snippets`` 走 PG 回填（向后兼容旧 collection）。
"""

from __future__ import annotations

from app.rag.retriever import (
    RAGRetriever,
    get_shared_rag_retriever,
    merge_deduplicate,
    reset_shared_rag_retriever,
)
from app.rag.schemas import FulltextHit, VectorHit


def setup_function(_: object) -> None:
    reset_shared_rag_retriever()


def teardown_function(_: object) -> None:
    reset_shared_rag_retriever()


def test_shared_rag_retriever_returns_same_instance() -> None:
    """进程级单例：重复调用必须返回同一实例。"""
    first = get_shared_rag_retriever()
    second = get_shared_rag_retriever()
    third = get_shared_rag_retriever()
    assert first is second is third
    assert isinstance(first, RAGRetriever)


def test_reset_shared_rag_retriever_creates_fresh_instance() -> None:
    """``reset_shared_rag_retriever`` 后下次调用必须新建实例（测试隔离）。"""
    first = get_shared_rag_retriever()
    reset_shared_rag_retriever()
    second = get_shared_rag_retriever()
    assert first is not second


def test_merge_deduplicate_prefers_milvus_content_over_pg_backfill() -> None:
    """TP3.5/M1: VectorHit.content 非空时直接进入 MergedHit.content_snippet。"""
    vector_hits = [
        VectorHit(
            chunk_id="c1",
            source_type="formula",
            source_id="s1",
            title="t1",
            content_hash="h1",
            vector_score=0.9,
            content="来自 Milvus output_fields 的完整内容",
        ),
    ]
    merged = merge_deduplicate(vector_hits, [], primary_sources=set())
    assert len(merged) == 1
    assert merged[0].content_snippet == "来自 Milvus output_fields 的完整内容"


def test_merge_deduplicate_empty_vector_content_falls_back_to_pg_backfill() -> None:
    """M1 兼容：旧 collection 无 content 字段时 VectorHit.content 为空，
    MergedHit.content_snippet 仍为空，留给 _backfill_content_snippets 回填 PG。"""
    vector_hits = [
        VectorHit(
            chunk_id="c1",
            source_type="formula",
            source_id="s1",
            title="t1",
            content_hash="h1",
            vector_score=0.9,
            content="",
        ),
    ]
    merged = merge_deduplicate(vector_hits, [], primary_sources=set())
    assert len(merged) == 1
    assert merged[0].content_snippet == ""


def test_merge_deduplicate_fulltext_overrides_milvus_content_when_both_hit() -> None:
    """同一 chunk 同时向量+全文命中：全文 snippet 覆盖（全文内容通常更完整）。"""
    vector_hits = [
        VectorHit(
            chunk_id="c1",
            source_type="formula",
            source_id="s1",
            title="t1",
            content_hash="h1",
            vector_score=0.9,
            content="milvus 内容",
        ),
    ]
    fulltext_hits = [
        FulltextHit(
            chunk_id="c1",
            source_type="formula",
            source_id="s1",
            title="t1",
            content="fulltext 更完整的内容",
            fulltext_score=0.8,
        ),
    ]
    merged = merge_deduplicate(vector_hits, fulltext_hits, primary_sources=set())
    assert len(merged) == 1
    assert merged[0].content_snippet == "fulltext 更完整的内容"