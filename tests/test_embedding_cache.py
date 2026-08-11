"""R8-A Embedding 缓存来源版本化与抗污染单元测试（fakes，无需真实 Redis）。

覆盖：
- namespace 随模型/维度变化；遗留无版本 key 不被消费
- 损坏 JSON / 错误形状 / bool / NaN / Infinity → miss 且 best-effort 删除
- 合法向量归一化为纯 float；写入/批量写入计数只计有效成功
- Redis 不可用 / 读 / 写 / 删故障 → miss / no-op，不破坏 RAG
- 日志中不含 query 原文 / 向量 / payload / 任意异常文本
- 预热行为保持不变
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

import pytest

import app.rag.embedding_cache as ec

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSettings:
    """为 embedding_cache 提供可控 settings。"""

    def __init__(
        self,
        *,
        embedding_model: str = "model-a",
        embedding_dim: int = 3,
        embedding_cache_ttl_seconds: int = 3600,
    ) -> None:
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self.embedding_cache_ttl_seconds = embedding_cache_ttl_seconds


class _FakePipe:
    def __init__(self, client: _FakeRedisCache) -> None:
        self._client = client
        self._cmds: list[tuple[str, Any, Any, Any]] = []

    def setex(self, key: str, ttl: int, value: str) -> _FakePipe:
        self._cmds.append(("setex", key, ttl, value))
        return self

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self) -> list[bool]:
        results: list[bool] = []
        for kind, key, _ttl, value in self._cmds:
            if kind != "setex":
                results.append(False)
                continue
            if self._client.fail_set:
                raise RuntimeError("redis set boom")
            self._client.store[key] = value
            results.append(True)
        return results


class _FakeRedisCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail_get_redis = False
        self.fail_get = False
        self.fail_set = False
        self.fail_delete = False

    async def get(self, key: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("redis get boom")
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        del ttl
        if self.fail_set:
            raise RuntimeError("redis set boom")
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        if self.fail_delete:
            raise RuntimeError("redis del boom")
        removed = sum(1 for k in keys if k in self.store)
        for k in keys:
            self.store.pop(k, None)
        return removed

    async def scan(self, *, cursor: int = 0, match: str = "*", count: int = 100) -> tuple[int, list[str]]:
        del count
        keys = [k for k in self.store if fnmatch.fnmatch(k, match)]
        return (0, keys)

    def pipeline(self) -> _FakePipe:
        return _FakePipe(self)


class _PagingFakeRedis:
    """分页 fake：scan 逐页返回大量 key（cursor 非 0 直到扫完）。

    ``store`` 为实际存在子集（可模拟服务端已过期/缺失），delete 返回真实删除数。
    用于证明 cache_stats 采样有界、clear_cache 计数实际删除。
    """

    def __init__(self, keys: list[str], *, store_keys: list[str] | None = None) -> None:
        self.keys = keys
        self.store: dict[str, bytes] = {
            k: b"[1.0,2.0,3.0]" for k in (store_keys if store_keys is not None else keys)
        }
        self.get_calls = 0

    async def scan(self, *, cursor: int = 0, match: str = "*", count: int = 100) -> tuple[int, list[str]]:
        del match
        start = cursor
        page = self.keys[start : start + count]
        nxt = start + count
        if nxt >= len(self.keys):
            nxt = 0
        return (nxt, page)

    async def get(self, key: str) -> bytes | None:
        self.get_calls += 1
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        removed = sum(1 for k in keys if k in self.store)
        for k in keys:
            self.store.pop(k, None)
        return removed


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisCache:
    client = _FakeRedisCache()

    async def _provider() -> _FakeRedisCache:
        if client.fail_get_redis:
            raise RuntimeError("redis unreachable")
        return client

    monkeypatch.setattr(ec, "get_redis", _provider)
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings())
    return client


def _seed_current(client: _FakeRedisCache, text: str, payload: str) -> str:
    key = ec._make_key(text)
    client.store[key] = payload
    return key


# ---------------------------------------------------------------------------
# 来源版本化
# ---------------------------------------------------------------------------


def test_key_namespace_changes_with_model_and_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    ec.get_settings.cache_clear()  # 复位，避免影响
    text = "咳嗽一周"
    key_a = ec._make_key(text)
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings(embedding_model="model-b"))
    key_b = ec._make_key(text)
    assert key_a != key_b  # 模型变化 → 硬 miss namespace
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings(embedding_dim=768))
    key_c = ec._make_key(text)
    assert key_a != key_c  # 维度变化 → 硬 miss namespace
    assert "咳嗽一周" not in key_a  # 键只含 query 摘要，不含原文


async def test_legacy_unversioned_entry_not_consumed(fake_redis: _FakeRedisCache) -> None:
    """遗留无版本 key（embed:<sha1>）不会被当前 namespace 消费。"""
    text = "咳嗽一周"
    legacy_key = f"embed:{__import__('hashlib').sha1(text.encode()).hexdigest()}"
    fake_redis.store[legacy_key] = json.dumps([1.0, 2.0, 3.0])
    assert ec._make_key(text) != legacy_key
    assert await ec.get_embedding(text) is None  # miss，不读遗留 key


# ---------------------------------------------------------------------------
# 抗污染：读
# ---------------------------------------------------------------------------


async def test_corrupt_json_is_miss_and_deleted(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    key = _seed_current(fake_redis, text, "{not-json")
    assert await ec.get_embedding(text) is None
    assert key not in fake_redis.store  # best-effort 删除


async def test_wrong_shape_is_miss(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    key = _seed_current(fake_redis, text, json.dumps([1.0, 2.0]))  # dim 2 != 3
    assert await ec.get_embedding(text) is None
    assert key not in fake_redis.store


async def test_non_list_shape_is_miss(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    key = _seed_current(fake_redis, text, json.dumps({"a": 1}))
    assert await ec.get_embedding(text) is None
    assert key not in fake_redis.store


async def test_bool_nan_inf_read_rejected(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    for payload in (
        json.dumps([1.0, True, 3.0]),  # bool 混入
        "[1.0, 2.0, NaN]",  # NaN
        "[1.0, 2.0, Infinity]",  # Infinity
        '[1.0, 2.0, "x"]',  # 非数值
    ):
        key = _seed_current(fake_redis, text, payload)
        assert await ec.get_embedding(text) is None
        assert key not in fake_redis.store  # 每类污染都被清理


# ---------------------------------------------------------------------------
# 抗污染：写 / 归一化
# ---------------------------------------------------------------------------


async def test_valid_write_and_read_normalize_to_floats(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    await ec.set_embedding(text, [1, 2.5, 3])  # int/float 混入
    raw = fake_redis.store[ec._make_key(text)]
    assert json.loads(raw) == [1.0, 2.5, 3.0]  # 序列化为纯 float
    got = await ec.get_embedding(text)
    assert got == [1.0, 2.5, 3.0]
    assert all(isinstance(v, float) for v in got)


async def test_invalid_vector_write_rejected(fake_redis: _FakeRedisCache) -> None:
    text = "咳嗽一周"
    for bad in (
        [1.0, True, 3.0],  # bool
        [1.0, 2.0],  # 维度不符
        [float("nan"), 2.0, 3.0],  # NaN → allow_nan=False
        [1.0, 2.0, float("inf")],  # Infinity
        "not-a-vector",
    ):
        await ec.set_embedding(text, bad)
        assert ec._make_key(text) not in fake_redis.store


async def test_batch_write_counts_only_valid(fake_redis: _FakeRedisCache) -> None:
    items = [
        ("t1", [1.0, 2.0, 3.0]),
        ("t2", [1.0, 2.0]),  # 维度不符
        ("t3", [1.0, True, 3.0]),  # bool
        ("t4", [4.0, 5.0, 6.0]),
    ]
    n = await ec.batch_set_embeddings(items, batch_size=2)
    assert n == 2  # 只计有效成功
    assert ec._make_key("t1") in fake_redis.store
    assert ec._make_key("t4") in fake_redis.store
    assert ec._make_key("t2") not in fake_redis.store
    assert ec._make_key("t3") not in fake_redis.store


async def test_batch_write_pipeline_failure_noop(fake_redis: _FakeRedisCache) -> None:
    fake_redis.fail_set = True
    n = await ec.batch_set_embeddings([("t1", [1.0, 2.0, 3.0])])
    assert n == 0
    assert ec._make_key("t1") not in fake_redis.store


# ---------------------------------------------------------------------------
# 降级 / 故障
# ---------------------------------------------------------------------------


async def test_redis_outage_degrades_to_miss(fake_redis: _FakeRedisCache) -> None:
    fake_redis.fail_get_redis = True
    assert await ec.get_embedding("咳嗽一周") is None
    await ec.set_embedding("咳嗽一周", [1.0, 2.0, 3.0])  # no-op，不抛
    assert await ec.clear_cache() == 0


async def test_get_and_delete_errors_do_not_break_rag(fake_redis: _FakeRedisCache) -> None:
    fake_redis.fail_get = True
    assert await ec.get_embedding("咳嗽一周") is None  # 读故障 → miss

    fake_redis.fail_get = False
    fake_redis.fail_delete = True
    _seed_current(fake_redis, "咳嗽一周", "{bad")
    assert await ec.get_embedding("咳嗽一周") is None  # 删除失败不传播
    assert ec._make_key("咳嗽一周") in fake_redis.store  # 删除失败但调用不抛


async def test_ttl_zero_disables_cache(fake_redis: _FakeRedisCache, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings(embedding_cache_ttl_seconds=0))
    await ec.set_embedding("咳嗽一周", [1.0, 2.0, 3.0])
    assert not fake_redis.store
    assert await ec.get_embedding("咳嗽一周") is None


async def test_clear_and_stats_operate_on_current_namespace(fake_redis: _FakeRedisCache) -> None:
    await ec.set_embedding("a", [1.0, 2.0, 3.0])
    await ec.set_embedding("b", [4.0, 5.0, 6.0])
    # 遗留无版本 key 不应被统计/清除
    legacy = f"embed:{__import__('hashlib').sha1(b'x').hexdigest()}"
    fake_redis.store[legacy] = "[1.0,2.0,3.0]"
    stats = await ec.cache_stats()
    assert stats["redis_ok"] is True
    assert stats["key_count"] == 2
    cleared = await ec.clear_cache()
    assert cleared == 2
    assert legacy in fake_redis.store  # 遗留 key 保留


async def test_cache_stats_bounded_on_large_keyset(monkeypatch: pytest.MonkeyPatch) -> None:
    """超大 key 集下：key_count 精确、采样/回显/内存估算均有界、只读少量键。"""
    keys = [ec._make_key(f"query-{i}") for i in range(10_000)]
    paging = _PagingFakeRedis(keys)

    async def _provider() -> _PagingFakeRedis:
        return paging

    monkeypatch.setattr(ec, "get_redis", _provider)
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings())

    stats = await ec.cache_stats()
    assert stats["redis_ok"] is True
    assert stats["key_count"] == 10_000
    # 回显采样有界，绝不随 key 总量增长
    assert len(stats["sample_keys"]) <= ec._STATS_OUTPUT_SAMPLE
    # 内存估算只读取至多有界的采样键，而非扫描到的全部 key
    assert paging.get_calls <= ec._STATS_SAMPLE_SIZE
    assert stats["estimated_memory_mb"] > 0


async def test_clear_cache_counts_actual_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_cache 计数真实 Redis delete 结果，而非扫描到的 key 数。"""
    keys = [ec._make_key(f"query-{i}") for i in range(100)]
    # 服务端已有部分 key 过期/缺失：store 只含其中 30 个
    paging = _PagingFakeRedis(keys, store_keys=keys[:30])

    async def _provider() -> _PagingFakeRedis:
        return paging

    monkeypatch.setattr(ec, "get_redis", _provider)
    monkeypatch.setattr(ec, "get_settings", lambda: _FakeSettings())

    cleared = await ec.clear_cache()
    assert cleared == 30  # 扫描到 100，实际删除 30


async def test_clear_cache_single_counts_actual_delete(fake_redis: _FakeRedisCache) -> None:
    """单文本清除返回 delete 的真实返回值（存在=1，不存在=0）。"""
    assert await ec.clear_cache("不存在的文本") == 0
    await ec.set_embedding("存在的文本", [1.0, 2.0, 3.0])
    assert await ec.clear_cache("存在的文本") == 1


# ---------------------------------------------------------------------------
# 日志隐私
# ---------------------------------------------------------------------------


async def test_no_sensitive_values_in_logs(fake_redis: _FakeRedisCache, caplog: pytest.LogCaptureFixture) -> None:
    fake_redis.fail_get = True
    fake_redis.fail_set = True
    secret_query = "患者主诉：咳嗽一周伴随胸闷气短"
    secret_vector = [1.0, 2.0, 3.0]
    with caplog.at_level(logging.WARNING, logger="xuanhu.embedding_cache"):
        await ec.get_embedding(secret_query)
        await ec.set_embedding(secret_query, secret_vector)
        await ec.batch_set_embeddings([(secret_query, secret_vector)])
        await ec.clear_cache(secret_query)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_query not in joined
    assert "1.0" not in joined or "2.0" not in joined
    assert "boom" not in joined  # 不记录任意异常文本（仅类型名）


# ---------------------------------------------------------------------------
# 预热行为保持
# ---------------------------------------------------------------------------


async def test_generate_template_queries_preserved() -> None:
    queries = ec.generate_template_queries(["黄芪"], ["四君子汤"])
    assert "黄芪的功效" in queries
    assert "四君子汤的组成" in queries
    assert len(queries) == len(set(queries))  # 去重


async def test_batch_embed_and_cache_contract(fake_redis: _FakeRedisCache, monkeypatch: pytest.MonkeyPatch) -> None:
    """预热入口：已缓存跳过、有效向量写入。"""

    class _Gateway:
        async def embed(self, batch: list[str], trace_id: str = "") -> list[list[float]]:
            del trace_id
            return [[1.0, 2.0, 3.0]] * len(batch)

    await ec.set_embedding("已缓存查询", [1.0, 2.0, 3.0])
    result = await ec.batch_embed_and_cache(["已缓存查询", "新查询"], _Gateway(), batch_size=1)
    assert result["total"] == 2
    assert result["skipped"] == 1
    assert result["cached"] == 1
    assert ec._make_key("新查询") in fake_redis.store
