"""Agent Runtime 配置常量与 thread_id 构造工具。

L1-2/L1-3 只定义图版本和 checkpoint config 构造/校验函数，不读取生产 ``Settings``
或环境变量，不改变 ``AGENT_RUNTIME_VERSION`` 默认值。

对齐 ADR-002 / 迁移边界 §2：
- ``thread_id`` 使用 ``{graph_version}:{session_id}`` 的稳定命名空间。
- 图版本变更时旧 checkpoint 不可用于恢复。
"""

from __future__ import annotations

from typing import Any

from app.agent_runtime.errors import CheckpointConfigMismatchError

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


def parse_thread_id(thread_id: str) -> tuple[str, str]:
    """解析版本化 ``thread_id``：``{graph_version}:{session_id}``。

    参数:
        thread_id: 形如 ``v1:session-abc`` 的版本化 thread_id。

    返回:
        ``(graph_version, session_id)`` 元组。

    Raises:
        ValueError: 如果 thread_id 格式无效（不含 ``:`` 分隔符）。
    """
    if ":" not in thread_id:
        raise ValueError("Invalid thread_id format: expected '{graph_version}:{session_id}'")
    parts = thread_id.split(":", 1)
    return parts[0], parts[1]


def validate_checkpoint_config(
    config: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """校验 checkpoint config 的 thread_id 与 Graph State 的 session_id/graph_version 一致。

    对齐验收标准 7：config/state session_id 或 graph_version 错配被确定性拒绝。
    错误消息脱敏，不含 DB URL 或密码。

    参数:
        config: LangGraph runnable config，包含 ``configurable.thread_id``。
        state: Graph State（或包含 ``session_id`` 和 ``graph_version`` 字段的 dict）。

    Raises:
        CheckpointConfigMismatchError: 如果 session_id 或 graph_version 不一致。
    """
    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id", "")

    if not thread_id or ":" not in thread_id:
        raise CheckpointConfigMismatchError(
            field="thread_id",
            config_value=thread_id,
            state_value="(missing or invalid)",
        )

    config_graph_version, config_session_id = parse_thread_id(thread_id)

    state_session_id = state.get("session_id", "")
    state_graph_version = state.get("graph_version", "")

    if state_session_id and config_session_id != state_session_id:
        raise CheckpointConfigMismatchError(
            field="session_id",
            config_value=config_session_id,
            state_value=state_session_id,
        )

    if state_graph_version and config_graph_version != state_graph_version:
        raise CheckpointConfigMismatchError(
            field="graph_version",
            config_value=config_graph_version,
            state_value=state_graph_version,
        )
