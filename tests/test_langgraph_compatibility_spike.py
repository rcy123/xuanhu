"""L1-1 兼容性 Spike：LangGraph 在当前 Python / Pydantic 2.13 / FastAPI async / Windows 环境的运行验证。

本文件仅做兼容性 Spike，不实现 MainGraph、GraphState、Runner 或业务 Agent（边界见
`docs/01_agent部分优化/agent-runtime-migration-boundary.md` §5 L1）：
- 使用 LangGraph 公开 API 构造最小异步图；
- 验证异步 invoke；
- 验证 Pydantic 2.13 强类型状态模型可正常作为图节点返回值运行；
- 使用 ``InMemorySaver`` 验证最小 checkpoint 行为；
- 用测试内局部 FastAPI 应用验证 async 请求中调用最小图，不修改生产应用。

无真实模型、Redis 或患者数据依赖。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# 纯数据 state schema —— 等价强类型状态（TypedDict 是 LangGraph 原生推荐）。
# ---------------------------------------------------------------------------
from typing_extensions import TypedDict


class SpikeState(TypedDict, total=False):
    """最小可序列化执行游标状态（与 ADR-002 Graph State 边界一致：纯 JSON-safe 类型）。"""

    counter: int
    last_node: str
    notes: list[str]


class SpikeArtifact(BaseModel):
    """Pydantic 2.13 强类型节点输出模型（等价 RunArtifact 的最小骨架）。"""

    node: str = Field(description="产生该 artifact 的节点名")
    value: int = Field(ge=0, description="节点累加后的计数")
    marker: str = Field(description="确定性标记，用于断言")


# ---------------------------------------------------------------------------
# 节点：返回 Pydantic 模型实例，验证 Pydantic 2.13 与 LangGraph 的兼容性。
# ---------------------------------------------------------------------------


async def _async_increment(state: SpikeState) -> dict[str, Any]:
    """异步节点：累加 counter 并返回 Pydantic 模型实例作为更新片段。

    LangGraph 会调用 ``model_dump`` / 序列化把 Pydantic 实例合并进 state；
    这里验证 Pydantic 2.13 模型可在异步节点中作为返回值正常工作。
    """
    current = state.get("counter", 0)
    artifact = SpikeArtifact(node="increment", value=current + 1, marker="ok")
    # Pydantic BaseModel 实例作为 state delta —— 验证序列化友好。
    return {
        "counter": artifact.value,
        "last_node": artifact.node,
        "notes": [*state.get("notes", []), artifact.node],
    }


def _sync_finalize(state: SpikeState) -> dict[str, Any]:
    """同步节点：验证同步节点同样可在异步图中工作。"""
    return {"last_node": "finalize"}


def _build_spike_graph(checkpointer: InMemorySaver | None = None) -> CompiledStateGraph:
    """构造最小异步图：START -> increment -> finalize -> END。"""
    graph = StateGraph(SpikeState)
    graph.add_node("increment", _async_increment)
    graph.add_node("finalize", _sync_finalize)
    graph.add_edge(START, "increment")
    graph.add_edge("increment", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 测试：异步 invoke + Pydantic 2.13 兼容
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minimal_async_graph_invoke_without_checkpointer() -> None:
    """最小异步图可在当前 Python / Windows 环境运行并返回预期状态。"""
    graph = _build_spike_graph()
    result = await graph.ainvoke(
        {"counter": 0, "notes": []},
        config={"configurable": {"thread_id": "spike-noop-1"}},
    )
    assert result["counter"] == 1
    assert result["last_node"] == "finalize"
    assert result["notes"] == ["increment"]


@pytest.mark.asyncio
async def test_pydantic_v2_state_model_roundtrip_in_graph() -> None:
    """Pydantic 2.13 强类型模型作为节点输出可在图中往返且校验生效。"""
    artifact = SpikeArtifact(node="increment", value=5, marker="ok")
    assert artifact.model_dump() == {"node": "increment", "value": 5, "marker": "ok"}

    # 校验生效：value < 0 应被拒绝。
    with pytest.raises(ValidationError):
        SpikeArtifact(node="x", value=-1, marker="ok")

    graph = _build_spike_graph()
    result = await graph.ainvoke(
        {"counter": 4, "notes": []},
        config={"configurable": {"thread_id": "spike-pydantic-1"}},
    )
    assert result["counter"] == 5


# ---------------------------------------------------------------------------
# 测试：InMemorySaver checkpoint 最小行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inmemory_saver_checkpoint_persists_state() -> None:
    """``InMemorySaver`` 在 super-step 后持久化 state，可按 thread_id 读取快照。"""
    checkpointer = InMemorySaver()
    graph = _build_spike_graph(checkpointer=checkpointer)

    thread_id = "spike-checkpoint-1"
    config = {"configurable": {"thread_id": thread_id}}

    await graph.ainvoke({"counter": 0, "notes": []}, config=config)

    snapshot: StateSnapshot = await graph.aget_state(config)
    assert snapshot is not None
    # 图执行完成后 state 已更新到 finalize。
    assert snapshot.values.get("counter") == 1
    assert snapshot.values.get("last_node") == "finalize"


@pytest.mark.asyncio
async def test_inmemory_saver_thread_isolation() -> None:
    """不同 thread_id 的 checkpoint 互相隔离（对齐 ADR-002 namespace 边界）。"""
    checkpointer = InMemorySaver()
    graph = _build_spike_graph(checkpointer=checkpointer)

    cfg_a = {"configurable": {"thread_id": "spike-thread-a"}}
    cfg_b = {"configurable": {"thread_id": "spike-thread-b"}}

    await graph.ainvoke({"counter": 10, "notes": []}, config=cfg_a)
    await graph.ainvoke({"counter": 100, "notes": []}, config=cfg_b)

    snap_a = await graph.aget_state(cfg_a)
    snap_b = await graph.aget_state(cfg_b)
    assert snap_a.values.get("counter") == 11
    assert snap_b.values.get("counter") == 101


# ---------------------------------------------------------------------------
# 测试：测试内局部 FastAPI async 应用调用最小图
# ---------------------------------------------------------------------------


def _build_local_fastapi_app() -> FastAPI:
    """构造仅用于本 Spike 的临时 FastAPI 应用，不触碰生产 ``app.main``。"""
    app = FastAPI(title="xuanhu-l1-1-spike", version="0.0.0")
    graph = _build_spike_graph()

    @app.post("/spike/run")
    async def spike_run() -> dict[str, Any]:
        """在 async 请求上下文中调用最小图。"""
        thread_id = "spike-fastapi-1"
        result = await graph.ainvoke(
            {"counter": 0, "notes": []},
            config={"configurable": {"thread_id": thread_id}},
        )
        return {
            "counter": result["counter"],
            "last_node": result["last_node"],
            "notes": result["notes"],
        }

    return app


@pytest.mark.asyncio
async def test_local_fastapi_async_request_invokes_graph() -> None:
    """测试内 FastAPI async 请求可成功调用最小图，且不依赖生产应用。"""
    app = _build_local_fastapi_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/spike/run")
    assert response.status_code == 200
    body = response.json()
    assert body["counter"] == 1
    assert body["last_node"] == "finalize"
    assert body["notes"] == ["increment"]
