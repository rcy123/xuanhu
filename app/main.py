"""悬壶（Xuanhu）FastAPI 应用入口。

创建 ASGI 应用实例，注册路由，启动时输出脱敏配置。
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agent_runtime.lifecycle import (
    LangGraphRuntimeState,
    safe_runtime_error_code,
    shared_langgraph_runtime,
)
from app.api.advance import advance_exception_handlers
from app.api.advance import router as advance_router
from app.api.health import router as health_router
from app.api.messages import message_exception_handlers
from app.api.messages import router as messages_router
from app.api.record import record_exception_handlers
from app.api.record import router as record_router
from app.api.recovery import recovery_exception_handlers
from app.api.recovery import router as recovery_router
from app.api.review import review_exception_handlers
from app.api.review import router as review_router
from app.api.safety_confirmations import router as safety_confirmations_router
from app.api.safety_confirmations import safety_confirmation_exception_handlers
from app.api.sessions import router as sessions_router
from app.api.sessions import session_exception_handlers
from app.api.stream import router as stream_router
from app.core.config import get_settings
from app.core.exceptions import HttpCommandRecoveryRequiredError, HttpCommandReplayError
from app.core.gateway import ModelGatewayClient

logger = logging.getLogger("xuanhu")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Own process-scoped runtime resources and close them reliably."""
    settings = get_settings()
    logger.info("应用启动，当前配置（已脱敏）: %s", settings.safe_dump())

    # ── shared ModelGatewayClient（lifespan 托管连接池） ──
    gateway = ModelGatewayClient()
    app.state.gateway_client = gateway

    runtime_cm = shared_langgraph_runtime(settings.database_url)
    runtime_entered = False
    app.state.langgraph_runtime_state = LangGraphRuntimeState(status="starting")
    try:
        runtime = await runtime_cm.__aenter__()
        runtime_entered = True
        app.state.langgraph_runtime_state = LangGraphRuntimeState.ready(runtime)
    except Exception as exc:
        error_code = safe_runtime_error_code(exc)
        app.state.langgraph_runtime_state = LangGraphRuntimeState.unavailable(
            error_code=error_code
        )
        logger.error(
            "LangGraph 共享运行时启动失败，readiness 将保持 degraded: code=%s",
            error_code,
        )

    stop: asyncio.Event | None = None
    publisher_task: asyncio.Task[None] | None = None
    try:
        if settings.outbox_publisher_enabled:
            from app.agent_runtime.repository import PostgresDomainRepository
            from app.db.session import get_session_factory
            from app.services.events import EventService
            from app.services.outbox_publisher import OutboxPublisher

            stop = asyncio.Event()
            publisher = OutboxPublisher(
                PostgresDomainRepository(get_session_factory()),
                EventService(dedupe_ttl_seconds=settings.event_dedupe_ttl_seconds),
                worker_id=f"api-{uuid.uuid4().hex}",
                batch_size=settings.outbox_publisher_batch_size,
                lease_seconds=settings.outbox_publisher_lease_seconds,
                max_attempts=settings.outbox_publisher_max_attempts,
                base_retry_seconds=settings.outbox_publisher_base_retry_seconds,
                max_retry_seconds=settings.outbox_publisher_max_retry_seconds,
                poll_interval_seconds=settings.outbox_publisher_poll_interval_seconds,
            )
            app.state.outbox_publisher = publisher
            publisher_task = asyncio.create_task(
                publisher.run_forever(stop),
                name="xuanhu-outbox-publisher",
            )
        yield
    finally:
        try:
            if stop is not None and publisher_task is not None:
                stop.set()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(publisher_task),
                        timeout=settings.outbox_publisher_shutdown_grace_seconds,
                    )
                except TimeoutError:
                    publisher_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await publisher_task
        finally:
            try:
                if runtime_entered:
                    await runtime_cm.__aexit__(None, None, None)
            finally:
                app.state.langgraph_runtime_state = LangGraphRuntimeState(
                    status="closed"
                )
                # 关闭 gateway 连接池
                await gateway.aclose()


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
app.include_router(advance_router)
app.include_router(safety_confirmations_router)

# 注册会话、消息、恢复、review 与 record 路由自定义异常处理器
for exc_cls, handler in {
    **session_exception_handlers,
    **message_exception_handlers,
    **recovery_exception_handlers,
    **review_exception_handlers,
    **record_exception_handlers,
    **advance_exception_handlers,
    **safety_confirmation_exception_handlers,
}.items():
    app.add_exception_handler(exc_cls, handler)


async def http_command_outcome_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Render a persisted error replay or a fail-closed ambiguous command."""

    assert isinstance(exc, HttpCommandReplayError | HttpCommandRecoveryRequiredError)
    trace_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or str(uuid.uuid4())
    )
    payload = {
        "code": exc.code,
        "message": exc.message,
        "detail": exc.detail,
        "retryable": exc.retryable,
        "stage": None,
        "trace_id": trace_id,
    }
    if isinstance(exc, HttpCommandReplayError):
        payload.update(exc.extra_payload)
    return JSONResponse(status_code=exc.status_code, content=payload)


app.add_exception_handler(HttpCommandReplayError, http_command_outcome_handler)
app.add_exception_handler(HttpCommandRecoveryRequiredError, http_command_outcome_handler)


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
async def health_llm_root(request: Request) -> JSONResponse:
    """根路径模型网关连通性检查（兼容运维探针）。

    复用 lifespan 托管的共享 ``ModelGatewayClient`` 实例，避免临时创建。
    """
    client: ModelGatewayClient = request.app.state.gateway_client
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
