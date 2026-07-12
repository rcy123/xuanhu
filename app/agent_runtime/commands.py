"""Agent Runtime 命令类型与路由常量。

对齐实施计划 §3 目标图结构：
- ``message`` -> IntakeSubgraph（L1-2 占位为 ``intake_placeholder``）
- ``advance`` -> ReasoningSubgraph（当前节点为 ``reasoning_subgraph_v1``，保留旧 placeholder 常量兼容测试/历史 checkpoint）
- ``review`` -> ReviewRecordSubgraph（L1-2 占位为 ``review_placeholder``）
- ``recover`` -> Recovery Router（L1-2 占位为 ``recovery_placeholder``）
- unknown/invalid -> blocked/manual terminal

对齐兼容矩阵端点：
- POST /messages -> command=message
- POST /advance -> command=advance
- POST /review -> command=review
- POST /recover -> command=recover
"""

from __future__ import annotations

from enum import StrEnum


class XuanhuCommand(StrEnum):
    """悬壶 MainGraph 支持的命令类型。

    使用 ``StrEnum`` 以确保 JSON 序列化友好（``str(command)`` 产出值字符串）。
    """

    MESSAGE = "message"
    ADVANCE = "advance"
    REVIEW = "review"
    RECOVER = "recover"


# ---------------------------------------------------------------------------
# 占位节点名（L1-2 空骨架，不接入业务 Agent）
# ---------------------------------------------------------------------------

NODE_COMMAND_ROUTER: str = "command_router"
NODE_INTAKE_SUBGRAPH_V1: str = "intake_subgraph_v1"
NODE_INTAKE_PLACEHOLDER: str = NODE_INTAKE_SUBGRAPH_V1
NODE_REASONING_SUBGRAPH_V1: str = "reasoning_subgraph_v1"
NODE_REASONING_PLACEHOLDER: str = "reasoning_placeholder"
NODE_REVIEW_PLACEHOLDER: str = "review_placeholder"
NODE_RECOVERY_PLACEHOLDER: str = "recovery_placeholder"
NODE_BLOCKED_TERMINAL: str = "blocked_terminal"
NODE_MANUAL_TERMINAL: str = "manual_terminal"

# 终端节点集合：这些节点执行后直接进入 END。
TERMINAL_NODES: frozenset[str] = frozenset(
    {
        NODE_INTAKE_SUBGRAPH_V1,
        NODE_REASONING_SUBGRAPH_V1,
        NODE_REASONING_PLACEHOLDER,
        NODE_REVIEW_PLACEHOLDER,
        NODE_RECOVERY_PLACEHOLDER,
        NODE_BLOCKED_TERMINAL,
        NODE_MANUAL_TERMINAL,
    }
)

# 路由目标 -> 占位节点映射（正向路由）。
COMMAND_ROUTE_MAP: dict[str, str] = {
    XuanhuCommand.MESSAGE.value: NODE_INTAKE_SUBGRAPH_V1,
    XuanhuCommand.ADVANCE.value: NODE_REASONING_SUBGRAPH_V1,
    XuanhuCommand.REVIEW.value: NODE_REVIEW_PLACEHOLDER,
    XuanhuCommand.RECOVER.value: NODE_RECOVERY_PLACEHOLDER,
}

# 所有合法 command 值集合（用于路由判断）。
VALID_COMMANDS: frozenset[str] = frozenset(COMMAND_ROUTE_MAP.keys())
