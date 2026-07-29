"""LangGraph-native Safety/Review interrupt node.

The historical node name stays ``review_placeholder`` so already completed v1
threads can continue using the same graph namespace.  The implementation is no
longer a placeholder: it prepares the durable Safety/Review request, suspends
with ``interrupt()``, and applies only a reference-only resume command.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.agent_runtime.commands import NODE_REVIEW_PLACEHOLDER
from app.agent_runtime.state import XuanhuGraphState

ReviewExecutor = Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]
NODE_REVIEW_PREPARE = "review.prepare_interrupt"
NODE_REVIEW_INTERRUPT = "review.interrupt"


def _invalid_refs(state: XuanhuGraphState) -> dict[str, Any]:
    return {
        "route": NODE_REVIEW_PLACEHOLDER,
        "last_error": {
            "code": "REVIEW_COMMAND_REF_INVALID",
            "trace_id": state.get("run_id", ""),
            "detail": "review command refs are invalid",
        },
    }


def _has_valid_refs(state: XuanhuGraphState) -> bool:
    try:
        uuid.UUID(state.get("session_id", ""))
        uuid.UUID(state.get("run_id", ""))
    except (TypeError, ValueError):
        return False
    return bool(state.get("command_id"))


async def _prepare_node(state: XuanhuGraphState) -> dict[str, Any]:
    """Persist the safety request before reaching the interrupt node."""

    if not _has_valid_refs(state):
        return _invalid_refs(state)

    from app.services.langgraph_review import prepare_review_interrupt

    return await prepare_review_interrupt(state)


async def run_review_interrupt_node(state: XuanhuGraphState) -> dict[str, Any]:
    """Interrupt, then apply a reference-only review submission.

    The preceding prepare node has already committed every pre-interrupt side
    effect and returned a minimal ``pending_interrupt`` state update.  On
    resume LangGraph restarts only this node, so preparation is not repeated.
    """

    if not _has_valid_refs(state):
        return _invalid_refs(state)

    from app.services.langgraph_review import (
        ReviewResumeRejected,
        apply_review_resume,
        load_prepared_review,
    )

    prepared = await load_prepared_review(state)
    retry_error_code: str | None = None
    while True:
        interrupt_payload = dict(prepared.interrupt_payload)
        if retry_error_code is not None:
            interrupt_payload["retry_error_code"] = retry_error_code
        resume_value = interrupt(interrupt_payload)
        try:
            return await apply_review_resume(
                state,
                prepared=prepared,
                resume_value=resume_value,
            )
        except ReviewResumeRejected as exc:
            # Returning a graph-shaped error here would complete this node and
            # permanently consume the interrupt.  A second interrupt call
            # allocates the next resumable slot in the same node/thread.  On
            # the next invocation LangGraph deterministically replays rejected
            # values before supplying the new reference.
            retry_error_code = exc.code
            prepared = await load_prepared_review(state)


def _route_after_prepare(state: XuanhuGraphState) -> str:
    if state.get("last_error") is not None or state.get("pending_interrupt") is None:
        return END
    return NODE_REVIEW_INTERRUPT


def build_review_subgraph(
    *,
    review_executor: ReviewExecutor | None = None,
) -> CompiledStateGraph[XuanhuGraphState, None, XuanhuGraphState, XuanhuGraphState]:
    """Build the two-phase durable prepare -> interrupt/resume subgraph."""

    graph = StateGraph(XuanhuGraphState)
    if review_executor is not None:
        async def _injected(state: XuanhuGraphState) -> dict[str, Any]:
            return await review_executor(state)

        _injected.__name__ = NODE_REVIEW_PLACEHOLDER
        _injected.__qualname__ = NODE_REVIEW_PLACEHOLDER
        graph.add_node(NODE_REVIEW_PREPARE, _injected)
        graph.add_edge(START, NODE_REVIEW_PREPARE)
        graph.add_edge(NODE_REVIEW_PREPARE, END)
        return graph.compile()

    graph.add_node(NODE_REVIEW_PREPARE, _prepare_node)
    graph.add_node(NODE_REVIEW_INTERRUPT, run_review_interrupt_node)
    graph.add_edge(START, NODE_REVIEW_PREPARE)
    graph.add_conditional_edges(
        NODE_REVIEW_PREPARE,
        _route_after_prepare,
        {NODE_REVIEW_INTERRUPT: NODE_REVIEW_INTERRUPT, END: END},
    )
    graph.add_edge(NODE_REVIEW_INTERRUPT, END)
    return graph.compile()


__all__ = ["ReviewExecutor", "build_review_subgraph", "run_review_interrupt_node"]
