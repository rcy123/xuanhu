"""Recoverable L4-4 ReasoningSubgraph.

The graph stores only JSON-safe execution references in Graph State. Clinical
payloads are rebuilt by service nodes from Repository authority and durable
artifact payload rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.commands import NODE_REASONING_SUBGRAPH_V1
from app.agent_runtime.state import XuanhuGraphState

REASONING_SUBGRAPH_VERSION = "reasoning-subgraph.v1"
ReasoningExecutor = Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]

NODE_REASONING_PRECHECK = "reasoning.precheck"
NODE_REASONING_BUILD_SYNDROME_CONTEXT = "reasoning.build_syndrome_context"
NODE_REASONING_DRAFT_SYNDROME = "reasoning.draft_syndrome"
NODE_REASONING_VERIFY_SYNDROME = "reasoning.verify_syndrome"
NODE_REASONING_BUILD_FORMULA_CONTEXT = "reasoning.build_formula_context"
NODE_REASONING_DRAFT_FORMULA = "reasoning.draft_formula"
NODE_REASONING_VERIFY_FORMULA = "reasoning.verify_formula_consistency"
NODE_REASONING_INVALIDATE_DOWNSTREAM = "reasoning.invalidate_downstream"
NODE_REASONING_MANUAL_REQUIRED = "reasoning.manual_required"
NODE_REASONING_READY_FOR_SAFETY = "reasoning.ready_for_safety"

ROUTE_SYNDROME_COMPLETED = "syndrome_completed"
ROUTE_FORMULA_COMPLETED = "formula_completed"
ROUTE_NEEDS_MORE_INFO = "needs_more_info"
ROUTE_MANUAL_REQUIRED = "manual_required"


def _safe_error(state: XuanhuGraphState, code: str, detail: str) -> dict[str, Any]:
    return {
        "route": NODE_REASONING_SUBGRAPH_V1,
        "reasoning_route": ROUTE_MANUAL_REQUIRED,
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


async def _precheck_node(state: XuanhuGraphState) -> dict[str, Any]:
    if not _has_valid_refs(state):
        return _safe_error(state, "REASONING_COMMAND_REF_INVALID", "reasoning command refs are invalid")
    from app.services.langgraph_reasoning import run_reasoning_precheck_node

    return await run_reasoning_precheck_node(state)


async def _build_syndrome_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_build_syndrome_context_node

    return await run_reasoning_build_syndrome_context_node(state)


async def _draft_syndrome_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_draft_syndrome_node

    return await run_reasoning_draft_syndrome_node(state)


async def _verify_syndrome_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_verify_syndrome_node

    return await run_reasoning_verify_syndrome_node(state)


async def _build_formula_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_build_formula_context_node

    return await run_reasoning_build_formula_context_node(state)


async def _draft_formula_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_draft_formula_node

    return await run_reasoning_draft_formula_node(state)


async def _verify_formula_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_verify_formula_node

    return await run_reasoning_verify_formula_node(state)


async def _invalidate_downstream_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_invalidate_downstream_node

    return await run_reasoning_invalidate_downstream_node(state)


async def _manual_required_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_manual_required_node

    return await run_reasoning_manual_required_node(state)


async def _ready_for_safety_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_reasoning import run_reasoning_ready_for_safety_node

    return await run_reasoning_ready_for_safety_node(state)


def _route_after_syndrome(state: XuanhuGraphState) -> str:
    route = state.get("reasoning_route")
    if route == ROUTE_SYNDROME_COMPLETED:
        return NODE_REASONING_BUILD_FORMULA_CONTEXT
    if route == ROUTE_NEEDS_MORE_INFO:
        return NODE_REASONING_INVALIDATE_DOWNSTREAM
    return NODE_REASONING_MANUAL_REQUIRED


def _route_after_formula(state: XuanhuGraphState) -> str:
    route = state.get("reasoning_route")
    if route == ROUTE_FORMULA_COMPLETED:
        return NODE_REASONING_READY_FOR_SAFETY
    if route == ROUTE_NEEDS_MORE_INFO:
        return NODE_REASONING_INVALIDATE_DOWNSTREAM
    return NODE_REASONING_MANUAL_REQUIRED


def make_reasoning_subgraph_node(executor: ReasoningExecutor) -> ReasoningExecutor:
    async def _node(state: XuanhuGraphState) -> dict[str, Any]:
        return await executor(state)

    _node.__name__ = "reasoning_subgraph_v1"
    _node.__qualname__ = "reasoning_subgraph_v1"
    return _node


def build_reasoning_subgraph(
    *,
    reasoning_executor: ReasoningExecutor | None = None,
) -> CompiledStateGraph[XuanhuGraphState, None, XuanhuGraphState, XuanhuGraphState]:
    graph = StateGraph(XuanhuGraphState)
    if reasoning_executor is not None:
        injected_node: Any = make_reasoning_subgraph_node(reasoning_executor)
        graph.add_node(NODE_REASONING_PRECHECK, injected_node)
        graph.add_edge(START, NODE_REASONING_PRECHECK)
        graph.add_edge(NODE_REASONING_PRECHECK, END)
        return graph.compile()

    graph.add_node(NODE_REASONING_PRECHECK, _precheck_node)
    graph.add_node(NODE_REASONING_BUILD_SYNDROME_CONTEXT, _build_syndrome_context_node)
    graph.add_node(NODE_REASONING_DRAFT_SYNDROME, _draft_syndrome_node)
    graph.add_node(NODE_REASONING_VERIFY_SYNDROME, _verify_syndrome_node)
    graph.add_node(NODE_REASONING_BUILD_FORMULA_CONTEXT, _build_formula_context_node)
    graph.add_node(NODE_REASONING_DRAFT_FORMULA, _draft_formula_node)
    graph.add_node(NODE_REASONING_VERIFY_FORMULA, _verify_formula_node)
    graph.add_node(NODE_REASONING_INVALIDATE_DOWNSTREAM, _invalidate_downstream_node)
    graph.add_node(NODE_REASONING_MANUAL_REQUIRED, _manual_required_node)
    graph.add_node(NODE_REASONING_READY_FOR_SAFETY, _ready_for_safety_node)

    graph.add_edge(START, NODE_REASONING_PRECHECK)
    graph.add_edge(NODE_REASONING_PRECHECK, NODE_REASONING_BUILD_SYNDROME_CONTEXT)
    graph.add_edge(NODE_REASONING_BUILD_SYNDROME_CONTEXT, NODE_REASONING_DRAFT_SYNDROME)
    graph.add_edge(NODE_REASONING_DRAFT_SYNDROME, NODE_REASONING_VERIFY_SYNDROME)
    graph.add_conditional_edges(
        NODE_REASONING_VERIFY_SYNDROME,
        _route_after_syndrome,
        {
            NODE_REASONING_BUILD_FORMULA_CONTEXT: NODE_REASONING_BUILD_FORMULA_CONTEXT,
            NODE_REASONING_INVALIDATE_DOWNSTREAM: NODE_REASONING_INVALIDATE_DOWNSTREAM,
            NODE_REASONING_MANUAL_REQUIRED: NODE_REASONING_MANUAL_REQUIRED,
        },
    )
    graph.add_edge(NODE_REASONING_BUILD_FORMULA_CONTEXT, NODE_REASONING_DRAFT_FORMULA)
    graph.add_edge(NODE_REASONING_DRAFT_FORMULA, NODE_REASONING_VERIFY_FORMULA)
    graph.add_conditional_edges(
        NODE_REASONING_VERIFY_FORMULA,
        _route_after_formula,
        {
            NODE_REASONING_READY_FOR_SAFETY: NODE_REASONING_READY_FOR_SAFETY,
            NODE_REASONING_INVALIDATE_DOWNSTREAM: NODE_REASONING_INVALIDATE_DOWNSTREAM,
            NODE_REASONING_MANUAL_REQUIRED: NODE_REASONING_MANUAL_REQUIRED,
        },
    )
    graph.add_edge(NODE_REASONING_INVALIDATE_DOWNSTREAM, END)
    graph.add_edge(NODE_REASONING_MANUAL_REQUIRED, END)
    graph.add_edge(NODE_REASONING_READY_FOR_SAFETY, END)
    return graph.compile()
