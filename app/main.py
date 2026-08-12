"""悬壶（Xuanhu）FastAPI 应用入口。

创建 ASGI 应用实例，注册路由，启动时输出脱敏配置。
"""

import asyncio
import logging
import traceback
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent_runtime.lifecycle import (
    LangGraphRuntimeState,
    safe_runtime_error_code,
    shared_langgraph_runtime,
)
from app.api.advance import advance_exception_handlers
from app.api.advance import router as advance_router
from app.api.auth import auth_exception_handlers
from app.api.auth import router as auth_router
from app.api.commands import command_exception_handlers
from app.api.commands import router as commands_router
from app.api.health import router as health_router
from app.api.message_rollback import rollback_exception_handlers
from app.api.message_rollback import router as message_rollback_router
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
from app.core.config import ensure_production_secrets_ready, get_settings
from app.core.exceptions import (
    HttpCommandRecoveryRequiredError,
    HttpCommandReplayError,
    RateLimitedError,
)
from app.core.gateway import ModelGatewayClient
from app.core.log_filter import install_phi_redaction

logger = logging.getLogger("xuanhu")

# 阶段 2 加固：全进程日志 PHI 脱敏（挂 xuanhu 命名空间 + root logger，
# 覆盖 uvicorn 等第三方日志路径；幂等安装）。
install_phi_redaction(root=True)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Own process-scoped runtime resources and close them reliably."""
    settings = get_settings()
    # 生产密钥守卫：占位符 / 默认值 / 缺失 → fail-fast（T1.6/T3.3）。
    ensure_production_secrets_ready(settings)
    logger.info("应用启动，当前配置（已脱敏）: %s", settings.safe_dump())

    # M1 启动快照：写 config.snapshot 审计事件并对比上一次快照，关键安全
    # 开关（langgraph_product_ready / rollout_phase / base_url）偏离即告警。
    # best-effort——审计库不可用只记 warning，不阻断启动。
    try:
        from app.db.session import get_session_factory
        from app.services.config_drift import record_startup_config_snapshot

        async with get_session_factory()() as db:
            await record_startup_config_snapshot(db, settings, trace_id=f"startup-{uuid.uuid4().hex}")
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("config.snapshot 启动留痕失败（best-effort 忽略）", exc_info=True)

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
        app.state.langgraph_runtime_state = LangGraphRuntimeState.unavailable(error_code=error_code)
        logger.error(
            "LangGraph 共享运行时启动失败，readiness 将保持 degraded: code=%s",
            error_code,
        )

    stop: asyncio.Event | None = None
    publisher_task: asyncio.Task[None] | None = None
    command_stop: asyncio.Event | None = None
    command_task: asyncio.Task[None] | None = None
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
        # R6-A async-command worker, R6-B business handlers, R7 rollout default.
        # Admission is initialized disabled unconditionally (also clearing stale
        # state from a prior lifespan). When the worker is enabled (R7 default
        # true; operator kill switch = set false), handlers are registered ONLY
        # if the shared LangGraph runtime started; a runtime that failed to start
        # (or an empty registry) leaves admission disabled, so the three POST
        # routes fail closed to the synchronous R1-R5 path. Readiness is marked
        # only by the supervised worker task itself once it is actually running
        # (start handshake), so admission can never read "ready" with no consumer.
        from app.agent_runtime.async_command_lifecycle import (
            disable_async_command_state,
            init_async_command_state,
            run_supervised_async_command_worker,
        )

        init_async_command_state(app.state)
        command_started: asyncio.Event | None = None
        if settings.async_command_enabled:
            from app.agent_runtime.async_command import PostgresAsyncCommandRepository
            from app.agent_runtime.async_command_worker import build_async_command_worker
            from app.agent_runtime.async_handlers import build_async_command_handlers
            from app.db.session import get_session_factory

            runtime_state = getattr(app.state, "langgraph_runtime_state", None)
            ready_runtime = (
                runtime_state.runtime
                if runtime_state is not None and getattr(runtime_state, "status", "") == "ready"
                else None
            )
            handlers = build_async_command_handlers(ready_runtime)
            if handlers:
                command_stop = asyncio.Event()
                command_started = asyncio.Event()
                worker = build_async_command_worker(
                    PostgresAsyncCommandRepository(get_session_factory()),
                    handlers=handlers,
                    worker_id=f"async-{uuid.uuid4().hex}",
                    batch_size=settings.async_command_batch_size,
                    lease_seconds=settings.async_command_lease_seconds,
                    heartbeat_interval_seconds=settings.async_command_heartbeat_seconds,
                    max_attempts=settings.async_command_max_attempts,
                    retry_base_seconds=settings.async_command_retry_base_seconds,
                    retry_max_seconds=settings.async_command_retry_max_seconds,
                    poll_interval_seconds=settings.async_command_poll_interval_seconds,
                )
                app.state.async_command_worker = worker
                command_task = asyncio.create_task(
                    run_supervised_async_command_worker(
                        app_state=app.state,
                        worker=worker,
                        stop=command_stop,
                        started=command_started,
                        handler_operations=frozenset(handlers.keys()),
                    ),
                    name="xuanhu-async-command-worker",
                )
                # Start handshake: only begin serving once the worker task has
                # actually been scheduled and marked readiness. If the task never
                # runs (pathological loop failure), fail closed and stay disabled.
                await command_started.wait()
        yield
    finally:
        try:
            if command_stop is not None and command_task is not None:
                # Disable admission BEFORE stopping the worker so no new commands
                # are accepted during the shutdown drain window.
                disable_async_command_state(app.state)
                command_stop.set()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(command_task),
                        timeout=settings.async_command_shutdown_grace_seconds,
                    )
                except TimeoutError:
                    command_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await command_task
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
                    app.state.langgraph_runtime_state = LangGraphRuntimeState(status="closed")
                    # 关闭 gateway 连接池
                    await gateway.aclose()


_settings = get_settings()


def resolve_docs_url(app_env: str) -> str | None:
    """T4.3 交互式文档暴露面收敛（H9）：生产环境关闭 /docs。

    独立小函数便于单元测试；本地/staging 保留 /docs 供开发联调。
    """
    return None if app_env == "production" else "/docs"


app = FastAPI(
    title="悬壶（Xuanhu）",
    version=_settings.app_version,
    # T4.3 生产关闭交互式文档（H9）：docs/redoc 暴露路由与 schema 属信息
    # 泄露面，仅在 local/staging 环境保留 /docs 供开发联调。
    docs_url=resolve_docs_url(_settings.app_env),
    redoc_url=None,
    lifespan=lifespan,
)

# T4.5 CORS 白名单（M2）：只允许显式配置的来源。通配符 * 已在
# Settings 校验层直接禁止（cors_no_wildcard_origin fail-fast），
# 从根上杜绝「通配来源 + 携带凭据」的非法组合。
if _settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(messages_router)
app.include_router(message_rollback_router)
app.include_router(stream_router)
app.include_router(recovery_router)
app.include_router(review_router)
app.include_router(record_router)
app.include_router(advance_router)
app.include_router(safety_confirmations_router)
app.include_router(commands_router)

# 注册会话、消息、恢复、review 与 record 路由自定义异常处理器
for exc_cls, handler in {
    **session_exception_handlers,
    **message_exception_handlers,
    **recovery_exception_handlers,
    **review_exception_handlers,
    **record_exception_handlers,
    **advance_exception_handlers,
    **safety_confirmation_exception_handlers,
    **command_exception_handlers,
    **rollback_exception_handlers,
    **auth_exception_handlers,
}.items():
    app.add_exception_handler(exc_cls, handler)


async def http_command_outcome_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Render a persisted error replay or a fail-closed ambiguous command."""

    assert isinstance(exc, HttpCommandReplayError | HttpCommandRecoveryRequiredError)
    trace_id = request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())
    payload = {
        "code": exc.code,
        "message": exc.message,
        "detail": None,
        "retryable": exc.retryable,
        "stage": None,
        "trace_id": trace_id,
    }
    if isinstance(exc, HttpCommandReplayError):
        payload.update(exc.extra_payload)
    return JSONResponse(status_code=exc.status_code, content=payload)


app.add_exception_handler(HttpCommandReplayError, http_command_outcome_handler)
app.add_exception_handler(HttpCommandRecoveryRequiredError, http_command_outcome_handler)


@app.exception_handler(RateLimitedError)
async def rate_limited_handler(request: Request, exc: RateLimitedError) -> JSONResponse:
    """限流命中（H6）：429 + Retry-After，标准 envelope。

    Redis 滑动窗口拒绝时携带重试等待秒数，供客户端退避。
    """
    trace_id = request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())
    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )
    response.headers["Retry-After"] = str(exc.retry_after)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理器（T4.4）：未预期异常统一收敛为标准 500 envelope。

    完整堆栈只进服务端日志（含 trace_id 便于关联），响应体不携带任何
    异常信息（M6 脱敏约束——异常字符串可能包含 PHI/内部路径）。
    具体业务异常已在各路由级 handler 处理，此处仅捕获漏网之鱼。
    """
    trace_id = request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())
    logger.error(
        "unhandled exception trace_id=%s method=%s path=%s",
        trace_id,
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务器内部错误",
            "detail": None,
            "retryable": True,
            "stage": None,
            "trace_id": trace_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """将 FastAPI 请求校验失败转换为标准 envelope：VALIDATION_ERROR。

    阶段 2 加固（M6）：校验错误 detail 可能回显请求体输入（含 PHI），
    从响应体移除，仅保留 code/message/trace_id 给客户端排查。
    """
    del exc
    trace_id = request.headers.get("x-request-id") or request.headers.get("x-trace-id") or str(uuid.uuid4())

    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "detail": None,
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
    未运行 lifespan 的环境（如无 lifespan 的 ASGI 测试客户端）回退到临时
    实例并即时关闭，保证不依赖 lifespan 也能探测。
    """
    client: ModelGatewayClient | None = getattr(request.app.state, "gateway_client", None)
    if client is None:
        client = ModelGatewayClient()
        try:
            checks = await client.health_check()
        finally:
            await client.aclose()
    else:
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
