"""悬壶（Xuanhu）FastAPI 应用入口。

创建 ASGI 应用实例，注册路由，启动时输出脱敏配置。
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.messages import message_exception_handlers
from app.api.messages import router as messages_router
from app.api.record import record_exception_handlers
from app.api.record import router as record_router
from app.api.recovery import recovery_exception_handlers
from app.api.recovery import router as recovery_router
from app.api.review import review_exception_handlers
from app.api.review import router as review_router
from app.api.sessions import router as sessions_router
from app.api.sessions import session_exception_handlers
from app.api.stream import router as stream_router
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
app.include_router(sessions_router)
app.include_router(messages_router)
app.include_router(stream_router)
app.include_router(recovery_router)
app.include_router(review_router)
app.include_router(record_router)

# 注册会话、消息、恢复、review 与 record 路由自定义异常处理器
for exc_cls, handler in {
    **session_exception_handlers,
    **message_exception_handlers,
    **recovery_exception_handlers,
    **review_exception_handlers,
    **record_exception_handlers,
}.items():
    app.add_exception_handler(exc_cls, handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """将 FastAPI 请求校验失败转换为标准 envelope：VALIDATION_ERROR。"""
    trace_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or str(uuid.uuid4())
    )
    # 提取首条校验错误信息作为 detail
    detail_parts = []
    for err in exc.errors():
        loc = ".".join(str(loc) for loc in err.get("loc", []))
        msg = err.get("msg", "")
        detail_parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(detail_parts[:3]) if detail_parts else None

    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "detail": detail,
            "retryable": False,
            "stage": None,
            "trace_id": trace_id,
        },
    )


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
