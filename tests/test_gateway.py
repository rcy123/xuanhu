"""模型网关客户端测试。

使用 respx mock 覆盖成功与失败路径，不依赖真实外部服务。
验证 API key 不泄露、错误归一化、retry 行为、结构化输出解析等。
包括真实 httpx.TimeoutException、httpx.ConnectError 路径测试。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import Response
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ChatStructuredParseError,
    EmbeddingDimensionMismatchError,
    EmbeddingUnavailableError,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
)
from app.core.gateway import ModelGatewayClient
from app.schemas.agent import InquiryAgentOutput
from app.schemas.intake import IntakeExtractionOutput

# ---------------------------------------------------------------------------
# 测试用 Schema
# ---------------------------------------------------------------------------


class SampleOutput(BaseModel):
    """测试用结构化输出 Schema。"""

    name: str
    value: int = Field(ge=0, le=100)


def _intake_output_payload(patient_safety_delta: str) -> dict[str, Any]:
    return {
        "decision": "abstained",
        "observations": [],
        "patient_safety_delta": patient_safety_delta,
        "red_flag_candidates": [],
        "ambiguities": [],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """创建测试用 Settings 实例。"""
    monkeypatch.setenv("DB_URL", "postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu")
    monkeypatch.setenv("REDIS_URL", "redis://:xuanhu_dev@localhost:6379/0")
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "http://mock-gateway:8080/v1")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "sk-test-key-12345")
    monkeypatch.setenv("MODEL_GATEWAY_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MODEL_GATEWAY_MAX_RETRIES", "2")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# chat 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_success(mock_settings: Settings) -> None:
    """chat 成功调用测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "Hello, this is a test response."}}
                    ]
                },
            )
        )

        result = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            trace_id="test-trace-001",
        )

    assert result == "Hello, this is a test response."


@pytest.mark.asyncio
async def test_chat_normalizes_context_role_without_mutating_messages(
    mock_settings: Settings,
) -> None:
    """Internal context becomes bounded untrusted user data at the HTTP boundary."""
    client = ModelGatewayClient(mock_settings)
    call_payloads: list[dict[str, Any]] = []
    messages = [
        {"role": "system", "content": "System instruction"},
        {"role": "developer", "content": "Developer instruction"},
        {"role": "context", "content": "Untrusted clinical context"},
        {"role": "user", "content": "User question"},
        {"role": "assistant", "content": "Earlier response"},
        {"role": "tool", "tool_call_id": "call-1", "content": "Tool result"},
    ]
    original_messages = json.loads(json.dumps(messages))

    def side_effect(request: httpx.Request) -> Response:
        call_payloads.append(json.loads(request.content.decode()))
        return Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(side_effect=side_effect)
        result = await client.chat(messages=messages, trace_id="test-context-normalization")

    assert result == "ok"
    assert messages == original_messages
    outbound_messages = call_payloads[0]["messages"]
    assert [message["role"] for message in outbound_messages] == [
        "system",
        "developer",
        "user",
        "user",
        "assistant",
        "tool",
    ]
    assert "<untrusted_context_data>" in outbound_messages[2]["content"]
    assert "Untrusted clinical context" in outbound_messages[2]["content"]
    assert "</untrusted_context_data>" in outbound_messages[2]["content"]
    for index in (0, 1, 3, 4, 5):
        assert outbound_messages[index] == messages[index]


@pytest.mark.asyncio
async def test_chat_non_2xx_error(mock_settings: Settings) -> None:
    """chat 非 2xx 响应错误归一化测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(500, json={"error": "internal error"})
        )

        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-004",
            )

        assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_chat_response_structure_error(mock_settings: Settings) -> None:
    """chat 响应结构异常归一化测试 — 网关返回 200 但缺少 choices 字段。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        # 返回 200 但结构异常（缺少 choices）
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"error": "unexpected structure"},
            )
        )

        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-structure-001",
            )

        assert exc_info.value.retryable is False
        assert "结构异常" in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_response_empty_choices(mock_settings: Settings) -> None:
    """chat 响应 choices 为空数组归一化测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": []},
            )
        )

        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-structure-002",
            )

        assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# chat_structured 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_structured_success(mock_settings: Settings) -> None:
    """chat_structured 成功解析测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {"name": "test", "value": 42}
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        )

        result = await client.chat_structured(
            messages=[
                {"role": "context", "content": "Structured context"},
                {"role": "user", "content": "Generate"},
            ],
            output_schema=SampleOutput,
            trace_id="test-trace-005",
        )

    assert isinstance(result, SampleOutput)
    assert result.name == "test"
    assert result.value == 42
    call_payload = json.loads(route.calls[0].request.content.decode())
    assert call_payload["messages"][0]["role"] == "user"
    assert "<untrusted_context_data>" in call_payload["messages"][0]["content"]
    assert all(message["role"] != "context" for message in call_payload["messages"])


@pytest.mark.asyncio
async def test_chat_structured_observed_uses_response_model_and_usage(mock_settings: Settings) -> None:
    """Observed calls expose only response-side serving metadata."""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "model": "served-model-revision-42",
                    "usage": {
                        "prompt_tokens": 31,
                        "completion_tokens": 7,
                        "total_tokens": 38,
                    },
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps({"name": "observed", "value": 42})
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                },
            )
        )

        result = await client.chat_structured_observed(
            messages=[{"role": "user", "content": "Generate"}],
            output_schema=SampleOutput,
            model="requested-alias",
            trace_id="test-trace-observed",
        )

    assert result.output == SampleOutput(name="observed", value=42)
    assert result.model_actual == "served-model-revision-42"
    assert result.model_actual != "requested-alias"
    assert result.usage.prompt_tokens == 31
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 38


@pytest.mark.asyncio
async def test_chat_structured_parse_from_content(mock_settings: Settings) -> None:
    """chat_structured 从 content 解析 JSON 测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"name": "from-content", "value": 50})
                            }
                        }
                    ]
                },
            )
        )

        result = await client.chat_structured(
            messages=[{"role": "user", "content": "Generate"}],
            output_schema=SampleOutput,
            trace_id="test-trace-006",
        )

    assert result.name == "from-content"
    assert result.value == 50


@pytest.mark.asyncio
async def test_chat_structured_repairs_stringified_intake_safety_delta(
    mock_settings: Settings,
) -> None:
    client = ModelGatewayClient(mock_settings)
    arguments = _intake_output_payload(json.dumps({}))

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"arguments": json.dumps(arguments)}}
                                ]
                            }
                        }
                    ]
                },
            )
        )

        result = await client.chat_structured(
            messages=[{"role": "user", "content": "Extract intake"}],
            output_schema=IntakeExtractionOutput,
            trace_id="test-intake-stringified-safety-delta",
            max_requests=1,
        )

    assert result == IntakeExtractionOutput(decision="abstained")
    assert len(route.calls) == 1


@pytest.mark.parametrize(
    "encoded_delta",
    [
        pytest.param("not valid json", id="invalid-json"),
        pytest.param("[]", id="non-object-json"),
    ],
)
@pytest.mark.asyncio
async def test_chat_structured_rejects_invalid_stringified_intake_safety_delta(
    mock_settings: Settings,
    encoded_delta: str,
) -> None:
    client = ModelGatewayClient(mock_settings)
    arguments = _intake_output_payload(encoded_delta)

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"arguments": json.dumps(arguments)}}
                                ]
                            }
                        }
                    ]
                },
            )
        )

        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Extract intake"}],
                output_schema=IntakeExtractionOutput,
                trace_id="test-intake-invalid-stringified-safety-delta",
                max_requests=1,
            )

    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_chat_structured_does_not_relax_intake_safety_delta_schema(
    mock_settings: Settings,
) -> None:
    client = ModelGatewayClient(mock_settings)
    arguments = _intake_output_payload(json.dumps({"unexpected": True}))

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"arguments": json.dumps(arguments)}}
                                ]
                            }
                        }
                    ]
                },
            )
        )

        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Extract intake"}],
                output_schema=IntakeExtractionOutput,
                trace_id="test-intake-strict-stringified-safety-delta",
                max_requests=1,
            )

    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_chat_structured_repairs_stringified_intake_safety_delta_from_content(
    mock_settings: Settings,
) -> None:
    client = ModelGatewayClient(mock_settings)
    content = json.dumps(_intake_output_payload(json.dumps({})))

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
            )
        )

        result = await client.chat_structured(
            messages=[{"role": "user", "content": "Extract intake"}],
            output_schema=IntakeExtractionOutput,
            trace_id="test-intake-content-stringified-safety-delta",
            max_requests=1,
        )

    assert result == IntakeExtractionOutput(decision="abstained")
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_chat_structured_json_mode_fallback(mock_settings: Settings) -> None:
    """tool-call arguments malformed 时自动退回 JSON mode。"""
    client = ModelGatewayClient(mock_settings)
    call_payloads: list[dict[str, Any]] = []

    def side_effect(request: httpx.Request) -> Response:
        call_payloads.append(json.loads(request.content.decode()))
        if len(call_payloads) == 1:
            return Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"arguments": "not valid json"}}
                                ]
                            }
                        }
                    ]
                },
            )
        return Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"name": "fallback", "value": 64})}}
                ]
            },
        )

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )

        result = await client.chat_structured(
            messages=[
                {"role": "context", "content": "Fallback context"},
                {"role": "user", "content": "Generate JSON"},
            ],
            output_schema=SampleOutput,
            trace_id="test-trace-json-fallback",
        )

    assert result.name == "fallback"
    assert result.value == 64
    assert call_payloads[1]["response_format"] == {"type": "json_object"}
    assert "tools" not in call_payloads[1]
    for payload in call_payloads:
        assert all(message["role"] != "context" for message in payload["messages"])
        assert payload["messages"][0]["role"] == "user"
        assert "<untrusted_context_data>" in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_chat_structured_json_fallback_repairs_inquiry_output(mock_settings: Settings) -> None:
    """InquiryAgent fallback 对多问句和 dotted dimension 做窄修复。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        responses = [
            Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"arguments": "not valid json"}}
                                ]
                            }
                        }
                    ]
                },
            ),
            Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "chief_complaint": None,
                                        "present_illness": "头痛三天",
                                        "past_history": None,
                                        "personal_family_history": None,
                                        "ten_questions_delta": None,
                                        "four_diagnosis_delta": None,
                                        "next_question": "请问头痛是持续性的吗？有没有恶心呕吐？",
                                        "asked_dimension": "ten_questions_delta.head_body",
                                        "safety_info_requested": [],
                                        "safety_notes": None,
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            ),
        ]
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=responses
        )

        result = await client.chat_structured(
            messages=[{"role": "user", "content": "Generate JSON"}],
            output_schema=InquiryAgentOutput,
            trace_id="test-trace-inquiry-repair",
        )

    assert result.next_question == "请问头痛是持续性的吗？"
    assert result.asked_dimension == "ten_questions"


@pytest.mark.asyncio
async def test_chat_structured_parse_failure(mock_settings: Settings) -> None:
    """chat_structured 解析失败测试（重试耗尽后抛出）。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        # 返回无效 JSON
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "not valid json"}}
                    ]
                },
            )
        )

        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="test-trace-007",
            )


@pytest.mark.asyncio
async def test_chat_structured_validation_failure(mock_settings: Settings) -> None:
    """chat_structured Pydantic 校验失败测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        # 返回不符合 schema 的数据（value 超出范围）
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {"name": "test", "value": 999}  # 超出 le=100
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        )

        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="test-trace-008",
            )


# ---------------------------------------------------------------------------
# embed 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_success(mock_settings: Settings) -> None:
    """embed 成功调用测试。"""
    client = ModelGatewayClient(mock_settings)

    # 768 维向量
    fake_embedding = [0.1] * 768

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"embedding": fake_embedding},
                        {"embedding": fake_embedding},
                    ]
                },
            )
        )

        result = await client.embed(
            texts=["hello", "world"],
            trace_id="test-trace-009",
        )

    assert len(result) == 2
    assert len(result[0]) == 768
    assert len(result[1]) == 768


@pytest.mark.asyncio
async def test_embed_dimension_mismatch(mock_settings: Settings) -> None:
    """embed 维度不一致测试。"""
    client = ModelGatewayClient(mock_settings)

    # 返回 512 维向量（与配置 768 不一致）
    wrong_embedding = [0.1] * 512

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"embedding": wrong_embedding},
                    ]
                },
            )
        )

        with pytest.raises(EmbeddingDimensionMismatchError) as exc_info:
            await client.embed(
                texts=["hello"],
                trace_id="test-trace-010",
            )

        assert exc_info.value.expected == 768
        assert exc_info.value.actual == 512


@pytest.mark.asyncio
async def test_embed_unavailable(mock_settings: Settings) -> None:
    """embed 服务不可用测试（非 2xx 响应）。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(503)
        )

        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(
                texts=["hello"],
                trace_id="test-trace-011",
            )


@pytest.mark.asyncio
async def test_embed_response_structure_error(mock_settings: Settings) -> None:
    """embed 响应结构异常归一化测试 — 网关返回 200 但缺少 data 字段。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        # 返回 200 但结构异常（缺少 data 字段）
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={"error": "unexpected structure"},
            )
        )

        with pytest.raises(EmbeddingUnavailableError) as exc_info:
            await client.embed(
                texts=["hello"],
                trace_id="test-trace-structure-003",
            )

        assert exc_info.value.retryable is False
        assert "结构异常" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 真实 httpx 异常路径测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_real_timeout_exception(mock_settings: Settings) -> None:
    """chat 真实 httpx.TimeoutException 归一化测试。

    使用 respx side_effect 触发真实的 httpx.TimeoutException，
    验证代码将其归一化为 ModelGatewayTimeoutError。
    """
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        # respx 支持 side_effect 抛出真实 httpx 异常
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=httpx.TimeoutException("request timed out")
        )

        with pytest.raises(ModelGatewayTimeoutError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-real-timeout",
            )


@pytest.mark.asyncio
async def test_chat_real_connect_error(mock_settings: Settings) -> None:
    """chat 真实 httpx.ConnectError 归一化测试。

    使用 respx side_effect 触发真实的 httpx.ConnectError，
    验证代码将其归一化为 ModelGatewayUnavailableError。
    """
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with pytest.raises(ModelGatewayUnavailableError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-real-connect",
            )


@pytest.mark.asyncio
async def test_embed_real_timeout_exception(mock_settings: Settings) -> None:
    """embed 真实 httpx.TimeoutException 归一化测试。

    验证真实超时异常被归一化为 EmbeddingUnavailableError。
    """
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            side_effect=httpx.TimeoutException("request timed out")
        )

        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(
                texts=["hello"],
                trace_id="test-trace-real-embed-timeout",
            )


@pytest.mark.asyncio
async def test_embed_real_connect_error(mock_settings: Settings) -> None:
    """embed 真实 httpx.ConnectError 归一化测试。

    验证真实连接异常被归一化为 EmbeddingUnavailableError。
    """
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(
                texts=["hello"],
                trace_id="test-trace-real-embed-connect",
            )


# ---------------------------------------------------------------------------
# health_check 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_all_ok(mock_settings: Settings) -> None:
    """health_check 全部正常测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        checks = await client.health_check()

    assert checks["chat"] == "ok"
    assert checks["embedding"] == "ok"


@pytest.mark.asyncio
async def test_health_check_chat_unavailable(mock_settings: Settings) -> None:
    """health_check chat 不可用测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(503)
        )
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 768}]},
            )
        )

        checks = await client.health_check()

    assert checks["chat"] == "unavailable"
    assert checks["embedding"] == "ok"


@pytest.mark.asyncio
async def test_health_check_embedding_unavailable(mock_settings: Settings) -> None:
    """health_check embedding 不可用测试。"""
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(503)
        )

        checks = await client.health_check()

    assert checks["chat"] == "ok"
    assert checks["embedding"] == "unavailable"


# ---------------------------------------------------------------------------
# API key 不泄露测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_not_in_exception(mock_settings: Settings) -> None:
    """API key 不出现在异常消息中。"""
    client = ModelGatewayClient(mock_settings)
    api_key = mock_settings.model_gateway_api_key

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-012",
            )

    # 异常消息不包含 API key
    assert api_key not in str(exc_info.value)
    assert api_key not in repr(exc_info.value)


def test_api_key_not_in_safe_dump(mock_settings: Settings) -> None:
    """API key 不出现在 safe_dump 输出中。"""
    safe_config = mock_settings.safe_dump()
    api_key = mock_settings.model_gateway_api_key

    assert safe_config["model_gateway_api_key"] == "***"
    assert api_key not in str(safe_config)


# ---------------------------------------------------------------------------
# retry 行为测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_retry_on_5xx(mock_settings: Settings) -> None:
    """chat 在 5xx 错误时重试测试。"""
    client = ModelGatewayClient(mock_settings)

    call_count = 0

    def side_effect(request: Any) -> Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:  # 前 2 次失败
            return Response(503)
        return Response(  # 第 3 次成功
            200,
            json={"choices": [{"message": {"content": "success"}}]},
        )

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )

        result = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            trace_id="test-trace-013",
        )

    assert result == "success"
    assert call_count == 3  # 初始 + 2 次重试


@pytest.mark.asyncio
async def test_chat_no_retry_on_4xx(mock_settings: Settings) -> None:
    """chat 在 4xx 错误时不重试测试（除 429 外）。"""
    client = ModelGatewayClient(mock_settings)

    call_count = 0

    def side_effect(request: Any) -> Response:
        nonlocal call_count
        call_count += 1
        return Response(400, json={"error": "bad request"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )

        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="test-trace-014",
            )

    assert call_count == 1  # 不重试
    assert exc_info.value.retryable is False
