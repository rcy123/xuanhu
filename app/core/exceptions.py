"""项目统一异常体系。

业务异常与模型网关异常均在本模块定义，API 层通过全局异常处理转换为
标准 envelope 响应。所有异常消息与详情中不得泄露 API Key、prompt 原文、
完整模型输出或真实患者隐私数据。
"""

from __future__ import annotations

from typing import Any


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


class LangGraphPublicDisabledError(XuanhuError):
    """公共 API 的 LangGraph 会话创建尚未开放。"""

    code = "LANGGRAPH_PUBLIC_DISABLED"
    message = "LangGraph 公共会话创建尚未开放"
    status_code = 403
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


class IdempotencyConflictError(XuanhuError):
    """An idempotency key was already claimed by a different payload."""

    code = "IDEMPOTENCY_KEY_REUSED"
    message = "相同幂等键不能用于不同请求"
    status_code = 409
    retryable = False


class HttpCommandRecoveryRequiredError(XuanhuError):
    """A prior owner vanished after the command may have changed durable state."""

    code = "HTTP_COMMAND_RECOVERY_REQUIRED"
    message = "请求执行状态不明确，需要人工核对后恢复"
    status_code = 409
    retryable = False


class HttpCommandReplayError(XuanhuError):
    """Replay a previously persisted business error without executing again."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.code = str(payload.get("code") or "INTERNAL_ERROR")
        self.status_code = int(payload.get("status_code") or 500)
        self.extra_payload = dict(payload.get("extra_payload") or {})
        super().__init__(
            str(payload.get("message") or "请求执行失败"),
            detail=(str(payload["detail"]) if payload.get("detail") is not None else None),
            retryable=bool(payload.get("retryable", False)),
        )


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


class LangGraphRecoveryNotImplementedError(XuanhuError):
    """LangGraph 会话恢复尚未实现，禁止回退到 Legacy 恢复链路。"""

    code = "LANGGRAPH_RECOVERY_NOT_IMPLEMENTED"
    message = "LangGraph 会话恢复尚未实现"
    status_code = 501
    retryable = False


# ---------------------------------------------------------------------------
# P7-1 医师确认异常
# ---------------------------------------------------------------------------


class InvalidReviewActionError(XuanhuError):
    """无效的 review action。"""

    code = "INVALID_REVIEW_ACTION"
    message = "无效的医师确认动作"
    status_code = 400
    retryable = False


class FormulaOverrideRequiredError(XuanhuError):
    """修改处方时必须提供完整处方。"""

    code = "FORMULA_OVERRIDE_REQUIRED"
    message = "修改处方时必须提供完整处方"
    status_code = 400
    retryable = False


class SafetyReviewBlockedError(XuanhuError):
    """安全审核阻断。

    医师修改处方后二次安全审核未通过，需医师再次修改。
    """

    code = "SAFETY_REVIEW_BLOCKED"
    message = "安全审核阻断，处方未通过二次安全审核"
    status_code = 409
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        retryable: bool | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, detail=detail, retryable=retryable)
        self.issues: list[dict[str, Any]] = issues or []

    def to_payload(self) -> dict[str, Any]:
        """返回包含安全问题的 payload，供 API 层附加到错误响应。"""
        return {"issues": self.issues}


class SafetyAcceptRiskUnsupportedError(XuanhuError):
    """MVP 不支持接受风险继续。"""

    code = "SAFETY_ACCEPT_RISK_UNSUPPORTED"
    message = "MVP 不支持接受风险继续"
    status_code = 409
    retryable = False


# ---------------------------------------------------------------------------
# P8-6 阶段推进异常
# ---------------------------------------------------------------------------


class InsufficientInquiryError(XuanhuError):
    """问诊信息不充分，不能推进。

    接口设计文档 §4.3.1 定义：完备性不足时调用 advance 返回此错误。
    """

    code = "INSUFFICIENT_INQUIRY"
    message = "问诊信息不充分，不能推进"
    status_code = 400
    retryable = False


class PendingDoctorReviewError(XuanhuError):
    """有待确认处方，请先处理。

    接口设计文档 §4.3.1 定义：有 pending review 时调用 advance 返回此错误。
    """

    code = "PENDING_DOCTOR_REVIEW"
    message = "有待确认处方，请先处理"
    status_code = 409
    retryable = False


class AgentTriggerFailedError(XuanhuError):
    """Agent 触发失败（模型网关不可用 / Agent 执行异常）。

    用于 POST /messages 的段 B 失败时返回，医生消息已落库。
    """

    code = "AGENT_TRIGGER_FAILED"
    message = "Agent 触发失败，医生消息已保存"
    status_code = 503
    retryable = True

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        retryable: bool | None = None,
        agent_error_code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, retryable=retryable)
        self.agent_error_code = agent_error_code
