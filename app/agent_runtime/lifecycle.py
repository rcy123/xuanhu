"""Application-scoped LangGraph runtime lifecycle and readiness checks.

The production ASGI lifespan owns exactly one PostgreSQL checkpointer context
and one compiled MainGraph per worker process.  Request handlers receive this
shared runtime through ``app.state``.  Unit and integration tests that invoke
services directly can still opt into the explicit, request-local fallback.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent_runtime.checkpoint import ConfigValidatingCheckpointer
from app.agent_runtime.config import (
    DEFAULT_GRAPH_VERSION,
    make_run_config,
    validate_checkpoint_config,
)
from app.agent_runtime.errors import CheckpointError, GraphRunnerError
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner

logger = logging.getLogger("xuanhu.langgraph_lifecycle")

RuntimeStatus = Literal["starting", "ready", "unavailable", "closed"]
CheckpointPool = AsyncConnectionPool[AsyncConnection[DictRow]]
CHECKPOINT_POOL_MIN_SIZE = 1
CHECKPOINT_POOL_MAX_SIZE = 10
CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS = 10.0
CHECKPOINT_POOL_CLOSE_TIMEOUT_SECONDS = 5.0


class LangGraphRuntimeUnavailableError(GraphRunnerError):
    """The application-scoped runtime did not complete startup safely."""

    def __init__(self) -> None:
        super().__init__(
            "Shared LangGraph runtime is unavailable",
            code="LANGGRAPH_RUNTIME_UNAVAILABLE",
        )


@dataclass(frozen=True, slots=True)
class SharedLangGraphRuntime:
    """Process-local, immutable references shared by all request handlers."""

    checkpointer: ConfigValidatingCheckpointer
    graph: CompiledStateGraph[Any, Any, Any, Any]
    pool: CheckpointPool
    graph_version: str = DEFAULT_GRAPH_VERSION

    def runner(self, *, timeout_seconds: float) -> GraphRunner:
        """Create a lightweight runner around the already-compiled graph."""

        return GraphRunner(self.graph, timeout_seconds=timeout_seconds)


@dataclass(frozen=True, slots=True)
class LangGraphRuntimeState:
    """Privacy-safe startup state stored on ``FastAPI.state``."""

    status: RuntimeStatus
    runtime: SharedLangGraphRuntime | None = None
    error_code: str | None = None

    @classmethod
    def ready(cls, runtime: SharedLangGraphRuntime) -> LangGraphRuntimeState:
        return cls(status="ready", runtime=runtime)

    @classmethod
    def unavailable(cls, *, error_code: str) -> LangGraphRuntimeState:
        return cls(status="unavailable", error_code=error_code)


def safe_runtime_error_code(exc: Exception) -> str:
    """Return a fixed, non-sensitive startup error code."""

    if isinstance(exc, CheckpointError) and exc.code in {
        "CHECKPOINT_CREATE_FAILED",
        "CHECKPOINT_CLOSE_FAILED",
    }:
        return exc.code
    return "LANGGRAPH_RUNTIME_STARTUP_FAILED"


def allow_request_local_runtime_fallback(
    state: LangGraphRuntimeState | None,
    *,
    test_fallback_enabled: bool = False,
) -> bool:
    """Permit the explicit test-harness fallback outside a live lifespan.

    ASGITransport does not start lifespan automatically, and the shared global
    test app may retain the terminal ``closed`` marker from an earlier test.
    The opt-in comes from in-memory test application state, never a production
    environment variable. A startup failure remains ``unavailable`` and must
    never use the fallback.
    """

    return test_fallback_enabled and (state is None or state.status == "closed")


@asynccontextmanager
async def shared_langgraph_runtime(
    db_url: str,
    *,
    pool_min_size: int = CHECKPOINT_POOL_MIN_SIZE,
    pool_max_size: int = CHECKPOINT_POOL_MAX_SIZE,
    open_timeout_seconds: float = CHECKPOINT_POOL_OPEN_TIMEOUT_SECONDS,
    close_timeout_seconds: float = CHECKPOINT_POOL_CLOSE_TIMEOUT_SECONDS,
) -> AsyncIterator[SharedLangGraphRuntime]:
    """Own one async connection pool, saver, and compiled graph per process."""

    pool = cast(
        CheckpointPool,
        AsyncConnectionPool(
            conninfo=db_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            open=False,
            timeout=open_timeout_seconds,
            reconnect_timeout=open_timeout_seconds,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        ),
    )
    try:
        await pool.open(wait=True, timeout=open_timeout_seconds)
        saver = AsyncPostgresSaver(pool)
        # DDL/setup runs exactly once during application startup.  Readiness
        # below performs only pool/table reads and never repeats setup.
        await saver.setup()
        checkpointer = ConfigValidatingCheckpointer(saver)
        graph = build_main_graph(checkpointer=checkpointer)
    except Exception:
        with suppress(Exception):
            await pool.close(timeout=close_timeout_seconds)
        raise CheckpointError(
            "Failed to initialize pooled postgres checkpointer",
            code="CHECKPOINT_CREATE_FAILED",
        ) from None

    try:
        yield SharedLangGraphRuntime(
            checkpointer=checkpointer,
            graph=graph,
            pool=pool,
            graph_version=DEFAULT_GRAPH_VERSION,
        )
    finally:
        try:
            await pool.close(timeout=close_timeout_seconds)
        except Exception:
            raise CheckpointError(
                "Failed to close pooled postgres checkpointer",
                code="CHECKPOINT_CLOSE_FAILED",
            ) from None


async def check_shared_langgraph_runtime(
    state: LangGraphRuntimeState | None,
) -> dict[str, str]:
    """Check the shared saver, checkpoint tables, and graph-version contract.

    Only fixed status values are returned.  Connection details and exception
    messages never enter the readiness response or logs.
    """

    unavailable = {
        "langgraph_checkpointer": "unavailable",
        "langgraph_checkpoint_tables": "unavailable",
        "langgraph_graph_version": "unavailable",
    }
    if state is None or state.status != "ready" or state.runtime is None:
        return unavailable

    runtime = state.runtime
    checks = dict(unavailable)
    try:
        # ``check`` validates every currently idle pooled connection and
        # replaces broken ones.  It does not execute checkpoint DDL.
        await runtime.pool.check()
        checks["langgraph_checkpointer"] = "ok"
    except Exception as exc:
        logger.warning(
            "shared LangGraph checkpointer readiness failed: error_type=%s",
            type(exc).__name__,
        )
        return checks

    try:
        # A read through the shared saver proves that the checkpoint table
        # path is available without creating a per-probe saver or graph.
        await runtime.checkpointer.aget_tuple(
            make_run_config("__readiness__", graph_version=runtime.graph_version)
        )
        checks["langgraph_checkpoint_tables"] = "ok"
    except Exception as exc:
        logger.warning(
            "shared LangGraph checkpoint-table readiness failed: error_type=%s",
            type(exc).__name__,
        )

    try:
        if runtime.graph_version != DEFAULT_GRAPH_VERSION:
            raise ValueError("incompatible graph version")
        validate_checkpoint_config(
            make_run_config("__readiness__", graph_version=DEFAULT_GRAPH_VERSION),
            {
                "session_id": "__readiness__",
                "graph_version": runtime.graph_version,
            },
        )
        checks["langgraph_graph_version"] = "ok"
    except Exception as exc:
        logger.warning(
            "shared LangGraph graph-version readiness failed: error_type=%s",
            type(exc).__name__,
        )
        checks["langgraph_graph_version"] = "incompatible"

    return checks
