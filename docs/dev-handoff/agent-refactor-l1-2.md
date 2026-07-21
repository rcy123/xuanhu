# L1-2 交接：GraphState、MainGraph 与命令路由

## 结论

L1-2 已完成。最小 `XuanhuGraphState`、MainGraph 骨架和命令路由已建立，使用 `InMemorySaver` 验证了命令路由正反路径、状态序列化、thread 隔离和 graph version 命名空间隔离。

本任务只建立不含临床逻辑的最小运行骨架；未接入业务 Agent、生产 API 路由、PG checkpointer、Runner/stream 或 Legacy 改造。

## 交付内容

### 新增文件

| 文件 | 说明 |
|---|---|
| `app/agent_runtime/__init__.py` | Agent Runtime 包初始化，声明 L1-2 范围与禁止事项 |
| `app/agent_runtime/config.py` | 图版本常量（`GRAPH_VERSION_V1`）、`make_thread_id`/`make_run_config` 工具函数，使用 `{graph_version}:{session_id}` 命名空间 |
| `app/agent_runtime/errors.py` | `GraphStateError`、`CommandRoutingError` 错误类型，消息脱敏 |
| `app/agent_runtime/commands.py` | `XuanhuCommand` StrEnum（message/advance/review/recover）、占位节点名常量、`COMMAND_ROUTE_MAP` 路由映射 |
| `app/agent_runtime/state.py` | `XuanhuGraphState` TypedDict 及子结构（`GateResultRef`、`ArtifactRef`、`PendingInterrupt`、`Budget`、`LastError`），全部 JSON-safe 类型；`validate_state_json_safe` 和 `default_state` 工具函数 |
| `app/agent_runtime/routing.py` | `resolve_command_route` 确定性路由函数、`command_router` 节点、`route_after_router` conditional edge 函数 |
| `app/agent_runtime/graph.py` | `build_main_graph` 构造最小 MainGraph：START -> command_router -> [conditional] -> 占位节点 -> END |
| `tests/test_l1_2_graph_state_and_routing.py` | L1-2 专项测试：44 项，覆盖命令路由正反路径、state 序列化、InMemorySaver checkpoint、thread 隔离、graph version 隔离、图结构完整性 |

### 修改文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | 新增 `psycopg-binary` dev 依赖（修复 PG Spike import）；新增 `numpy>=2.4,<2.5` 版本约束（锁定传递依赖，修复 numpy 2.5+ stub PEP 695 与 mypy `python_version="3.11"` 的兼容性）；mypy `python_version` 保持 `"3.11"` 不变（对齐 `requires-python>=3.11` 和 Ruff `py311`）；移除未使用的 `uvicorn.*` override |
| `uv.lock` | 新增 `psycopg-binary` v3.3.4 条目；numpy 统一为 2.4.6（移除 2.5.0）（83 packages） |
| `test_agent/test_l0_1_contract.py` | 更新 3 个 L0-1 契约断言：`app/agent_runtime` 从"不得存在"改为"若存在则只含 L1 骨架文件且不含业务 Agent"；`agent_runtime_version` 默认值检查保留 |

## MainGraph 结构

```text
START
  │
  ▼
command_router
  │ (conditional edge: route_after_router)
  ├─ message  ──► intake_placeholder     ──► END
  ├─ advance  ──► reasoning_placeholder  ──► END
  ├─ review   ──► review_placeholder     ──► END
  ├─ recover  ──► recovery_placeholder   ──► END
  ├─ empty    ──► blocked_terminal       ──► END
  └─ unknown  ──► manual_terminal        ──► END
```

## XuanhuGraphState 字段

对齐实施计划 §6.2 和 ADR-002：

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | 会话标识，对应 `thread_id` |
| `domain_state_version` | `int` | Domain State 版本指针（整型引用） |
| `command` | `str` | 当前待执行命令类型 |
| `command_id` | `str` | 命令幂等键 |
| `graph_version` | `str` | 图版本标识 |
| `run_id` | `str` | 本次图执行运行唯一标识 |
| `route` | `str` | 当前路由目标（节点名） |
| `gate_results` | `list[GateResultRef]` | Policy Gate 结果引用 |
| `artifact_refs` | `list[ArtifactRef]` | Domain artifact UUID 引用集合 |
| `pending_interrupt` | `PendingInterrupt \| None` | 挂起中断信息 |
| `budget` | `Budget` | 执行预算追踪 |
| `last_error` | `LastError \| None` | 脱敏错误码和 trace 引用 |

## 验收证据

```bash
# L1-2 专项测试
uv run pytest tests/test_l1_2_graph_state_and_routing.py -q -rs
# 44 passed

# L1-1 Spike 测试
uv run pytest tests/test_langgraph_compatibility_spike.py tests/test_langgraph_postgres_checkpoint_spike.py -q -rs
# 12 passed

# L0-1 契约测试
uv run pytest test_agent/test_l0_1_contract.py -q -rs
# 131 passed

# 全量后端测试
uv run pytest -q -rs
# 1010 passed, 1 xfailed, 2 warnings

# 静态检查
uv run ruff check .
# All checks passed!

uv run mypy app --no-incremental
# Success: no issues found in 79 source files

uv run mypy app --no-incremental --python-version 3.11
# Success: no issues found in 79 source files

uv lock --check
# Resolved 83 packages

git diff --check
# passed
```

## 验收标准对照

| # | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| 1 | MainGraph 可按 command 路由到正确占位节点 | ✅ | `TestCommandRoutingPositive` 4 项 + parametrize 4 项 |
| 2 | unknown/invalid command 有确定性失败或 blocked/manual 结果 | ✅ | `TestCommandRoutingNegative` 7 项 + `TestConditionalEdges` parametrize 2 负向 |
| 3 | Graph State 只包含可序列化执行数据和引用 | ✅ | `TestStateSerialization` 6 项（JSON dumps、PII 检查、runtime 对象检查） |
| 4 | 相同 thread_id 可用 InMemorySaver 读取状态 | ✅ | `TestInMemorySaverCheckpoint` 3 项 |
| 5 | 不同 thread_id 互相隔离 | ✅ | `TestThreadIdIsolation` 2 项 |
| 6 | graph_version 命名空间不互相读取 | ✅ | `TestGraphVersionNamespaceIsolation` 4 项 |
| 7 | 所有 conditional edge 至少有正向和负向测试 | ✅ | `TestConditionalEdges` 6 项（4 正向 + 2 负向 + parametrize 6 路径） |
| 8 | 不引入真实模型依赖 | ✅ | 无 model/Redis/RAG import |
| 9 | 不改变 Legacy 生产行为 | ✅ | `AGENT_RUNTIME_VERSION` 默认 `legacy` 未改；全量 1010 passed |

## 边界确认

- 未接入业务 Agent（IntakeExtractionAgent、SyndromeDraftAgent 等）。
- 未接入 FastAPI 生产路由。
- 未接入 AsyncPostgresSaver 生产 checkpointer（留给 L1-3）。
- 未实现 GraphRunner/stream（留给 L1-4）。
- 未修改 Supervisor、MessageService、ReviewService、RecoveryService。
- 未改变 `AGENT_RUNTIME_VERSION` 默认值（仍为 `legacy`）。
- 未引入真实模型、Redis、RAG 或患者数据依赖。
- 未开始 L2/L3 的 Harness、Domain State、Intake 业务逻辑。

## 环境修复说明

本任务在执行过程中修复了以下预存环境问题（非 L1-2 功能变更）：

1. **psycopg-binary 缺失**：L1-1 Spike 的 `psycopg` 依赖需要 pq wrapper（C/binary/libpq），但 `psycopg-binary` 未在 dev 依赖中声明。已添加到 `[project.optional-dependencies] dev`。

2. **numpy 2.5+ stub 与 mypy `python_version="3.11"` 不兼容（AR-B-004 返工）**：numpy 2.5.0 的 `__init__.pyi` 使用 PEP 695 `type` 语句（Python 3.12+），导致 mypy `python_version="3.11"` 无法解析 stub 文件。首版交付错误地将 mypy `python_version` 改为 `"3.12"`，破坏了项目 `requires-python>=3.11` 的 Python 3.11 支持契约。返工修复方案：
   - 恢复 mypy `python_version = "3.11"`，保持与 `requires-python>=3.11` 和 Ruff `py311` 一致。
   - 在 `dependencies` 中新增 `numpy>=2.4,<2.5` 版本约束（本项目不直接依赖 numpy，此约束仅锁定 pandas/pymilvus 的传递依赖版本）。
   - numpy 2.4.6 的 stub 文件不使用 PEP 695 `type` 语句（已验证：6202 行 stub 中无 PEP 695 type alias），与 mypy `python_version="3.11"` 完全兼容。
   - 不使用全局 `follow_imports=skip`，不降低 app 严格检查（`strict=true` 不变）。
   - uv.lock 从双版本（2.4.6 + 2.5.0）统一为单一版本 2.4.6（83 packages）。

3. **L0-1 契约测试 L0→L1 边界更新**：3 个断言检查 `app/agent_runtime` 不应存在（L0 约束），L1-2 合法创建后已更新为"若存在则只含 L1 骨架文件且不含业务 Agent"。

## 后续建议

- L1-3 可基于本骨架接入 `AsyncPostgresSaver` 生产 checkpointer，验证跨进程恢复。
- L1-3 需确认 root graph 的 `checkpoint_ns` 策略（L1-1 Spike 发现 LangGraph 1.2.x 的 namespace 解释问题）。
- L1-4 可基于 `build_main_graph` 实现 GraphRunner（ainvoke/astream 包装、超时取消、错误归一化）。
- L2 可基于 `XuanhuGraphState` 字段定义 Harness 协议（AgentSpec、RunSpec、ContextPacket 等）。
