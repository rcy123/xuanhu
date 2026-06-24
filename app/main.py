"""悬壶（Xuanhu）FastAPI 应用入口。

创建 ASGI 应用实例，注册路由，启动时输出脱敏配置。
"""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.gateway import ModelGatewayClient

logger = logging.getLogger("xuanhu")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """应用生命周期 — 启动时输出脱敏配置，确保不泄露敏感信息。"""
    settings = get_settings()
    logger.info("应用启动，当前配置（已脱敏）: %s", settings.safe_dump())
    yield


app = FastAPI(
    title="悬壶（Xuanhu）",
    version=get_settings().app_version,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(health_router)


@app.get("/health")
async def health_root() -> JSONResponse:
    """根路径基础健康检查（兼容运维探针）。

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


@app.get("/health/llm")
async def health_llm_root() -> JSONResponse:
    """根路径模型网关连通性检查（兼容运维探针）。

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
