"""L1 production lifecycle, shared hot path, and readiness regression tests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import ConfigValidatingCheckpointer
from app.agent_runtime.errors import CheckpointError
from app.agent_runtime.lifecycle import (
    LangGraphRuntimeState,
    SharedLangGraphRuntime,
    allow_request_local_runtime_fallback,
    check_shared_langgraph_runtime,
    shared_langgraph_runtime,
)
from app.schemas.message import MessageCreateRequest, MessageCreateResponse


class _FakeCheckpointer:
    def __init__(self) -> None:
        self.table_reads = 0

    async def aget_tuple(self, config: dict[str, Any]) -> None:
        assert config["configurable"]["thread_id"] == "v1:__readiness__"
        self.table_reads += 1


class _FakePool:
    def __init__(self, *, health_error: Exception | None = None) -> None:
        self.health_error = health_error
        self.check_calls = 0

    async def check(self) -> None:
        self.check_calls += 1
        if self.health_error is not None:
            raise self.health_error


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.invocations: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.active = 0
        self.max_active = 0

    async def ainvoke(
        self,
        state: dict[str, Any],
        *,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            self.invocations.append((state, config))
            return state
        finally:
            self.active -= 1

    async def aget_state(
        self,
        config: dict[str, Any],
        *,
        subgraphs: bool = False,
    ) -> Any:
        # Contract matching the production CompiledStateGraph.aget_state call
        # in ``_start_or_resume_intake``: return a snapshot with no pending
        # tasks so the intake graph is invoked for the first time.
        return SimpleNamespace(tasks=[], values={}, subgraph_states={})


def _runtime(
    saver: object | None = None,
    graph: object | None = None,
    pool: object | None = None,
    *,
    graph_version: str = "v1",
) -> SharedLangGraphRuntime:
    return SharedLangGraphRuntime(
        checkpointer=cast(ConfigValidatingCheckpointer, saver or _FakeCheckpointer()),
        graph=cast(
            CompiledStateGraph[Any, Any, Any, Any],
            graph or _FakeCompiledGraph(),
        ),
        pool=cast(AsyncConnectionPool[Any], pool or _FakePool()),
        graph_version=graph_version,
    )


def test_request_local_fallback_is_test_only_and_never_masks_startup_failure() -> None:
    assert allow_request_local_runtime_fallback(None, test_fallback_enabled=True) is True
    assert (
        allow_request_local_runtime_fallback(
            LangGraphRuntimeState(status="closed"),
            test_fallback_enabled=True,
        )
        is True
    )
    assert (
        allow_request_local_runtime_fallback(
            LangGraphRuntimeState.unavailable(error_code="CHECKPOINT_CREATE_FAILED"),
            test_fallback_enabled=True,
        )
        is False
    )
    assert allow_request_local_runtime_fallback(None) is False


def test_supported_uvicorn_entry_uses_selector_loop_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import uvicorn

    from app import server

    factory = uvicorn.Config(
        "app.main:app",
        loop=server.UVICORN_LOOP_FACTORY,
    ).get_loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()

    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace(api_host="127.0.0.1", api_port=8123))
    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    server.main()
    assert captured == {
        "app": "app.main:app",
        "host": "127.0.0.1",
        "port": 8123,
        "loop": server.UVICORN_LOOP_FACTORY,
    }


@pytest.mark.asyncio
async def test_production_service_defaults_fail_before_langgraph_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.advance import _run_langgraph_advance
    from app.core.exceptions import AgentTriggerFailedError, ModelGatewayUnavailableError
    from app.services.langgraph_intake import LangGraphIntakeMessageRunner
    from app.services.message import MessageService

    db = cast(AsyncSession, object())
    service = MessageService(db)

    async def langgraph_session(_session_id: str) -> Any:
        return SimpleNamespace(agent_runtime="langgraph")

    monkeypatch.setattr(service, "_load_session", langgraph_session)
    with pytest.raises(AgentTriggerFailedError) as message_error:
        await service.submit_message(
            str(uuid.uuid4()),
            MessageCreateRequest(role="patient_proxy", content="headache"),
            doctor_id="doctor-test",
            trace_id="trace-test",
        )
    assert message_error.value.agent_error_code == "LANGGRAPH_RUNTIME_UNAVAILABLE"

    runner = LangGraphIntakeMessageRunner(db)
    with pytest.raises(AgentTriggerFailedError):
        await runner.submit_message(
            "not-even-parsed",
            MessageCreateRequest(role="patient_proxy", content="headache"),
            doctor_id="doctor-test",
            trace_id="trace-test",
            x_state_version=None,
        )

    with pytest.raises(ModelGatewayUnavailableError):
        await _run_langgraph_advance(
            db,
            cast(Any, SimpleNamespace(agent_runtime="langgraph")),
            session_id=str(uuid.uuid4()),
            state_version=None,
            trace_id="trace-test",
        )


@pytest.mark.asyncio
async def test_shared_runtime_context_opens_compiles_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent_runtime import lifecycle as lifecycle_module

    calls = {"construct": 0, "open": 0, "setup": 0, "compile": 0, "close": 0}
    pools: list[Any] = []
    graph = cast(CompiledStateGraph[Any, Any, Any, Any], _FakeCompiledGraph())

    class LifecyclePool:
        def __init__(self, **kwargs: object) -> None:
            calls["construct"] += 1
            assert kwargs["open"] is False
            assert kwargs["min_size"] == 2
            assert kwargs["max_size"] == 4
            pools.append(self)

        async def open(self, *, wait: bool, timeout: float) -> None:
            assert wait is True
            assert timeout == 1.0
            calls["open"] += 1

        async def close(self, *, timeout: float) -> None:
            assert timeout == 2.0
            calls["close"] += 1

    class LifecycleSaver(BaseCheckpointSaver[Any]):
        def __init__(self, pool: object) -> None:
            super().__init__()
            assert pool is pools[0]

        async def setup(self) -> None:
            calls["setup"] += 1

    def fake_build(*, checkpointer: BaseCheckpointSaver[Any]) -> Any:
        assert isinstance(checkpointer, ConfigValidatingCheckpointer)
        calls["compile"] += 1
        return graph

    monkeypatch.setattr(lifecycle_module, "AsyncConnectionPool", LifecyclePool)
    monkeypatch.setattr(lifecycle_module, "AsyncPostgresSaver", LifecycleSaver)
    monkeypatch.setattr(lifecycle_module, "build_main_graph", fake_build)

    async with shared_langgraph_runtime(
        "postgresql://must-not-be-logged",
        pool_min_size=2,
        pool_max_size=4,
        open_timeout_seconds=1.0,
        close_timeout_seconds=2.0,
    ) as runtime:
        assert isinstance(runtime.checkpointer, ConfigValidatingCheckpointer)
        assert runtime.graph is graph
        assert runtime.pool is pools[0]
        assert calls == {
            "construct": 1,
            "open": 1,
            "setup": 1,
            "compile": 1,
            "close": 0,
        }

    assert calls == {
        "construct": 1,
        "open": 1,
        "setup": 1,
        "compile": 1,
        "close": 1,
    }


@pytest.mark.asyncio
async def test_fastapi_lifespan_shares_runtime_and_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    calls = {"open": 0, "close": 0}
    runtime = _runtime()

    @asynccontextmanager
    async def fake_shared_runtime(_db_url: str):  # type: ignore[no-untyped-def]
        calls["open"] += 1
        try:
            yield runtime
        finally:
            calls["close"] += 1

    settings = SimpleNamespace(
        app_env="local",
        xuanhu_prod_secret_guard=True,
        database_url="postgresql://must-not-be-logged",
        outbox_publisher_enabled=False,
        outbox_publisher_shutdown_grace_seconds=1.0,
        async_command_enabled=False,
        async_command_shutdown_grace_seconds=1.0,
        safe_dump=lambda: {"database_url": "***"},
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "shared_langgraph_runtime",
        fake_shared_runtime,
    )
    application = FastAPI()

    with pytest.raises(RuntimeError, match="request failed"):
        async with main_module.lifespan(application):
            state = application.state.langgraph_runtime_state
            assert state.status == "ready"
            assert state.runtime is runtime
            assert calls == {"open": 1, "close": 0}
            raise RuntimeError("request failed")

    assert calls == {"open": 1, "close": 1}
    assert application.state.langgraph_runtime_state.status == "closed"


@pytest.mark.asyncio
async def test_fastapi_lifespan_startup_failure_is_degraded_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import main as main_module

    secret = "postgresql://user:password@private-host:5432/private-db"

    @asynccontextmanager
    async def failing_runtime(_db_url: str):  # type: ignore[no-untyped-def]
        raise CheckpointError(
            f"setup failed for {secret}",
            code="CHECKPOINT_CREATE_FAILED",
        )
        yield _runtime()  # pragma: no cover

    settings = SimpleNamespace(
        app_env="local",
        xuanhu_prod_secret_guard=True,
        database_url=secret,
        outbox_publisher_enabled=False,
        outbox_publisher_shutdown_grace_seconds=1.0,
        async_command_enabled=False,
        async_command_shutdown_grace_seconds=1.0,
        safe_dump=lambda: {"database_url": "***"},
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "shared_langgraph_runtime", failing_runtime)
    application = FastAPI()

    with caplog.at_level(logging.ERROR, logger="xuanhu"):
        async with main_module.lifespan(application):
            state = application.state.langgraph_runtime_state
            assert state.status == "unavailable"
            assert state.runtime is None
            assert state.error_code == "CHECKPOINT_CREATE_FAILED"

    assert secret not in caplog.text
    assert "password" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_readiness_checks_shared_saver_tables_and_graph_version_once() -> None:
    saver = _FakeCheckpointer()
    pool = _FakePool()
    checks = await check_shared_langgraph_runtime(LangGraphRuntimeState.ready(_runtime(saver, pool=pool)))

    assert checks == {
        "langgraph_checkpointer": "ok",
        "langgraph_checkpoint_tables": "ok",
        "langgraph_graph_version": "ok",
    }
    assert pool.check_calls == 1
    assert saver.table_reads == 1


@pytest.mark.asyncio
async def test_readiness_degrades_without_leaking_checkpoint_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://user:password@private-host:5432/private-db"
    pool = _FakePool(health_error=RuntimeError(f"connection failed: {secret}"))
    with caplog.at_level(logging.WARNING, logger="xuanhu.langgraph_lifecycle"):
        checks = await check_shared_langgraph_runtime(LangGraphRuntimeState.ready(_runtime(pool=pool)))

    assert checks == {
        "langgraph_checkpointer": "unavailable",
        "langgraph_checkpoint_tables": "unavailable",
        "langgraph_graph_version": "unavailable",
    }
    assert secret not in str(checks)
    assert secret not in caplog.text
    assert "password" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_readiness_rejects_stale_compiled_graph_version() -> None:
    pool = _FakePool()
    checks = await check_shared_langgraph_runtime(LangGraphRuntimeState.ready(_runtime(pool=pool, graph_version="v0")))

    assert pool.check_calls == 1
    assert checks["langgraph_checkpointer"] == "ok"
    assert checks["langgraph_checkpoint_tables"] == "unavailable"
    assert checks["langgraph_graph_version"] == "incompatible"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["ok", "mismatch"])
async def test_runtime_switch_readiness_uses_independent_session_and_fixed_status(
    monkeypatch: pytest.MonkeyPatch,
    audit_status: str,
) -> None:
    from app.services.health import HealthService
    from app.services.runtime_switch_audit import RuntimeSwitchAuditService

    session = object()
    opened = 0
    closed = 0
    configured: list[str] = []

    class SessionContext:
        async def __aenter__(self) -> object:
            nonlocal opened
            opened += 1
            return session

        async def __aexit__(self, *_args: object) -> None:
            nonlocal closed
            closed += 1

    async def fake_status(_self: object, runtime: str) -> Any:
        configured.append(runtime)
        return SimpleNamespace(status=audit_status)

    monkeypatch.setattr(
        "app.db.session.get_session_factory",
        lambda: SessionContext,
    )
    monkeypatch.setattr(
        "app.services.health.get_settings",
        lambda: SimpleNamespace(agent_runtime_version="legacy"),
    )
    monkeypatch.setattr(RuntimeSwitchAuditService, "status", fake_status)

    result = await HealthService()._check_runtime_switch_audit()  # noqa: SLF001

    assert result == audit_status
    assert opened == 1
    assert closed == 1
    assert configured == ["legacy"]


@pytest.mark.asyncio
async def test_runtime_switch_readiness_failure_is_unavailable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services.health import HealthService
    from app.services.runtime_switch_audit import RuntimeSwitchAuditService

    secret = "postgresql://user:password@private-host:5432/private-db"

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def fail_status(_self: object, _runtime: str) -> Any:
        raise RuntimeError(f"audit database failed: {secret}")

    monkeypatch.setattr(
        "app.db.session.get_session_factory",
        lambda: SessionContext,
    )
    monkeypatch.setattr(RuntimeSwitchAuditService, "status", fail_status)

    with caplog.at_level(logging.WARNING, logger="xuanhu.health"):
        result = await HealthService()._check_runtime_switch_audit()  # noqa: SLF001

    assert result == "unavailable"
    assert secret not in caplog.text
    assert "password" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_ready_check_exposes_runtime_switch_mismatch_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import health as health_module

    async def ok(_self: object) -> str:
        return "ok"

    async def disabled(_self: object) -> str:
        return "disabled"

    async def mismatch(_self: object) -> str:
        return "mismatch"

    async def gateway_ok(_self: object) -> dict[str, str]:
        return {"chat": "ok", "embedding": "ok"}

    async def langgraph_ok(_state: object) -> dict[str, str]:
        return {
            "langgraph_checkpointer": "ok",
            "langgraph_checkpoint_tables": "ok",
            "langgraph_graph_version": "ok",
        }

    monkeypatch.setattr(health_module.HealthService, "_check_database", ok)
    monkeypatch.setattr(health_module.HealthService, "_check_redis", ok)
    monkeypatch.setattr(health_module.HealthService, "_check_outbox", disabled)
    monkeypatch.setattr(
        health_module.HealthService,
        "_check_runtime_switch_audit",
        mismatch,
    )
    monkeypatch.setattr(health_module.HealthService, "_check_milvus", ok)
    monkeypatch.setattr(health_module.HealthService, "_check_gateway", gateway_ok)
    monkeypatch.setattr(
        health_module,
        "check_shared_langgraph_runtime",
        langgraph_ok,
    )

    result = await health_module.HealthService().ready_check()

    assert result["checks"]["runtime_switch_audit"] == "mismatch"
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_messages_hot_path_uses_shared_graph_without_local_setup_or_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import langgraph_intake as intake_module

    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    graph = _FakeCompiledGraph()
    runtime = _runtime(graph=graph)
    response = MessageCreateResponse(
        message_id=str(uuid.uuid4()),
        session_id=str(session_id),
        role="patient_proxy",
        stage="inquiry",
        content="headache",
        current_stage="inquiry",
        state_version=2,
        created_at=datetime.now(UTC),
    )

    async def fake_claim(*_args: object, **_kwargs: object) -> Any:
        claim = SimpleNamespace(id=claim_id, run_id=run_id)
        return intake_module._ClaimResult(  # noqa: SLF001
            cast(Any, claim),
            cast(Any, object()),
        )

    async def fake_wait(*_args: object, **_kwargs: object) -> MessageCreateResponse:
        return response

    def fail_fallback(*_args: object, **_kwargs: object) -> None:
        pytest.fail("request-local checkpointer/compile fallback was called")

    monkeypatch.setattr(
        intake_module.LangGraphIntakeMessageRunner,
        "_claim_or_replay",
        fake_claim,
    )
    monkeypatch.setattr(
        intake_module.LangGraphIntakeMessageRunner,
        "_wait_for_completed_claim",
        fake_wait,
    )
    monkeypatch.setattr(intake_module, "postgres_checkpointer", fail_fallback)
    monkeypatch.setattr(intake_module, "build_main_graph", fail_fallback)

    runner = intake_module.LangGraphIntakeMessageRunner(
        cast(AsyncSession, object()),
        shared_runtime=runtime,
        allow_request_local_runtime=False,
    )
    result = await runner.submit_message(
        str(session_id),
        MessageCreateRequest(role="patient_proxy", content="headache"),
        doctor_id="doctor-a",
        trace_id="trace-a",
        x_state_version=1,
        idempotency_key="message-a",
    )

    assert result is response
    assert len(graph.invocations) == 1
    state, config = graph.invocations[0]
    assert state["command"] == "message"
    assert config["configurable"]["thread_id"] == f"v1:{session_id}"


@pytest.mark.asyncio
async def test_advance_hot_path_uses_shared_compiled_graph() -> None:
    from app.api.advance import _invoke_shared_reasoning_graph

    session_id = str(uuid.uuid4())
    graph = _FakeCompiledGraph()
    await _invoke_shared_reasoning_graph(
        _runtime(graph=graph),
        session_id=session_id,
        command_key="advance-a",
        run_id=uuid.uuid4(),
    )

    assert len(graph.invocations) == 1
    state, config = graph.invocations[0]
    assert state["command"] == "advance"
    assert config["configurable"]["thread_id"] == f"v1:{session_id}"


@pytest.mark.asyncio
async def test_shared_compiled_graph_accepts_concurrent_request_invocations() -> None:
    from app.agent_runtime.config import make_run_config

    graph = _FakeCompiledGraph()
    runtime = _runtime(graph=graph)

    async def invoke(index: int) -> None:
        session_id = f"concurrent-{index}"
        await runtime.runner(timeout_seconds=1).ainvoke(
            {
                "session_id": session_id,
                "graph_version": "v1",
            },
            config=make_run_config(session_id),
        )

    await asyncio.gather(*(invoke(index) for index in range(8)))

    assert len(graph.invocations) == 8
    assert graph.max_active == 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_shared_pool_concurrent_checkpoint_reads_and_close() -> None:
    from app.agent_runtime.config import make_run_config
    from app.core.config import get_settings

    async with shared_langgraph_runtime(
        get_settings().database_url,
        pool_min_size=2,
        pool_max_size=4,
        open_timeout_seconds=10,
        close_timeout_seconds=5,
    ) as runtime:
        pool = runtime.pool
        delegate = runtime.checkpointer._delegate  # noqa: SLF001
        assert delegate.conn is pool  # type: ignore[attr-defined]
        assert pool.closed is False

        results = await asyncio.gather(
            *(runtime.checkpointer.aget_tuple(make_run_config(f"pool-concurrent-{index}")) for index in range(8))
        )
        checks = await check_shared_langgraph_runtime(LangGraphRuntimeState.ready(runtime))
        stats = pool.get_stats()

        assert results == [None] * 8
        assert checks == {
            "langgraph_checkpointer": "ok",
            "langgraph_checkpoint_tables": "ok",
            "langgraph_graph_version": "ok",
        }
        assert stats["pool_size"] >= 2
        assert stats["pool_size"] <= 4

    assert pool.closed is True
