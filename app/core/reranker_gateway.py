"""Reranker 专用模型网关配置。

``ModelGatewayClient`` 接收 base URL，并在发送 reranker 请求时追加
``/rerank``。专用配置同时允许填写 base URL 或完整 reranker endpoint，
因此需要在创建客户端前统一还原为 base URL。

与 ``embedding_gateway.py`` 模式一致：只有 URL 与 API key 同时存在时
才启用专用配置；否则回退到全局 ``MODEL_GATEWAY_*``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

_RERANK_PATH_SUFFIX = "/rerank"


def normalize_reranker_gateway_base_url(configured_url: str) -> str:
    """将 base URL 或完整 reranker endpoint 归一化为客户端所需的 base URL。"""
    normalized_url = configured_url.rstrip("/")
    if normalized_url.endswith(_RERANK_PATH_SUFFIX):
        return normalized_url.removesuffix(_RERANK_PATH_SUFFIX)
    # 也兼容 /v1/rerank 格式
    if normalized_url.endswith("/v1/rerank"):
        return normalized_url.removesuffix("/rerank")
    return normalized_url


def build_reranker_gateway_settings(settings: Any) -> Any:
    """将 reranker 专用配置映射为 ``ModelGatewayClient`` settings。

    只有 URL 与 API key 同时存在时才启用专用配置；否则返回原始 settings，
    由调用方沿用默认模型网关。
    """
    reranker_url = getattr(settings, "reranker_gateway_base_url", "") or ""
    reranker_key = getattr(settings, "reranker_gateway_api_key", "") or ""

    if not reranker_url or not reranker_key:
        return settings

    return SimpleNamespace(
        model_gateway_base_url=normalize_reranker_gateway_base_url(reranker_url),
        model_gateway_api_key=reranker_key,
        model_gateway_timeout_seconds=(
            settings.reranker_gateway_timeout_seconds
            if getattr(settings, "reranker_gateway_timeout_seconds", 0) > 0
            else settings.model_gateway_timeout_seconds
        ),
        model_gateway_max_retries=(
            settings.reranker_gateway_max_retries
            if getattr(settings, "reranker_gateway_max_retries", 0) > 0
            else settings.model_gateway_max_retries
        ),
        model_gateway_route_profile=settings.model_gateway_route_profile,
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
    )
