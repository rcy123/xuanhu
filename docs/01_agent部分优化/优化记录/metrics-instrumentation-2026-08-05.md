# Optimization: Prometheus Per-Stage Histogram Instrumentation

## Date
2026-08-05

## Commit
bcf4070

## Changes Summary
Added Prometheus histogram instrumentation across 3 key subsystems: **RAG retrieval**, **Model Gateway**, and **Graph Runner**. Created a centralized metrics module (`app/core/metrics.py`) with a unified `measure()` async context manager.

## Metric Registry

| Metric Name | Subsystem | Labels | Purpose |
|---|---|---|---|
| `xuanhu_rag_vector_search_seconds` | RAG | (none) | Milvus vector search (embed + search) |
| `xuanhu_rag_fulltext_search_seconds` | RAG | (none) | PG fulltext search |
| `xuanhu_rag_backfill_seconds` | RAG | (none) | Content snippet backfill PG round-trip |
| `xuanhu_rag_embed_seconds` | RAG | (none) | Embedding generation gateway call |
| `xuanhu_gateway_chat_seconds` | Gateway | host, route_profile | LLM chat/chat_structured request |
| `xuanhu_gateway_embed_seconds` | Gateway | (none) | Embedding API request |
| `xuanhu_graph_node_seconds` | Runner | subgraph, node | Per-node duration in LangGraph astream |

## Bucket Profile
All histograms: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]` seconds — covers ingest (100ms–30s), LLM gateway (1s–30s), and lightweight DB reads (10ms–1s).

## Design Decisions
1. **No high-cardinality labels** — session_id, patient_ref, query text deliberately excluded to prevent label explosion.
2. **Graceful degradation** — `prometheus_client` import is optional; all histograms become no-ops when the package is not installed.
3. **Unified context manager** — `measure(stage, labels=None)` yields a `MeasureResult` with `.seconds` set on exit; callers can log elapsed time if desired.
4. **`as m` variable removed** — since no caller currently reads `m.seconds`, the result binding was dropped for cleanliness. Callers can add `as m` back when needed.
5. **Graph node emit counts, not durations** — `astream_events` lacks a wall-clock per-node start timestamp (the stream yields events as they arrive); emits `1` per node as a proxy.

## Files Modified
- `app/core/metrics.py` (new, +164 lines)
- `app/core/gateway.py` (+10/-8 lines)
- `app/rag/retriever.py` (+11/-8 lines)
- `app/agent_runtime/runner.py` (+15/-2 lines)