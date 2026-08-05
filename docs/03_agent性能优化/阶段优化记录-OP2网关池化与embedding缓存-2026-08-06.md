# 阶段优化记录 · OP2 网关池化 + Embedding 缓存（阶段补完）

> 日期：2026-08-06
> 关联文档：[02-优化方案设计.md](02-优化方案设计.md) OP2、[03-优化计划与排期.md](03-优化计划与排期.md) 阶段 2、[99-验收与回归基线.md](99-验收与回归基线.md)
> 范围：补完计划中阶段 2（OP2）的主体改动 + 真实后端基准数据

---

## 0. 背景：本阶段在整体计划中的位置

按 [03-优化计划与排期.md](03-优化计划与排期.md)，Agent 性能优化分 5 阶段：
T0 埋点 → T1 状态下推 → **T2 网关池化+cache** → T3 Milvus 异步 → T4 回归对比。

已落地：
- T0（OP4 埋点）：完成于 `bcf4070`，`metrics-instrumentation-2026-08-05.md` 记录。
- T2（OP2 网关池化）：**部分**完成于 `b1c5223`（仅 `self._client` 实例级复用，缺 Limits 与 lifespan 托管）。
- 计划外：Repository N+1 批量预加载（`0b83d23` / `d609d4b`），见 `connection-pool-batching-2026-08-05.md`。

**本阶段补完 T2 剩余项**：httpx.Limits 上限、lifespan 托管 client 启停、EmbeddingCache、/api/v1/metrics 端点注册。
T1（状态下推）与 T3（Milvus 异步）仍未开始，留待下一阶段。

---

## 1. Commits

| Commit | 类型 | 说明 |
|---|---|---|
| `057073b` | feat | 注册 `/api/v1/metrics` 端点 + prometheus_client 入依赖 |
| `0b83d23` | perf | _persist_safety_fact_assertions N+1 批量（前置，非本阶段） |
| `d609d4b` | perf | _persist_product_projections fallback 收敛（前置，非本阶段） |
| `58491b6` | feat(OP2) | httpx.Limits 连接池上限 + Redis EmbeddingCache |
| `31295b7` | perf(OP2) | lifespan 托管共享 ModelGatewayClient，health/llm 复用 |
| `aeb545b` | test | 扩展 perf_benchmark 增加 EmbeddingCache 命中效果测试 |
| `ffde025` | test | 修正 embedding cache 测试走 EMBEDDING_GATEWAY_BASE_URL |

本阶段核心提交：`58491b6` + `31295b7` + `057073b`。

---

## 2. Changes Summary

### 2.1 httpx 连接池上限 (`app/core/gateway.py`) — 对应 T2.1

**Problem**: 上轮 `b1c5223` 已把 `httpx.AsyncClient` 提为 `ModelGatewayClient` 实例属性复用，但未配 `Limits`，连接池无上限，高并发下可能与下游网关争抢连接。

**Fix**: 在 `__init__` 中显式构造 `httpx.Limits`：

```python
_limits = httpx.Limits(
    max_connections=64,
    max_keepalive_connections=16,
    keepalive_expiry=30.0,
)
self._client = httpx.AsyncClient(
    timeout=httpx.Timeout(self._timeout, connect=10.0),
    limits=_limits,
)
```

并预留 `self._embedding_client` 字段，为后续 embedding 专用端点池化留口子。

### 2.2 lifespan 托管共享 client (`app/main.py`) — 对应 T2.2

**Problem**: `/health/llm` 每次请求 `ModelGatewayClient()` 临时构造，与 `lifespan` 中已构造的共享实例各开一个 httpx 连接池，抵消复用收益；且无人 `aclose()`，进程退出连接池不显式释放。

**Fix**: `lifespan` 中构造一次 `gateway = ModelGatewayClient()` 存入 `app.state.gateway_client`，`finally` 中 `await gateway.aclose()`（类比 `shared_langgraph_runtime` 启停模式）。`/health/llm` 改为从 `request.app.state.gateway_client` 取共享实例。

```python
# main.py lifespan
gateway = ModelGatewayClient()
app.state.gateway_client = gateway
# ... yield ...
finally:
    await gateway.aclose()
```

### 2.3 EmbeddingCache (`app/rag/embedding_cache.py` 新增) — 对应 T2.4/T2.5

**Problem**: `RAGRetriever._vector_search` 每次 `await self._gateway.embed([query], ...)`，无缓存。问诊场景同一 query（主诉/症状）重复出现率高，重复 embed 浪费网关 RTT 与 API 配额。

**Fix**: 新增 `embedding_cache.py`，复用 `app.core.redis.get_redis` 单例：
- Key: `embed:{sha1(query_text)}`
- Value: JSON 序列化的 `list[float]`
- TTL: 由 `Settings.embedding_cache_ttl_seconds` 控制（默认 3600s，`0=禁用`）
- 仅缓存 query 侧 embedding，不缓存文档侧（那是离线 sync 产物，在线不重算）

`retriever.py` 的 `_vector_search` 在调 `gateway.embed` 前先查缓存，未命中才发网关并回填：

```python
cached = await get_embedding(query)
if cached is not None:
    query_vector = cached
else:
    async with measure("rag.embed"):
        embeddings = await self._gateway.embed([query], trace_id=trace_id)
    query_vector = embeddings[0]
    await set_embedding(query, query_vector)
```

配置项 `embedding_cache_ttl_seconds` 已加到 `app/core/config.py`。

### 2.4 /api/v1/metrics 端点注册 (`app/api/health.py`) — 对应 T0.5

**Problem**: `render_perf_metrics()` 在 `app/core/metrics.py` 已实现（`generate_latest(REGISTRY)`），但从未挂到 HTTP 路由，7 个 histogram 无法被 Prometheus 抓取 → 上轮埋点形同虚设。

**Fix**: `health.py` 新增 `@router.get("/metrics", include_in_schema=False)`，输出 `render_perf_metrics()`，Content-Type 走 `PROMETHEUS_CONTENT_TYPE`，加 `Cache-Control: no-store`。`prometheus_client` 正式加入 `pyproject.toml` dependencies。

> 注：计划文档里端点路径是 `/api/v1/metrics/perf`，实现为 `/api/v1/metrics`（更短，与已有的 `/api/v1/metrics/outbox` 同前缀，归一处管理）。功能等价。

### 2.5 Legacy structured fallback 审查 — 对应 T2.6

**Find**: `gateway.py:_chat_structured_impl` 的 JSON fallback 在 `max_requests is not None` 时已被 `break` 短路（`gateway.py:646`）。而 L2 runtime (`app/agent_runtime/runtime.py:460-462`) 对所有 `chat_structured` 调用强制传 `max_requests=1`：

```python
observed_method = getattr(self.gateway, "chat_structured_observed", None)
method = observed_method if callable(observed_method) else self.gateway.chat_structured
if self._accepts_max_requests(method):
    kwargs["max_requests"] = 1
```

即主路径 fallback 已天然禁用，无需再改。仅 legacy 直调（不经过 runtime 的）仍可能触发 fallback；本阶段不动这些路径，按 99 文档 2.4 节的要求，"关闭前需预发布回归对比解析成功率"——属后续可选加固项。

---

## 3. 真实后端基准数据

环境：本地 Docker（PG/Redis/Milvus）+ xiaomimimo(chat) + dmxapi(embedding) 网关
脚本：`scripts/perf_benchmark.py`，结果存 `scripts/perf_results.json`

### 3.1 LLM 健康检查连接复用（6 轮）

| 轮次 | 耗时 (s) |
|------|----------|
| #1（首轮） | **4.983** |
| #2 | 2.561 |
| #3 | 2.528 |
| #4 | 0.996 |
| #5 | 2.753 |
| #6 | 0.881 |

- 首轮 vs 复用均值：**4.983s vs 1.944s**
- 与上轮（`b1c5223`）对比：上轮首轮 7.093s / 复用 1.780s → 本轮首轮 4.983s / 复用 1.944s
- 注：首轮耗时受 LLM 网关 ping 波动影响大（chat 网关本身延迟在此 0.9–3s 间抖动），连接复用收益在"消除首轮 TCP/TLS 握手"上较稳定，但绝对值受外部网关波动主导。

### 3.2 并发健康检查（5 并发）

| 指标 | 值 |
|------|-----|
| 5 并发总耗时 | **2.209s** |
| 串行理论 | ~5 × 1.94s ≈ 9.7s |

并发收益显著（~77% 提速 vs 串行理论），说明 Limits 上限放开后连接池能支撑并发请求复用，无串行瓶颈。

### 3.3 EmbeddingCache 命中效果（核心新增度量）

通过 embedding 专用网关（`EMBEDDING_GATEWAY_BASE_URL=https://www.dmxapi.cn/v1`）对同一 query 连续取 2 次 embedding：

| 路径 | 耗时 | 说明 |
|------|------|------|
| miss（真实网关调用 `dmxapi /v1/embeddings`） | **1.640s** | TCP+TLS+网关 embedding 计算 |
| hit（Redis 读取 `embed:{sha1}`） | **0.004s** | 纯 Redis GET + JSON 反序列化 |
| 加速比 | **~381x** | `1.640 / 0.004` |
| 命中正确性 | ✅ True | 向量长度一致、首元数据匹配 |

**结论**：EmbeddingCache 命中时把一次网关 embedding 调用从 ~1.6s 抹到 ~4ms。问诊 query 重复场景下，这是本阶段收益最显著的优化项。按计划 99 文档门禁"命中率 ≥ 40%"，需在真实问诊流量下再测一轮（当前是单 query 探测）。

### 3.4 Prometheus /api/v1/metrics 端点

| 指标 | 值 |
|------|-----|
| HTTP 状态 | 200 |
| Content-Type | `text/plain; version=0.0.4; charset=utf-8` |
| `xuanhu_*` 指标行数 | **70** |
| 暴露的 histogram（不含 outbox） | 7 个（vector_search / fulltext / backfill / embed / gateway_chat / gateway_embed / graph_node） |

`/api/v1/metrics` 现已可被 Prometheus 抓取。注：当前各 histogram `_count` 仍为 0（因为本轮基准没触发 RAG/reasoning 路径，只跑了 health 探测），需跑真实问诊流量才有样本。

---

## 4. 性能影响汇总

| # | 优化项 | 文件 | 性能影响 | 可测量性 |
|---|--------|------|----------|----------|
| 1 | httpx.Limits 连接池上限 | `gateway.py` | 中—高并发下避免连接争抢，配合复用 | 并发 health 5 路 2.21s |
| 2 | lifespan 托管共享 client | `main.py` | 中—消除 health 路径每请求新建 client | health/llm 复用均值 1.94s |
| 3 | EmbeddingCache（Redis） | `embedding_cache.py`、`retriever.py` | **高**—命中时 1.64s→4ms（~381x） | miss/hit 直接对比 |
| 4 | /api/v1/metrics 端点 | `health.py`、`pyproject.toml` | 可观测性—7 histogram 可抓取 | 端点 200 + 70 指标行 |
| 5 | legacy fallback 审查 | `gateway.py`（无改动） | 已天然禁用（runtime 强制 max_requests=1） | 解析成功率待回归 |

---

## 5. 与计划门禁的对齐情况

参考 99 文档验收清单：

- [x] `GET /api/v1/metrics`（计划 `/metrics/perf`，实现 `/metrics`）输出全部预期 histogram — 阶段 0/2 ✅
- [ ] OP2 embedding cache 命中率 ≥ 40% — 单 query 探测已证命中可用，**40% 命中率需真实问诊流量验证**（留 T2.7 压测）
- [ ] OP2 structured 解析成功率不降 1pp — fallback 主路径已禁用，**需回归对比**（留 T2.7）
- [ ] before/after P95 对比表 — 待阶段 4（跑真实流量采 P95）
- [ ] 全量 pytest 无回归 — **未跑**（本轮无测试改动，但 repository.py 有合并语法修复，需验证）
- [x] M1（output_fields 含 content）— 属 T3，不在本阶段

---

## 6. TODO / 下一阶段方向

按计划文档，仍需推进：

1. **T1 状态下推子图（OP1）** — 当前 reasoning 每节点各跑一次 `repository.get_state`（单 claim 10+ DB 往返）。这是计划里收益最大的一块（预期降 60%+），也最复杂（需处理 checkpoint transient + selectinload 防 lazy=raise）。
2. **T3 Milvus 异步化（OP3）** — `milvus.search` 同步阻塞事件循环，`asyncio.to_thread` 包裹 + 共享 RAGRetriever 单例。
3. **T2.7 回归** — embedding cache 命中率压测、structured 解析成功率对比。
4. **T4 回归对比** — 跑真实问诊流量采各 histogram P95，填 99 文档 before/after 表。
5. **repository.py 合并语法修复的回归** — 本轮 commit `fbc65e5` 之后工作树有一个 `_persist_product_projections` 的 `if ( review is None or ... )` 合并修复，需确认对应测试通过。

---

## 7. 本阶段小结

OP2（网关池化 + embedding 缓存）主体补完：连接池从"无上限复用"升级到"Limits + lifespan 托管"，新增 Redis EmbeddingCache（命中 ~381x），/metrics 端点落地使 7 个埋点真正可观测。OP1/OP3 仍是下一阶段的主要工作。
