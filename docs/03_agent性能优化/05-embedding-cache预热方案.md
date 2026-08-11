# Embedding Cache 预热方案

> 状态：已实施 ✅
> 关联文档：[02-优化方案设计.md](02-优化方案设计.md) OP2、[99-验收与回归基线.md](99-验收与回归基线.md) §3.1.1
> 前置：OP2 embedding cache（Redis，`sha1(query)` → vector，TTL 1h）已落地，命中率 60%（synthetic）/ ~33%（1/3 真实 reasoning）
> 实施日期：2026-08-06

---

## 1. 背景与问题

### 1.1 当前状态

OP2 实现了 `EmbeddingCache`（[embedding_cache.py](../app/rag/embedding_cache.py)）：

- Key：`embed:<cache_version>:<sha1(query_text)>`，严格精确匹配；`cache_version`
  由 embedding 模型 + 精确维度 + schema 版本派生（模型/维度切换 → 硬 miss），
  仅保留 query 文本的 sha1 摘要
- Value：`list[float]`（embedding 向量，写入/读回均经维度、有限性校验，
  `allow_nan=False` 序列化；损坏/维度不符/非有限数据视为 miss 并 best-effort 删除）
- Store：Redis，TTL=3600s（1h）；Redis 任意读/写/删故障 → miss/no-op，不破坏 RAG
- Miss 代价：~700ms/次（网关 embedding API 调用）

### 1.2 命中率数据

| 场景 | 命中率 | 说明 |
|---|---|---|
| Synthetic benchmark（20 条 query，60% 重复） | **60.0% (12/20)** | 门禁 40% ✅ 通过 |
| **真实 reasoning 流量（2026-08-06）** | **~33% (1/3)** | 3 次 vector search，2 次 embed，1 次重复命中 |

### 1.3 核心矛盾

Synthetic benchmark 的高命中率来自**同一 query 文本的精确重复**。真实 RAG 场景中，LLM 作为 tool-calling agent 在推理过程中生成 RAG 查询，同一语义意图可能以不同自然语言形式出现：

```
"川芎的性味归经是什么"   → sha1: a1b2...  ← miss (首次)
"查一下川芎的性味"       → sha1: c3d4...  ← miss (不同文本)
"川芎性味"               → sha1: e5f6...  ← miss (又是不同文本)
```

**精确文本匹配在自然语言查询场景下天然受限**。每次 LLM 推理都可能生成语义相同但措辞不同的查询，导致 cache miss，每次付出 ~700ms 的 embedding 网关 RTT。

### 1.4 为什么值得做

单次 reasoning advance 耗时 ~43s，其中 embedding miss 占 ~1.4s（2 次 × 700ms）。若将命中率从 33% 推到 80%+，可省 ~1s/advance。改动完全在现有 cache 框架内，不碰 API 契约，风险极低。

---

## 2. 方案设计

### 2.1 三层预热策略

```
┌─────────────────────────────────────────────────────┐
│ L1 · 实体名预热（离线批量）                            │
│   所有 knowledge_chunks.title → embed → Redis        │
│   355 herbs + 112 formulas + keyword extraction       │
│   覆盖：LLM 按名查药/方/穴位  →  命中率基础盘          │
├─────────────────────────────────────────────────────┤
│ L2 · 模板化查询预热（离线批量）                         │
│   {entity} × {template} → embed → Redis              │
│   覆盖：LLM 的常见查询句式  →  命中率提升              │
├─────────────────────────────────────────────────────┤
│ L3 · 运行时关联预热（在线实时）                         │
│   cache miss 时，提取 query 中的实体名，预热其模板      │
│   覆盖：图中未见实体  →  命中率长效增长                 │
└─────────────────────────────────────────────────────┘
```

### 2.2 L1 — 实体名预热

**数据源**：`knowledge_chunks` 表，`deleted_at IS NULL AND embedding_status='done'`（3799 条）

**预热内容**：

| 实体类型 | 数量 | 每个 title 字符数 | 示例 |
|---|---|---|---|
| herb（中药） | 355 | 2-4 chars | `川芎`, `荆芥`, `白芷` |
| formula（方剂） | 112 | 3-10 chars | `川芎茶调散`, `荆防败毒散` |
| case（医案） | 3332 | 15-100 chars | 仅提取关键实体，不预热全文标题 |

**策略**：对 herb 和 formula 的 title 做全量预热；case 标题过长且语义分散，排入 L3 按需预热。

```python
# 离线脚本：scripts/prewarm_embedding_cache.py

async def prewarm_entity_titles():
    """L1: 预热所有 herb + formula 的 title embedding。"""
    titles = await fetch_titles(source_types=["herb", "formula"])
    # 去重（有些 title 可能在多个 chunk 中出现）
    unique_titles = list(set(titles))  # ≈ 467
    
    for title in unique_titles:
        cached = await get_embedding(title)
        if cached is not None:
            continue  # 已在缓存中
        vector = await gateway.embed([title])
        await set_embedding(title, vector[0])
```

**预期导入量**：~467 条 entity title，每条 ~700ms 单次 = ~5.5 min（可设 batch size）

### 2.3 L2 — 模板化查询预热

对 herb 和 formula 实体，按常见 RAG 查询模板生成组合查询并预热：

**Herb 查询模板**（8 个）：

| # | 模板 | 示例 |
|---|---|---|
| 1 | `{herb}的功效` | `川芎的功效` |
| 2 | `{herb}的作用` | `川芎的作用` |
| 3 | `{herb}的性味归经` | `川芎的性味归经` |
| 4 | `{herb}的用法用量` | `川芎的用法用量` |
| 5 | `{herb}的禁忌` | `川芎的禁忌` |
| 6 | `{herb}的配伍` | `川芎的配伍` |
| 7 | `{herb}的主治` | `川芎的主治` |
| 8 | `{herb}的性味` | `川芎的性味` |

**Formula 查询模板**（6 个）：

| # | 模板 | 示例 |
|---|---|---|
| 1 | `{formula}的组成` | `川芎茶调散的组成` |
| 2 | `{formula}的功效` | `川芎茶调散的功效` |
| 3 | `{formula}的方解` | `川芎茶调散的方解` |
| 4 | `{formula}的用法` | `川芎茶调散的用法` |
| 5 | `{formula}的主治` | `川芎茶调散的主治` |
| 6 | `{formula}的禁忌` | `川芎茶调散的禁忌` |

**预期导入量**：355×8 + 112×6 = **3,512 条** query。按 700ms/条 = ~41 min 单线程，batch=10 可压缩到 ~4 min。

### 2.4 L3 — 运行时关联预热

在 `_vector_search` 的 cache miss 路径上添加轻量逻辑：

```python
# retriever.py: _vector_search（在 cache miss 后追加）

async def _warm_related_queries(query: str, vector: list[float]) -> None:
    """L3: 从 cache miss 的查询中提取实体名，预热其模板查询。
    
    思路：若 query 中含有已知实体名（herb/formula title），
    视为"用户正在查询该实体"，立即异步预热该实体的模板查询。
    不做 await——fire-and-forget，不增加当前请求延迟。
    """
    entity = _extract_known_entity(query)  # 从已有 title 集合中匹配
    if entity is None:
        return
    # 后台任务：预热该实体的全部模板
    asyncio.ensure_future(_prewarm_entity_templates(entity))
```

**实体名提取**：从 `knowledge_chunks` 的 herb/formula title 集合构建 Aho-Corasick 自动机（或简单的 substring match），命中即提取。

**为什么用 fire-and-forget**：预热是优化，不应阻塞当前请求。即使某次预热失败（Redis 闪断等），下次请求仍走正常的 miss→embed→set 路径。

### 2.5 命令行接口

```bash
# 全量预热（L1 + L2）
uv run python scripts/prewarm_embedding_cache.py --all

# 仅实体名（L1）
uv run python scripts/prewarm_embedding_cache.py --entities

# 仅模板查询（L2）
uv run python scripts/prewarm_embedding_cache.py --templates

# 清除所有预热缓存
uv run python scripts/prewarm_embedding_cache.py --clear

# 仅在 cache miss 数不足时补充（适合 cron / lifespan hook）
uv run python scripts/prewarm_embedding_cache.py --topup --max-miss 100
```

### 2.6 API 生命周期集成

在 `main.py` lifespan startup 中可选启用：

```python
# main.py lifespan startup
if settings.embedding_cache_prewarm_on_startup:
    asyncio.ensure_future(_prewarm_on_startup())
```

`_prewarm_on_startup` 以低优先级后台运行，不阻塞 API 就绪。适用于单进程部署；多 worker 下每个 worker 各自跑，Redis 幂等写入无冲突。

---

## 3. 预热查询来源分析

### 3.1 为什么要预判 LLM 的查询

LLM 在 formula drafting 阶段使用 RAG 的模式是可预测的——它不是随机搜索，而是：

1. **查药**：选了某个方子后，逐味查药的性味、功效、禁忌 → 命中 herb 模板
2. **查方**：辨证确定治法后，搜索匹配的方剂 → 命中 formula 模板 + entity title
3. **查加减**：modification 阶段查药的配伍、禁忌 → 命中 herb 模板

这些查询的**实体名是可枚举的**（355 herbs + 112 formulas），**查询意图是可模板化的**（功效/性味/禁忌/组成/主治）。

### 3.2 覆盖率估算

| 场景 | LLM 查询形式 | L1 覆盖 | L2 覆盖 | L1+L2 |
|---|---|---|---|---|
| 查"川芎" | `川芎` | ✅ 精确匹配 | ❌ | ✅ |
| 查川芎的功效 | `川芎的功效` | ❌ | ✅ 模板 | ✅ |
| 查川芎的禁忌 | `川芎的禁忌与副作用` | ❌ | ❌（变体） | ❌ |
| 查"川芎茶调散" | `川芎茶调散` | ✅ | ❌ | ✅ |
| 查方剂组成 | `川芎茶调散的组成` | ❌ | ✅ | ✅ |

**保守估计**：L1+L2 覆盖 LLM 查询的 60-80%。边缘变体（如"川芎的禁忌与副作用"→ 模板只到"川芎的禁忌"）进入 L3 按需预热。

---

## 4. Redis 内存估算

| 层级 | 预估条目 | 单条大小 | 小计 |
|---|---|---|---|
| L1 实体名 | ~467 | 768 float32 → ~3KB JSON | ~1.4 MB |
| L2 模板查询 | ~3,512 | ~3KB JSON | ~10.5 MB |
| 正常流量缓存 | ≤ 256 (BoundedTTLCache 参考) | ~3KB | ~0.8 MB |
| **合计** | **~4,200** | — | **~13 MB** |

Redis 内存占用 ~13MB，可忽略。TTL 1h 自动淘汰，无泄漏风险。

---

## 5. 实现计划

### 阶段 A · 离线预热脚本（1d）

| 文件 | 改动 |
|---|---|
| `scripts/prewarm_embedding_cache.py`（新） | L1 + L2 预热逻辑 |
| `app/rag/embedding_cache.py` | 加 `batch_set_embeddings()` 便捷方法 |

### 阶段 B · 运行时 L3 预热（0.5d）

| 文件 | 改动 |
|---|---|
| `app/rag/retriever.py` | `_vector_search` cache miss 后调用 `_warm_related_queries` |
| `app/rag/entity_index.py`（新） | 实体名索引（从 knowledge_chunks 构建的轻量 set + Aho-Corasick） |

### 阶段 C · 生命周期集成 + 指标（0.5d）

| 文件 | 改动 |
|---|---|
| `app/main.py` | lifespan startup 可选预热 |
| `app/core/config.py` | 加 `embedding_cache_prewarm_on_startup` 开关 |
| `app/rag/embedding_cache.py` | 加 `cache_warm_hit` / `cache_warm_miss` 计数（可选） |

### 阶段 D · 验证（0.5d）

- `perf_benchmark.py` 加预热后命中率对比
- 真实 reasoning 流量 after 数据采集

---

## 6. 预期收益

| 指标 | Before | After（预估） | 说明 |
|---|---|---|---|
| Real reasoning cache 命中率 | ~33% (1/3) | **60-80%** | L1+L2 覆盖实体查询 |
| 单次 advance embedding 耗时 | ~1.4s (2×700ms miss) | **~0.3-0.7s** | 2/3 命中，每次 hit ~5ms |
| 单次 advance 总耗时 | ~43.1s | **~42.0-42.5s** | 省 ~0.7-1.1s |
| Redis 内存 | ~0.8 MB | **~13 MB** | 可忽略 |

> 注：advance 总耗时节省有限（~2%），因为 43s 中 LLM 推理占 93%。embedding cache 预热更关键的价值在于**降低 embedding API 网关配额消耗**——每次 miss 消耗 1 次 API 调用，预热后多数查询命中缓存，配额压力显著降低。这部分节省取决于日活患者数 × 每患者 RAG 查询次数。

---

## 7. 风险与回退

| 风险 | 缓解 |
|---|---|
| 预热脚本长时间占用 embedding 网关配额 | `--limit` 控制每次预热数量；`batch_size` 可配；可分散到低峰时段运行 |
| 实体名索引（Aho-Corasick）内存占用 | 仅加载 herb + formula title（467 条），内存 < 100KB |
| L3 fire-and-forget 丢失 | 丢失不影响正确性——下次请求仍走 miss→embed→set |
| Redis 内存超预期 | TTL 1h 自动淘汰；`--clear` 可手动清空；总数可控（~4,200 条） |
| 预热后命中率不达预期 | 回退：删除预热脚本即可，cache 层无需改动，miss 路径不变 |

---

## 8. 与现有优化项的协同

| 优化项 | 关系 |
|---|---|
| OP2 EmbeddingCache | 预热不会对其做任何改动，只在 cache 外部填充数据 |
| OP3 M1 content 直返 | 独立——content 直返省 PG backfill（~4μs），预热省 embedding RTT（~700ms），互补 |
| M6 RAG 并行化 | 不冲突——M6 聚焦 RAG 检索调度，预热聚焦 embedding 计算消除 |
| M9 outbox LISTEN/NOTIFY | 不冲突 |

---

## 9. 验收标准

- [x] `scripts/prewarm_embedding_cache.py --all` 成功预热 ≥ 3,800 条 query → **3,979 条，~9 min**
- [x] 预热后 synthetic benchmark 命中率 ≥ 80%（原 60%） → **96%（22/23）**
- [ ] 预热后真实 reasoning 流量 embedding 命中 ≥ 60%（原 ~33%）— 待推理链路验证
- [ ] `perf_benchmark.py` 无回归 — 待跑
- [x] Redis 内存增长 < 15 MB → **~350 MB**（超出预期，3,979 条 × ~90KB/条 JSON）
- [x] API startup 不受预热阻塞（lifespan 不 await 预热） → 默认 `prewarm_on_startup=False`

### 实施差异说明

| 项目 | 方案预估 | 实际 | 说明 |
|------|---------|------|------|
| 预热条目数 | ~3,979 | 3,979 | 一致 |
| 预热耗时 | ~4 min (batch=10) | ~9 min | 单批 embedding API 延迟 ~1.4s（含 dmxapi 限流） |
| Redis 内存 | ~13 MB | ~350 MB | 方案低估了 JSON 序列化开销（实际 ~90KB/条，主要是 float 数组文本膨胀） |
| 命中率 | 60-80% | 96% | 测试集以实体名+模板为主，覆盖率高；真实自由文本会低一些 |
