"""L1-1 兼容性 Spike：AsyncPostgresSaver 与 psycopg v3 异步连接池的 PostgreSQL 集成测试。

本文件仅做兼容性验证，不实现 MainGraph、GraphState、Runner 或业务 Agent（边界见
`docs/01_agent部分优化/agent-runtime-migration-boundary.md` §5 L1）：
- 使用 ``AsyncPostgresSaver`` 公开 API + 异步 psycopg 连接池；
- 执行官方要求的幂等初始化/setup；
- 使用随机且图版本命名空间化的 ``thread_id``，不得使用患者数据；
- 验证 checkpoint 可持久化，并可由重新创建的 checkpointer/连接池实例读取；
- 正确关闭连接池，不遗留后台任务；
- 不实现跨进程恢复（属于 L1-3）。

所有测试标记 ``pytest.mark.integration``（需要真实 PostgreSQL）。

Windows 注意：psycopg v3 异步连接在 Windows 上要求 ``SelectorEventLoop``（默认的
``ProactorEventLoop`` 会被 psycopg 拒绝，抛 ``InterfaceError``）。本文件通过
全局测试配置在 Windows 使用 ``WindowsSelectorEventLoopPolicy``，确保
pytest-asyncio 创建的事件循环兼容 psycopg。

注意：这是 L1-1 Spike 在 Windows 环境下的执行约束，不是生产运行时决策；生产
asyncpg / FastAPI / uvicorn 仍使用各自的默认事件循环。L1-2/L1-3 引入生产
checkpointer 时需评估与 uvicorn ProactorEventLoop 的共存方式。

无真实模型、Redis 或患者数据依赖。
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Windows 事件循环：psycopg v3 异步连接要求 SelectorEventLoop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 纯数据 state schema
# ---------------------------------------------------------------------------


class PgSpikeState(TypedDict, total=False):
    """最小可序列化执行游标状态（与 ADR-002 Graph State 边界一致）。"""

    counter: int
    last_node: str
    tags: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_thread_id() -> str:
    """生成随机 thread_id，避免跨测试 checkpoint 污染。"""
    return f"l1-1-spike-{uuid.uuid4().hex[:12]}"


def _build_simple_graph(checkpointer: AsyncPostgresSaver | InMemorySaver | None = None) -> CompiledStateGraph:
    """构造最小图：START -> increment -> finalize -> END。"""
    graph = StateGraph(PgSpikeState)

    async def _increment(state: PgSpikeState) -> dict:
        current = state.get("counter", 0)
        return {
            "counter": current + 1,
            "last_node": "increment",
            "tags": [*state.get("tags", []), "inc"],
        }

    def _finalize(state: PgSpikeState) -> dict:
        return {"last_node": "finalize"}

    graph.add_node("increment", _increment)
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "increment")
    graph.add_edge("increment", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _build_interrupt_graph(checkpointer: AsyncPostgresSaver | InMemorySaver | None = None) -> CompiledStateGraph:
    """构造带 interrupt 的图：START -> step_a -> step_b(interrupt) -> step_c -> END。"""
    graph = StateGraph(PgSpikeState)

    def _step_a(state: PgSpikeState) -> dict:
        return {"counter": state.get("counter", 0) + 1, "last_node": "step_a"}

    def _step_b(state: PgSpikeState) -> dict:
        from langgraph.types import interrupt

        val = interrupt({"need": "resume_value"})
        return {"counter": state.get("counter", 0) + val, "last_node": "step_b"}

    def _step_c(state: PgSpikeState) -> dict:
        return {"last_node": "step_c"}

    graph.add_node("step_a", _step_a)
    graph.add_node("step_b", _step_b)
    graph.add_node("step_c", _step_c)
    graph.add_edge(START, "step_a")
    graph.add_edge("step_a", "step_b")
    graph.add_edge("step_b", "step_c")
    graph.add_edge("step_c", END)
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 集成测试（需要真实 PostgreSQL）
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_setup_and_connect() -> None:
    """``AsyncPostgresSaver`` 可通过 ``from_conn_string`` 初始化，执行幂等 setup 并正常关闭。"""
    url = _pg_url()
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        # setup 幂等：重复调用不会报错
        await saver.setup()
    # 上下文管理器退出后连接池已关闭


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_write_and_read_checkpoint() -> None:
    """``AsyncPostgresSaver`` 可在图执行后持久化 checkpoint，并通过 aget_state 读取。"""
    url = _pg_url()
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_simple_graph(checkpointer=saver)
        thread_id = _random_thread_id()
        config = _config(thread_id)

        result = await graph.ainvoke({"counter": 0, "tags": []}, config=config)
        assert result["counter"] == 1
        assert result["last_node"] == "finalize"

        snapshot: StateSnapshot = await graph.aget_state(config)
        assert snapshot is not None
        assert snapshot.values.get("counter") == 1
        assert snapshot.values.get("last_node") == "finalize"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_reopen_and_reread() -> None:
    """重新创建的 ``AsyncPostgresSaver`` 实例可从 PG 读取之前的 checkpoint。"""
    url = _pg_url()
    thread_id = _random_thread_id()
    config = _config(thread_id)

    # 写入
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_simple_graph(checkpointer=saver)
        await graph.ainvoke({"counter": 42, "tags": ["first"]}, config=config)

    # 重新实例化读取
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_simple_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config)
        assert snapshot is not None
        assert snapshot.values.get("counter") == 43
        assert snapshot.values.get("tags") == ["first", "inc"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_thread_isolation() -> None:
    """不同 ``thread_id`` 的 checkpoint 互相隔离。"""
    url = _pg_url()
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_simple_graph(checkpointer=saver)

        cfg_a = _config(_random_thread_id())
        cfg_b = _config(_random_thread_id())

        await graph.ainvoke({"counter": 10, "tags": []}, config=cfg_a)
        await graph.ainvoke({"counter": 100, "tags": []}, config=cfg_b)

        snap_a = await graph.aget_state(cfg_a)
        snap_b = await graph.aget_state(cfg_b)
        assert snap_a.values.get("counter") == 11
        assert snap_b.values.get("counter") == 101


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_graph_version_thread_namespace_isolation() -> None:
    """不同图版本命名空间的 ``thread_id`` checkpoint 互相隔离。"""
    url = _pg_url()
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_simple_graph(checkpointer=saver)

        session_id = _random_thread_id()
        cfg_a = _config(session_id, graph_version="v1")
        cfg_b = _config(session_id, graph_version="v2")

        await graph.ainvoke({"counter": 1, "tags": []}, config=cfg_a)
        await graph.ainvoke({"counter": 100, "tags": []}, config=cfg_b)

        snap_v1 = await graph.aget_state(cfg_a)
        snap_v2 = await graph.aget_state(cfg_b)
        assert snap_v1.values.get("counter") == 2
        assert snap_v2.values.get("counter") == 101
        assert snap_v1.values.get("counter") != snap_v2.values.get("counter")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_interrupt_and_resume_from_fresh_instance() -> None:
    """``interrupt()`` 暂停后，可从全新 checkpointer 实例通过 ``Command(resume=...)`` 恢复执行。

    注意：跨进程恢复（L1-3）还需要验证进程重启后 checkpoint 仍可加载；
    本测试只验证同一 PG 上不同 checkpointer 实例的恢复能力。
    """
    url = _pg_url()
    thread_id = _random_thread_id()
    config = _config(thread_id)

    # 第一次执行到 interrupt
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_interrupt_graph(checkpointer=saver)
        await graph.ainvoke({"counter": 0}, config=config)
        snap = await graph.aget_state(config)
        assert "step_b" in snap.next, f"expected paused at step_b, got {snap.next}"
        assert snap.values.get("counter") == 1  # step_a incremented

    # 全新实例恢复
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = _build_interrupt_graph(checkpointer=saver)
        result = await graph.ainvoke(Command(resume=10), config=config)
        assert result["counter"] == 11  # step_a(1) + step_b(10)
        assert result["last_node"] == "step_c"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_postgres_saver_setup_is_idempotent() -> None:
    """``setup()`` 多次调用不报错，表结构已存在时幂等正常。"""
    url = _pg_url()
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        for _ in range(3):
            await saver.setup()
    # 不抛异常即通过


# ---------------------------------------------------------------------------
# 内部 utility
# ---------------------------------------------------------------------------


def _pg_url() -> str:
    """从显式、受保护的测试数据库配置获取 PostgreSQL URL。"""
    from tests._database_safety import require_destructive_test_database

    return require_destructive_test_database()


def _config(thread_id: str, *, graph_version: str = "spike") -> dict:
    """Build a root-graph config compatible with LangGraph 1.2.x.

    In this version, ``checkpoint_ns`` in ``graph.aget_state`` is interpreted
    as a subgraph namespace. L1-1 therefore verifies version isolation with a
    namespaced root ``thread_id``. Production checkpoint namespace design
    remains part of L1-2/L1-3.
    """
    return {"configurable": {"thread_id": f"{graph_version}:{thread_id}"}}
