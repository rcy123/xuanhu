"""命令路由逻辑。

对齐实施计划 §3 目标图结构：
START -> command_router -> [conditional edge] -> 占位节点 -> END/blocked/manual

路由规则：
- ``message``  -> ``intake_placeholder``
- ``advance``  -> ``reasoning_placeholder``
- ``review``   -> ``review_placeholder``
- ``recover``  -> ``recovery_placeholder``
- unknown/empty/invalid -> ``blocked_terminal`` 或 ``manual_terminal``

L1-2 使用确定性路由（不依赖模型判断），对齐 ADR-002 §2.2 确定性规则优先。
"""

from __future__ import annotations

from typing import Any

from app.agent_runtime.commands import (
    COMMAND_ROUTE_MAP,
    NODE_BLOCKED_TERMINAL,
    NODE_MANUAL_TERMINAL,
    VALID_COMMANDS,
)
from app.agent_runtime.state import XuanhuGraphState

# 路由函数返回的下一节点名。
RouteTarget = str


def resolve_command_route(command: str | None) -> RouteTarget:
    """根据 command 值确定性地解析路由目标。

    对齐验收标准 1/2：unknown/invalid command 有确定性失败或 blocked/manual 结果。

    路由策略：
    - command 为 None 或空字符串 -> ``blocked_terminal``（缺少必需输入）
    - command 在 VALID_COMMANDS 中 -> 对应占位节点
    - command 不在 VALID_COMMANDS 中 -> ``manual_terminal``（未知命令，需人工介入）

    参数:
        command: 命令字符串（message/advance/review/recover）或 None/空。

    返回:
        目标节点名。
    """
    if command is None or command == "":
        return NODE_BLOCKED_TERMINAL

    if command in VALID_COMMANDS:
        return COMMAND_ROUTE_MAP[command]

    # 未知命令路由到 manual_terminal（确定性失败，非随机行为）
    return NODE_MANUAL_TERMINAL


def command_router(state: XuanhuGraphState) -> dict[str, Any]:
    """MainGraph 的 command router 节点。

    读取 state 中的 ``command`` 字段，解析路由目标并写入 ``route`` 字段。
    不执行任何业务逻辑，不调用模型，不访问数据库。

    参数:
        state: 当前 Graph State。

    返回:
        State 增量 ``{"route": <目标节点名>}``。
    """
    command = state.get("command", "")
    route = resolve_command_route(command if command else None)
    return {"route": route}


def route_after_router(state: XuanhuGraphState) -> RouteTarget:
    """conditional edge 函数：从 command_router 后路由到目标节点。

    直接返回 state 中已由 ``command_router`` 设置的 ``route`` 字段。
    若 route 未设置（异常路径），回退到 ``blocked_terminal``。

    参数:
        state: 当前 Graph State。

    返回:
        下一节点名。
    """
    route = state.get("route", "")
    if route and route != "":
        return route
    return NODE_BLOCKED_TERMINAL
