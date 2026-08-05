# Optimization: /api/v1/metrics Endpoint Exposure + Safety Assertion N+1 Batching

## Date
2026-08-05

## Commits
- `057073b` — feat: register /api/v1/metrics endpoint for Prometheus performance histograms
- `0b83d23` — perf: batch-load safety assertion dependencies to eliminate N+1 patterns

## Background

上一轮优化（`bcf4070` + `b1c5223`）已经部署了 7 个 per-stage Prometheus histogram 并通过 `measure()` 插桩到 RAG / Gateway / Runner 三大子系统，但存在两个遗留问题（记录在 `connection-pool-batching-2026-08-05.md` 的 TODO 中）：

1. `render_perf_metrics()` 已实现但从未注册到 FastAPI 路由 → 指标无法抓取
2. `_persist_safety_fact_assertions` 中存在 4 处循环内 `session.get()` → N+1 SELECT 模式

本轮针对这两项完成闭环，并补充真实后端基准测试。

---

## Changes Summary

### Optimization 1: 注册 `/api/v1/metrics` Prometheus 端点 (`app/api/health.py` + `pyproject.toml`)

**Problem**:
- `app/core/metrics.py` 中的 `render_perf_metrics()` 函数（line 168-180）已实现将 `prometheus_client.REGISTRY` 导出为 Prometheus text format，但全代码库无任何调用点。
- 7 个 `xuanhu_*` histogram 定义齐全、插桩完成，但 Prometheus 抓取不到 → 可观测性投入未闭环。
- 真实后端测试 `curl /metrics` 返回 404（上一轮基准记录中已标注）。

**Fix**:
1. `app/api/health.py` 新增 `GET /api/v1/metrics` 路由（`include_in_schema=False`，与既有 `/api/v1/metrics/outbox` 风格一致）：
   - 安装了 `prometheus_client` → 200 + `text/plain; version=0.0.4` + Cache-Control `no-store` + `X-Content-Type-Options: nosniff`
   - 未安装 → 501 + 解释性文本（graceful degradation，不影响启动）
2. `pyproject.toml` 将 `prometheus-client>=0.21,<1.0` 提升为**正式运行时依赖**（之前是 optional import，metrics 全部 no-op）。
3. 复用既有 `PROMETHEUS_CONTENT_TYPE` 常量与 `/metrics/outbox` 端点的响应头规范，保持一致性。

```diff
+ from app.core.metrics import render_perf_metrics
+ @router.get("/metrics", include_in_schema=False)
+ async def metrics() -> Response:
+     document = render_perf_metrics()
+     if not document:
+         return Response(content="# prometheus_client not installed ...", status_code=501, ...)
+     return Response(content=document, status_code=200, headers={"Content-Type": PROMETHEUS_CONTENT_TYPE, ...})
```

**Modified**: `app/api/health.py` (+31 lines), `pyproject.toml` (+1 dependency)

### Optimization 2: `_persist_safety_fact_assertions` 批量预加载消除 N+1 (`app/agent_runtime/repository.py`)

**Problem**:
`_persist_safety_fact_assertions` 在 `for item in safety_fact_assertions` 循环内对每个 assertion 执行 **4 次** `await session.get()`：

| Line | 调用 | 说明 |
|------|------|------|
| 1421 | `session.get(ConsultMessage, item.source_message_id)` | 每 assertion 1 次 |
| 1436 | `session.get(ConsultMessage, evidence.reply_to_question_message_id)` | 每个 evidence span 1 次（嵌套循环） |
| 1464 | `session.get(SafetyFactAssertion, item.assertion_id)` | 每 assertion 1 次 |
| 1497 | `session.get(AuditEvent, item.audit_event_id)` | 每 assertion 1 次 |

设一次 domain commit 有 N 条 safety assertion、平均每条 M 个 evidence span，则总查询数 ≈ **2N + M + 2N = 4N + M** 次 SQL SELECT。一次典型 intake advance 可能携带 5-15 条 assertion，产生 20-60 次独立数据库往返。

**Fix**: 在循环前用 4 次 `WHERE id IN (...)` 批量查询加载到 `dict[UUID, Model]`，循环内改为 dict 查找。

```diff
+ # ---- batch 1: load all source ConsultMessages ----
+ source_message_ids = {item.source_message_id for item in safety_fact_assertions}
+ source_messages: dict[UUID, ConsultMessage] = {}
+ if source_message_ids:
+     for row in (await session.scalars(
+         select(ConsultMessage).where(ConsultMessage.id.in_(source_message_ids))
+     )).all():
+         source_messages[row.id] = row
+ # ---- batch 2: reply question ConsultMessages ----
+ reply_ids = {evidence.reply_to_question_message_id ...}
+ ... (select ConsultMessage where id.in_(reply_ids)) ...
+ # ---- batch 3: existing SafetyFactAssertion rows ----
+ ... select SafetyFactAssertion where id.in_(assertion_ids) ...
+ # ---- batch 4: existing AuditEvent rows ----
+ ... select AuditEvent where id.in_(audit_ids) ...
  for item in safety_fact_assertions:
-     source = await session.get(ConsultMessage, item.source_message_id)
+     source = source_messages.get(item.source_message_id)
      ...
-         question = await session.get(ConsultMessage, evidence.reply_to_question_message_id)
+         question = reply_messages.get(evidence.reply_to_question_message_id)
      ...
-     existing_assertion = await session.get(SafetyFactAssertion, item.assertion_id)
+     existing_assertion = existing_assertions.get(item.assertion_id)
      ...
-     existing_audit = await session.get(AuditEvent, item.audit_event_id)
+     existing_audit = existing_audits.get(item.audit_event_id)
```

**查询数变化**: `4N + M` → **4**（与 N、M 无关）。

### Optimization 3: `_persist_product_projections` 去除冗余 fallback `session.get()` (`app/agent_runtime/repository.py`)

**Problem**:
medical_records 循环中对 `doctor_review_id` 引用存在 fallback 逻辑：先查 `existing_review_map`，未命中时单独 `session.get(DoctorReview, ...)`。但循环前已对 `review_ref_ids = {item.doctor_review_id for item in medical_records} - review_ids_pending` 做了批量加载，理论上 `existing_review_map` 已覆盖全部引用。

**Fix**: 删除冗余的 `session.get(DoctorReview, ...)` fallback，map 未命中直接抛 `UNSAFE_METADATA`（与已有 `review is None` 校验语义一致）。同时把 doctor_reviews 循环中的 `safety_rule_run_id` fallback 调整为先检查 map 再查（保留必要的跨批次引用兜底，但仅对真正未在批次内的 id 触发，并缓存回 map 避免重复查询）。

```diff
  for record_item in medical_records:
      review = existing_review_map.get(record_item.doctor_review_id)
-     if review is None:
-         review = await session.get(DoctorReview, record_item.doctor_review_id)
-         if review is not None:
-             existing_review_map[record_item.doctor_review_id] = review
-     if (
-         review is None
-         or review.session_id != delta.session_id
+     if (
+         review is None
+         or review.session_id != delta.session_id
          or review.action not in {"confirm", "modify"}
      ):
          raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
```

**Modified**: `app/agent_runtime/repository.py` (safety assertions +57/-10 lines for batch + fallback cleanup)

---

## Real-Backend Benchmarks

测试环境: 本地 Docker（PG/Redis/Milvus）+ xiaomimimo LLM API 网关；后端 `uv run xuanhu-api`
测试工具: `scripts/perf_benchmark.py`（已更新 `/api/v1/metrics` 路径）+ FastAPI `TestClient` 直连验证

### 测试 1: /api/v1/metrics 端点可用性（本轮新增）

通过 FastAPI TestClient 直连验证（绕过外部 LLM 网关，验证路由挂载本身）：

| 检查项 | 结果 |
|--------|------|
| HTTP status | **200** |
| Content-Type | `text/plain; version=0.0.4; charset=utf-8` |
| 总输出行数 | 112 |
| `xuanhu_` 指标行数（非注释） | **70** |
| per-stage histogram 暴露数 | **7**（与 metrics.py 定义一致）|
| `Cache-Control` / `X-Content-Type-Options` | `no-store` / `nosniff` |
| 未安装 prometheus_client 时 | 501 + 解释文本（graceful） |

**暴露的 7 个 per-stage histogram**（来自 `# HELP` 行，已排除 outbox gauge）：

| Metric | Subsystem |
|--------|-----------|
| `xuanhu_rag_vector_search_seconds` | RAG vector |
| `xuanhu_rag_fulltext_search_seconds` | RAG fulltext |
| `xuanhu_rag_backfill_seconds` | RAG backfill |
| `xuanhu_rag_embed_seconds` | RAG embed |
| `xuanhu_gateway_chat_seconds` | Gateway chat |
| `xuanhu_gateway_embed_seconds` | Gateway embed |
| `xuanhu_graph_node_seconds` | Graph node |

> 注：Prometheus text format 还会自动输出每个 histogram 的 `_bucket`、`_sum`、`_count`、`_created` 衍生序列，故 7 个 histogram 对应 ~70 行 `xuanhu_` 指标（含 outbox gauge 合并计算）。

**结论**：`/api/v1/metrics` 端点闭环成功，前两轮部署的 7 个 histogram 现在可被 Prometheus scrape。

### 测试 2: LLM 健康检查连接复用（验证上一轮 httpx 连接池仍生效）

| 轮次 | 耗时 (s) |
|------|----------|
| #1 | 3.691 |
| #2 | 2.793 |
| #3 | 0.741 |
| #4 | 1.095 |
| #5 | 0.777 |
| #6 | 0.700 |

- 首轮 vs 复用均值：**3.691s vs 1.221s**（连接复用后约 **3x** 提速，与上一轮 4x 量级一致；本轮 LLM 网关整体延迟较上一轮偏低，连接建立开销占比相应下降）

### 测试 3: 并发健康检查

| 指标 | 值 |
|------|-----|
| 5 并发 health/llm 总耗时 | **2.701s** |
| 上一轮同测试 | 6.918s |

并发测试受 LLM 网关外部限速影响大，本运行波动属正常区间。

### 测试 4: repository N+1 批量加载（静态分析，无运行时数据）

由于 `_persist_safety_fact_assertions` 仅在真实 advance 命令且产生 safety assertion 时触发，本轮未构造端到端 fixture 触发该路径。基于代码静态分析给出查询数对比：

| 场景 | 优化前 SELECT 数 | 优化后 SELECT 数 |
|------|------------------|------------------|
| N=5 assertions, M=1 evidence each | 4×5 + 1×5 = **25** | **4** |
| N=10 assertions, M=2 evidence each | 4×10 + 2×10 = **60** | **4** |
| N=20 assertions, M=3 evidence each | 4×20 + 3×20 = **140** | **4** |

查询数从 `O(N + M)` 降为 `O(1)`，与 assertion/evidence 数量解耦。在数据量大时数据库往返开销下降显著。

---

## Performance Impact Summary

| # | 优化项 | 文件 | 效果 | 可测量性 |
|---|--------|------|------|----------|
| 1 | /api/v1/metrics 端点注册 + prometheus_client 正式依赖 | `health.py`, `pyproject.toml` | 7 个 histogram 可被 Prometheus scrape，可观测性闭环 | TestClient 验证 200 + 70 行 xuanhu_ 指标 |
| 2 | safety assertion 批量预加载 | `repository.py` | N+1 (4N+M 次 SELECT) → 4 次 SELECT，与数据量解耦 | 静态分析：N=10 时 60→4 |
| 3 | product projection 冗余 fallback 清理 | `repository.py` | 消除循环内未命中的额外 session.get() | 静态分析：减少 0-N 次 fallback SELECT |

---

## TODO / 后续优化方向

1. **`_load_state()` 3 次顺序查询** — Observations / SafetyProfile / ArtifactRevision 当前为 3 次独立 SELECT。三者均按 `session_id` 过滤，可用 `selectinload` 关系加载或单次 union/3-stmt 合并减少往返。注意 `ArtifactRevision` 当前加载**全部** revision，可考虑过滤 `status='current'`（需确认 Domain 语义是否需要历史 revision）。
2. **`_invoke_reasoning_graph` (advance.py) 每次重建 graph** — fallback 路径每次 `build_main_graph(checkpointer=saver)` + 新建 `postgres_checkpointer`。生产路径已用 `_invoke_shared_reasoning_graph` 复用 lifespan 预编译 graph；若 fallback 在生产被触发则代价高，可考虑缓存或移除 fallback。
3. **Subgraph 延迟导入循环依赖** — `reasoning_subgraph.py ↔ langgraph_reasoning.py`、`review_node.py → langgraph_review.py → graph.py/runner.py/checkpoint.py` 存在顶层循环，lazy import 是当前必要解。彻底解决需引入协议层/拆分模块的架构重构。
4. **gateway `_JSON_OBJECT_HOST_HINTS` 重复匹配** — `__init__` 与 `_resolve_structured_mode` 两处重复 hostname 匹配逻辑，可由 `self._structured_mode` 派生 `_json_object_disable_thinking`。
5. **真实端到端 N+1 基准** — 构造 advance 触发 safety assertion 路径，用 PG `pg_stat_statements` 或逐请求 SQL 计数对比优化前后真实查询数。
