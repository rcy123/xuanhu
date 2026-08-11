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
from langgraph.types import interrupt

from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1
from app.agent_runtime.state import XuanhuGraphState

INTAKE_SUBGRAPH_VERSION = "intake-subgraph.v1"
IntakeExecutor = Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]

NODE_INTAKE_PERSIST_MESSAGE = "intake.persist_message"
NODE_INTAKE_CLARIFY_PRECHECK = "intake.clarify_precheck"
NODE_INTAKE_CLARIFY_REPLY = "intake.clarify_reply"
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
# R1: 跨轮次 interrupt/resume 循环节点 — 仿照 review_node.py 两阶段模式
NODE_INTAKE_PREPARE_QUESTION = "intake.prepare_question"
NODE_INTAKE_INTERRUPT_QUESTION = "intake.interrupt_question"


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


async def _clarify_precheck_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_clarify_precheck_node

    return await run_intake_clarify_precheck_node(state)


async def _clarify_reply_node(state: XuanhuGraphState) -> dict[str, Any]:
    from app.services.langgraph_intake import run_intake_clarify_reply_node

    return await run_intake_clarify_reply_node(state)


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


# ---------------------------------------------------------------------------
# R1: 跨轮次 interrupt/resume 节点 — 仿照 review_node.py 两阶段 durable 模式
# ---------------------------------------------------------------------------


async def _prepare_question_node(state: XuanhuGraphState) -> dict[str, Any]:
    """Compose the next intake question and persist all pre-interrupt side effects.

    Follows the review prepare_interrupt pattern: commits durable domain state
    (delta, gates, question message, claim completion) and returns a minimal
    ``pending_interrupt`` state update.  The interrupt node then suspends; on
    resume LangGraph restarts only that node, so preparation is not repeated.
    """
    from app.services.langgraph_intake import run_intake_prepare_question_node

    return await run_intake_prepare_question_node(state)


async def _interrupt_question_node(state: XuanhuGraphState) -> dict[str, Any]:
    """Suspend with ``interrupt()`` carrying PHI-safe refs to the composed
    question, then on resume validate the new answer claim reference and route
    back to triage_precheck for the next extraction cycle.

    Follows the review interrupt pattern: the preceding prepare node has already
    committed every pre-interrupt side effect.  On resume LangGraph restarts
    only this node.  The resume value is validated against PostgreSQL state and
    rejected with a retry loop when stale or invalid.
    """
    from app.services.langgraph_intake import (
        IntakeInterruptRejected,
        apply_intake_resume,
        load_prepared_intake_question,
    )

    # Validate that the prepare node ran successfully
    pending = state.get("pending_interrupt")
    if pending is None or pending.get("kind") != "intake_question":
        return _safe_error(state, "INTAKE_INTERRUPT_INVALID", "no pending intake interrupt")

    prepared = await load_prepared_intake_question(state)
    retry_error_code: str | None = None
    while True:
        interrupt_payload = dict(prepared.interrupt_payload)
        if retry_error_code is not None:
            interrupt_payload["retry_error_code"] = retry_error_code
        resume_value = interrupt(interrupt_payload)
        try:
            return await apply_intake_resume(
                state,
                prepared=prepared,
                resume_value=resume_value,
            )
        except IntakeInterruptRejected as exc:
            # Rejected resume values do not permanently consume the interrupt.
            # A second interrupt call allocates the next resumable slot in the
            # same node/thread.  LangGraph deterministically replays rejected
            # values before supplying the new reference.
            retry_error_code = exc.code
            prepared = await load_prepared_intake_question(state)


def _route_after_gates(state: XuanhuGraphState) -> str:
    if state.get("last_error") is not None:
        return NODE_INTAKE_ROUTE_MANUAL
    route = state.get("intake_route")
    if route == "ready":
        return NODE_INTAKE_ROUTE_READY
    # R1: incomplete / conflict 不再经过旧路由节点（旧节点会直接 complete claim），
    # 直接进入 prepare_question → interrupt_question 跨轮次循环。
    if route == "incomplete":
        return NODE_INTAKE_PREPARE_QUESTION
    if route == "conflict":
        return NODE_INTAKE_PREPARE_QUESTION
    return NODE_INTAKE_ROUTE_MANUAL


def _route_after_clarify_precheck(state: XuanhuGraphState) -> str:
    if state.get("clarify_requested") is True:
        return NODE_INTAKE_CLARIFY_REPLY
    return NODE_INTAKE_BUILD_CONTEXT


def _route_after_extract(state: XuanhuGraphState) -> str:
    # L3-6：抽取放弃提取（abstained）时转向澄清 Agent，避免对非回答输入静默重问。
    # 社交消息和问诊结束信号（"诊毕"等）例外：跳过澄清，走正常 verify→reduce→gates
    # 管线重新计算 completeness，避免非医疗输入永久困在澄清循环中
    # （REAL-SESSION 0a456c42 "诊毕"→clarification 死锁）。
    if state.get("intake_decision") == "abstained":
        if state.get("intake_skip_clarification"):
            return NODE_INTAKE_VERIFY
        return NODE_INTAKE_CLARIFY_REPLY
    return NODE_INTAKE_VERIFY


# ---------------------------------------------------------------------------
# R1: 跨轮次 interrupt/resume 路由
# ---------------------------------------------------------------------------


def _route_after_prepare(state: XuanhuGraphState) -> str:
    """After prepare_question: if error or no pending interrupt, end.
    Otherwise proceed to interrupt_question to suspend.
    """
    if state.get("last_error") is not None or state.get("pending_interrupt") is None:
        return END
    return NODE_INTAKE_INTERRUPT_QUESTION


def _route_after_interrupt(state: XuanhuGraphState) -> str:
    """After interrupt_question resume: loop back to triage_precheck for the
    next extraction cycle, or route to manual on unrecoverable error.
    """
    if state.get("last_error") is not None:
        return NODE_INTAKE_ROUTE_MANUAL
    # R1: on successful resume, loop back through triage/build-context/extract/verify/reduce/gates
    return NODE_INTAKE_TRIAGE_PRECHECK


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
    graph.add_node(NODE_INTAKE_CLARIFY_PRECHECK, _clarify_precheck_node)
    graph.add_node(NODE_INTAKE_CLARIFY_REPLY, _clarify_reply_node)
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
    # R1: 跨轮次 interrupt/resume 节点
    graph.add_node(NODE_INTAKE_PREPARE_QUESTION, _prepare_question_node)
    graph.add_node(NODE_INTAKE_INTERRUPT_QUESTION, _interrupt_question_node)

    graph.add_edge(START, NODE_INTAKE_PERSIST_MESSAGE)
    # L3-6 澄清：triage_precheck（红旗检测）永远先于澄清短路执行，避免反问消息漏掉红旗标记；
    # clarify_precheck 只做强信号判定，命中才短路到澄清回复，否则走正常抽取流程。
    graph.add_edge(NODE_INTAKE_PERSIST_MESSAGE, NODE_INTAKE_TRIAGE_PRECHECK)
    graph.add_edge(NODE_INTAKE_TRIAGE_PRECHECK, NODE_INTAKE_CLARIFY_PRECHECK)
    graph.add_conditional_edges(
        NODE_INTAKE_CLARIFY_PRECHECK,
        _route_after_clarify_precheck,
        {
            NODE_INTAKE_CLARIFY_REPLY: NODE_INTAKE_CLARIFY_REPLY,
            NODE_INTAKE_BUILD_CONTEXT: NODE_INTAKE_BUILD_CONTEXT,
        },
    )
    graph.add_edge(NODE_INTAKE_BUILD_CONTEXT, NODE_INTAKE_EXTRACT)
    graph.add_conditional_edges(
        NODE_INTAKE_EXTRACT,
        _route_after_extract,
        {
            NODE_INTAKE_CLARIFY_REPLY: NODE_INTAKE_CLARIFY_REPLY,
            NODE_INTAKE_VERIFY: NODE_INTAKE_VERIFY,
        },
    )
    graph.add_edge(NODE_INTAKE_VERIFY, NODE_INTAKE_REDUCE)
    graph.add_edge(NODE_INTAKE_REDUCE, NODE_INTAKE_GATES_AND_ROUTE)
    graph.add_conditional_edges(
        NODE_INTAKE_GATES_AND_ROUTE,
        _route_after_gates,
        {
            NODE_INTAKE_ROUTE_READY: NODE_INTAKE_ROUTE_READY,
            NODE_INTAKE_PREPARE_QUESTION: NODE_INTAKE_PREPARE_QUESTION,
            NODE_INTAKE_ROUTE_MANUAL: NODE_INTAKE_ROUTE_MANUAL,
        },
    )
    # R1: ready / manual / clarify 仍直接结束（不进入 interrupt/resume 循环）
    graph.add_edge(NODE_INTAKE_ROUTE_READY, END)
    graph.add_edge(NODE_INTAKE_ROUTE_MANUAL, END)
    graph.add_edge(NODE_INTAKE_CLARIFY_REPLY, END)
    # R1: incomplete / conflict 不再经过旧路由节点，直接从 gates 进入 prepare_question
    # → interrupt_question → (resume) → triage_precheck 跨轮次循环
    # 旧节点 NODE_INTAKE_ROUTE_INCOMPLETE / NODE_INTAKE_ROUTE_CONFLICT 保留但不再被路由到。
    graph.add_edge(NODE_INTAKE_ROUTE_INCOMPLETE, END)
    graph.add_edge(NODE_INTAKE_ROUTE_CONFLICT, END)
    graph.add_conditional_edges(
        NODE_INTAKE_PREPARE_QUESTION,
        _route_after_prepare,
        {
            NODE_INTAKE_INTERRUPT_QUESTION: NODE_INTAKE_INTERRUPT_QUESTION,
            END: END,
        },
    )
    graph.add_conditional_edges(
        NODE_INTAKE_INTERRUPT_QUESTION,
        _route_after_interrupt,
        {
            NODE_INTAKE_TRIAGE_PRECHECK: NODE_INTAKE_TRIAGE_PRECHECK,
            NODE_INTAKE_ROUTE_MANUAL: NODE_INTAKE_ROUTE_MANUAL,
        },
    )
    return graph.compile()

async def intake_subgraph_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _gates_node(state)
