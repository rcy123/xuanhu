"""健康检查接口。

所有健康检查接口不使用标准 envelope，直接返回扁平 JSON，
与 Kubernetes probe 和运维监控系统兼容。

响应不得包含 API key、prompt 原文、完整模型输出或患者信息。
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.gateway import ModelGatewayClient

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
