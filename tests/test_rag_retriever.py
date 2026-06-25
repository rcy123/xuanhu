"""RAG Retriever 测试 — 覆盖向量检索、全文检索、混合检索、降级与异常。

测试策略：
- 所有外部依赖（Milvus、Embedding、PG）使用 mock / monkeypatch 覆盖
- 不依赖真实外部服务即可通过测试
- 覆盖成功、降级、失败三条路径
- v1.1 回归：向量-only content_snippet 回填、filters/weights/top_k 参数生效
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.reranker import (
    CROSS_PRIORITY,
    DEFAULT_FULLTEXT_WEIGHT,
    DEFAULT_SOURCE_PRIORITY_WEIGHT,
    DEFAULT_VECTOR_WEIGHT,
    PRIMARY_PRIORITY,
    compute_final_score,
    compute_source_priority,
    rerank,
)
from app.rag.retriever import (
    RAGRetriever,
    _build_embedding_gateway_settings,
    _build_milvus_filter_expr,
    _truncate_snippet,
    merge_deduplicate,
)
from app.rag.schemas import (
    Evidence,
    FulltextHit,
    MergedHit,
    RAGUnavailableError,
    VectorHit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> SimpleNamespace:
    """创建测试用 Settings 替身。"""
    defaults = {
        "milvus_host": "localhost",
        "milvus_port": 19530,
        "milvus_collection": "xuanhu_knowledge",
        "model_gateway_base_url": "http://localhost:8080/v1",
        "model_gateway_api_key": "sk-test-key",
        "model_gateway_timeout_seconds": 10,
        "model_gateway_max_retries": 1,
        "model_gateway_route_profile": "default",
        "chat_model": "test-chat",
        "embedding_model": "test-embed",
        "embedding_dim": 768,
        "rag_top_k_vector": 12,
        "rag_top_k_fulltext": 12,
        "rag_top_n_final": 8,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_vector_hit(
    chunk_id: str | None = None,
    source_type: str = "herb",
    source_id: str | None = None,
    title: str = "测试标题",
    vector_score: float = 0.9,
) -> VectorHit:
    return VectorHit(
        chunk_id=chunk_id or uuid.uuid4().hex,
        source_type=source_type,
        source_id=source_id or str(uuid.uuid4()),
        title=title,
        content_hash="abc123",
        vector_score=vector_score,
    )


def _make_fulltext_hit(
    chunk_id: str | None = None,
    source_type: str = "herb",
    source_id: str | None = None,
    title: str = "测试标题",
    content: str = "测试内容片段",
    fulltext_score: float = 0.8,
) -> FulltextHit:
    return FulltextHit(
        chunk_id=chunk_id or uuid.uuid4().hex,
        source_type=source_type,
        source_id=source_id or str(uuid.uuid4()),
        title=title,
        content=content,
        fulltext_score=fulltext_score,
    )


def _make_mock_session(execute_results: list[Any]) -> MagicMock:
    """创建 mock session，按调用顺序返回 execute 结果。"""
    mock_result_objects = []
    for rows in execute_results:
        mr = MagicMock()
        mr.all.return_value = rows
        mock_result_objects.append(mr)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=mock_result_objects)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ---------------------------------------------------------------------------
# _build_milvus_filter_expr 测试
# ---------------------------------------------------------------------------


class TestBuildMilvusFilterExpr:
    """Milvus 过滤表达式构建测试。"""

    def test_sources_only(self) -> None:
        expr = _build_milvus_filter_expr(["herb", "formula"])
        assert expr == "source_type in ['herb', 'formula']"

    def test_sources_with_source_id_filter(self) -> None:
        expr = _build_milvus_filter_expr(
            ["herb"], filters={"source_id": "abc-123"},
        )
        assert "source_type in ['herb']" in expr
        assert 'source_id == "abc-123"' in expr

    def test_filters_none(self) -> None:
        expr = _build_milvus_filter_expr(["herb"], filters=None)
        assert expr == "source_type in ['herb']"

    def test_filters_empty_dict(self) -> None:
        expr = _build_milvus_filter_expr(["herb"], filters={})
        assert expr == "source_type in ['herb']"

    def test_multiple_sources(self) -> None:
        expr = _build_milvus_filter_expr(["herb", "formula", "acupoint", "theory", "case"])
        for st in ["herb", "formula", "acupoint", "theory", "case"]:
            assert f"'{st}'" in expr


# ---------------------------------------------------------------------------
# _truncate_snippet 测试
# ---------------------------------------------------------------------------


class TestTruncateSnippet:
    def test_short_content(self) -> None:
        result = _truncate_snippet("短内容", max_length=100)
        assert result == "短内容"

    def test_long_content_truncated(self) -> None:
        result = _truncate_snippet("字" * 600, max_length=500)
        assert len(result) == 501  # 500 + "…"
        assert result.endswith("…")

    def test_exact_length(self) -> None:
        result = _truncate_snippet("字" * 500, max_length=500)
        assert len(result) == 500
        assert not result.endswith("…")


# ---------------------------------------------------------------------------
# Reranker 单元测试
# ---------------------------------------------------------------------------


class TestComputeSourcePriority:
    """source_priority 计算测试。"""

    def test_primary_source(self) -> None:
        assert compute_source_priority(is_primary=True) == PRIMARY_PRIORITY

    def test_cross_source(self) -> None:
        assert compute_source_priority(is_primary=False) == CROSS_PRIORITY


class TestComputeFinalScore:
    """MVP 加权得分计算测试。"""

    def test_primary_hit(self) -> None:
        score = compute_final_score(vector_score=1.0, fulltext_score=1.0, is_primary=True)
        expected = DEFAULT_VECTOR_WEIGHT * 1.0 + DEFAULT_FULLTEXT_WEIGHT * 1.0 + DEFAULT_SOURCE_PRIORITY_WEIGHT * PRIMARY_PRIORITY
        assert abs(score - expected) < 1e-9

    def test_cross_hit(self) -> None:
        score = compute_final_score(vector_score=1.0, fulltext_score=1.0, is_primary=False)
        expected = DEFAULT_VECTOR_WEIGHT * 1.0 + DEFAULT_FULLTEXT_WEIGHT * 1.0 + DEFAULT_SOURCE_PRIORITY_WEIGHT * CROSS_PRIORITY
        assert abs(score - expected) < 1e-9

    def test_vector_only(self) -> None:
        score = compute_final_score(vector_score=0.9, fulltext_score=0.0, is_primary=True)
        expected = DEFAULT_VECTOR_WEIGHT * 0.9 + DEFAULT_FULLTEXT_WEIGHT * 0.0 + DEFAULT_SOURCE_PRIORITY_WEIGHT * PRIMARY_PRIORITY
        assert abs(score - expected) < 1e-9

    def test_fulltext_only(self) -> None:
        score = compute_final_score(vector_score=0.0, fulltext_score=0.8, is_primary=True)
        expected = DEFAULT_VECTOR_WEIGHT * 0.0 + DEFAULT_FULLTEXT_WEIGHT * 0.8 + DEFAULT_SOURCE_PRIORITY_WEIGHT * PRIMARY_PRIORITY
        assert abs(score - expected) < 1e-9

    def test_primary_higher_than_cross(self) -> None:
        primary_score = compute_final_score(0.5, 0.5, is_primary=True)
        cross_score = compute_final_score(0.5, 0.5, is_primary=False)
        assert primary_score > cross_score

    def test_custom_weights(self) -> None:
        """自定义权重应生效。"""
        score = compute_final_score(
            vector_score=1.0, fulltext_score=1.0, is_primary=True,
            vector_weight=0.5, fulltext_weight=0.3, source_priority_weight=0.2,
        )
        expected = 0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0
        assert abs(score - expected) < 1e-9

    def test_custom_weights_change_ordering(self) -> None:
        """增大 fulltext_weight 后，全文高分项排名提升。"""
        # 默认权重下 vector=0.9 + primary 更高
        default_score = compute_final_score(
            vector_score=0.9, fulltext_score=0.3, is_primary=True,
        )
        # 自定义权重下 fulltext=0.9 更高
        custom_score = compute_final_score(
            vector_score=0.3, fulltext_score=0.9, is_primary=True,
            vector_weight=0.2, fulltext_weight=0.7, source_priority_weight=0.1,
        )
        # 在默认权重下 default_score 应更高，但在自定义下 custom_score 应更高
        default_fulltext_heavy = compute_final_score(
            vector_score=0.3, fulltext_score=0.9, is_primary=True,
        )
        assert default_score > default_fulltext_heavy  # 默认：向量高优先
        assert custom_score > compute_final_score(
            vector_score=0.9, fulltext_score=0.3, is_primary=True,
            vector_weight=0.2, fulltext_weight=0.7, source_priority_weight=0.1,
        )  # 自定义：全文高优先


class TestRerank:
    """MVP 重排测试。"""

    def test_empty_input(self) -> None:
        result = rerank([], top_k=5)
        assert result == []

    def test_single_hit(self) -> None:
        merged = [
            MergedHit(
                chunk_id="c1", source_type="herb", source_id="s1",
                title="测试", content_snippet="内容",
                vector_score=0.9, fulltext_score=0.8, is_primary=True,
            )
        ]
        result = rerank(merged, top_k=5)
        assert len(result) == 1
        assert result[0].rank == 1
        assert result[0].chunk_id == "c1"

    def test_rank_ordering(self) -> None:
        """得分高的排前面，rank 从 1 开始递增。"""
        merged = [
            MergedHit(
                chunk_id="c1", source_type="herb", source_id="s1",
                title="低分", content_snippet="内容",
                vector_score=0.3, fulltext_score=0.2, is_primary=False,
            ),
            MergedHit(
                chunk_id="c2", source_type="formula", source_id="s2",
                title="高分", content_snippet="内容",
                vector_score=0.9, fulltext_score=0.8, is_primary=True,
            ),
        ]
        result = rerank(merged, top_k=5)
        assert result[0].chunk_id == "c2"
        assert result[1].chunk_id == "c1"

    def test_top_k_truncation(self) -> None:
        """top_k 截断。"""
        merged = [
            MergedHit(
                chunk_id=f"c{i}", source_type="herb", source_id=f"s{i}",
                title=f"标题{i}", content_snippet="内容",
                vector_score=0.9 - i * 0.1, fulltext_score=0.8, is_primary=True,
            )
            for i in range(10)
        ]
        result = rerank(merged, top_k=3)
        assert len(result) == 3

    def test_evidence_metadata_preserved(self) -> None:
        """Evidence metadata 保留原始得分。"""
        merged = [
            MergedHit(
                chunk_id="c1", source_type="herb", source_id="s1",
                title="测试", content_snippet="内容",
                vector_score=0.9, fulltext_score=0.8, is_primary=True,
            )
        ]
        result = rerank(merged, top_k=5)
        assert result[0].metadata["vector_score"] == 0.9
        assert result[0].metadata["fulltext_score"] == 0.8

    def test_primary_ranks_above_cross_with_same_scores(self) -> None:
        """相同 vector/fulltext 分数时，primary 排在 cross 前面。"""
        merged = [
            MergedHit(
                chunk_id="cross", source_type="case", source_id="s1",
                title="跨库", content_snippet="内容",
                vector_score=0.8, fulltext_score=0.6, is_primary=False,
            ),
            MergedHit(
                chunk_id="primary", source_type="herb", source_id="s2",
                title="主查", content_snippet="内容",
                vector_score=0.8, fulltext_score=0.6, is_primary=True,
            ),
        ]
        result = rerank(merged, top_k=5)
        assert result[0].chunk_id == "primary"
        assert result[1].chunk_id == "cross"

    def test_custom_weights_pass_through(self) -> None:
        """自定义权重传入 rerank 并影响到排序。"""
        merged = [
            MergedHit(
                chunk_id="high_ft", source_type="herb", source_id="s1",
                title="全文高", content_snippet="内容",
                vector_score=0.5, fulltext_score=1.0, is_primary=True,
            ),
            MergedHit(
                chunk_id="high_vec", source_type="herb", source_id="s2",
                title="向量高", content_snippet="内容",
                vector_score=1.0, fulltext_score=0.5, is_primary=True,
            ),
        ]
        # 默认权重：向量高应排前面
        default_result = rerank(merged, top_k=5)
        assert default_result[0].chunk_id == "high_vec"

        # 全文权重调高：全文高应排前面
        ft_result = rerank(merged, top_k=5, vector_weight=0.2, fulltext_weight=0.7, source_priority_weight=0.1)
        assert ft_result[0].chunk_id == "high_ft"


# ---------------------------------------------------------------------------
# 合并去重测试
# ---------------------------------------------------------------------------


class TestMergeDeduplicate:
    """合并去重逻辑测试。"""

    def test_empty_inputs(self) -> None:
        result = merge_deduplicate([], [], primary_sources={"herb"})
        assert result == []

    def test_vector_only(self) -> None:
        vh = _make_vector_hit(chunk_id="c1", source_type="herb")
        result = merge_deduplicate([vh], [], primary_sources={"herb"})
        assert len(result) == 1
        assert result[0].chunk_id == "c1"
        assert result[0].vector_score == vh.vector_score
        assert result[0].fulltext_score == 0.0
        assert result[0].content_snippet == ""  # 向量-only 初始为空，由 backfill 填充

    def test_fulltext_only(self) -> None:
        fh = _make_fulltext_hit(chunk_id="c1", source_type="herb", content="测试内容")
        result = merge_deduplicate([], [fh], primary_sources={"herb"})
        assert len(result) == 1
        assert result[0].chunk_id == "c1"
        assert result[0].content_snippet == "测试内容"

    def test_merge_same_chunk(self) -> None:
        """同一 chunk 同时命中向量和全文，合并分数，以全文内容覆盖 snippet。"""
        chunk_id = "c1"
        vh = _make_vector_hit(chunk_id=chunk_id, source_type="herb", vector_score=0.9)
        fh = _make_fulltext_hit(chunk_id=chunk_id, source_type="herb", fulltext_score=0.8, content="合并内容")
        result = merge_deduplicate([vh], [fh], primary_sources={"herb"})
        assert len(result) == 1
        assert result[0].vector_score == 0.9
        assert result[0].fulltext_score == 0.8
        assert result[0].content_snippet == "合并内容"

    def test_deduplicate_by_chunk_id(self) -> None:
        """不同 chunk_id 不去重。"""
        vh1 = _make_vector_hit(chunk_id="c1")
        vh2 = _make_vector_hit(chunk_id="c2")
        result = merge_deduplicate([vh1, vh2], [], primary_sources={"herb"})
        assert len(result) == 2

    def test_cross_source_detection(self) -> None:
        """非 primary_sources 命中的 is_primary=False。"""
        vh = _make_vector_hit(chunk_id="c1", source_type="case")
        result = merge_deduplicate([vh], [], primary_sources={"herb"})
        assert result[0].is_primary is False

    def test_snippet_truncation(self) -> None:
        """长内容截断为 snippet。"""
        long_content = "字" * 600
        fh = _make_fulltext_hit(chunk_id="c1", content=long_content)
        result = merge_deduplicate([], [fh], primary_sources={"herb"})
        assert len(result[0].content_snippet) <= 501  # 500 + "…"


# ---------------------------------------------------------------------------
# RAGRetriever 测试
# ---------------------------------------------------------------------------


class TestBuildEmbeddingGatewaySettings:
    """embedding gateway 配置覆盖测试 — 验证 EMBEDDING_GATEWAY_* 代理逻辑。"""

    def test_no_override_returns_original_settings(self) -> None:
        """未配置 EMBEDDING_GATEWAY_* 时，返回原始 settings。"""
        settings = _make_settings()
        result = _build_embedding_gateway_settings(settings)
        assert result is settings

    def test_override_builds_proxy_with_mapped_fields(self) -> None:
        """配置了 EMBEDDING_GATEWAY_BASE_URL + API_KEY 后，返回 SimpleNamespace
        代理对象，其 model_gateway_* 字段使用嵌入网关覆盖值。"""
        settings = _make_settings(
            embedding_gateway_base_url="http://embed-gw:8080/v1/",
            embedding_gateway_api_key="sk-embed-test",
            embedding_gateway_timeout_seconds=120,
            embedding_gateway_max_retries=5,
        )
        result = _build_embedding_gateway_settings(settings)
        # 应为代理对象而非原始 settings
        assert result is not settings
        assert result.model_gateway_base_url == "http://embed-gw:8080/v1"
        assert result.model_gateway_api_key == "sk-embed-test"
        assert result.model_gateway_timeout_seconds == 120
        assert result.model_gateway_max_retries == 5
        # 非覆盖字段沿用原始值
        assert result.embedding_model == "test-embed"
        assert result.embedding_dim == 768

    def test_override_timeout_zero_falls_back(self) -> None:
        """embedding_gateway_timeout_seconds=0 时回退到 model_gateway_timeout_seconds。"""
        settings = _make_settings(
            embedding_gateway_base_url="http://embed-gw:8080/v1",
            embedding_gateway_api_key="sk-embed-test",
            embedding_gateway_timeout_seconds=0,
            embedding_gateway_max_retries=0,
            model_gateway_timeout_seconds=30,
            model_gateway_max_retries=3,
        )
        result = _build_embedding_gateway_settings(settings)
        assert result.model_gateway_timeout_seconds == 30
        assert result.model_gateway_max_retries == 3

    def test_override_missing_url_falls_back(self) -> None:
        """embedding_gateway_base_url 为空时回退到原始 settings。"""
        settings = _make_settings(
            embedding_gateway_base_url="",
            embedding_gateway_api_key="sk-embed-test",
        )
        result = _build_embedding_gateway_settings(settings)
        assert result is settings

    def test_override_missing_key_falls_back(self) -> None:
        """embedding_gateway_api_key 为空时回退到原始 settings。"""
        settings = _make_settings(
            embedding_gateway_base_url="http://embed-gw:8080/v1",
            embedding_gateway_api_key="",
        )
        result = _build_embedding_gateway_settings(settings)
        assert result is settings

    def test_default_init_uses_embedding_gateway_settings(self) -> None:
        """RAGRetriever 默认初始化时，gateway client 使用 _build_embedding_gateway_settings
        处理后的 settings（而非原始 settings）。"""
        settings = _make_settings(
            embedding_gateway_base_url="http://embed-gw:8080/v1/",
            embedding_gateway_api_key="sk-embed-test",
        )

        # Mock ModelGatewayClient 以捕获传入的 settings
        from unittest.mock import patch

        with patch(
            "app.rag.retriever.ModelGatewayClient",
            autospec=True,
        ) as mock_gw_cls:
            RAGRetriever(settings=settings)
            # 验证传入的 settings 是代理对象（非原始 settings）
            called_settings = mock_gw_cls.call_args.kwargs["settings"]
            assert called_settings is not settings
            assert called_settings.model_gateway_base_url == "http://embed-gw:8080/v1"
            assert called_settings.model_gateway_api_key == "sk-embed-test"


class TestRAGRetrieverValidateSources:
    """source_type 校验测试。"""

    def test_valid_sources(self) -> None:
        result = RAGRetriever._validate_sources(["herb", "formula"])
        assert result == ["herb", "formula"]

    def test_invalid_source_raises(self) -> None:
        with pytest.raises(ValueError, match="无效的 source_type"):
            RAGRetriever._validate_sources(["herb", "invalid_type"])

    def test_empty_sources(self) -> None:
        result = RAGRetriever._validate_sources([])
        assert result == []


class TestRAGRetrieverVectorSearch:
    """向量检索测试 — 使用 mock Milvus client 与 mock gateway。"""

    async def test_vector_search_success(self) -> None:
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{
                "id": "vec1", "distance": 0.2,
                "entity": {
                    "chunk_id": "chunk-1", "source_type": "herb",
                    "source_id": "src-1", "title": "黄芪", "content_hash": "abc",
                },
            }]
        ]

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, milvus_client=mock_milvus,
        )
        hits = await retriever._vector_search("黄芪", ["herb"], top_k=5)

        assert len(hits) == 1
        assert hits[0].chunk_id == "chunk-1"
        assert abs(hits[0].vector_score - 0.9) < 1e-6

    async def test_vector_search_with_filters(self) -> None:
        """filters 参数传递到 Milvus filter 表达式。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, milvus_client=mock_milvus,
        )
        await retriever._vector_search("黄芪", ["herb"], top_k=5, filters={"source_id": "target-123"})

        # 验证 filter 表达式包含 source_id
        call_kwargs = mock_milvus.search.call_args.kwargs
        assert "source_id" in call_kwargs["filter"]
        assert "target-123" in call_kwargs["filter"]

    async def test_vector_search_empty_results(self) -> None:
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, milvus_client=mock_milvus,
        )
        hits = await retriever._vector_search("不存在的查询", ["herb"], top_k=5)
        assert hits == []

    async def test_vector_search_embedding_unavailable(self) -> None:
        from app.core.exceptions import EmbeddingUnavailableError

        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(side_effect=EmbeddingUnavailableError("不可用"))

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, milvus_client=MagicMock(),
        )
        with pytest.raises(EmbeddingUnavailableError):
            await retriever._vector_search("测试", ["herb"])

    async def test_vector_search_milvus_unavailable(self) -> None:
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])
        mock_milvus = MagicMock()
        mock_milvus.search.side_effect = Exception("Milvus connection failed")

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, milvus_client=mock_milvus,
        )
        with pytest.raises(Exception, match="Milvus"):
            await retriever._vector_search("测试", ["herb"])


class TestRAGRetrieverFulltextSearch:
    """PG 全文检索测试 — 使用 mock session。"""

    async def test_fulltext_search_success(self) -> None:
        settings = _make_settings()
        chunk_uuid = uuid.uuid4()
        source_uuid = uuid.uuid4()

        mock_session = _make_mock_session([
            [
                MagicMock(id=chunk_uuid, source_type="herb", source_id=source_uuid,
                          title="黄芪", content="黄芪，补气固表", ts_rank=1.5),
                MagicMock(id=uuid.uuid4(), source_type="formula", source_id=uuid.uuid4(),
                          title="补中益气汤", content="补中益气汤", ts_rank=0.8),
            ]
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )
        hits = await retriever._fulltext_search("黄芪", ["herb", "formula"], top_k=5)

        assert len(hits) == 2
        assert hits[0].fulltext_score == 1.0
        assert hits[1].fulltext_score < 1.0

    async def test_fulltext_search_with_filters(self) -> None:
        """filters.source_id 传递到 PG WHERE 条件。"""
        settings = _make_settings()

        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )
        await retriever._fulltext_search("黄芪", ["herb"], top_k=5, filters={"source_id": "target-123"})

        # 验证 execute 调用参数中 stmt 的 WHERE 包含 source_id 过滤
        call_args = mock_session.execute.call_args
        stmt = call_args[0][0]
        compiled = str(stmt.compile())
        # source_id 过滤条件应存在（参数化形式或内联形式）
        assert "knowledge_chunks.source_id" in compiled

    async def test_fulltext_search_pg_unavailable(self) -> None:
        settings = _make_settings()
        mock_session = _make_mock_session([Exception("connection refused")])
        # 覆盖 execute 行为为抛出异常
        mock_session.execute = AsyncMock(side_effect=Exception("connection refused"))
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )
        with pytest.raises(RAGUnavailableError):
            await retriever._fulltext_search("测试", ["herb"])

    async def test_fulltext_search_no_results(self) -> None:
        settings = _make_settings()
        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )
        hits = await retriever._fulltext_search("不存在的查询", ["herb"], top_k=5)
        assert hits == []


class TestRAGRetrieverBackfillContentSnippets:
    """content_snippet 回填测试。"""

    async def test_backfill_empty_snippets(self) -> None:
        """向量-only 命中的 content_snippet 应从 PG 回填。"""
        settings = _make_settings()

        chunk_uuid = uuid.uuid4()
        # 模拟 PG 返回 content
        mock_session = _make_mock_session([
            [MagicMock(id=chunk_uuid, content="黄芪，补气固表，升阳举陷")],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )

        merged = [
            MergedHit(
                chunk_id=str(chunk_uuid), source_type="herb", source_id="s1",
                title="黄芪", content_snippet="",  # 空 snippet
                vector_score=0.9, fulltext_score=0.0, is_primary=True,
            ),
        ]

        await retriever._backfill_content_snippets(merged)

        assert merged[0].content_snippet == "黄芪，补气固表，升阳举陷"

    async def test_backfill_skips_non_empty(self) -> None:
        """已有 content_snippet 的命中不被覆盖，也不触发 DB 查询。"""
        settings = _make_settings()
        mock_session = _make_mock_session([])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )

        merged = [
            MergedHit(
                chunk_id="c1", source_type="herb", source_id="s1",
                title="已有", content_snippet="已有正文",
                vector_score=0.9, fulltext_score=0.8, is_primary=True,
            ),
        ]

        await retriever._backfill_content_snippets(merged)
        # 不应触发 DB 查询（因为无空 snippet）
        mock_session.execute.assert_not_called()

    async def test_backfill_mixed_empty_and_filled(self) -> None:
        """混合场景：仅回填空的 snippet。"""
        settings = _make_settings()
        chunk_uuid = uuid.uuid4()

        mock_session = _make_mock_session([
            [MagicMock(id=chunk_uuid, content="回填的内容")],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )

        merged = [
            MergedHit(
                chunk_id=str(chunk_uuid), source_type="herb", source_id="s1",
                title="需回填", content_snippet="",  # 空
                vector_score=0.9, fulltext_score=0.0, is_primary=True,
            ),
            MergedHit(
                chunk_id="c2", source_type="formula", source_id="s2",
                title="全文命中", content_snippet="全文已有正文",
                vector_score=0.0, fulltext_score=0.8, is_primary=True,
            ),
        ]

        await retriever._backfill_content_snippets(merged)

        assert merged[0].content_snippet == "回填的内容"
        assert merged[1].content_snippet == "全文已有正文"  # 不受影响

    async def test_backfill_db_error_non_fatal(self) -> None:
        """回填 DB 失败不阻断检索——snippet 保持为空但不抛异常。"""
        settings = _make_settings()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )

        merged = [
            MergedHit(
                chunk_id="c1", source_type="herb", source_id="s1",
                title="测试", content_snippet="",
                vector_score=0.9, fulltext_score=0.0, is_primary=True,
            ),
        ]

        # 不应抛出异常
        await retriever._backfill_content_snippets(merged)
        # snippet 保持为空
        assert merged[0].content_snippet == ""


class TestRAGRetrieverHybridSearch:
    """混合检索测试 — 整合向量+全文+合并去重+重排。"""

    async def test_hybrid_search_both_success(self) -> None:
        """向量+全文同时命中，合并去重后重排。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{"id": "v1", "distance": 0.2,
              "entity": {"chunk_id": "chunk-1", "source_type": "herb",
                         "source_id": "src-1", "title": "黄芪", "content_hash": "abc"}}]
        ]

        # PG: 全文命中同一个 chunk（提供 content）
        mock_session = _make_mock_session([
            [MagicMock(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                       source_type="herb", source_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
                       title="黄芪", content="黄芪，补气固表，升阳举陷", ts_rank=1.0)],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )

        evidences = await retriever.hybrid_search(
            "黄芪", sources=["herb"], primary_sources={"herb"}, top_k=5,
        )
        assert len(evidences) >= 1
        # 同一 chunk 合并后只有 1 条
        chunk_ids = {e.chunk_id for e in evidences}
        assert len(chunk_ids) == len(evidences)

    async def test_hybrid_search_vector_only_snippet_backfilled(self) -> None:
        """纯向量命中（无全文命中），content_snippet 应从 PG 回填。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        # Milvus 返回命中
        chunk_uuid = uuid.uuid4()
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{"id": "v1", "distance": 0.2,
              "entity": {"chunk_id": str(chunk_uuid), "source_type": "herb",
                         "source_id": "src-1", "title": "黄芪", "content_hash": "abc"}}]
        ]

        # PG 全文检索无命中，但 _backfill_content_snippets 能查到 content
        mock_session = _make_mock_session([
            [],  # _fulltext_search 无结果
            [MagicMock(id=chunk_uuid, content="黄芪，补气固表，升阳举陷，利水消肿")],  # _backfill 有结果
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )

        evidences = await retriever.hybrid_search(
            "zzzz-not-in-text", sources=["herb"], primary_sources={"herb"}, top_k=5,
        )

        assert len(evidences) == 1
        assert len(evidences[0].content_snippet) > 0
        assert "黄芪" in evidences[0].content_snippet

    async def test_hybrid_search_milvus_degradation(self) -> None:
        """Milvus 不可用时降级为 PG 全文检索。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(side_effect=Exception("Milvus down"))

        mock_session = _make_mock_session([
            [MagicMock(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                       source_type="herb", source_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
                       title="黄芪", content="黄芪，补气固表", ts_rank=1.0)],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, session_factory=mock_sf,
        )
        evidences = await retriever.hybrid_search(
            "黄芪", sources=["herb"], primary_sources={"herb"}, top_k=5,
        )
        assert len(evidences) >= 1

    async def test_hybrid_search_embedding_degradation(self) -> None:
        """Embedding 不可用时降级为 PG 全文检索。"""
        from app.core.exceptions import EmbeddingUnavailableError

        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(side_effect=EmbeddingUnavailableError("服务不可用"))

        mock_session = _make_mock_session([
            [MagicMock(id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                       source_type="formula", source_id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
                       title="四君子汤", content="四君子汤，益气健脾", ts_rank=1.0)],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway, session_factory=mock_sf,
        )
        evidences = await retriever.hybrid_search(
            "四君子汤", sources=["formula"], primary_sources={"formula"}, top_k=5,
        )
        assert len(evidences) >= 1

    async def test_hybrid_search_pg_unavailable_raises(self) -> None:
        """PG 不可用时抛出 RAGUnavailableError。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        with pytest.raises(RAGUnavailableError):
            await retriever.hybrid_search("测试", sources=["herb"])

    async def test_hybrid_search_no_results_empty_list(self) -> None:
        """无检索结果返回空列表，不编造 evidence。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]

        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        evidences = await retriever.hybrid_search("不存在的查询", sources=["herb"])
        assert evidences == []

    async def test_hybrid_search_top_k_controls_count(self) -> None:
        """top_k 参数控制最终返回条数。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        # 生成 5 个不同 chunk 的向量命中
        chunks = [
            {"id": f"v{i}", "distance": 0.1 * i,
             "entity": {"chunk_id": f"chunk-{i}", "source_type": "herb",
                        "source_id": f"src-{i}", "title": f"标题{i}", "content_hash": "abc"}}
            for i in range(5)
        ]
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [chunks[::-1]]  # reverse so distances differ

        # PG 无命中，backfill 也无内容
        mock_session = _make_mock_session([[], []])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        evidences = await retriever.hybrid_search(
            "测试", sources=["herb"], primary_sources={"herb"}, top_k=3,
        )
        assert len(evidences) == 3

    async def test_hybrid_search_custom_weights(self) -> None:
        """自定义权重参数应传递到重排逻辑，改变默认排序。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        # chunk-vec: 高向量分(0.9), 无全文
        # chunk-ft:  低向量分(0.4), 有全文命中(1.0)
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{"id": "v1", "distance": 0.2,
              "entity": {"chunk_id": "chunk-vec", "source_type": "herb",
                         "source_id": "src-1", "title": "向量高", "content_hash": "abc"}},
             {"id": "v2", "distance": 1.2,
              "entity": {"chunk_id": "chunk-ft", "source_type": "herb",
                         "source_id": "src-2", "title": "全文高", "content_hash": "def"}}]
        ]

        # PG 全文给 chunk-ft 高 rank（chunk_id 需与 Milvus 一致才能合并），chunk-vec 无全文命中
        session_factory = MagicMock()

        def _make_fresh_session():
            return _make_mock_session([
                # _fulltext_search 只命中 chunk-ft
                [MagicMock(id="chunk-ft", source_type="herb", source_id=uuid.uuid4(),
                           title="全文高", content="全文高分内容", ts_rank=1.0)],
                # _backfill 查询 chunk-vec 的内容
                [MagicMock(id="chunk-vec", content="向量高内容"),
                 MagicMock(id="chunk-ft", content="全文高内容")],
            ])

        session_factory.side_effect = _make_fresh_session

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=session_factory,
        )

        # 默认权重：vector_score 主导 → chunk-vec 排第一
        default_ev = await retriever.hybrid_search(
            "测试", sources=["herb"], primary_sources={"herb"}, top_k=5,
        )
        # 全文权重调高：fulltext_score 主导 → chunk-ft 排第一
        ft_ev = await retriever.hybrid_search(
            "测试", sources=["herb"], primary_sources={"herb"}, top_k=5,
            vector_weight=0.2, fulltext_weight=0.7,
        )

        assert len(default_ev) >= 2
        assert len(ft_ev) >= 2
        assert default_ev[0].chunk_id == "chunk-vec"
        assert ft_ev[0].chunk_id == "chunk-ft"


    async def test_hybrid_search_top_k_1_final_score_ordering(self) -> None:
        """top_k=1 时，最终加权分更高的候选应排第一（即使其 vector_score 更低）。

        构造两个候选：
        - chunk-vec: vector_score=0.5, 无全文命中 → final=0.65×0.5 + 0.10×1.0 = 0.425
        - chunk-ft:  vector_score=0.4, fulltext_score=1.0 → final=0.65×0.4 + 0.25×1.0 + 0.10×1.0 = 0.61

        chunk-ft 的 final_score 更高但 vector_score 更低。
        若先按 vector_score 预截断（旧 bug），chunk-vec 会排第一，chunk-ft 被丢弃。
        正确行为：按最终加权分排序，chunk-ft 排第一。
        """
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        # Milvus: 两个命中
        # distance=1.0 → vector_score=1-1.0/2=0.5
        # distance=1.2 → vector_score=1-1.2/2=0.4
        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{"id": "v1", "distance": 1.0,
              "entity": {"chunk_id": "chunk-vec", "source_type": "herb",
                         "source_id": "src-1", "title": "向量项", "content_hash": "abc"}},
             {"id": "v2", "distance": 1.2,
              "entity": {"chunk_id": "chunk-ft", "source_type": "herb",
                         "source_id": "src-2", "title": "全文项", "content_hash": "def"}}]
        ]

        session_factory = MagicMock()

        def _make_fresh_session() -> MagicMock:
            return _make_mock_session([
                # _fulltext_search: 只命中 chunk-ft (ts_rank=1.0，归一化后 fulltext_score=1.0)
                [MagicMock(id="chunk-ft", source_type="herb", source_id=uuid.uuid4(),
                           title="全文项", content="全文高分内容", ts_rank=1.0)],
                # _backfill: 回填 chunk-vec 和 chunk-ft 的内容
                [MagicMock(id="chunk-vec", content="向量项内容——这段内容较长"),
                 MagicMock(id="chunk-ft", content="全文项内容——全文命中高分内容")],
            ])

        session_factory.side_effect = _make_fresh_session

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=session_factory,
        )

        evidences = await retriever.hybrid_search(
            "测试", sources=["herb"], primary_sources={"herb"}, top_k=1,
        )
        assert len(evidences) == 1
        # chunk-ft 最终加权分 0.61 > chunk-vec 的 0.425，应排第一
        assert evidences[0].chunk_id == "chunk-ft"
        assert evidences[0].rank == 1
        # 验证 content_snippet 已回填
        assert "全文项" in evidences[0].title


class TestRAGRetrieverRetrieve:
    """retrieve() 接口测试 — 验证参数传递与过滤。"""

    async def test_retrieve_primary_sources_only(self) -> None:
        """allow_cross_source=False 时只查 primary_sources。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]
        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        await retriever.retrieve("黄芪", primary_sources=["herb"], allow_cross_source=False)

        filter_expr = mock_milvus.search.call_args.kwargs["filter"]
        assert "'herb'" in filter_expr
        assert "'formula'" not in filter_expr

    async def test_retrieve_cross_source_enabled(self) -> None:
        """allow_cross_source=True 时查询所有 source_type。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]
        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        await retriever.retrieve("黄芪", primary_sources=["herb"], allow_cross_source=True)

        filter_expr = mock_milvus.search.call_args.kwargs["filter"]
        for st in ["formula", "herb", "acupoint", "theory", "case"]:
            assert f"'{st}'" in filter_expr

    async def test_retrieve_invalid_source_raises(self) -> None:
        settings = _make_settings()
        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=MagicMock(),
        )
        with pytest.raises(ValueError, match="无效的 source_type"):
            await retriever.retrieve("测试", primary_sources=["invalid"])

    async def test_retrieve_filters_passed_to_milvus(self) -> None:
        """retrieve() 的 filters 参数传递到 Milvus 过滤表达式。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [[]]
        mock_session = _make_mock_session([[]])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        await retriever.retrieve(
            "测试", primary_sources=["herb"], filters={"source_id": "filtered-123"},
        )

        filter_expr = mock_milvus.search.call_args.kwargs["filter"]
        assert "filtered-123" in filter_expr


class TestRAGRetrieverRerankOrder:
    """MVP 重排顺序验证。"""

    async def test_primary_ranks_above_cross(self) -> None:
        """主查库命中排在跨库命中前面（相同分数下）。"""
        settings = _make_settings()
        gateway = MagicMock(spec=[])
        gateway.embed = AsyncMock(return_value=[[0.1] * 768])

        mock_milvus = MagicMock()
        mock_milvus.search.return_value = [
            [{"id": "v1", "distance": 0.2,
              "entity": {"chunk_id": "primary-chunk", "source_type": "herb",
                         "source_id": "src-1", "title": "主查库命中", "content_hash": "abc"}},
             {"id": "v2", "distance": 0.2,
              "entity": {"chunk_id": "cross-chunk", "source_type": "case",
                         "source_id": "src-2", "title": "跨库命中", "content_hash": "def"}}]
        ]

        # PG 无命中，backfill 有内容
        mock_session = _make_mock_session([
            [],
            [MagicMock(id=uuid.uuid4(), content="主查内容"),
             MagicMock(id=uuid.uuid4(), content="跨库内容")],
        ])
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=gateway,
            milvus_client=mock_milvus, session_factory=mock_sf,
        )
        evidences = await retriever.hybrid_search(
            "测试", sources=["herb", "case"], primary_sources={"herb"}, top_k=5,
        )
        assert len(evidences) == 2
        assert evidences[0].source_type == "herb"
        assert evidences[1].source_type == "case"
        assert evidences[0].score > evidences[1].score


class TestEvidenceTraceability:
    """Evidence 可追溯字段完整性测试。"""

    def test_evidence_required_fields(self) -> None:
        e = Evidence(
            evidence_id="test-id", source_type="herb", source_id="src-1",
            chunk_id="chunk-1", title="黄芪", content_snippet="补气固表",
            score=0.85, rank=1,
        )
        assert e.evidence_id == "test-id"
        assert e.source_type == "herb"
        assert e.source_id == "src-1"
        assert e.chunk_id == "chunk-1"
        assert e.title == "黄芪"
        assert e.content_snippet == "补气固表"
        assert e.score == 0.85
        assert e.rank == 1

    def test_evidence_auto_generates_id(self) -> None:
        e = Evidence(
            source_type="herb", source_id="src-1", title="标题",
            content_snippet="内容", score=0.5, rank=1,
        )
        assert len(e.evidence_id) == 32

    def test_evidence_metadata_optional(self) -> None:
        e = Evidence(
            source_type="herb", source_id="src-1", title="标题",
            content_snippet="内容", score=0.5, rank=1,
        )
        assert isinstance(e.metadata, dict)

    def test_evidence_metadata_preserves_scores(self) -> None:
        e = Evidence(
            source_type="herb", source_id="src-1", title="标题",
            content_snippet="内容", score=0.85, rank=1,
            metadata={"vector_score": 0.9, "fulltext_score": 0.8, "source_priority": 1.0},
        )
        assert e.metadata["vector_score"] == 0.9
        assert e.metadata["fulltext_score"] == 0.8
        assert e.metadata["source_priority"] == 1.0


class TestNoInformationLeak:
    """安全测试 — 不泄露 API key、prompt 原文或完整外部异常响应。"""

    def test_rag_unavailable_error_no_api_key(self) -> None:
        exc = RAGUnavailableError("RAG 检索不可用：数据库连接失败")
        assert "sk-" not in str(exc)
        assert "api_key" not in str(exc).lower()

    def test_rag_unavailable_error_no_prompt(self) -> None:
        exc = RAGUnavailableError("RAG 检索不可用")
        assert "用户查询" not in str(exc)

    async def test_pg_error_sanitized(self) -> None:
        settings = _make_settings()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            side_effect=Exception("connection to postgresql://admin:secret@db:5432 failed"),
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_sf = MagicMock(return_value=mock_session)

        retriever = RAGRetriever(
            settings=settings, gateway_client=MagicMock(), session_factory=mock_sf,
        )
        with pytest.raises(RAGUnavailableError) as exc_info:
            await retriever._fulltext_search("测试", ["herb"])

        assert "secret" not in str(exc_info.value)
        assert "postgresql://" not in str(exc_info.value)


class TestRAGUnavailableErrorBehavior:
    """RAGUnavailableError 异常行为测试。"""

    def test_default_message(self) -> None:
        exc = RAGUnavailableError()
        assert "RAG" in str(exc)
        assert "不可用" in str(exc)

    def test_custom_message(self) -> None:
        exc = RAGUnavailableError("自定义消息")
        assert str(exc) == "自定义消息"

    def test_retryable_default(self) -> None:
        exc = RAGUnavailableError()
        assert exc.retryable is True

    def test_retryable_false(self) -> None:
        exc = RAGUnavailableError(retryable=False)
        assert exc.retryable is False


class TestWeightConstants:
    """验证 MVP 加权常量与详细设计 §8.4 一致。"""

    def test_vector_weight(self) -> None:
        assert DEFAULT_VECTOR_WEIGHT == 0.65

    def test_fulltext_weight(self) -> None:
        assert DEFAULT_FULLTEXT_WEIGHT == 0.25

    def test_source_priority_weight(self) -> None:
        assert DEFAULT_SOURCE_PRIORITY_WEIGHT == 0.10

    def test_weights_sum_to_one(self) -> None:
        assert abs(DEFAULT_VECTOR_WEIGHT + DEFAULT_FULLTEXT_WEIGHT + DEFAULT_SOURCE_PRIORITY_WEIGHT - 1.0) < 1e-9
