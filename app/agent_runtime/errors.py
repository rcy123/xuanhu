"""Agent Runtime 错误类型。

L1-2/L1-3/L1-4 只定义图执行、checkpoint 和 runner 相关的最小错误类型，
不引入业务异常。错误消息不得包含 prompt 原文、API key、完整模型输出、
DB URL、密码或患者数据。
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


class CheckpointError(GraphStateError):
    """Checkpoint 操作失败的基类。

    所有 checkpoint 相关错误（创建、健康检查、关闭、config 校验）均继承此类。
    消息不得包含 DB URL、密码、连接字符串或底层异常堆栈。
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


class CheckpointConfigMismatchError(CheckpointError):
    """checkpoint config 与 Graph State 的 session_id 或 graph_version 不一致。

    对齐验收标准 7：config/state session_id 或 graph_version 错配被确定性拒绝。
    """

    def __init__(self, *, field: str, config_value: str, state_value: str) -> None:
        super().__init__(
            f"Checkpoint config mismatch: field={field!r}, "
            f"config_value={config_value!r}, state_value={state_value!r}",
            code="CHECKPOINT_CONFIG_MISMATCH",
        )
        self.field = field
        self.config_value = config_value
        self.state_value = state_value


class CheckpointHealthCheckError(CheckpointError):
    """checkpoint 健康检查失败。

    错误消息已脱敏，不含 DB URL、密码或底层连接细节。
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(
            f"Checkpoint health check failed: {detail}",
            code="CHECKPOINT_HEALTH_CHECK_FAILED",
        )
        self.detail = detail


class GraphRunnerError(GraphStateError):
    """GraphRunner 执行失败的基类。

    所有 runner 相关错误（超时、取消、执行异常）均继承此类。
    消息不得包含完整 state、prompt、模型原始输出、密钥或患者身份。
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


class GraphRunnerTimeoutError(GraphRunnerError):
    """GraphRunner 总超时。

    在可配置的总超时时间内图未完成时抛出。
    ``asyncio.CancelledError`` 不被吞掉；超时通过 ``asyncio.timeout`` 实现，
    超时后内部任务被取消，此异常在 ``asyncio.TimeoutError`` 捕获后包装抛出。
    """

    def __init__(self, *, timeout_seconds: float) -> None:
        super().__init__(
            f"Graph runner timed out after {timeout_seconds}s",
            code="RUNNER_TIMEOUT",
        )
        self.timeout_seconds = timeout_seconds
