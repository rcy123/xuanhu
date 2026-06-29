"""Agent 基础设施异常。"""

from __future__ import annotations


class AgentRunError(Exception):
    """Agent 执行失败。

    消息与 detail 不得包含 prompt 原文、API key 或完整模型响应。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        detail: str | None = None,
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.detail = detail
        self.retry_count = retry_count


class PromptManifestError(AgentRunError):
    """Prompt manifest 或模板加载失败。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            "Prompt 配置不可用",
            code="PROMPT_MANIFEST_ERROR",
            retryable=False,
            detail=detail,
        )
