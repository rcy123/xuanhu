"""阶段 4 运行态安全测试：模型白名单（T4.6 / M5）。

- 白名单配置后：非白名单模型名在发出任何请求前即被拒绝
  （``ModelNotAllowedError``），覆盖 chat / chat_structured / embed。
- 配置自相矛盾（默认模型不在白名单内）→ 构造期 ValueError fail-fast。
- 空白名单 = 不过滤（no-op），不影响既有调用方。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import BaseModel

from app.core.exceptions import ModelNotAllowedError
from app.core.gateway import ModelGatewayClient

pytestmark = [pytest.mark.asyncio(loop_scope="module")]


class _Output(BaseModel):
    value: str


def _settings(*, whitelist: list[str] | None = None, chat_model: str = "chat-v1", embedding_model: str = "embed-v1"):
    values = dict(
        model_gateway_base_url="http://127.0.0.1:1/v1",  # 保证端口必然拒绝连接，白名单先行
        model_gateway_api_key="sk-test",
        model_gateway_timeout_seconds=5,
        model_gateway_max_retries=0,
        model_gateway_route_profile="test",
        chat_model=chat_model,
        embedding_model=embedding_model,
        embedding_dim=8,
    )
    if whitelist is not None:
        values["model_whitelist"] = whitelist
    return SimpleNamespace(**values)


@pytest_asyncio.fixture(loop_scope="module")
async def gateway() -> ModelGatewayClient:
    client = ModelGatewayClient(_settings(whitelist=["chat-v1", "embed-v1"]))
    try:
        yield client
    finally:
        await client.aclose()


async def test_allowlist_rejects_unknown_model(gateway: ModelGatewayClient) -> None:
    with pytest.raises(ModelNotAllowedError) as exc_info:
        gateway._assert_model_allowed("evil-model")
    assert "evil-model" in str(exc_info.value)
    assert exc_info.value.retryable is False


async def test_allowlist_accepts_known_model(gateway: ModelGatewayClient) -> None:
    gateway._assert_model_allowed("chat-v1")
    gateway._assert_model_allowed("embed-v1")


async def test_chat_rejects_before_network(gateway: ModelGatewayClient) -> None:
    with pytest.raises(ModelNotAllowedError):
        await gateway.chat(
            [{"role": "user", "content": "hi"}],
            model="evil-model",
            trace_id="whitelist-test",
        )


async def test_chat_structured_rejects_before_network(gateway: ModelGatewayClient) -> None:
    with pytest.raises(ModelNotAllowedError):
        await gateway.chat_structured(
            [{"role": "user", "content": "hi"}],
            _Output,
            model="evil-model",
            trace_id="whitelist-test",
        )


async def test_embed_rejects_before_network(gateway: ModelGatewayClient) -> None:
    with pytest.raises(ModelNotAllowedError):
        await gateway.embed(["text"], model="evil-model", trace_id="whitelist-test")


async def test_config_mismatch_fails_fast() -> None:
    """默认 chat_model 不在白名单内 → 构造期即 ValueError。"""
    with pytest.raises(ValueError, match="不在 MODEL_WHITELIST"):
        ModelGatewayClient(_settings(whitelist=["other-model"]))


async def test_empty_whitelist_is_noop() -> None:
    """未配置白名单 = 全部放行（不抛 ModelNotAllowedError，走真实网关失败）。"""
    client = ModelGatewayClient(_settings())
    try:
        with pytest.raises(Exception) as exc_info:
            await client.chat(
                [{"role": "user", "content": "hi"}],
                model="any-model",
                trace_id="whitelist-test",
            )
        assert not isinstance(exc_info.value, ModelNotAllowedError)
    finally:
        await client.aclose()
