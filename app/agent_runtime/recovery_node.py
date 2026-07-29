"""LangGraph-native product recovery node.

The historical outer node name remains ``recovery_placeholder`` for v1
checkpoint compatibility.  Its implementation now consumes only durable
command references; recovery request details stay in PostgreSQL and never
enter Graph State.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.commands import NODE_RECOVERY_PLACEHOLDER
from app.agent_runtime.state import XuanhuGraphState

RecoveryExecutor = Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]
NODE_RECOVERY_EXECUTE = "recovery.execute"


def _invalid_refs(state: XuanhuGraphState) -> dict[str, Any]:
    return {
        "route": NODE_RECOVERY_PLACEHOLDER,
        "last_error": {
            "code": "RECOVERY_COMMAND_REF_INVALID",
            "trace_id": state.get("run_id", ""),
            "detail": "recovery command refs are invalid",
        },
    }


def _has_valid_refs(state: XuanhuGraphState) -> bool:
    try:
        uuid.UUID(state.get("session_id", ""))
        uuid.UUID(state.get("run_id", ""))
    except (TypeError, ValueError):
        return False
    return bool(state.get("command_id"))


async def run_recovery_node(state: XuanhuGraphState) -> dict[str, Any]:
    """Execute one durable, reference-only recovery command."""

    if not _has_valid_refs(state):
        return _invalid_refs(state)

    from app.services.langgraph_recovery import execute_recovery_command

    return await execute_recovery_command(state)


def build_recovery_subgraph(
    *,
    recovery_executor: RecoveryExecutor | None = None,
) -> CompiledStateGraph[XuanhuGraphState, None, XuanhuGraphState, XuanhuGraphState]:
    """Build the single-step product recovery subgraph."""

    graph = StateGraph(XuanhuGraphState)
    # LangGraph's overloaded ``StateNode`` protocol does not preserve the
    # concrete TypedDict update shape for an injected async callable.  Keep
    # the public ``RecoveryExecutor`` contract above and narrow only this
    # third-party registration boundary, as the sibling graph builders do.
    executor: Any = recovery_executor or run_recovery_node
    graph.add_node(NODE_RECOVERY_EXECUTE, executor)
    graph.add_edge(START, NODE_RECOVERY_EXECUTE)
    graph.add_edge(NODE_RECOVERY_EXECUTE, END)
    return graph.compile()


__all__ = ["RecoveryExecutor", "build_recovery_subgraph", "run_recovery_node"]
