# L1-4 交接：GraphRunner、超时取消与事件转换

> 任务：L1-4 GraphRunner、超时取消与事件转换
> 交付时间：2026-07-10
> 状态：验收通过，未提交 git commit
> 对应返工：无（首轮交付）

## 1. 变更文件清单

### 1.1 新增文件

| 文件 | 用途 |
|------|------|
| `app/agent_runtime/runner.py` | `GraphRunner` 封装：提供 `ainvoke` / `astream_events`，统一处理总超时、取消传播、脱敏错误归一化、运行前 config/state 校验。 |
| `app/agent_runtime/events.py` | LangGraph 事件到版本化业务事件的纯转换层：定义 `XuanhuRunEvent` Schema、`EVENT_SCHEMA_VERSION`、转换/构造工具函数。 |
| `tests/test_l1_4_graph_runner.py` | 32 项 L1-4 专项测试，覆盖 invoke、stream、事件 schema、超时、取消、错误、错配拒绝、事件顺序、工具函数。 |

### 1.2 修改文件

| 文件 | 修改内容 |
|------|----------|
| `app/agent_runtime/errors.py` | 新增 `GraphRunnerError` / `GraphRunnerTimeoutError`（继承 `GraphStateError`），用于 runner 错误归一化。 |
| `test_agent/test_l0_1_contract.py` | L0-1 范围检查允许 `runner.py` 和 `events.py` 存在。 |

## 2. 精确测试结果

所有必跑命令均在本地通过。

```bash
$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest tests/test_l1_4_graph_runner.py -q -rs
# 34 passed

uv run pytest -q -rs
# 1077 passed, 1 xfailed, 2 warnings

uv run ruff check .
# All checks passed!

uv run mypy app --no-incremental
# Success: no issues found in 82 source files

uv run mypy app --no-incremental --python-version 3.11
# Success: no issues found in 82 source files

uv lock --check
# Resolved 83 packages

git diff --check
# passed
```

### 2.1 测试覆盖矩阵

| 测试类 | 数量 | 覆盖目标 |
|--------|------|----------|
| `TestNormalInvoke` | 4 | 正常 `ainvoke`（message/advance/review/recover）、无超时、零超时 |
| `TestNormalStream` | 3 | 正常 `astream_events`、首尾事件类型 |
| `TestEventSchema` | 5 | JSON 序列化、版本、时间戳、不含敏感字段、node_name |
| `TestTimeout` | 2 | `ainvoke` 总超时、`astream_events` 超时后 `graph_failed` 事件 |
| `TestCancellation` | 2 | 外部 `CancelledError` 在 `ainvoke` / `astream_events` 中正确传播 |
| `TestErrorNormalization` | 4 | 执行异常归一化、stream `graph_failed` 事件，以及 API key、token、prompt、身份文本、DB URL 在异常消息、异常链和事件中均不泄露 |
| `TestConfigMismatch` | 4 | session_id 错配、graph_version 错配、stream 错配、错配不产生 checkpoint |
| `TestEventOrdering` | 3 | 顺序 `graph_started → node_completed → graph_completed`、route、run_id |
| `TestEventConversion` | 7 | `convert_updates_chunk` 与事件构造函数 |

## 3. 事件 Schema（XuanhuRunEvent）

版本：`EVENT_SCHEMA_VERSION = "1"`

所有事件均为 `TypedDict(total=False)`，可被 `json.dumps` 序列化，且不包含敏感字段。

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_version` | `str` | Schema 版本，固定为 `"1"` |
| `event_type` | `str` | `graph_started` / `node_completed` / `graph_completed` / `graph_failed` |
| `timestamp` | `str` | ISO 8601 UTC 时间戳 |
| `node_name` | `str` | 仅 `node_completed`：产生事件的节点名 |
| `route` | `str` | 从 `state_delta["route"]` 提取的安全字段 |
| `run_id` | `str` | 从 `state_delta["run_id"]` 提取的安全字段 |
| `command` | `str` | 从 `state_delta["command"]` 提取的安全字段 |
| `error_code` | `str` | 仅 `graph_failed`：脱敏错误码（如 `RUNNER_TIMEOUT`） |

### 3.1 转换规则

- `graph.astream(..., stream_mode="updates")` 产出 `{node_name: state_delta}` 形式 chunk。
- `convert_updates_chunk` 提取 `node_name`，并从 `state_delta` 只提取 `_SAFE_STATE_FIELDS`：`{route, run_id, command}`。
- 完整 state、config、checkpoint、prompt、模型输出、密钥、患者身份等字段一律不进入事件。

## 4. 超时 / 取消 / 错误行为说明

### 4.1 总超时

- 通过 `asyncio.timeout(self._timeout_seconds)` 实现。
- `timeout_seconds` 为 `None` 或 `0` 时使用 `_NullTimeout`（无限制）。
- 超时后内部任务被取消，捕获 `TimeoutError`，抛出 `GraphRunnerTimeoutError`（code=`RUNNER_TIMEOUT`）。
- 在 `astream_events` 中，超时前会先产出 `graph_failed` 事件，再抛异常。

### 4.2 取消传播

- `asyncio.CancelledError` 在 `ainvoke` 和 `astream_events` 中均不捕获，直接 `raise`。
- 因此外部 `task.cancel()` 能够正常传播取消信号。

### 4.3 错误归一化

- 非超时、非取消、非 `CheckpointConfigMismatchError` 的异常统一归一化为 `GraphRunnerError`（code=`RUNNER_EXECUTION_FAILED`）。
- 对外错误使用固定文本 `Graph execution failed` 和固定错误码，绝不读取或拼接 `str(exc)`。
- 在 `except` 块外抛出归一化错误，不保留 `__cause__` 或 `__context__` 中的底层异常；事件仅输出固定 `error_code`。

### 4.4 错配拒绝

- `GraphRunner` 在 `ainvoke` / `astream_events` 入口均调用 `validate_checkpoint_config`（复用 L1-3 逻辑）。
- 校验失败抛出 `CheckpointConfigMismatchError`，不调用 `graph.ainvoke` / `astream`，因此目标 thread 不产生 checkpoint。
- 测试 `test_mismatch_does_not_invoke_graph` 通过 `graph.aget_state` 验证错配后 checkpoint 仍为空。

## 5. 未触及边界说明

- **未接入 FastAPI / SSE / WebSocket / 生产 API**：`GraphRunner` 仅是一个纯 Python async 封装，没有 HTTP 层。
- **未实现业务 Agent / 模型调用 / 领域 Schema / Repository / Outbox / RAG / Safety / Legacy 改造**：测试中只使用最小占位图和自定义慢图/错误图，没有调用真实模型或患者数据。
- **未改变 `AGENT_RUNTIME_VERSION=legacy`**：未读取或修改 `app/core/config.py` 中的 Feature Flag 默认值。
- **未修改 .gitignore / .claude / .workbuddy**：未触碰这些文件。
- **未实现 L1-4 之后任务**：没有开始 L2/L3 Harness、业务 Agent、API 路由、生产 SSE 等。

## 6. 设计决策

- `GraphRunner` 持有已编译的 `CompiledStateGraph`，而不是构建图。checkpointer 的创建/关闭仍由调用方（L1-3 `postgres_checkpointer` 或测试 `InMemorySaver`）负责，保持生命周期边界清晰。
- 运行前校验与 L1-3 `ConfigValidatingCheckpointer` 的写入校验形成双重保护：
  - Runner 入口：运行前拒绝错配；
  - Checkpointer：即使绕过 Runner，checkpoint 写入前仍拒绝错配。
- 事件流使用 `stream_mode="updates"` 而不是内部 `astream_events` API，减少版本锁定并只暴露节点完成信息。
- 事件字段显式白名单化，确保未来新增 state 字段不会意外泄露到事件流。
