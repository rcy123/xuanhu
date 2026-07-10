"""L1-3 跨进程恢复测试的子进程 helper。

本脚本由 ``tests/test_l1_3_postgres_checkpoint.py`` 通过 ``subprocess.run`` 调用，
用于验证跨进程 checkpoint 持久化。

用法:
    python tests/_l1_3_subprocess.py write <thread_id> <command>
    python tests/_l1_3_subprocess.py read <thread_id>
    python tests/_l1_3_subprocess.py interrupt <thread_id>
    python tests/_l1_3_subprocess.py resume <thread_id> <resume_value>

DB_URL 从环境变量 ``DB_URL`` 读取，不通过 argv 传递。

输出:
    JSON 格式的结果字典，包含 status 和 data 字段。
    status=ok 表示成功，status=error 表示失败。
    失败时 data 只包含脱敏的错误码/类型，不含原始异常消息。

Windows 事件循环:
    本脚本在入口点显式设置 ``WindowsSelectorEventLoopPolicy``，
    不在模块 import 时隐式修改全局策略。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from typing_extensions import TypedDict


class SubprocessState(TypedDict, total=False):
    """跨进程测试用的最小 state。"""

    counter: int
    last_node: str
    tags: list[str]
    session_id: str
    graph_version: str
    command: str
    route: str


def _setup_windows_event_loop() -> None:
    """在 Windows 上切换到 SelectorEventLoop 以兼容 psycopg v3 异步连接。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _get_db_url() -> str:
    """从环境变量获取 DB_URL。

    不通过 argv 接收 DB_URL，避免在进程参数中暴露连接字符串。
    """
    db_url = os.environ.get("DB_URL", "")
    if not db_url:
        raise RuntimeError("DB_URL environment variable is not set")
    return db_url


def _sanitize_error(exc: Exception) -> dict[str, str]:
    """将异常转换为固定的安全错误响应。"""
    return {
        "error_type": type(exc).__name__,
        "code": "CHECKPOINT_SUBPROCESS_FAILED",
    }


async def _do_write(db_url: str, thread_id: str, command: str) -> dict[str, Any]:
    """进程 A：写入 checkpoint 并退出。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    graph_def = StateGraph(SubprocessState)

    async def _increment(state: SubprocessState) -> dict[str, Any]:
        current = state.get("counter", 0)
        return {
            "counter": current + 1,
            "last_node": "increment",
            "tags": [*state.get("tags", []), "inc"],
        }

    def _finalize(state: SubprocessState) -> dict[str, Any]:
        return {"last_node": "finalize"}

    graph_def.add_node("increment", _increment)
    graph_def.add_node("finalize", _finalize)
    graph_def.add_edge(START, "increment")
    graph_def.add_edge("increment", "finalize")
    graph_def.add_edge("finalize", END)

    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        await saver.setup()
        graph = graph_def.compile(checkpointer=saver)
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        state: SubprocessState = {
            "counter": 42,
            "tags": ["process-a"],
            "session_id": thread_id.split(":", 1)[1] if ":" in thread_id else thread_id,
            "graph_version": thread_id.split(":", 1)[0] if ":" in thread_id else "v1",
            "command": command,
        }
        result = await graph.ainvoke(state, config=config)
        return {"status": "ok", "data": {"counter": result.get("counter"), "last_node": result.get("last_node")}}


async def _do_read(db_url: str, thread_id: str) -> dict[str, Any]:
    """进程 B：重建 checkpointer 并读取 checkpoint。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    graph_def = StateGraph(SubprocessState)

    async def _increment(state: SubprocessState) -> dict[str, Any]:
        current = state.get("counter", 0)
        return {"counter": current + 1, "last_node": "increment", "tags": [*state.get("tags", []), "inc"]}

    def _finalize(state: SubprocessState) -> dict[str, Any]:
        return {"last_node": "finalize"}

    graph_def.add_node("increment", _increment)
    graph_def.add_node("finalize", _finalize)
    graph_def.add_edge(START, "increment")
    graph_def.add_edge("increment", "finalize")
    graph_def.add_edge("finalize", END)

    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        await saver.setup()
        graph = graph_def.compile(checkpointer=saver)
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        snapshot = await graph.aget_state(config)
        if snapshot is None:
            return {"status": "error", "data": {"error_type": "ValueError", "detail": "snapshot is None"}}
        values = snapshot.values
        return {
            "status": "ok",
            "data": {
                "counter": values.get("counter"),
                "last_node": values.get("last_node"),
                "tags": values.get("tags"),
            },
        }


async def _do_interrupt(db_url: str, thread_id: str) -> dict[str, Any]:
    """进程 A：运行到 interrupt 点并退出。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    graph_def = StateGraph(SubprocessState)

    def _step_a(state: SubprocessState) -> dict[str, Any]:
        return {"counter": state.get("counter", 0) + 1, "last_node": "step_a"}

    def _step_b(state: SubprocessState) -> dict[str, Any]:
        val = interrupt({"need": "resume_value"})
        return {"counter": state.get("counter", 0) + val, "last_node": "step_b"}

    def _step_c(state: SubprocessState) -> dict[str, Any]:
        return {"last_node": "step_c"}

    graph_def.add_node("step_a", _step_a)
    graph_def.add_node("step_b", _step_b)
    graph_def.add_node("step_c", _step_c)
    graph_def.add_edge(START, "step_a")
    graph_def.add_edge("step_a", "step_b")
    graph_def.add_edge("step_b", "step_c")
    graph_def.add_edge("step_c", END)

    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        await saver.setup()
        graph = graph_def.compile(checkpointer=saver)
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        await graph.ainvoke({"counter": 0}, config=config)
        snap = await graph.aget_state(config)
        return {
            "status": "ok",
            "data": {
                "next": list(snap.next) if snap.next else [],
                "counter": snap.values.get("counter"),
            },
        }


async def _do_resume(db_url: str, thread_id: str, resume_value: int) -> dict[str, Any]:
    """进程 B：从 interrupt 点恢复并完成。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    graph_def = StateGraph(SubprocessState)

    def _step_a(state: SubprocessState) -> dict[str, Any]:
        return {"counter": state.get("counter", 0) + 1, "last_node": "step_a"}

    def _step_b(state: SubprocessState) -> dict[str, Any]:
        val = interrupt({"need": "resume_value"})
        return {"counter": state.get("counter", 0) + val, "last_node": "step_b"}

    def _step_c(state: SubprocessState) -> dict[str, Any]:
        return {"last_node": "step_c"}

    graph_def.add_node("step_a", _step_a)
    graph_def.add_node("step_b", _step_b)
    graph_def.add_node("step_c", _step_c)
    graph_def.add_edge(START, "step_a")
    graph_def.add_edge("step_a", "step_b")
    graph_def.add_edge("step_b", "step_c")
    graph_def.add_edge("step_c", END)

    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        await saver.setup()
        graph = graph_def.compile(checkpointer=saver)
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        result = await graph.ainvoke(Command(resume=resume_value), config=config)
        return {
            "status": "ok",
            "data": {"counter": result.get("counter"), "last_node": result.get("last_node")},
        }


async def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"status": "error", "data": {"error_type": "ValueError", "code": "CHECKPOINT_SUBPROCESS_FAILED"}}))
        return 1

    action = argv[1]

    try:
        db_url = _get_db_url()
        if action == "write":
            result = await _do_write(db_url, argv[2], argv[3])
        elif action == "read":
            result = await _do_read(db_url, argv[2])
        elif action == "interrupt":
            result = await _do_interrupt(db_url, argv[2])
        elif action == "resume":
            result = await _do_resume(db_url, argv[2], int(argv[3]))
        else:
            result = {"status": "error", "data": {"error_type": "ValueError", "code": "CHECKPOINT_SUBPROCESS_FAILED"}}
    except Exception as exc:
        result = {"status": "error", "data": _sanitize_error(exc)}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


def main() -> None:
    """脚本入口：设置 Windows 事件循环后执行异步 main。"""
    _setup_windows_event_loop()
    exit_code = asyncio.run(_main(sys.argv))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
