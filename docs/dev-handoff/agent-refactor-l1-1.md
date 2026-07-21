# L1-1 交接：LangGraph 与 PG Checkpointer 兼容性 Spike

## 结论

L1-1 已完成。当前 Python 3.12、Pydantic 2、FastAPI async、Windows 开发环境、psycopg v3 与 `langgraph-checkpoint-postgres` 的最小兼容性验证通过。

本任务只引入依赖、锁文件和隔离 Spike 测试；未实现 MainGraph、GraphState、Runner、业务 Agent、生产路由或 Legacy 改造。

## 交付内容

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | 新增 `langgraph`、`langgraph-checkpoint-postgres`、`psycopg`、`psycopg-pool` 依赖 |
| `uv.lock` | 锁定 LangGraph、Postgres checkpointer、psycopg v3 及传递依赖 |
| `tests/test_langgraph_compatibility_spike.py` | 验证最小异步图、Pydantic 2 模型、InMemorySaver、FastAPI async 请求内调用 |
| `tests/test_langgraph_postgres_checkpoint_spike.py` | 验证 AsyncPostgresSaver setup、PG checkpoint 写读、重建实例读取、thread 隔离、版本化 thread 隔离、interrupt/resume |
| `test_agent/test_l0_1_contract.py` | AR-B-003 返工中恢复，内容与基线一致 |

## 关键发现

- Windows 下 psycopg v3 async 连接需要 `WindowsSelectorEventLoopPolicy`；Spike 只在测试模块内切换事件循环策略，不改变生产运行时。
- 当前 LangGraph 1.2.x 中，root graph 的 `graph.aget_state(config)` 会把 `checkpoint_ns` 解释为 subgraph namespace。L1-1 已改用版本化 `thread_id` 验证隔离：`{graph_version}:{session_id}`。
- L1-2/L1-3 引入生产 GraphRunner 和 checkpointer 时，需要重新确认 root graph、subgraph 与生产 namespace 的最终策略。

## 验收证据

```powershell
uv run pytest tests/test_langgraph_compatibility_spike.py -q -rs
# 5 passed

$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest tests/test_langgraph_postgres_checkpoint_spike.py -q -rs
# 7 passed

uv run pytest test_agent/test_l0_1_contract.py -q -rs
# 131 passed

uv run pytest -q -rs
# 966 passed, 1 xfailed, 2 warnings

uv run ruff check .
# All checks passed

uv run mypy app
# Success: no issues found in 72 source files

uv lock --check
# Resolved 83 packages

git diff --check
# passed
```

## 边界确认

- 未接入业务 Agent。
- 未创建 `app/agent_runtime/` 或 `app/agents/langgraph/` 生产实现。
- 未修改 `Supervisor`、`MessageService`、`ReviewService`、`RecoveryService`。
- 未改变 `AGENT_RUNTIME_VERSION` 默认值。
- 未引入真实模型、Redis 或患者数据依赖。

## 后续建议

L1-2 可以基于本 Spike 继续定义 `XuanhuGraphState`、最小 MainGraph 和命令路由；生产 checkpointer namespace 与跨实例恢复细节留给 L1-3 验收。
