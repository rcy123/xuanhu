"""Agent Runtime 配置常量与 thread_id 构造工具。

L1-2 只定义图版本和 checkpoint config 构造函数，不读取生产 ``Settings``
或环境变量，不改变 ``AGENT_RUNTIME_VERSION`` 默认值。

对齐 ADR-002 / 迁移边界 §2：
- ``thread_id`` 使用 ``{graph_version}:{session_id}`` 的稳定命名空间。
- 图版本变更时旧 checkpoint 不可用于恢复。
"""

from __future__ import annotations

from typing import Any

# 当前 MainGraph 主版本。图结构发生不兼容变更时递增。
# 对齐实施计划 §6.2 ``graph_version`` 字段。
GRAPH_VERSION_V1: str = "v1"

# 默认使用的图版本（L1-2 骨架只有 v1）。
DEFAULT_GRAPH_VERSION: str = GRAPH_VERSION_V1


def make_thread_id(session_id: str, graph_version: str = DEFAULT_GRAPH_VERSION) -> str:
    """构造版本化 ``thread_id``：``{graph_version}:{session_id}``。

    对齐迁移边界 §2.2：每个图版本、每个会话一个 thread，旧图版本 checkpoint
    不得被新图版本恢复。

    参数:
        session_id: 会话标识（对应 Domain State 中的 session UUID 字符串）。
        graph_version: 图主版本，默认 ``v1``。

    返回:
        形如 ``v1:session-abc`` 的版本化 thread_id。
    """
    return f"{graph_version}:{session_id}"


def make_run_config(
    session_id: str,
    *,
    graph_version: str = DEFAULT_GRAPH_VERSION,
) -> dict[str, Any]:
    """构造 LangGraph runnable config。

    对齐 L1-1 Spike 验证的模式：root graph 的 ``aget_state`` 会把
    ``checkpoint_ns`` 解释为 subgraph namespace，因此使用版本化 root
    ``thread_id`` 而非 ``checkpoint_ns`` 来隔离图版本。

    参数:
        session_id: 会话标识。
        graph_version: 图主版本。

    返回:
        ``{"configurable": {"thread_id": "v1:session-abc"}}`` 格式的 config dict。
    """
    return {"configurable": {"thread_id": make_thread_id(session_id, graph_version)}}
