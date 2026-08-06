"""共享 RAG 检索器 — Milvus 向量检索 + PostgreSQL 全文检索 + 合并去重 + MVP 重排。

设计依据：
- 详细设计文档 §8.3 检索流程
- 详细设计文档 §8.4 重排策略
- 详细设计文档 §8.5 RAG 失败降级
- 接口设计文档 §7.3 RAG 检索接口
- 多Agent架构设计 §8 共享 + 主查库配置

降级策略：
- Embedding 不可用 → 仅 PG 全文检索
- Milvus 不可用 → 仅 PG 全文检索
- PG 不可用 → 抛出 RAGUnavailableError，阻塞需要证据的 Agent
- 无结果 → 返回空列表，不得编造 evidence
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.embedding_gateway import build_embedding_gateway_settings
from app.core.gateway import ModelGatewayClient
from app.core.reranker_gateway import build_reranker_gateway_settings
from app.core.metrics import measure
from app.db.session import get_session_factory
from app.models.knowledge import KnowledgeChunk
from app.rag.reranker import (
    DEFAULT_FULLTEXT_WEIGHT,
    DEFAULT_VECTOR_WEIGHT,
    cross_encoder_rerank,
    llm_rerank,
    rerank,
)
from app.rag.schemas import (
    VALID_SOURCE_TYPES,
    Evidence,
    FulltextHit,
    MergedHit,
    RAGUnavailableError,
    VectorHit,
)

logger = logging.getLogger("xuanhu.rag")

# ---------------------------------------------------------------------------
# snippet 截断
# ---------------------------------------------------------------------------

_SNIPPET_MAX_LENGTH = 500


def _truncate_snippet(content: str, max_length: int = _SNIPPET_MAX_LENGTH) -> str:
    """截断内容为 snippet，保留 max_length 字符。"""
    if len(content) <= max_length:
        return content
    return content[:max_length] + "…"


# ---------------------------------------------------------------------------
# 进程级共享 RAGRetriever（TP3.2: Milvus client 进程单例）
# ---------------------------------------------------------------------------

# 进程级共享实例：推理子图每 stage 调一次 _rag_retriever()（辨证+开方两阶段
# 合计 6 次），逐次构造会让 _get_milvus_client 各懒建一次 Milvus gRPC channel，
# 多路并发检索时复用率低。共享后 Milvus 内部 channel 池能合并，连接建立
# 开销由 N 次/推理降为 1 次/进程。
#
# first-call race：多线程/协程并发首次调用时可能各构造一个 RAGRetriever，但
# RAGRetriever.__init__ 不做 I/O（httpx.AsyncClient 延迟建连、Milvus 延迟实例化），
# 未被引用的实例随 GC 释放，不留副作用——最后持久化的实例即为"共享单例"。
_shared_rag_retriever: RAGRetriever | None = None


def get_shared_rag_retriever() -> RAGRetriever:
    """获取进程级共享 RAGRetriever（懒加载，sync 与 async 上下文均可调）。

    返回共享实例。``settings.rag_enabled=False`` 时调用方应自行短路；
    本函数不检查该开关（推理侧 ``_rag_retriever`` 自己做开关短路）。
    """
    global _shared_rag_retriever
    if _shared_rag_retriever is None:
        _shared_rag_retriever = RAGRetriever()
    return _shared_rag_retriever


def reset_shared_rag_retriever() -> None:
    """测试隔离：清理进程级共享 RAGRetriever（next call 重建）。"""
    global _shared_rag_retriever
    _shared_rag_retriever = None


# ---------------------------------------------------------------------------
# 合并去重
# ---------------------------------------------------------------------------


def merge_deduplicate(
    vector_hits: Sequence[VectorHit],
    fulltext_hits: Sequence[FulltextHit],
    primary_sources: set[str],
) -> list[MergedHit]:
    """以 chunk_id 为主去重，合并向量与全文命中结果。

    同一 chunk 同时命中向量和全文时合并分数，保留原始 vector_score 与 fulltext_score。
    向量命中优先用 Milvus output_fields.content（TP3.5/M1），缺失则由调用方的
    ``_backfill_content_snippets`` 回退到 PG 回填——保留向后兼容（旧 collection 无 content 字段）。
    """
    merged: dict[str, MergedHit] = {}

    # 先处理向量命中
    for vh in vector_hits:
        merged[vh.chunk_id] = MergedHit(
            chunk_id=vh.chunk_id,
            source_type=vh.source_type,
            source_id=vh.source_id,
            title=vh.title,
            content_snippet=_truncate_snippet(vh.content) if vh.content else "",
            vector_score=vh.vector_score,
            fulltext_score=0.0,
            is_primary=vh.source_type in primary_sources,
        )

    # 合并全文命中
    for fh in fulltext_hits:
        if fh.chunk_id in merged:
            # 同一 chunk 同时命中：合并分数，以全文内容覆盖 snippet（全文内容通常更完整）
            existing = merged[fh.chunk_id]
            merged[fh.chunk_id] = MergedHit(
                chunk_id=fh.chunk_id,
                source_type=fh.source_type,
                source_id=fh.source_id,
                title=fh.title,
                content_snippet=_truncate_snippet(fh.content),
                vector_score=existing.vector_score,
                fulltext_score=fh.fulltext_score,
                is_primary=existing.is_primary or fh.source_type in primary_sources,
            )
        else:
            merged[fh.chunk_id] = MergedHit(
                chunk_id=fh.chunk_id,
                source_type=fh.source_type,
                source_id=fh.source_id,
                title=fh.title,
                content_snippet=_truncate_snippet(fh.content),
                vector_score=0.0,
                fulltext_score=fh.fulltext_score,
                is_primary=fh.source_type in primary_sources,
            )

    return list(merged.values())


# ---------------------------------------------------------------------------
# Milvus filter 构建
# ---------------------------------------------------------------------------


def _build_milvus_filter_expr(sources: list[str], filters: dict[str, Any] | None = None) -> str:
    """构建 Milvus 过滤表达式。

    Args:
        sources: source_type 列表。
        filters: 附加过滤条件，当前支持 source_id 精确匹配。

    Returns:
        Milvus filter 表达式字符串。
    """
    source_filter = ", ".join(f"'{s}'" for s in sources)
    expr = f"source_type in [{source_filter}]"

    if filters:
        source_id = filters.get("source_id")
        if source_id is not None:
            expr += f' and source_id == "{source_id}"'

    return expr


# ---------------------------------------------------------------------------
# Embedding Gateway 配置覆盖
# ---------------------------------------------------------------------------


def _build_embedding_gateway_settings(settings: Any) -> Any:
    """构建 embedding 专用 gateway settings，应用 EMBEDDING_GATEWAY_* 覆盖。

    当 EMBEDDING_GATEWAY_BASE_URL 和 EMBEDDING_GATEWAY_API_KEY 均配置时，
    将 embedding 专用配置映射到 model_gateway_* 字段名。URL 可填写 base URL
    或完整的 ``/embeddings`` endpoint。
    否则回退到全局 settings。

    与 scripts/sync_knowledge_chunks.py _embed_batch() 中的逻辑一致。
    """
    return build_embedding_gateway_settings(settings)


# ---------------------------------------------------------------------------
# RAGRetriever
# ---------------------------------------------------------------------------


class RAGRetriever:
    """共享 RAG 检索器，Milvus 向量检索 + PG 全文检索混合。

    作为后续 Agent 的内部调用能力，不开放业务 API。
    """

    def __init__(
        self,
        *,
        settings: Any = None,
        gateway_client: ModelGatewayClient | None = None,
        milvus_client: Any = None,
        session_factory: Any = None,
    ) -> None:
        """初始化 RAGRetriever。

        Args:
            settings: 配置实例，默认使用 get_settings()。
            gateway_client: 模型网关客户端，默认自动创建。
            milvus_client: Milvus 客户端，默认自动创建。
            session_factory: SQLAlchemy async session 工厂，默认使用 get_session_factory()。
        """
        self._settings = settings or get_settings()
        self._gateway = gateway_client or ModelGatewayClient(
            settings=_build_embedding_gateway_settings(self._settings),
        )
        self._milvus_client = milvus_client
        self._session_factory = session_factory
        # Reranker 网关（Cross-Encoder/LLM 模式专用，延迟建连）
        self._reranker_gateway: ModelGatewayClient | None = None
        # L3 实体名索引（运行时关联预热，延迟加载）
        self._entity_index_loaded: bool = False
        # T3.5/M1 容错：collection 是否含 content 字段（一次性检测，进程内缓存）。
        # 旧 collection（sync 脚本 schema 无 content）搜带 content 的 output_fields
        # 会抛 MilvusException field content not exist → 向量检索整路静默降级为纯全文。
        # None=未检测；True/False=已知。
        self._milvus_has_content: bool | None = None

    def _get_session_factory(self) -> Any:
        """延迟获取 session factory，便于测试注入。"""
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory

    def _get_milvus_client(self) -> Any:
        """延迟创建 Milvus 客户端，便于测试注入。"""
        if self._milvus_client is None:
            from pymilvus import MilvusClient

            self._milvus_client = MilvusClient(
                uri=f"http://{self._settings.milvus_host}:{self._settings.milvus_port}",
                timeout=self._settings.milvus_timeout_seconds,
            )
        return self._milvus_client

    def _get_reranker_gateway(self) -> ModelGatewayClient:
        """延迟创建 reranker 专用 ModelGatewayClient。

        Cross-Encoder / LLM Reranker 共用此网关：Cross-Encoder 模式 POST 到
        ``/rerank``，LLM 模式走标准 ``/chat/completions``。
        网关配置优先使用 ``RERANKER_GATEWAY_*`` 覆盖，否则回退
        ``MODEL_GATEWAY_*``（与 embedding 覆盖同模式）。
        """
        if self._reranker_gateway is None:
            self._reranker_gateway = ModelGatewayClient(
                settings=build_reranker_gateway_settings(self._settings),
            )
        return self._reranker_gateway

    async def _ensure_entity_index(self) -> None:
        """L3: 延迟加载实体名索引（从 knowledge_chunks 表）。

        首次调用时从 PG 拉取 herb + formula title，构建内存索引。
        后续调用为 no-op。
        """
        if self._entity_index_loaded:
            return
        try:
            from app.models.knowledge import KnowledgeChunk
            from app.rag.entity_index import get_entity_index
            from sqlalchemy import select

            # 使用全局 session factory 而非 self._get_session_factory()：
            # 实体索引是进程级单例，应直连 PG，不受测试 mock 注入影响。
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(KnowledgeChunk.title)
                    .where(
                        KnowledgeChunk.source_type.in_(["herb", "formula"]),
                        KnowledgeChunk.deleted_at.is_(None),
                    )
                    .distinct()
                )
                titles = list(result.scalars().all())
            if titles:
                get_entity_index().load(titles)
                logger.info("entity index 加载完成: %d titles", len(titles))
        except Exception:
            logger.warning("entity index 加载失败（不影响检索主路径）", exc_info=True)
        finally:
            self._entity_index_loaded = True

    async def _warm_related_queries(self, query: str) -> None:
        """L3: 运行时关联预热（fire-and-forget）。

        从 cache miss 的查询文本中提取已知实体名，异步预热其模板查询。
        失败不影响检索主路径。
        """
        try:
            # 延迟加载实体索引（首次调用从 PG 拉取，后续 no-op）
            await self._ensure_entity_index()

            from app.rag.embedding_cache import (
                FORMULA_QUERY_TEMPLATES,
                HERB_QUERY_TEMPLATES,
                batch_set_embeddings,
                get_embedding,
            )
            from app.rag.entity_index import get_entity_index

            entity = get_entity_index().extract_entity(query)
            if entity is None:
                return

            # 判断是 herb 还是 formula（简单启发式：查 formula 模板时用全名判断）
            # 这里不区分类型，两种模板都生成
            templates: list[str] = []
            for tpl in HERB_QUERY_TEMPLATES:
                templates.append(tpl.format(herb=entity))
            for tpl in FORMULA_QUERY_TEMPLATES:
                templates.append(tpl.format(formula=entity))

            # 只为未缓存的模板生成 embedding（需 await，但在此 fire-and-forget
            # 协程内完成——不阻塞检索主路径，由 asyncio 调度）
            to_embed: list[str] = []
            for t in templates:
                if await get_embedding(t) is None:
                    to_embed.append(t)

            if not to_embed:
                return

            vectors = await self._gateway.embed(to_embed, trace_id="l3-warm")
            pairs = [
                (text, vec.tolist() if hasattr(vec, "tolist") else vec)
                for text, vec in zip(to_embed, vectors)
            ]
            n = await batch_set_embeddings(pairs)
            if n:
                logger.debug("L3 预热完成: entity=%s, cached=%d", entity, n)
        except Exception:
            logger.debug("L3 预热放弃（不影响检索主路径）", exc_info=True)

    async def _collection_has_content(self, milvus: Any, collection_name: str) -> bool:
        """一次性探测 collection 是否含 content 字段（进程内缓存）。

        旧 collection（sync 脚本老 schema 无 content）搜未知 output_field 会抛
        ``MilvusException: field content not exist``，必须显式探测后才决定字段列表。
        ``describe_collection`` 是同步阻塞调用 → to_thread 包裹。
        """
        if self._milvus_has_content is None:
            try:
                desc = await asyncio.to_thread(milvus.describe_collection, collection_name)
                fields = {str(f.get("name", "")) for f in desc.get("fields", [])}
                self._milvus_has_content = "content" in fields
            except Exception as exc:  # noqa: BLE001 - 探测失败保守回退（不阻塞检索主路径）
                logger.warning(
                    "Milvus collection 字段探测失败，按无 content 处理: error_type=%s",
                    type(exc).__name__,
                )
                self._milvus_has_content = False
        return self._milvus_has_content

    # -------------------------------------------------------------------
    # 公开接口
    # -------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        primary_sources: list[str],
        *,
        allow_cross_source: bool = True,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """统一检索入口，返回排序后的 Evidence 列表。

        Args:
            query: 检索查询文本。
            primary_sources: 主查库列表，如 ["formula", "herb"]。
            allow_cross_source: 是否允许跨库检索，默认 True。
            top_k: 最终返回条数，默认 8。
            filters: 附加过滤条件，当前支持 {"source_id": "<uuid>"} 精确匹配。

        Returns:
            排序后的 Evidence 列表。无结果时返回空列表，不编造 evidence。

        Raises:
            RAGUnavailableError: PG 不可用时抛出。
        """
        # 参数校验
        validated_sources = self._validate_sources(primary_sources)
        primary_set = set(validated_sources)

        # 确定检索范围
        search_sources = list(VALID_SOURCE_TYPES) if allow_cross_source else validated_sources

        # 检索 top-k 参数
        settings = self._settings
        vector_top_k = getattr(settings, "rag_top_k_vector", 12)
        fulltext_top_k = getattr(settings, "rag_top_k_fulltext", 12)

        # 执行混合检索，top_k 直接作为最终返回条数
        return await self.hybrid_search(
            query=query,
            sources=search_sources,
            primary_sources=primary_set,
            vector_top_k=vector_top_k,
            fulltext_top_k=fulltext_top_k,
            top_k=top_k,
            filters=filters,
        )

    async def hybrid_search(
        self,
        query: str,
        sources: list[str],
        *,
        primary_sources: set[str] | None = None,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        fulltext_weight: float = DEFAULT_FULLTEXT_WEIGHT,
        vector_top_k: int = 12,
        fulltext_top_k: int = 12,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """混合检索：向量 + PG 全文 → 合并 → 回填内容 → 重排（按最终加权分截取 top_k）。

        降级策略：
        - Embedding 或 Milvus 不可用 → 仅 PG 全文检索
        - PG 不可用 → 抛出 RAGUnavailableError
        - 无结果 → 返回空列表

        Args:
            query: 检索查询文本。
            sources: 检索范围 source_type 列表。
            primary_sources: 主查库集合，用于 source_priority 加权。
            vector_weight: 向量得分权重（默认 0.65）。
            fulltext_weight: 全文得分权重（默认 0.25）。
                source_priority_weight 自动由 1.0 - vector_weight - fulltext_weight 计算。
            vector_top_k: 向量检索 top-k。
            fulltext_top_k: 全文检索 top-k。
            top_k: 最终返回条数。
            filters: 附加过滤条件，当前支持 {"source_id": "<uuid>"}。

        Returns:
            排序后的 Evidence 列表。

        Raises:
            RAGUnavailableError: PG 不可用时抛出。
        """
        if primary_sources is None:
            primary_sources = set(sources)

        # 计算 source_priority_weight，确保三权重之和为 1.0
        source_priority_weight = round(1.0 - vector_weight - fulltext_weight, 6)
        if source_priority_weight < 0:
            source_priority_weight = 0.0

        vector_hits: list[VectorHit] = []
        fulltext_hits: list[FulltextHit] = []
        vector_failed = False

        # ---- 向量检索 ----
        try:
            async with measure("rag.vector"):
                vector_hits = await self._vector_search(
                    query, sources, top_k=vector_top_k, filters=filters,
                )
        except Exception as exc:
            vector_failed = True
            # 不泄露 API key 或完整异常响应
            logger.warning(
                "向量检索失败，降级为 PG 全文检索: error_type=%s",
                type(exc).__name__,
            )

        # ---- PG 全文检索 ----
        async with measure("rag.fulltext"):
            fulltext_hits = await self._fulltext_search(
                query, sources, top_k=fulltext_top_k, filters=filters,
            )

        # ---- 降级：向量失败且全文也无结果 ----
        if vector_failed and not fulltext_hits:
            return []

        # ---- 合并去重 ----
        merged = merge_deduplicate(vector_hits, fulltext_hits, primary_sources)

        if not merged:
            return []

        # ---- 回填向量命中缺失的 content_snippet ----
        async with measure("rag.backfill"):
            await self._backfill_content_snippets(merged)

        # ---- 重排（MVP 加权 或 Cross-Encoder / LLM Reranker）----
        settings = get_settings()
        if (
            settings.rag_reranker_enabled
            and len(merged) > settings.rag_reranker_final_top_k
        ):
            # 先取 top-K 送入 reranker
            candidates = merged[:settings.rag_reranker_top_k]

            if settings.rag_reranker_provider == "llm":
                evidences = await llm_rerank(
                    query=query,
                    merged_hits=candidates,
                    gateway=self._get_reranker_gateway(),
                    model=settings.rag_reranker_model or settings.chat_model,
                    top_k=settings.rag_reranker_final_top_k,
                    timeout=settings.rag_reranker_timeout_seconds,
                )
            else:
                evidences = await cross_encoder_rerank(
                    query=query,
                    merged_hits=candidates,
                    gateway=self._get_reranker_gateway(),
                    model=settings.rag_reranker_model,
                    top_k=settings.rag_reranker_final_top_k,
                    timeout=settings.rag_reranker_timeout_seconds,
                )
        else:
            # MVP 加权求和（默认路径 / 降级路径）
            evidences = rerank(
                merged,
                top_k=top_k,
                vector_weight=vector_weight,
                fulltext_weight=fulltext_weight,
                source_priority_weight=source_priority_weight,
            )

        return evidences

    # -------------------------------------------------------------------
    # 向量检索
    # -------------------------------------------------------------------

    async def _vector_search(
        self,
        query: str,
        sources: list[str],
        *,
        top_k: int = 12,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Milvus 向量检索。

        使用 ModelGatewayClient.embed() 对 query 生成 embedding，
        然后在 Milvus collection 中搜索，按 source_type 和 filters 过滤。

        Args:
            query: 查询文本。
            sources: source_type 过滤列表。
            top_k: 返回 top-k 结果。
            filters: 附加过滤条件（传入 _build_milvus_filter_expr）。

        Returns:
            VectorHit 列表。

        Raises:
            EmbeddingUnavailableError: Embedding 不可用。
            Exception: Milvus 不可用或其他错误。
        """
        # 1. 生成 query embedding —— 先查 Redis 缓存，未命中再调网关
        trace_id = f"rag-vector-{uuid.uuid4().hex[:8]}"
        from app.rag.embedding_cache import get_embedding, set_embedding

        cached = await get_embedding(query)
        if cached is not None:
            query_vector = cached
            logger.debug("embedding cache 命中: query_len=%d", len(query))
        else:
            async with measure("rag.embed"):
                embeddings = await self._gateway.embed([query], trace_id=trace_id)
            query_vector = embeddings[0]
            # 命中率统计留给后续；此处只缓存单条 query embedding（TTL 1h）
            await set_embedding(query, query_vector)
            # L3: 运行时关联预热（fire-and-forget，不阻塞检索主路径）
            asyncio.ensure_future(self._warm_related_queries(query))

        # 2. Milvus 检索（to_thread 释放事件循环；pymilvus 同步阻塞无成熟 async）
        milvus = self._get_milvus_client()
        collection_name = self._settings.milvus_collection

        # 构建过滤表达式（含 source_type + filters）
        filter_expr = _build_milvus_filter_expr(sources, filters)

        # T3.5/M1: 仅当 collection 含 content 字段时加入 output_fields（省 PG 回填往返；
        # chunks p99=783 chars, max=800 chars, 集合膨胀可忽略）。字段检测一次性缓存，
        # 避免旧 collection 每次搜索都先抛 MilvusException 再回退（双倍 RTT + 错误日志刷屏）。
        has_content = await self._collection_has_content(milvus, collection_name)
        output_fields = ["chunk_id", "source_type", "source_id", "title", "content_hash"]
        if has_content:
            output_fields.append("content")

        search_params = {
            "collection_name": collection_name,
            "data": [query_vector],
            "limit": top_k,
            "output_fields": output_fields,
            "filter": filter_expr,
        }

        results = await asyncio.to_thread(milvus.search, **search_params)

        # 3. 解析结果
        hits: list[VectorHit] = []
        if results and len(results) > 0:
            for item in results[0]:
                entity = item.get("entity", item)
                # PyMilvus 将 COSINE 相似度放在 distance 字段中；值越大越相似。
                cosine_similarity = float(item.get("distance", 0.0))
                score = max(0.0, min(1.0, (cosine_similarity + 1.0) / 2.0))

                hits.append(
                    VectorHit(
                        chunk_id=str(entity.get("chunk_id", "")),
                        source_type=str(entity.get("source_type", "")),
                        source_id=str(entity.get("source_id", "")),
                        title=str(entity.get("title", "")),
                        content_hash=str(entity.get("content_hash", "")),
                        vector_score=score,
                        content=str(entity.get("content", "")),
                    )
                )

        logger.info(
            "向量检索完成: query_len=%d, sources=%s, top_k=%d, hits=%d",
            len(query),
            sources,
            top_k,
            len(hits),
        )
        return hits

    # -------------------------------------------------------------------
    # PG 全文检索
    # -------------------------------------------------------------------

    async def _fulltext_search(
        self,
        query: str,
        sources: list[str],
        *,
        top_k: int = 12,
        filters: dict[str, Any] | None = None,
    ) -> list[FulltextHit]:
        """PostgreSQL 全文检索。

        使用 to_tsvector('simple', content) + plainto_tsquery 进行检索，
        并使用 ilike 作为补充 fallback 提升中文检索召回。

        仅查询 active（deleted_at IS NULL）且 embedding_status='done' 的 chunk。

        Args:
            query: 查询文本。
            sources: source_type 过滤列表。
            top_k: 返回 top-k 结果。
            filters: 附加过滤条件，当前支持 source_id 精确匹配。

        Returns:
            FulltextHit 列表。

        Raises:
            RAGUnavailableError: PG 不可用时抛出。
        """
        session_factory = self._get_session_factory()

        try:
            async with session_factory() as session:
                ts_query = func.plainto_tsquery("simple", query)
                rank_expr = func.ts_rank(
                    func.to_tsvector("simple", KnowledgeChunk.content),
                    ts_query,
                )

                ilike_pattern = f"%{query}%"

                # 基础 WHERE 条件
                conditions = [
                    KnowledgeChunk.source_type.in_(sources),
                    KnowledgeChunk.deleted_at.is_(None),
                    KnowledgeChunk.embedding_status == "done",
                    # 全文匹配 OR ilike 模糊匹配
                    (func.to_tsvector("simple", KnowledgeChunk.content).op("@@")(ts_query))
                    | (KnowledgeChunk.content.ilike(ilike_pattern)),
                ]

                # 应用 filters
                if filters:
                    source_id = filters.get("source_id")
                    if source_id is not None:
                        conditions.append(KnowledgeChunk.source_id == source_id)

                stmt = (
                    select(
                        KnowledgeChunk.id,
                        KnowledgeChunk.source_type,
                        KnowledgeChunk.source_id,
                        KnowledgeChunk.title,
                        KnowledgeChunk.content,
                        rank_expr.label("ts_rank"),
                    )
                    .where(*conditions)
                    .order_by(rank_expr.desc())
                    .limit(top_k)
                )

                result = await session.execute(stmt)
                rows = result.all()

                # 计算归一化得分
                hits: list[FulltextHit] = []
                max_rank = max((row.ts_rank for row in rows), default=0.0)

                for row in rows:
                    # 归一化 fulltext_score 到 [0, 1]
                    normalized_score = row.ts_rank / max_rank if max_rank > 0 else 0.5

                    hits.append(
                        FulltextHit(
                            chunk_id=str(row.id),
                            source_type=row.source_type,
                            source_id=str(row.source_id),
                            title=row.title,
                            content=row.content,
                            fulltext_score=round(min(normalized_score, 1.0), 6),
                        )
                    )

                logger.info(
                    "PG 全文检索完成: query_len=%d, sources=%s, top_k=%d, hits=%d",
                    len(query),
                    sources,
                    top_k,
                    len(hits),
                )
                return hits

        except RAGUnavailableError:
            raise
        except Exception as exc:
            logger.error(
                "PG 全文检索失败: error_type=%s",
                type(exc).__name__,
            )
            raise RAGUnavailableError(
                "RAG 检索不可用：数据库连接失败",
                retryable=True,
            ) from exc

    # -------------------------------------------------------------------
    # content_snippet 回填
    # -------------------------------------------------------------------

    async def _backfill_content_snippets(self, merged_hits: list[MergedHit]) -> None:
        """对 content_snippet 为空的命中，批量从 PG 回填 content。

        仅查询那些仅有向量命中（无全文命中）的 chunk，避免不必要的 DB 访问。
        """
        # 收集需要回填的 chunk_id
        empty_ids = [h.chunk_id for h in merged_hits if not h.content_snippet]
        if not empty_ids:
            return

        session_factory = self._get_session_factory()

        try:
            async with session_factory() as session:
                stmt = select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.content,
                ).where(
                    KnowledgeChunk.id.in_(empty_ids),
                    KnowledgeChunk.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                rows = result.all()

                content_map: dict[str, str] = {str(row.id): row.content for row in rows}

                for hit in merged_hits:
                    if not hit.content_snippet and hit.chunk_id in content_map:
                        hit.content_snippet = _truncate_snippet(content_map[hit.chunk_id])

        except RAGUnavailableError:
            raise
        except Exception as exc:
            logger.warning(
                "content_snippet 回填失败，部分向量命中可能缺少正文: error_type=%s",
                type(exc).__name__,
            )
            # 回填失败不应阻断检索——Agent 仍可通过 chunk_id 自行查询

    # -------------------------------------------------------------------
    # 辅助方法
    # -------------------------------------------------------------------

    @staticmethod
    def _validate_sources(sources: list[str]) -> list[str]:
        """校验 source_type 列表。"""
        invalid = set(sources) - VALID_SOURCE_TYPES
        if invalid:
            raise ValueError(f"无效的 source_type: {invalid}，合法值: {VALID_SOURCE_TYPES}")
        return sources
