"""健康检查接口。

所有健康检查接口不使用标准 envelope，直接返回扁平 JSON，
与 Kubernetes probe 和运维监控系统兼容。

响应不得包含 API key、prompt 原文、完整模型输出或患者信息。
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.gateway import ModelGatewayClient
from app.services.health import HealthService
from app.services.outbox_metrics import PROMETHEUS_CONTENT_TYPE, render_outbox_prometheus

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
async def health() -> JSONResponse:
    """基础健康检查。

    Returns:
        JSONResponse: 扁平 JSON，含 status、version、timestamp。
    """
    settings = get_settings()
    return JSONResponse(
        content={
            "status": "ok",
            "version": settings.app_version,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/health/llm")
async def health_llm() -> JSONResponse:
    """模型网关连通性检查（版本化路径）。

    检查 Chat 模型和 Embedding 模型网关的连通性与路由状态。
    响应不使用标准 envelope，不泄露 API key、prompt 或完整模型输出。

    Returns:
        JSONResponse: 扁平 JSON，含 status、checks、timestamp。
    """
    client = ModelGatewayClient()
    checks = await client.health_check()

    all_ok = all(v == "ok" for v in checks.values())
    overall_status = "ok" if all_ok else "degraded"

    return JSONResponse(
        content={
            "status": overall_status,
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    """就绪检查（含中间件连通性）。

    检查 database、redis、milvus、llm_gateway、embedding_gateway。
    所有 ok 时 status=ready，任一 unavailable 时 status=degraded。
    响应不泄露连接串、API key、异常堆栈。

    Returns:
        JSONResponse: 扁平 JSON，含 status、version、checks、timestamp。
    """
    runtime_state = getattr(request.app.state, "langgraph_runtime_state", None)
    service = HealthService(langgraph_runtime_state=runtime_state)
    result = await service.ready_check()
    return JSONResponse(
        status_code=200 if result.get("status") == "ready" else 503,
        content=result,
    )


@router.get("/health/rag")
async def health_rag() -> JSONResponse:
    """RAG 检索链路检查。

    检查 pg_fulltext、milvus_collection、sample_query。
    RAG 不可用时 status=degraded，不抛出 500。
    响应不泄露 query 原文之外的敏感信息、API key、完整模型响应。

    Returns:
        JSONResponse: 扁平 JSON，含 status、checks、timestamp。
    """
    service = HealthService()
    result = await service.rag_check()
    return JSONResponse(content=result)


@router.get("/health/outbox")
async def health_outbox() -> JSONResponse:
    """Outbox backlog/DLQ health with aggregate, privacy-safe metrics only."""
    service = HealthService()
    return JSONResponse(content=await service.outbox_check())


@router.get("/metrics/outbox", include_in_schema=False)
async def metrics_outbox() -> Response:
    """Expose fixed-name, aggregate-only Outbox gauges for Prometheus."""

    settings = get_settings()
    health_result = await HealthService().outbox_check()
    document = render_outbox_prometheus(
        health_result,
        publisher_enabled=settings.outbox_publisher_enabled,
        ready_max_oldest_age_seconds=settings.outbox_ready_max_oldest_age_seconds,
        ready_max_dead_letters=settings.outbox_ready_max_dead_letters,
    )
    return Response(
        content=document,
        status_code=200,
        headers={
            "Content-Type": PROMETHEUS_CONTENT_TYPE,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
