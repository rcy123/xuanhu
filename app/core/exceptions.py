"""模型网关统一异常。

所有模型调用错误均归一化为本模块中的异常类型，
不得在异常消息或详情中泄露 API Key、prompt 原文或完整模型输出。
"""

from __future__ import annotations


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
