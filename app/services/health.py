"""健康检查服务层。

P3-4 实现 ready 和 RAG 健康检查。
所有检查不得泄露 API key、连接串、异常堆栈。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.agent_runtime.lifecycle import (
    LangGraphRuntimeState,
    check_shared_langgraph_runtime,
)
from app.core.config import get_settings
from app.core.gateway import ModelGatewayClient
from app.services.runtime_switch_audit import (
    PostgresRuntimeSwitchAuditRepository,
    RuntimeSwitchAuditService,
)

logger = logging.getLogger("xuanhu.health")

# 用于 sample_query 的轻量检索查询
_RAG_SAMPLE_QUERY = "麻黄"
_RAG_SAMPLE_TOP_K = 2


def _now_iso() -> str:
    """返回 UTC ISO 时间字符串。"""
    return datetime.now(UTC).isoformat()


class HealthService:
    """就绪检查与 RAG 健康检查服务。"""

    def __init__(
        self,
        *,
        langgraph_runtime_state: LangGraphRuntimeState | None = None,
    ) -> None:
        self._langgraph_runtime_state = langgraph_runtime_state

    # -------------------------------------------------------------------
    # Ready 健康检查
    # -------------------------------------------------------------------

    async def ready_check(self) -> dict[str, Any]:
        """执行所有依赖组件的连通性检查。

        Returns:
            dict: 扁平 JSON，含 status、version、checks、timestamp。
            status: "ready" 或 "degraded"。
        """
        settings = get_settings()
        checks: dict[str, str] = {}

        # database
        checks["database"] = await self._check_database()

        # redis
        checks["redis"] = await self._check_redis()

        # durable outbox publisher (aggregate counters only)
        checks["outbox"] = await self._check_outbox()

        # The configured default runtime is deploy-time state; it must match
        # the independent durable switch ledger before this worker is ready.
        checks["runtime_switch_audit"] = await self._check_runtime_switch_audit()

        # Process-scoped LangGraph runtime.  These checks use the saver and
        # compiled-graph identity created by the ASGI lifespan; they never
        # create a request-local checkpointer.
        checks.update(await check_shared_langgraph_runtime(self._langgraph_runtime_state))

        # milvus
        checks["milvus"] = await self._check_milvus()

        # llm_gateway + embedding_gateway（通过网关 health_check）
        gw_checks = await self._check_gateway()
        checks["llm_gateway"] = gw_checks.get("chat", "unavailable")
        checks["embedding_gateway"] = gw_checks.get("embedding", "unavailable")

        # The durable publisher is part of the production write contract.
        # ``disabled`` is observable but is not a ready state.
        all_ok = all(v == "ok" for v in checks.values())
        overall_status = "ready" if all_ok else "degraded"

        return {
            "status": overall_status,
            "version": settings.app_version,
            "checks": checks,
            "timestamp": _now_iso(),
        }

    # -------------------------------------------------------------------
    # RAG 健康检查
    # -------------------------------------------------------------------

    async def rag_check(self) -> dict[str, Any]:
        """执行 RAG 检索链路健康检查。

        Returns:
            dict: 扁平 JSON，含 status、checks、timestamp。
            status: "ok" 或 "degraded"。
        """
        checks: dict[str, str] = {}

        # pg_fulltext
        checks["pg_fulltext"] = await self._check_pg_fulltext()

        # milvus_collection
        checks["milvus_collection"] = await self._check_milvus_collection()

        # sample_query
        checks["sample_query"] = await self._check_sample_query()

        all_ok = all(v == "ok" for v in checks.values())
        overall_status = "ok" if all_ok else "degraded"

        return {
            "status": overall_status,
            "checks": checks,
            "timestamp": _now_iso(),
        }

    # -------------------------------------------------------------------
    # 单项检查
    # -------------------------------------------------------------------

    async def _check_database(self) -> str:
        """检查 PostgreSQL 连通性（SELECT 1）。"""
        try:
            from sqlalchemy import text

            from app.db.session import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            return "ok"
        except Exception as exc:
            logger.warning("database 健康检查失败: %s", type(exc).__name__)
            return "unavailable"

    async def _check_redis(self) -> str:
        """检查 Redis 连通性（PING）。"""
        try:
            from app.core.redis import get_redis

            redis = await get_redis()
            await redis.ping()
            return "ok"
        except Exception as exc:
            logger.warning("redis 健康检查失败: %s", type(exc).__name__)
            return "unavailable"

    async def outbox_check(self) -> dict[str, Any]:
        """Return privacy-safe backlog, age and DLQ health metrics."""
        settings = get_settings()
        if not settings.outbox_publisher_enabled:
            return {
                "status": "disabled",
                "backlog_count": 0,
                "pending_count": 0,
                "leased_count": 0,
                "dead_letter_count": 0,
                "oldest_unpublished_age_seconds": 0.0,
                "timestamp": _now_iso(),
            }
        try:
            from app.agent_runtime.repository import PostgresDomainRepository
            from app.db.session import get_session_factory

            metrics = await PostgresDomainRepository(get_session_factory()).get_outbox_health()
            degraded = (
                metrics.dead_letter_count > settings.outbox_ready_max_dead_letters
                or metrics.oldest_unpublished_age_seconds > settings.outbox_ready_max_oldest_age_seconds
            )
            return {
                "status": "degraded" if degraded else "ok",
                **metrics.model_dump(),
                "timestamp": _now_iso(),
            }
        except Exception as exc:
            logger.warning("outbox 健康检查失败: %s", type(exc).__name__)
            return {
                "status": "unavailable",
                "backlog_count": 0,
                "pending_count": 0,
                "leased_count": 0,
                "dead_letter_count": 0,
                "oldest_unpublished_age_seconds": 0.0,
                "timestamp": _now_iso(),
            }

    async def _check_outbox(self) -> str:
        return str((await self.outbox_check())["status"])

    async def _check_runtime_switch_audit(self) -> str:
        """Validate the deployment default against the durable global ledger."""

        try:
            from app.db.session import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                result = await RuntimeSwitchAuditService(PostgresRuntimeSwitchAuditRepository(session)).status(
                    get_settings().agent_runtime_version
                )
            return result.status
        except Exception as exc:
            logger.warning(
                "runtime-switch audit readiness failed: error_type=%s",
                type(exc).__name__,
            )
            return "unavailable"

    async def _check_milvus(self) -> str:
        """检查 Milvus 连通性。"""
        try:
            settings = get_settings()
            from pymilvus import MilvusClient

            client = MilvusClient(
                uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
                timeout=settings.milvus_timeout_seconds,
            )
            # 尝试列出 collections 以验证连通性
            client.list_collections()
            return "ok"
        except Exception as exc:
            logger.warning("milvus 健康检查失败: %s", type(exc).__name__)
            return "unavailable"

    async def _check_gateway(self) -> dict[str, str]:
        """检查模型网关连通性（复用 ModelGatewayClient.health_check）。"""
        try:
            client = ModelGatewayClient()
            return await client.health_check()
        except Exception as exc:
            logger.warning("gateway 健康检查失败: %s", type(exc).__name__)
            return {"chat": "unavailable", "embedding": "unavailable"}

    async def _check_pg_fulltext(self) -> str:
        """检查 PostgreSQL 全文检索可用性。

        执行一条轻量 ts_query 确认全文检索功能正常。
        """
        try:
            from sqlalchemy import func, select, text

            from app.db.session import get_session_factory
            from app.models.knowledge import KnowledgeChunk

            factory = get_session_factory()
            async with factory() as session:
                # 确认表存在并全文检索可用
                await session.execute(text("SELECT 1 FROM knowledge_chunks LIMIT 0"))
                # 执行一次轻量全文检索
                ts_query = func.plainto_tsquery("simple", _RAG_SAMPLE_QUERY)
                stmt = (
                    select(func.count())
                    .where(
                        KnowledgeChunk.deleted_at.is_(None),
                        func.to_tsvector("simple", KnowledgeChunk.content).op("@@")(ts_query),
                    )
                    .limit(1)
                )
                await session.execute(stmt)
            return "ok"
        except Exception as exc:
            logger.warning("pg_fulltext 健康检查失败: %s", type(exc).__name__)
            return "unavailable"

    async def _check_milvus_collection(self) -> str:
        """检查 Milvus collection 是否存在且可用。"""
        try:
            settings = get_settings()
            from pymilvus import MilvusClient

            client = MilvusClient(
                uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
                timeout=settings.milvus_timeout_seconds,
            )
            collections = client.list_collections()
            if settings.milvus_collection in collections:
                # 进一步检查 collection 是否可查询
                client.describe_collection(settings.milvus_collection)
                return "ok"
            else:
                logger.warning(
                    "milvus_collection 健康检查: collection %s 不存在",
                    settings.milvus_collection,
                )
                return "unavailable"
        except Exception as exc:
            logger.warning("milvus_collection 健康检查失败: %s", type(exc).__name__)
            return "unavailable"

    async def _check_sample_query(self) -> str:
        """执行轻量 RAG 检索样本查询。

        使用小 top_k 避免大规模检索，不依赖真实医学质量。
        """
        try:
            from app.rag.retriever import RAGRetriever

            retriever = RAGRetriever()
            results = await retriever.retrieve(
                query=_RAG_SAMPLE_QUERY,
                primary_sources=["herb"],
                allow_cross_source=False,
                top_k=_RAG_SAMPLE_TOP_K,
            )
            # 检索无结果也视作功能正常（仅确认链路通畅）
            _ = len(results)
            return "ok"
        except Exception as exc:
            logger.warning("sample_query 健康检查失败: %s", type(exc).__name__)
            return "unavailable"
