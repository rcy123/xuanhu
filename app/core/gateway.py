"""模型网关统一客户端。

所有 LLM 和 Embedding 调用统一经过本模块的 ``ModelGatewayClient``，
不允许业务模块直接绕过网关访问模型服务。

配置读取 P1-2 Settings 中的 MODEL_GATEWAY_* 口径。
API Key 不得进入日志、异常详情、health 响应或测试快照。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.exceptions import (
    ChatStructuredParseError,
    EmbeddingDimensionMismatchError,
    EmbeddingUnavailableError,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
)

logger = logging.getLogger("xuanhu.gateway")


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """移除 headers 中的敏感信息用于日志记录。"""
    sanitized = {}
    for key, value in headers.items():
        if key.lower() == "authorization":
            sanitized[key] = "Bearer ***"
        else:
            sanitized[key] = value
    return sanitized


class ModelGatewayClient:
    """LLM / Embedding 统一网关客户端，封装内网 OpenAI 兼容接口。

    所有模型调用配置均来自 ``get_settings()``，使用 ``MODEL_GATEWAY_*`` 口径。
    """

    def __init__(self, settings: Any = None) -> None:
        if settings is None:
            settings = get_settings()
        self._settings = settings
        self._base_url = settings.model_gateway_base_url.rstrip("/")
        self._api_key = settings.model_gateway_api_key
        self._timeout = settings.model_gateway_timeout_seconds
        self._max_retries = settings.model_gateway_max_retries
        self._route_profile = settings.model_gateway_route_profile
        self._chat_model = settings.chat_model
        self._embedding_model = settings.embedding_model
        self._embedding_dim = settings.embedding_dim

    def _build_headers(self) -> dict[str, str]:
        """构建请求头，包含认证和路由信息。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Route-Profile": self._route_profile,
        }

    def _build_payload_overrides(
        self,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, str]:
        """构建请求额外字段（trace_id 等）。"""
        extras: dict[str, str] = {"trace_id": trace_id}
        if session_id is not None:
            extras["session_id"] = session_id
        if agent_name is not None:
            extras["agent_name"] = agent_name
        return extras

    async def _request_with_retry(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any],
        retryable_on_parse: bool = False,
    ) -> httpx.Response:
        """带重试的 HTTP 请求，处理超时、连接失败和非 2xx 响应。

        Args:
            method: HTTP 方法。
            path: 请求路径（相对于 base_url）。
            payload: 请求体。
            retryable_on_parse: 是否将解析失败也视为可重试（由调用方自行处理）。

        Returns:
            httpx.Response: 成功的 HTTP 响应。

        Raises:
            ModelGatewayTimeoutError: 请求超时。
            ModelGatewayUnavailableError: 连接失败或非 2xx 响应。
        """
        url = f"{self._base_url}{path}"
        headers = self._build_headers()
        last_exception: Exception | None = None
        max_attempts = 1 + self._max_retries

        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout, connect=10.0),
                ) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        json=payload,
                        headers=headers,
                    )

                if response.status_code >= 200 and response.status_code < 300:
                    return response

                # 非 2xx 响应
                status_code = response.status_code
                # 不在日志或异常中泄露完整响应体
                logger.warning(
                    "模型网关非 2xx 响应: status=%d, path=%s, attempt=%d/%d",
                    status_code,
                    path,
                    attempt + 1,
                    max_attempts,
                )
                last_exception = ModelGatewayUnavailableError(
                    f"模型网关返回非 2xx 状态码: {status_code}",
                    retryable=True,
                )
                # 4xx 错误通常不可重试（除 429 外）
                if 400 <= status_code < 500 and status_code != 429:
                    last_exception = ModelGatewayUnavailableError(
                        f"模型网关返回客户端错误: {status_code}",
                        retryable=False,
                    )
                    break

            except httpx.TimeoutException:
                logger.warning(
                    "模型网关请求超时: path=%s, attempt=%d/%d",
                    path,
                    attempt + 1,
                    max_attempts,
                )
                last_exception = ModelGatewayTimeoutError(
                    "模型网关请求超时",
                    retryable=True,
                )
            except httpx.ConnectError:
                logger.warning(
                    "模型网关连接失败: path=%s, attempt=%d/%d",
                    path,
                    attempt + 1,
                    max_attempts,
                )
                last_exception = ModelGatewayUnavailableError(
                    "模型网关连接失败",
                    retryable=True,
                )
            except httpx.HTTPError:
                logger.warning(
                    "模型网关 HTTP 错误: path=%s, attempt=%d/%d",
                    path,
                    attempt + 1,
                    max_attempts,
                )
                last_exception = ModelGatewayUnavailableError(
                    "模型网关请求失败",
                    retryable=True,
                )

        # 所有重试耗尽
        if last_exception is not None:
            raise last_exception
        raise ModelGatewayUnavailableError("模型网关请求失败（重试耗尽）")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> str:
        """普通对话补全。

        Args:
            messages: OpenAI 格式消息列表。
            model: 模型名称，默认使用 Settings.chat_model。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数。
            trace_id: 请求链路 ID（必填）。
            session_id: 会话 ID（可选）。
            agent_name: Agent 名称（可选）。

        Returns:
            str: 模型生成的文本内容。

        Raises:
            ModelGatewayUnavailableError: 网关不可用。
            ModelGatewayTimeoutError: 请求超时。
        """
        model_name = model or self._chat_model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self._build_payload_overrides(trace_id, session_id, agent_name),
        }

        logger.info(
            "chat 请求: model=%s, trace_id=%s, agent=%s",
            model_name,
            trace_id,
            agent_name,
        )

        response = await self._request_with_retry(
            method="POST",
            path="/chat/completions",
            payload=payload,
        )

        data: Any = response.json()
        try:
            content: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "chat 响应结构异常: model=%s, trace_id=%s, error=%s",
                model_name,
                trace_id,
                type(exc).__name__,
            )
            raise ModelGatewayUnavailableError(
                "模型网关返回结构异常的响应",
                retryable=False,
            ) from exc
        logger.info("chat 完成: model=%s, trace_id=%s", model_name, trace_id)
        return content

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        trace_id: str,
        session_id: str | None = None,
        agent_name: str | None = None,
    ) -> BaseModel:
        """结构化输出，通过 tools/function calling 强制 schema。

        解析失败时按 retry 策略重试，最终失败时抛出 ChatStructuredParseError，
        不泄露 prompt、API Key 或完整原始响应。

        Args:
            messages: OpenAI 格式消息列表。
            output_schema: 期望输出的 Pydantic BaseModel 类型。
            model: 模型名称，默认使用 Settings.chat_model。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数。
            trace_id: 请求链路 ID（必填）。
            session_id: 会话 ID（可选）。
            agent_name: Agent 名称（可选）。

        Returns:
            BaseModel: 解析成功的结构化输出实例。

        Raises:
            ChatStructuredParseError: 解析失败且重试耗尽。
            ModelGatewayUnavailableError: 网关不可用。
            ModelGatewayTimeoutError: 请求超时。
        """
        model_name = model or self._chat_model
        schema_dict = output_schema.model_json_schema()
        max_attempts = 1 + self._max_retries
        last_parse_error: str | None = None

        for attempt in range(max_attempts):
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "structured_output",
                            "description": "结构化输出",
                            "parameters": schema_dict,
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "structured_output"}},
                **self._build_payload_overrides(trace_id, session_id, agent_name),
            }

            logger.info(
                "chat_structured 请求: model=%s, schema=%s, trace_id=%s, attempt=%d/%d",
                model_name,
                output_schema.__name__,
                trace_id,
                attempt + 1,
                max_attempts,
            )

            try:
                response = await self._request_with_retry(
                    method="POST",
                    path="/chat/completions",
                    payload=payload,
                )
            except (ModelGatewayUnavailableError, ModelGatewayTimeoutError):
                raise  # 网关错误直接上抛，不重试解析

            data = response.json()

            # 尝试从 tool_calls 提取结构化输出
            try:
                tool_calls = data["choices"][0]["message"].get("tool_calls", [])
                if tool_calls:
                    args_str = tool_calls[0]["function"]["arguments"]
                    args_json = json.loads(args_str)
                    result = output_schema.model_validate(args_json)
                    logger.info(
                        "chat_structured 完成: model=%s, schema=%s, trace_id=%s",
                        model_name,
                        output_schema.__name__,
                        trace_id,
                    )
                    return result

                # 如果没有 tool_calls，尝试从 content 解析 JSON
                content = data["choices"][0]["message"]["content"]
                if content:
                    content_json = json.loads(content)
                    result = output_schema.model_validate(content_json)
                    logger.info(
                        "chat_structured 完成(content 解析): model=%s, schema=%s, trace_id=%s",
                        model_name,
                        output_schema.__name__,
                        trace_id,
                    )
                    return result

                last_parse_error = "模型返回内容为空"
            except (json.JSONDecodeError, KeyError, IndexError, ValidationError) as exc:
                last_parse_error = f"结构化输出解析失败: {type(exc).__name__}"
                logger.warning(
                    "chat_structured 解析失败: schema=%s, trace_id=%s, attempt=%d/%d, error=%s",
                    output_schema.__name__,
                    trace_id,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                )

        # 所有重试耗尽
        raise ChatStructuredParseError(
            last_parse_error or "结构化输出解析失败（重试耗尽）",
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]:
        """批量文本向量化。

        校验 embedding 维度必须等于 Settings.EMBEDDING_DIM，
        维度不一致时抛出 EmbeddingDimensionMismatchError。

        Args:
            texts: 待向量化的文本列表。
            model: 模型名称，默认使用 Settings.embedding_model。
            trace_id: 请求链路 ID（可选）。

        Returns:
            list[list[float]]: 向量化结果。

        Raises:
            EmbeddingUnavailableError: Embedding 服务不可用。
            EmbeddingDimensionMismatchError: 维度不一致。
            ModelGatewayTimeoutError: 请求超时。
        """
        model_name = model or self._embedding_model
        payload: dict[str, Any] = {
            "model": model_name,
            "input": texts,
            **self._build_payload_overrides(trace_id or "embed-no-trace"),
        }

        logger.info(
            "embed 请求: model=%s, count=%d, trace_id=%s",
            model_name,
            len(texts),
            trace_id,
        )

        try:
            response = await self._request_with_retry(
                method="POST",
                path="/embeddings",
                payload=payload,
            )
        except ModelGatewayUnavailableError as exc:
            raise EmbeddingUnavailableError(str(exc), retryable=exc.retryable) from exc
        except ModelGatewayTimeoutError as exc:
            raise EmbeddingUnavailableError(str(exc), retryable=exc.retryable) from exc

        data = response.json()
        try:
            embeddings: list[list[float]] = [item["embedding"] for item in data["data"]]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "embed 响应结构异常: model=%s, trace_id=%s, error=%s",
                model_name,
                trace_id,
                type(exc).__name__,
            )
            raise EmbeddingUnavailableError(
                "模型网关返回结构异常的 Embedding 响应",
                retryable=False,
            ) from exc

        # 校验维度
        for emb in embeddings:
            if len(emb) != self._embedding_dim:
                raise EmbeddingDimensionMismatchError(
                    expected=self._embedding_dim,
                    actual=len(emb),
                )

        logger.info(
            "embed 完成: model=%s, count=%d, trace_id=%s",
            model_name,
            len(embeddings),
            trace_id,
        )
        return embeddings

    async def health_check(self) -> dict[str, str]:
        """模型网关连通性检查。

        Returns:
            dict[str, str]: 各模型连通状态，如 {"chat": "ok", "embedding": "unavailable"}。
        """
        checks: dict[str, str] = {}
        health_timeout = self._settings.gateway_health_check_timeout_seconds

        # 检查 chat 连通性
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(health_timeout), connect=5.0),
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json={
                        "model": self._chat_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                    headers=self._build_headers(),
                )
                if response.status_code < 300:
                    checks["chat"] = "ok"
                else:
                    checks["chat"] = "unavailable"
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            checks["chat"] = "unavailable"

        # 检查 embedding 连通性
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(health_timeout), connect=5.0),
            ) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json={
                        "model": self._embedding_model,
                        "input": ["ping"],
                    },
                    headers=self._build_headers(),
                )
                if response.status_code < 300:
                    checks["embedding"] = "ok"
                else:
                    checks["embedding"] = "unavailable"
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            checks["embedding"] = "unavailable"

        return checks
