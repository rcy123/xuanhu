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

预热
----
``batch_set_embeddings()`` 支持离线批量预热（L1 实体名 + L2 模板查询），
将 embedding API 调用前置到低峰时段，提升在线命中率。

不缓存文档侧 embedding（那是离线 sync 脚本的产物，在线从不重算）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

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


async def _redis_ping() -> bool:
    """检查 Redis 是否可达。"""
    try:
        redis_conn = await get_redis()
        await redis_conn.ping()
        return True
    except Exception:
        return False


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


async def batch_set_embeddings(
    items: list[tuple[str, list[float]]],
    *,
    batch_size: int = 50,
) -> int:
    """批量写入 embedding 缓存。

    Args:
        items: ``[(text, vector), ...]`` 列表。
        batch_size: 每批 pipeline 写入条数。

    Returns:
        成功写入的条数。
    """
    ttl = _ttl_seconds()
    if ttl <= 0 or not items:
        return 0
    written = 0
    try:
        redis_conn = await get_redis()
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            async with redis_conn.pipeline() as pipe:
                for text, vector in batch:
                    pipe.setex(_make_key(text), ttl, json.dumps(vector))
                results = await pipe.execute()
                written += sum(1 for r in results if r)
    except Exception as exc:
        logger.warning("Embedding 批量缓存写入失败: %s", exc)
    return written


async def cache_stats() -> dict[str, Any]:
    """返回当前缓存统计信息（用于评测和监控）。

    Returns:
        ``{"key_count": int, "sample_keys": [str, ...], "redis_ok": bool}``
    """
    result: dict[str, Any] = {"key_count": 0, "sample_keys": [], "redis_ok": False}
    try:
        redis_conn = await get_redis()
        result["redis_ok"] = True
        cursor = 0
        keys: list[str] = []
        while True:
            cursor, batch = await redis_conn.scan(
                cursor=cursor, match=f"{_CACHE_PREFIX}*", count=100,
            )
            keys.extend(batch)
            if cursor == 0:
                break
        result["key_count"] = len(keys)
        result["sample_keys"] = [
            k.decode("utf-8") if isinstance(k, bytes) else k
            for k in keys[:10]
        ]
        # 估算内存
        if keys:
            total_bytes = 0
            for i, k in enumerate(keys[:20]):
                try:
                    val = await redis_conn.get(k)
                    if val:
                        total_bytes += len(val)
                except Exception:
                    pass
            avg_bytes = max(total_bytes / min(20, len(keys)), 1)
            result["estimated_memory_mb"] = round(len(keys) * avg_bytes / (1024 * 1024), 2)
    except Exception as exc:
        logger.warning("Embedding 缓存统计读取失败: %s", exc)
    return result


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


# ══════════════════════════════════════════════════════════════
# 预热辅助：模板生成
# ══════════════════════════════════════════════════════════════

HERB_QUERY_TEMPLATES: tuple[str, ...] = (
    "{herb}的功效",
    "{herb}的作用",
    "{herb}的性味归经",
    "{herb}的用法用量",
    "{herb}的禁忌",
    "{herb}的配伍",
    "{herb}的主治",
    "{herb}的性味",
)

FORMULA_QUERY_TEMPLATES: tuple[str, ...] = (
    "{formula}的组成",
    "{formula}的功效",
    "{formula}的方解",
    "{formula}的用法",
    "{formula}的主治",
    "{formula}的禁忌",
)


def generate_template_queries(
    herbs: list[str],
    formulas: list[str],
) -> list[str]:
    """从实体名列表生成 L2 模板查询。

    Args:
        herbs: 中药名列表。
        formulas: 方剂名列表。

    Returns:
        所有模板查询文本（去重后）。
    """
    queries: list[str] = []
    for herb in herbs:
        for tpl in HERB_QUERY_TEMPLATES:
            queries.append(tpl.format(herb=herb))
    for formula in formulas:
        for tpl in FORMULA_QUERY_TEMPLATES:
            queries.append(tpl.format(formula=formula))
    # 去重（不同实体可能生成相同模板文本——极少但防御）
    return list(dict.fromkeys(queries))


async def batch_embed_and_cache(
    queries: list[str],
    gateway: Any,
    *,
    batch_size: int = 10,
    trace_id: str = "prewarm",
) -> dict[str, Any]:
    """对一批查询文本做 embedding 并批量写入缓存。

    Args:
        queries: 查询文本列表。
        gateway: ``ModelGatewayClient`` 实例。
        batch_size: embedding API 的 batch 大小（一次请求的文本数）。
        trace_id: 链路追踪 ID。

    Returns:
        ``{"total": int, "cached": int, "skipped": int, "failed": int, "elapsed_ms": float}``
    """
    t0 = time.perf_counter()
    stats: dict[str, Any] = {"total": len(queries), "cached": 0, "skipped": 0, "failed": 0}

    if not queries:
        stats["elapsed_ms"] = 0.0
        return stats

    # 先过滤已缓存的（避免重复调用 embedding API）
    to_embed: list[str] = []
    for q in queries:
        if await get_embedding(q) is not None:
            stats["skipped"] += 1
        else:
            to_embed.append(q)
            # 去重防御
            if to_embed.count(q) > 1:
                to_embed.pop()

    if not to_embed:
        stats["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        return stats

    # 分批调用 embedding API
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        try:
            vectors = await gateway.embed(batch, trace_id=f"{trace_id}-{i // batch_size}")
        except Exception:
            logger.warning("预热 embedding 调用失败: batch %d-%d", i, i + len(batch), exc_info=True)
            stats["failed"] += len(batch)
            continue

        # 写入缓存
        pairs = [
            (text, vec.tolist() if hasattr(vec, "tolist") else vec)
            for text, vec in zip(batch, vectors)
        ]
        n = await batch_set_embeddings(pairs)
        stats["cached"] += n

    stats["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return stats
