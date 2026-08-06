"""Query 改写专用模型网关配置。

``ModelGatewayClient`` 接收 base URL，通过 ``chat()`` 方法发送改写请求。
与 ``reranker_gateway.py`` / ``embedding_gateway.py`` 模式一致：
只有 URL 与 API key 同时存在时才启用专用配置；否则回退到 ``runtime.gateway``。

注意：改写模型通常是轻量级（如 Qwen3.5-2B-free），需要部署在独立网关上，
因此与主推理网关（mimo-v2.5）分开配置。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def build_rewrite_gateway_settings(settings: Any) -> Any | None:
    """将 rewrite 专用配置映射为 ``ModelGatewayClient`` settings。

    只有 URL 与 API key 同时存在时才返回专用配置；否则返回 None，
    由调用方沿用 ``runtime.gateway``。
    """
    rewrite_url: str = getattr(settings, "rag_query_rewrite_gateway_base_url", "") or ""
    rewrite_key: str = getattr(settings, "rag_query_rewrite_gateway_api_key", "") or ""

    if not rewrite_url or not rewrite_key:
        return None

    gw_timeout = getattr(settings, "rag_query_rewrite_gateway_timeout_seconds", 0) or 0
    gw_retries = getattr(settings, "rag_query_rewrite_gateway_max_retries", 0) or 0

    return SimpleNamespace(
        model_gateway_base_url=rewrite_url.rstrip("/"),
        model_gateway_api_key=rewrite_key,
        model_gateway_timeout_seconds=(
            gw_timeout
            if gw_timeout > 0
            else getattr(settings, "rag_query_rewrite_timeout_seconds", 3.0)
        ),
        model_gateway_max_retries=(
            gw_retries
            if gw_retries > 0
            else settings.model_gateway_max_retries
        ),
        model_gateway_route_profile=settings.model_gateway_route_profile,
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
    )
