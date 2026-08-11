"""Embedding 缓存层（R8-A：来源版本化 + 抗污染校验）。

减少重复文本的 Embedding 网关调用，降低 LLM 网关 API 配额消耗和 RTT。
问诊场景中同一 query（如主诉"咳嗽一周"）的重复出现率较高，缓存收益明显。

策略
----
- Key: ``embed:<cache_version>:<sha1(query_text)>``
- ``cache_version`` 由 **embedding 模型 + 精确配置维度 + schema 版本** 派生的
  稳定、隐私安全的定长摘要。仅保留 query 文本的 sha1 摘要，绝不存原文。
- Value: JSON 序列化的 ``list[float]``（``allow_nan=False``）。
- Store: Redis（复用 ``app.core.redis.get_redis`` 单例）
- TTL: 由 ``Settings.embedding_cache_ttl_seconds`` 控制（默认 3600 秒），
  设为 0 则禁用缓存（每次回退到网关调用）。

来源版本化（provenance）
------------------------
模型/维度切换 → ``cache_version`` 变化 → 旧 namespace 下的键变为**硬 miss**，
绝不复用不兼容向量。遗留无版本 key（``embed:<hex>``）不会命中当前 namespace，
也不会被消费。``clear_cache``/``cache_stats`` 只作用于当前 namespace 前缀。

抗污染（poison resistance）
--------------------------
读回与写入的向量都经过 ``_normalize_vector`` 校验：必须是数值（非 bool）、
维度精确匹配、全部有限，并归一化为纯 ``float``。损坏/维度不符/非有限/错误版本
的缓存数据一律视为 miss，best-effort 删除，**绝不**进入 retriever/Milvus。

降级
----
Redis 不可用/读/写/删任意错误 → miss / no-op，不破坏 RAG 主路径。
日志/度量只含类型名与长度等有界信息，绝不记录原始 query、向量、Redis payload
或任意异常文本。

预热
----
``batch_set_embeddings()`` 支持离线批量预热（L1 实体名 + L2 模板查询），
将 embedding API 调用前置到低峰时段，提升在线命中率。行为保持不变。

不缓存文档侧 embedding（那是离线 sync 脚本的产物，在线从不重算）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import time
from numbers import Real
from typing import Any

from app.core.config import get_settings
from app.core.redis import get_redis

logger = logging.getLogger("xuanhu.embedding_cache")

_CACHE_PREFIX = "embed:"
# 缓存结构 schema 版本；变更结构/校验规则时递增，强制整体失效。
_CACHE_SCHEMA_VERSION = "1"
# cache_stats 为内存估算/回显保留的最大采样键数（有界，不随 key 总量增长）。
_STATS_SAMPLE_SIZE = 20
# cache_stats 对外回显的最大 sample_keys 条数。
_STATS_OUTPUT_SAMPLE = 10


def _cache_version() -> str:
    """由 embedding 模型 + 精确维度 + schema 版本派生的稳定 namespace 版本。"""
    settings = get_settings()
    seed = f"{settings.embedding_model}|{int(settings.embedding_dim)}|{_CACHE_SCHEMA_VERSION}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"v{_CACHE_SCHEMA_VERSION}:{digest}"


def _namespace() -> str:
    """当前 namespace 前缀（SCAN/DELETE 只作用于该前缀，不使用 KEYS）。"""
    return f"{_CACHE_PREFIX}{_cache_version()}:"


def _make_key(text: str) -> str:
    """生成缓存键（含来源版本 + query 文本 sha1 摘要）。"""
    return f"{_namespace()}{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _ttl_seconds() -> int:
    """从配置读取 TTL，0 表示禁用。"""
    return int(get_settings().embedding_cache_ttl_seconds or 0)


def _err_type(exc: BaseException) -> str:
    """返回异常类名（有界，不携带任意异常文本）。"""
    return type(exc).__name__


def _normalize_vector(vector: Any, dim: int) -> list[float] | None:
    """校验并归一化向量为纯 ``list[float]``。

    合法：可迭代、长度精确等于 ``dim``、每元素为数值（非 bool）且有限。
    非法：返回 None（调用方视为 miss / 拒绝写入）。
    """
    if vector is None:
        return None
    try:
        values = list(vector)
    except (TypeError, ValueError):
        return None
    if len(values) != dim:
        return None
    out: list[float] = []
    for value in values:
        # bool 是 int 的子类（Real），必须先排除
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        f = float(value)
        if not math.isfinite(f):
            return None
        out.append(f)
    return out


def _serialize_vector(vector: list[float]) -> str | None:
    """序列化向量；含 NaN/Infinity 时（allow_nan=False）返回 None。"""
    try:
        return json.dumps(vector, allow_nan=False)
    except (TypeError, ValueError):
        return None


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
        命中且通过来源/向量校验时返回向量，否则返回 None（含损坏数据）。
        当 TTL 配置为 0（禁用缓存）时永远返回 None。
    """
    if _ttl_seconds() <= 0:
        return None
    dim = int(get_settings().embedding_dim)
    key = _make_key(text)
    try:
        redis_conn = await get_redis()
        raw = await redis_conn.get(key)
    except Exception as exc:
        logger.warning("embedding cache read degraded (error=%s)", _err_type(exc))
        return None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    normalized = _normalize_vector(parsed, dim)
    if normalized is not None:
        return normalized
    # 损坏/维度不符/非有限/错误版本 → miss + best-effort 删除，绝不进入检索。
    logger.warning("embedding cache entry invalid, dropping key (ref=%s)", _bounded_key(key))
    with _suppress_redis_errors():
        await redis_conn.delete(key)
    return None


async def set_embedding(text: str, vector: Any) -> None:
    """将文本的 embedding 存入缓存。

    向量先经 ``_normalize_vector`` 校验并归一化为纯 ``float``；非法向量不写入。
    """
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    dim = int(get_settings().embedding_dim)
    normalized = _normalize_vector(vector, dim)
    if normalized is None:
        logger.warning("embedding cache write rejected: invalid vector (dim=%d)", dim)
        return
    payload = _serialize_vector(normalized)
    if payload is None:
        logger.warning("embedding cache write rejected: non-finite vector (dim=%d)", dim)
        return
    try:
        redis_conn = await get_redis()
        await redis_conn.setex(_make_key(text), ttl, payload)
    except Exception as exc:
        logger.warning("embedding cache write degraded (error=%s)", _err_type(exc))


async def batch_set_embeddings(
    items: list[tuple[str, Any]],
    *,
    batch_size: int = 50,
) -> int:
    """批量写入 embedding 缓存（仅统计有效且写入成功的条数）。

    Args:
        items: ``[(text, vector), ...]`` 列表。
        batch_size: 每批 pipeline 写入条数。

    Returns:
        成功写入（且向量校验通过）的条数。
    """
    ttl = _ttl_seconds()
    if ttl <= 0 or not items:
        return 0
    dim = int(get_settings().embedding_dim)
    written = 0
    try:
        redis_conn = await get_redis()
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            # 先过滤非法向量（不写入），再建 pipeline
            valid: list[tuple[str, str]] = []
            for text, vector in batch:
                normalized = _normalize_vector(vector, dim)
                if normalized is None:
                    logger.warning("embedding cache batch write skipped: invalid vector (dim=%d)", dim)
                    continue
                payload = _serialize_vector(normalized)
                if payload is None:
                    logger.warning("embedding cache batch write skipped: non-finite vector (dim=%d)", dim)
                    continue
                valid.append((_make_key(text), payload))
            if not valid:
                continue
            async with redis_conn.pipeline() as pipe:
                for key, payload in valid:
                    pipe.setex(key, ttl, payload)
                results = await pipe.execute()
                written += sum(1 for r in results if r)
    except Exception as exc:
        logger.warning("embedding cache batch write degraded (error=%s)", _err_type(exc))
    return written


async def cache_stats() -> dict[str, Any]:
    """返回当前 namespace 缓存统计（有界、非阻塞，不使用 KEYS）。

    计数增量完成，只为内存估算/回显保留至多 ``_STATS_SAMPLE_SIZE`` 个采样键，
    不随 key 总量增长。

    Returns:
        ``{"key_count": int, "sample_keys": [str, ...], "redis_ok": bool}``
    """
    result: dict[str, Any] = {"key_count": 0, "sample_keys": [], "redis_ok": False}
    try:
        redis_conn = await get_redis()
        result["redis_ok"] = True
        cursor = 0
        count = 0
        sample: list[str] = []
        prefix = _namespace()
        while True:
            cursor, batch = await redis_conn.scan(
                cursor=cursor, match=f"{prefix}*", count=100,
            )
            for k in batch:
                count += 1
                if len(sample) < _STATS_SAMPLE_SIZE:
                    sample.append(k.decode("utf-8") if isinstance(k, bytes) else k)
            if cursor == 0:
                break
        result["key_count"] = count
        result["sample_keys"] = sample[:_STATS_OUTPUT_SAMPLE]
        if sample:
            total_bytes = 0
            for k in sample:
                with _suppress_redis_errors():
                    val = await redis_conn.get(k)
                    if val:
                        total_bytes += len(val)
            avg_bytes = max(total_bytes / len(sample), 1)
            result["estimated_memory_mb"] = round(
                count * avg_bytes / (1024 * 1024), 2
            )
    except Exception as exc:
        logger.warning("embedding cache stats degraded (error=%s)", _err_type(exc))
    return result


async def clear_cache(text: str | None = None) -> int:
    """清除缓存项。

    Args:
        text: 若提供，只清除该文本的缓存；否则清除当前 namespace 前缀的键
              （不使用 KEYS，有界分页）。

    Returns:
        实际被清除的键数量（取自 Redis delete 的真实返回值，而非扫描到的键数）。
    """
    try:
        redis_conn = await get_redis()
    except Exception as exc:
        logger.warning("embedding cache clear degraded (error=%s)", _err_type(exc))
        return 0
    if text:
        try:
            return int(await redis_conn.delete(_make_key(text)))
        except Exception as exc:
            logger.warning("embedding cache clear degraded (error=%s)", _err_type(exc))
            return 0
    cursor = 0
    count = 0
    prefix = _namespace()
    try:
        while True:
            cursor, keys = await redis_conn.scan(cursor=cursor, match=f"{prefix}*", count=100)
            if keys:
                deleted = await redis_conn.delete(*keys)
                count += int(deleted)
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning("embedding cache clear degraded (error=%s)", _err_type(exc))
    return count


def _bounded_key(key: str) -> str:
    """返回缓存键的定长摘要（不回显完整 key / query 原文）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@contextlib.contextmanager
def _suppress_redis_errors() -> Any:
    """吞掉 Redis 读/删错误，保证 cache 操作不破坏 RAG 主路径。"""
    with contextlib.suppress(Exception):
        yield


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
            for text, vec in zip(batch, vectors, strict=False)
        ]
        n = await batch_set_embeddings(pairs)
        stats["cached"] += n

    stats["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return stats
