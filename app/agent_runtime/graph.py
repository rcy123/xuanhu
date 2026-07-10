"""MainGraph 构造。

对齐实施计划 §6.2 工作项 5：
创建最小 MainGraph：START -> command router -> 空占位节点 -> END/blocked/manual terminal。

图结构：

```text
START
  │
  ▼
command_router
  │ (conditional edge: route_after_router)
  ├─ message  ──► intake_placeholder     ──► END
  ├─ advance  ──► reasoning_placeholder  ──► END
  ├─ review   ──► review_placeholder     ──► END
  ├─ recover  ──► recovery_placeholder   ──► END
  ├─ empty    ──► blocked_terminal       ──► END
  └─ unknown  ──► manual_terminal        ──► END
```

L1-2 边界：
- 所有占位节点只写入 ``route`` 标记，不执行业务逻辑。
- 不接入 IntakeSubgraph、ReasoningSubgraph 等真实子图（留给 L3/L4）。
- 不接入 AsyncPostgresSaver（留给 L1-3），测试使用 InMemorySaver。
- 不实现 GraphRunner/stream（留给 L1-4）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.checkpoint import ConfigValidatingCheckpointer
from app.agent_runtime.commands import (
    NODE_BLOCKED_TERMINAL,
    NODE_COMMAND_ROUTER,
    NODE_INTAKE_PLACEHOLDER,
    NODE_MANUAL_TERMINAL,
    NODE_REASONING_PLACEHOLDER,
    NODE_RECOVERY_PLACEHOLDER,
    NODE_REVIEW_PLACEHOLDER,
)
from app.agent_runtime.routing import command_router, route_after_router
from app.agent_runtime.state import XuanhuGraphState

# 占位节点列表（不含 command_router 和终端节点）。
_PLACEHOLDER_NODES: tuple[str, ...] = (
    NODE_INTAKE_PLACEHOLDER,
    NODE_REASONING_PLACEHOLDER,
    NODE_REVIEW_PLACEHOLDER,
    NODE_RECOVERY_PLACEHOLDER,
    NODE_BLOCKED_TERMINAL,
    NODE_MANUAL_TERMINAL,
)


def _make_placeholder_node(
    node_name: str,
) -> Callable[[XuanhuGraphState], Awaitable[dict[str, Any]]]:
    """创建一个占位节点函数。

    占位节点只标记自身已执行（写入 ``route`` 字段），不执行任何业务逻辑。
    使用闭包绑定节点名，确保每个占位节点有可区分的标记。

    参数:
        node_name: 占位节点名。

    返回:
        async 节点函数。
    """

    async def _placeholder(state: XuanhuGraphState) -> dict[str, Any]:
        """L1-2 占位节点：标记路由目标，不执行业务逻辑。

        后续阶段（L3+）将替换为真实子图节点。
        """
        return {"route": node_name}

    _placeholder.__name__ = f"_placeholder_{node_name}"
    _placeholder.__qualname__ = f"_placeholder_{node_name}"
    return _placeholder


def build_main_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[XuanhuGraphState, None, XuanhuGraphState, XuanhuGraphState]:
    """构造最小 MainGraph。

    图结构：
        START -> command_router -> [conditional] -> placeholder -> END

    参数:
        checkpointer: LangGraph checkpointer 实例。支持 ``InMemorySaver``（单测）
            或 ``AsyncPostgresSaver``（L1-3 生产 checkpointer）。传入 ``None``
            表示不使用 checkpointer（适用于无状态单次 invoke 测试）。

    返回:
        编译后的 ``CompiledStateGraph``。
    """
    graph = StateGraph(XuanhuGraphState)

    # 注册 command_router 节点
    graph.add_node(NODE_COMMAND_ROUTER, command_router)

    # 注册占位节点（每个占位节点有独立的闭包函数）
    # LangGraph add_node 的类型签名使用 Never 作为输入类型参数，
    # 与闭包返回的 async 节点函数类型不匹配；这是 LangGraph 类型标注的已知限制。
    for node_name in _PLACEHOLDER_NODES:
        graph.add_node(node_name, _make_placeholder_node(node_name))  # type: ignore[arg-type]

    # START -> command_router
    graph.add_edge(START, NODE_COMMAND_ROUTER)

    # command_router -> conditional edge -> 占位节点
    graph.add_conditional_edges(
        NODE_COMMAND_ROUTER,
        route_after_router,
        {
            NODE_INTAKE_PLACEHOLDER: NODE_INTAKE_PLACEHOLDER,
            NODE_REASONING_PLACEHOLDER: NODE_REASONING_PLACEHOLDER,
            NODE_REVIEW_PLACEHOLDER: NODE_REVIEW_PLACEHOLDER,
            NODE_RECOVERY_PLACEHOLDER: NODE_RECOVERY_PLACEHOLDER,
            NODE_BLOCKED_TERMINAL: NODE_BLOCKED_TERMINAL,
            NODE_MANUAL_TERMINAL: NODE_MANUAL_TERMINAL,
        },
    )

    # 所有占位节点 -> END
    for node_name in _PLACEHOLDER_NODES:
        graph.add_edge(node_name, END)

    if checkpointer is not None and not isinstance(checkpointer, ConfigValidatingCheckpointer):
        checkpointer = ConfigValidatingCheckpointer(checkpointer)

    return graph.compile(checkpointer=checkpointer)
