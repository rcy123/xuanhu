"""Embedding 专用模型网关配置。

``ModelGatewayClient`` 接收 base URL，并在发送向量请求时追加
``/embeddings``。专用配置同时允许填写 base URL 或完整 embedding
endpoint，因此需要在创建客户端前统一还原为 base URL。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

_EMBEDDINGS_PATH_SUFFIX = "/embeddings"


def normalize_embedding_gateway_base_url(configured_url: str) -> str:
    """将 base URL 或完整 embedding endpoint 归一化为客户端所需的 base URL。"""
    normalized_url = configured_url.rstrip("/")
    if normalized_url.endswith(_EMBEDDINGS_PATH_SUFFIX):
        return normalized_url.removesuffix(_EMBEDDINGS_PATH_SUFFIX)
    return normalized_url


def build_embedding_gateway_settings(settings: Any) -> Any:
    """将 embedding 专用配置映射为 ``ModelGatewayClient`` settings。

    只有 URL 与 API key 同时存在时才启用专用配置；否则返回原始 settings，
    由调用方沿用默认模型网关。
    """
    embedding_url = getattr(settings, "embedding_gateway_base_url", "") or ""
    embedding_key = getattr(settings, "embedding_gateway_api_key", "") or ""

    if not embedding_url or not embedding_key:
        return settings

    return SimpleNamespace(
        model_gateway_base_url=normalize_embedding_gateway_base_url(embedding_url),
        model_gateway_api_key=embedding_key,
        model_gateway_timeout_seconds=(
            settings.embedding_gateway_timeout_seconds
            if getattr(settings, "embedding_gateway_timeout_seconds", 0) > 0
            else settings.model_gateway_timeout_seconds
        ),
        model_gateway_max_retries=(
            settings.embedding_gateway_max_retries
            if getattr(settings, "embedding_gateway_max_retries", 0) > 0
            else settings.model_gateway_max_retries
        ),
        model_gateway_route_profile=settings.model_gateway_route_profile,
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
    )
