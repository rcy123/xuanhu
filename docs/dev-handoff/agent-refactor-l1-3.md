# L1-3 交接：AsyncPostgresSaver 与跨进程恢复

## 结论

L1-3 已完成（含 AR-B-005/006 两轮返工）。生产级 AsyncPostgresSaver checkpointer 底座已接入，config/state 一致性校验通过 `ConfigValidatingCheckpointer` 置于不可绕过的 checkpointer 写入边界——拦截 `put`/`aput`，在任何持久化前执行校验。

本任务只接入生产级 checkpointer 底座；未实现 Runner/stream、API 路由、业务 Agent 或 Legacy 恢复改造。

## AR-B-005/006 返工说明

### 第一轮（AR-B-005）

1. **config/state 校验未接入实际写入路径** → 新增 `ConfigValidatingCheckpointer` 代理类。
2. **缺少真实 graph.ainvoke + PG 回归测试** → 新增 3 项 PG 集成测试。
3. **资源关闭语义无效** → 删除 `close_postgres_checkpointer`，由 `from_conn_string` context manager 管理。
4. **子进程通过 argv 接收 DB_URL** → 改为环境变量。
5. **子进程异常输出 str(exc)** → 改为固定错误码 + 异常类型。

### 第二轮（AR-B-006）

1. **校验仍可被调用方绕过** → `postgres_checkpointer` 现在 yield `ConfigValidatingCheckpointer`（自动包装），而非原始 `AsyncPostgresSaver`。校验在 checkpointer 的 `put`/`aput` 方法中执行，LangGraph 在首节点前调用 `aput` 写入输入 state，校验在任何持久化前发生。
2. **不得仅新增另一个需要调用方主动选择的 wrapper/helper** → 不再有 `validated_ainvoke`。调用方使用 `graph.ainvoke` 即可，校验自动发生。
3. **回归测试直接使用公开图入口** → 测试使用 `graph.ainvoke(state, config=config)`，不经过任何额外 wrapper。session_id/graph_version 错配时 `CheckpointConfigMismatchError` 在 `aput` 中抛出，目标 thread 无 checkpoint。
4. **子进程连接失败只输出固定错误码和异常类型** → `_sanitize_error` 只返回 `{"error_type": ..., "code": "CHECKPOINT_SUBPROCESS_FAILED"}`，无 `detail`、无 `str(exc)`。
5. **不得输出 IP/端口/主机名/用户名/数据库名/连接器底层文本** → 所有子进程错误响应只含 `error_type` 和 `code` 两个字段。
6. **安全测试使用伪造 DB_URL** → `test_subprocess_connection_failure_has_no_sensitive_data` 使用 `postgresql://secret_user:secret_pass@%ZZ` 触发真实连接失败，检查 `args`/`stdout`/`stderr` 不含 `postgresql://`、`secret_user`、`secret_pass`、`%zz`、`connection failed`。

## 交付内容

### 新增文件

| 文件 | 说明 |
|---|---|
| `app/agent_runtime/checkpoint.py` | `ConfigValidatingCheckpointer`（拦截 put/aput 校验 config/state）、`postgres_checkpointer`（yield ConfigValidatingCheckpointer）、`check_postgres_health`、`extract_thread_id`、`_sanitize_error_message` |
| `tests/test_l1_3_postgres_checkpoint.py` | 33 项测试，覆盖全部 9 个验收标准 + 子进程安全 |
| `tests/_l1_3_subprocess.py` | 跨进程 helper：DB_URL 从环境变量读取；异常只输出固定错误码 + 异常类型 |

### 修改文件

| 文件 | 说明 |
|---|---|
| `app/agent_runtime/errors.py` | 新增 `CheckpointError`、`CheckpointConfigMismatchError`、`CheckpointHealthCheckError` |
| `app/agent_runtime/config.py` | 新增 `parse_thread_id`、`validate_checkpoint_config` |
| `app/agent_runtime/graph.py` | checkpointer 参数类型改为 `BaseCheckpointSaver[Any] \| None` |
| `test_agent/test_l0_1_contract.py` | `_ALLOWED_RUNTIME_FILES` 新增 `checkpoint.py` |
| `pyproject.toml` | ruff per-file-ignores 新增 `tests/_l1_3_subprocess.py` T20 豁免 |

## 验收证据

```bash
# L1-3 专项测试
$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest tests/test_l1_3_postgres_checkpoint.py -q -rs
# 33 passed

# L1-2 + Spike + L0-1
uv run pytest tests/test_l1_2_graph_state_and_routing.py tests/test_langgraph_compatibility_spike.py tests/test_langgraph_postgres_checkpoint_spike.py test_agent/test_l0_1_contract.py -q -rs
# 187 passed

# 全量后端测试
uv run pytest -q -rs
# 1043 passed, 1 xfailed, 2 warnings

# 静态检查
uv run ruff check .
# All checks passed!

uv run mypy app --no-incremental
# Success: no issues found in 80 source files

uv run mypy app --no-incremental --python-version 3.11
# Success: no issues found in 80 source files

uv lock --check
# Resolved 83 packages

git diff --check
# passed
```

## 校验机制说明

### ConfigValidatingCheckpointer（不可绕过）

```
graph.ainvoke(state, config)
  └─ LangGraph 内部调用 checkpointer.aput(config, checkpoint, ...)
       └─ ConfigValidatingCheckpointer._validate_write(config, checkpoint)
            └─ validate_checkpoint_config(config, state)
                 └─ 校验 thread_id 的 session_id/graph_version 与 state 一致
                      ├─ 一致 → 委托 delegate.aput 写入 checkpoint
                      └─ 不一致 → 抛出 CheckpointConfigMismatchError（不写入）
```

- `postgres_checkpointer` 自动 yield `ConfigValidatingCheckpointer(saver)`，调用方无法获取原始 saver。
- 校验在 LangGraph 写入 checkpoint 前执行，不需要调用方主动选择。
- 测试直接使用 `graph.ainvoke`，校验自动发生。

### 子进程安全

- DB_URL 从 `DB_URL` 环境变量读取，不通过 argv 传递。
- 异常输出只含 `{"error_type": "ExceptionClass", "code": "CHECKPOINT_SUBPROCESS_FAILED"}`，不含 `str(exc)`、IP、端口、主机名、用户名、数据库名。
- 安全测试使用伪造 DB_URL (`postgresql://secret_user:secret_pass@%ZZ`) 触发真实连接失败。

## 边界确认

- **未实现** GraphRunner、ainvoke/astream 包装、超时取消或事件转换
- **未接入** FastAPI 生产路由
- **未接入** 业务 Agent、模型、Redis、RAG 或患者数据
- **未修改** Legacy RecoveryService、Supervisor、MessageService 或 ReviewService
- **未新增** 业务表、SQLAlchemy 模型或 Alembic 迁移
- **未改变** `AGENT_RUNTIME_VERSION` 默认 `legacy`
- **未开始** L1-4、L2 或 L3 业务任务
