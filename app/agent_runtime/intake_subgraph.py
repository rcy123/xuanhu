"""Recoverable versioned IntakeSubgraph for L3-5.

The production path rebuilds all execution inputs from durable command refs in
Graph State. It does not capture request-scoped callables, ORM objects, model
clients, or patient payloads in checkpointed state.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1
from app.agent_runtime.state import XuanhuGraphState

INTAKE_SUBGRAPH_VERSION = "intake-subgraph.v1"
IntakeExecutor = Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]

NODE_INTAKE_PERSIST_MESSAGE = "intake.persist_message"
NODE_INTAKE_TRIAGE_PRECHECK = "intake.triage_precheck"
NODE_INTAKE_BUILD_CONTEXT = "intake.build_intake_context"
NODE_INTAKE_EXTRACT = "intake.extract_intake"
NODE_INTAKE_VERIFY = "intake.verify_intake"
NODE_INTAKE_REDUCE = "intake.reduce_observations"
NODE_INTAKE_GATES_AND_ROUTE = "intake.gates_and_route"
NODE_INTAKE_ROUTE_READY = "intake.route.ready"
NODE_INTAKE_ROUTE_INCOMPLETE = "intake.route.incomplete"
NODE_INTAKE_ROUTE_CONFLICT = "intake.route.conflict"
NODE_INTAKE_ROUTE_MANUAL = "intake.route.manual"


def _safe_error(state: XuanhuGraphState, code: str, detail: str) -> dict[str, Any]:
    return {
        "route": NODE_INTAKE_SUBGRAPH_V1,
        "last_error": {
            "code": code,
            "trace_id": state.get("run_id", ""),
            "detail": detail,
        },
    }


def _has_valid_refs(state: XuanhuGraphState) -> bool:
    try:
        uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        return False
    return bool(state.get("command_id"))


async def _persist_message_node(state: XuanhuGraphState) -> dict[str, Any]:
    if not _has_valid_refs(state):
        return _safe_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command refs are invalid")
    from app.services.langgraph_intake import run_intake_persist_message_node

    return await run_intake_persist_message_node(state)


async def _triage_precheck_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_triage_precheck_node

    return await run_intake_triage_precheck_node(state)


async def _build_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_build_context_node

    return await run_intake_build_context_node(state)


async def _extract_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_extract_node

    return await run_intake_extract_node(state)


async def _verify_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_verify_node

    return await run_intake_verify_node(state)


async def _reduce_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_reduce_node

    return await run_intake_reduce_node(state)


async def _gates_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_gates_node

    return await run_intake_gates_node(state)


async def _ready_route_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_route_ready_node

    return await run_intake_route_ready_node(state)


async def _incomplete_route_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_route_incomplete_node

    return await run_intake_route_incomplete_node(state)


async def _conflict_route_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_route_conflict_node

    return await run_intake_route_conflict_node(state)


async def _manual_route_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_route_manual_node

    return await run_intake_route_manual_node(state)


def _route_after_gates(state: XuanhuGraphState) -> str:
    if state.get("last_error") is not None:
        return NODE_INTAKE_ROUTE_MANUAL
    route = state.get("intake_route")
    if route == "ready":
        return NODE_INTAKE_ROUTE_READY
    if route == "incomplete":
        return NODE_INTAKE_ROUTE_INCOMPLETE
    if route == "conflict":
        return NODE_INTAKE_ROUTE_CONFLICT
    return NODE_INTAKE_ROUTE_MANUAL


def make_intake_subgraph_node(executor: IntakeExecutor) -> IntakeExecutor:
    async def _node(state: XuanhuGraphState) -> dict[str, Any]:
        return await executor(state)

    _node.__name__ = "intake_subgraph_v1"
    _node.__qualname__ = "intake_subgraph_v1"
    return _node


def build_intake_subgraph(
    *,
    intake_executor: IntakeExecutor | None = None,
) -> CompiledStateGraph[XuanhuGraphState, None, XuanhuGraphState, XuanhuGraphState]:
    graph = StateGraph(XuanhuGraphState)
    if intake_executor is not None:
        injected_node: Any = make_intake_subgraph_node(intake_executor)
        graph.add_node(NODE_INTAKE_GATES_AND_ROUTE, injected_node)
        graph.add_edge(START, NODE_INTAKE_GATES_AND_ROUTE)
        graph.add_edge(NODE_INTAKE_GATES_AND_ROUTE, END)
        return graph.compile()

    persist_node: Any = _persist_message_node
    graph.add_node(NODE_INTAKE_PERSIST_MESSAGE, persist_node)
    graph.add_node(NODE_INTAKE_TRIAGE_PRECHECK, _triage_precheck_node)
    graph.add_node(NODE_INTAKE_BUILD_CONTEXT, _build_context_node)
    graph.add_node(NODE_INTAKE_EXTRACT, _extract_node)
    graph.add_node(NODE_INTAKE_VERIFY, _verify_node)
    graph.add_node(NODE_INTAKE_REDUCE, _reduce_node)
    graph.add_node(NODE_INTAKE_GATES_AND_ROUTE, _gates_node)
    graph.add_node(NODE_INTAKE_ROUTE_READY, _ready_route_node)
    graph.add_node(NODE_INTAKE_ROUTE_INCOMPLETE, _incomplete_route_node)
    graph.add_node(NODE_INTAKE_ROUTE_CONFLICT, _conflict_route_node)
    graph.add_node(NODE_INTAKE_ROUTE_MANUAL, _manual_route_node)

    graph.add_edge(START, NODE_INTAKE_PERSIST_MESSAGE)
    graph.add_edge(NODE_INTAKE_PERSIST_MESSAGE, NODE_INTAKE_TRIAGE_PRECHECK)
    graph.add_edge(NODE_INTAKE_TRIAGE_PRECHECK, NODE_INTAKE_BUILD_CONTEXT)
    graph.add_edge(NODE_INTAKE_BUILD_CONTEXT, NODE_INTAKE_EXTRACT)
    graph.add_edge(NODE_INTAKE_EXTRACT, NODE_INTAKE_VERIFY)
    graph.add_edge(NODE_INTAKE_VERIFY, NODE_INTAKE_REDUCE)
    graph.add_edge(NODE_INTAKE_REDUCE, NODE_INTAKE_GATES_AND_ROUTE)
    graph.add_conditional_edges(
        NODE_INTAKE_GATES_AND_ROUTE,
        _route_after_gates,
        {
            NODE_INTAKE_ROUTE_READY: NODE_INTAKE_ROUTE_READY,
            NODE_INTAKE_ROUTE_INCOMPLETE: NODE_INTAKE_ROUTE_INCOMPLETE,
            NODE_INTAKE_ROUTE_CONFLICT: NODE_INTAKE_ROUTE_CONFLICT,
            NODE_INTAKE_ROUTE_MANUAL: NODE_INTAKE_ROUTE_MANUAL,
        },
    )
    graph.add_edge(NODE_INTAKE_ROUTE_READY, END)
    graph.add_edge(NODE_INTAKE_ROUTE_INCOMPLETE, END)
    graph.add_edge(NODE_INTAKE_ROUTE_CONFLICT, END)
    graph.add_edge(NODE_INTAKE_ROUTE_MANUAL, END)
    return graph.compile()


async def intake_subgraph_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _gates_node(state)
