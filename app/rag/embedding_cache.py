"""Embedding 缓存层。

减少重复文本的 Embedding 网关调用，降低 LLM 网关 API 配额消耗和 RTT。
问诊场景中同一 query（如主诉"咳嗽一周"）的重复出现率较高，缓存收益明显。

策略
----
- Key: ``sha1(query_text)``
- Value: JSON 序列化的 ``list[float]``
- Store: Redis（复用 ``app.core.redis.get_redis`` 单例）
- TTL: 由 ``Settings.embedding_cache_ttl_seconds`` 控制（默认 3600 秒），
  设为 0 则禁用缓存（每次回退到网关调用）。

不缓存文档侧 embedding（那是离线 sync 脚本的产物，在线从不重算）。
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.core.config import get_settings
from app.core.redis import get_redis

logger = logging.getLogger("xuanhu.embedding_cache")

_CACHE_PREFIX = "embed:"


def _make_key(text: str) -> str:
    """生成缓存键。"""
    return f"{_CACHE_PREFIX}{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _ttl_seconds() -> int:
    """从配置读取 TTL，0 表示禁用。"""
    return int(get_settings().embedding_cache_ttl_seconds or 0)


async def get_embedding(text: str) -> list[float] | None:
    """从缓存中获取文本的 embedding。

    Returns:
        如果命中缓存返回向量，否则返回 None。
        当 TTL 配置为 0（禁用缓存）时永远返回 None。
    """
    if _ttl_seconds() <= 0:
        return None
    try:
        redis_conn = await get_redis()
        raw = await redis_conn.get(_make_key(text))
        if raw is not None:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Embedding 缓存读取失败: %s", exc)
    return None


async def set_embedding(text: str, vector: list[float]) -> None:
    """将文本的 embedding 存入缓存。

    Args:
        text: 原始查询文本。
        vector: embedding 向量。
    """
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    try:
        redis_conn = await get_redis()
        await redis_conn.setex(_make_key(text), ttl, json.dumps(vector))
    except Exception as exc:
        logger.warning("Embedding 缓存写入失败: %s", exc)


async def clear_cache(text: str | None = None) -> int:
    """清除缓存项。

    Args:
        text: 若提供，只清除该文本的缓存；否则清除所有 ``embed:`` 前缀的键。

    Returns:
        被清除的键数量。
    """
    redis_conn = await get_redis()
    if text:
        await redis_conn.delete(_make_key(text))
        return 1
    cursor = 0
    count = 0
    while True:
        cursor, keys = await redis_conn.scan(cursor=cursor, match=f"{_CACHE_PREFIX}*", count=100)
        if keys:
            await redis_conn.delete(*keys)
            count += len(keys)
        if cursor == 0:
            break
    return count
