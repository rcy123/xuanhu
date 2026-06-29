"""项目统一异常体系。

业务异常与模型网关异常均在本模块定义，API 层通过全局异常处理转换为
标准 envelope 响应。所有异常消息与详情中不得泄露 API Key、prompt 原文、
完整模型输出或真实患者隐私数据。
"""

from __future__ import annotations


class XuanhuError(Exception):
    """业务异常基类。

    Attributes:
        code: 业务错误码，与接口设计文档 §6 对齐。
        message: 面向用户的简短中文描述。
        detail: 面向开发者的调试信息（不得包含敏感数据）。
        retryable: 客户端是否可以原样重试同一请求。
        status_code: 对应的 HTTP 状态码。
    """

    code: str = "INTERNAL_ERROR"
    message: str = "内部错误"
    status_code: int = 500
    retryable: bool = True

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable


class ValidationError(XuanhuError):
    """请求参数校验失败。"""

    code = "VALIDATION_ERROR"
    message = "请求参数校验失败"
    status_code = 422
    retryable = False


class SessionNotFoundError(XuanhuError):
    """会话不存在或已终止。"""

    code = "SESSION_NOT_FOUND"
    message = "会话不存在或已终止"
    status_code = 404
    retryable = False


class SessionTerminatedError(XuanhuError):
    """对已终止会话执行写操作。"""

    code = "SESSION_TERMINATED"
    message = "会话已终止"
    status_code = 400
    retryable = False


class InvalidStageTransitionError(XuanhuError):
    """当前阶段不支持此操作。"""

    code = "INVALID_STAGE_TRANSITION"
    message = "当前阶段不支持此操作"
    status_code = 409
    retryable = False


class SessionBusyError(XuanhuError):
    """会话正在处理其他请求，获取锁失败。"""

    code = "SESSION_BUSY"
    message = "会话正在处理其他请求，请稍后重试"
    status_code = 409
    retryable = True


class InvalidStateVersionError(XuanhuError):
    """客户端 State 版本落后于服务端。"""

    code = "INVALID_STATE_VERSION"
    message = "客户端状态版本落后，请刷新后重试"
    status_code = 409
    retryable = True


# ---------------------------------------------------------------------------
# 模型网关异常（保留自 P1-4）
# ---------------------------------------------------------------------------


class ModelGatewayError(Exception):
    """模型网关错误基类。

    所有网关相关异常的公共父类，用于统一捕获和日志记录。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelGatewayUnavailableError(ModelGatewayError):
    """模型网关不可用（连接失败、超时、非 2xx 响应）。

    不包含 API Key、请求体或完整响应内容。
    """

    def __init__(
        self,
        message: str = "模型网关不可用",
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, retryable=retryable)


class ModelGatewayTimeoutError(ModelGatewayError):
    """模型网关请求超时。"""

    def __init__(
        self,
        message: str = "模型网关请求超时",
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, retryable=retryable)


class EmbeddingUnavailableError(ModelGatewayError):
    """Embedding 服务不可用。"""

    def __init__(
        self,
        message: str = "Embedding 服务不可用",
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, retryable=retryable)


class EmbeddingDimensionMismatchError(ModelGatewayError):
    """Embedding 维度与配置不一致。"""

    def __init__(
        self,
        expected: int,
        actual: int,
    ) -> None:
        super().__init__(
            f"Embedding 维度不一致: 期望 {expected}, 实际 {actual}",
            retryable=False,
        )
        self.expected = expected
        self.actual = actual


class ChatStructuredParseError(ModelGatewayError):
    """结构化输出解析失败（重试耗尽后抛出）。

    不泄露 prompt、API Key 或完整原始响应。
    """

    def __init__(
        self,
        message: str = "结构化输出解析失败",
    ) -> None:
        super().__init__(message, retryable=False)


# ---------------------------------------------------------------------------
# P3-4 恢复与健康检查异常
# ---------------------------------------------------------------------------


class RecoveryNotNeededError(XuanhuError):
    """会话状态正常，无需恢复。"""

    code = "RECOVERY_NOT_NEEDED"
    message = "会话状态正常，无需恢复"
    status_code = 400
    retryable = False


class StateRecoveryRequiredError(XuanhuError):
    """无法自动恢复，需人工处理。"""

    code = "STATE_RECOVERY_REQUIRED"
    message = "无法自动恢复，需人工处理"
    status_code = 409
    retryable = False
