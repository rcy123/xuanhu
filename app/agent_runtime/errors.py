"""Agent Runtime 错误类型。

L1-2 只定义图执行相关的最小错误类型，不引入业务异常。
错误消息不得包含 prompt 原文、API key、完整模型输出或患者数据。
"""

from __future__ import annotations


class GraphStateError(Exception):
    """Graph State 校验或操作失败。

    用于 State 字段类型不合法、不可序列化值写入 State、或版本冲突等场景。
    消息只包含脱敏错误码和简短描述。
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CommandRoutingError(GraphStateError):
    """命令路由失败：未知或无效的 command。

    对齐验收标准 2：unknown/invalid command 必须有确定性失败或 blocked/manual 结果。
    """

    def __init__(self, command: str) -> None:
        super().__init__(
            f"Unknown or invalid command: {command!r}",
            code="COMMAND_ROUTING_UNKNOWN",
        )
        self.command = command
