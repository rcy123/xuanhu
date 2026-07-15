# ADR-001：采用 LangGraph 作为 Agent 编排框架

## 状态

已采纳（2026-07-09）

## 背景

悬壶项目当前 Agent 编排由 `Supervisor`（`app/agents/supervisor.py`）直接实现，通过 `AgentRegistry` + 阶段路由 + 手动 checkpoint（PG `state_snapshot` + Redis `xuanhu:checkpoint:` 键）驱动会话状态机。当前实现存在以下问题：

1. **无标准化图语义**：阶段路由是 Python `if/elif` 链，推理→辨证→处方→加减→安全审核→复核→病历的节点拓扑隐含在代码中，无法可视化、无法 trace 整个执行图。
2. **恢复机制手写**：checkpoint 写入为 best-effort Redis + PG 双写，恢复逻辑在 `RecoveryService` 中手动实现 `resume_from_pg_snapshot`、`retry_current_stage`、`rollback_to_stage`、`terminate` 四种动作，无框架级暂停/恢复语义。
3. **Human-in-the-loop 缺乏标准抽象**：Doctor Review 阶段通过 `pending_review` 标志 + 阶段锁挂起，而非使用框架中断（interrupt），导致暂停后缺少标准的 `Command(resume=...)` 语义。
4. **与 Harness 架构设计文档的目标差距**：《多Agent架构设计-Harness版.md》第 3 节定义 Harness 需包含 Run Controller、State Machine、Policy Gates 等组件，而当前 Supervisor 混合了路由、状态更新、审计、事件发射、checkpoint 写入等职责，不符合单一职责原则。

《Agent整体大修实施计划-LangGraph版.md》第 2 节定义了 LangGraph 使用原则，第 3 节给出了目标图结构（MainGraph 含 IntakeSubgraph、ReasoningSubgraph、ReviewRecordSubgraph 及 Recovery Router）。

## 决策

采用 **LangGraph**（`langgraph` Python 库）作为 Agent 编排框架，替代当前手写的 `Supervisor` 状态机。

具体决策如下：

1. **StateGraph** 建模完整临床流程为有向图，每个阶段为节点，阶段间迁移为条件边。
2. **checkpointer**（`AsyncPostgresSaver`）替代当前 PG `state_snapshot` + Redis checkpoint 双写。根图 `thread_id` 使用 `{graph_version}:{session_id}`；LangGraph 1.2.x 的 root config 不写 `checkpoint_ns`，避免把 graph version 错当成子图 namespace。
3. **`interrupt()` / `Command(resume=...)`** 替代当前 `pending_review` 标志，实现 Doctor Review 的 Human-in-the-loop 暂停/恢复。
4. **子图（Subgraph）** 封装 Intake（问诊）、Reasoning（辨证+处方+加减）、ReviewRecord（复核+病历）为独立可测试单元。
5. **Recovery Router** 作为条件边，根据 `RecoveryStatus` 决定是继续执行、回退到指定节点还是终止。
6. **Conditional edges** 实现 Sufficiency Policy、Safety 通过/回退的路由判断，替代当前 Python `_decide_next_stage` 的 if/elif 链。

## 决策依据

1. **LangGraph 是 Python 生态中最成熟的 Agent 图框架**：原生支持 StateGraph、条件边、checkpointer、`interrupt()`、子图、`astream()`、State history，完全覆盖《Agent整体大修实施计划-LangGraph版.md》第 2.1 节所列全部核心能力需求。
2. **与 Harness 架构兼容**：LangGraph 的节点即 Harness 的 Agent 执行单元，条件边即 Policy Gates，checkpointer 即 State Reducer 的底层实现。LangGraph 不承担 Harness 的 Context Builder、Tool Registry、Verifier Chain 等职责，分层清晰。
3. **标准化 Human-in-the-loop**：`interrupt()` 提供了标准的暂停机制，`Command(resume=...)` 提供了带载荷的恢复机制，避免了当前 `pending_review` 标志 + 手动校验的脆弱性。
4. **可观测性**：LangGraph 的 `astream()` 提供原生节点级流式事件，可映射为 SSE `agent.started`/`agent.finished`/`stage.changed` 等事件，优于当前 Supervisor 手动发射事件的方式。
5. **恢复能力**：checkpointer 自动保存每个 super-step 后的状态，支持从任意 checkpoint 恢复，优于当前手动管理 PG snapshot + Redis checkpoint 双写。
6. **测试友好**：`InMemorySaver` 支持无数据库依赖的图单元测试和 Golden E2E 测试。

## 明确边界

### LangGraph 负责

- 节点（Node）：每个阶段 Agent 的执行
- 条件边（Conditional Edge）：Sufficiency Policy、Safety 通过/回退、Recovery Router
- checkpointer：图状态的序列化与恢复
- `interrupt()` / `Command(resume=...)`：Human-in-the-loop 暂停/恢复
- 子图：IntakeSubgraph、ReasoningSubgraph、ReviewRecordSubgraph 的封装
- `astream()`：图执行的事件流

### LangGraph 不负责

- **数据库写入**：所有 DB 写入（consult_messages、medical_records、audit_events、safety_rule_runs 等）由节点内部的服务层执行，不在图节点中直接操作 ORM
- **SQLAlchemy Session 管理**：图节点不持有 `AsyncSession`，通过 Harness 注入
- **Redis Stream 事件**：事件写入由 EventService 负责，图节点调用 EventService 而非直接操作 Redis
- **安全规则引擎**：`SafetyRuleEngine` 是确定性纯函数引擎，不在图中执行，图节点调用它
- **Prompt 构建**：每条消息的 Prompt 由 Context Builder 构建，图节点接收已构建的 Prompt
- **患者身份**：PatientInfo 中 `name` 字段在 checkpoint 中不可序列化（见 ADR-002）

### 不采用的 LangGraph 能力

1. **MessagesState**：不使用 LangGraph 内置的 MessagesState（会泄露完整对话历史到 checkpoint），而是使用 Domain State + Graph State 双 State 边界（见 ADR-002）。
2. **LangSmith**：L0–L2 阶段不集成 LangSmith，避免引入额外外部依赖。
3. **React Agent**：不使用 LangGraph 的 `create_react_agent()`，临床流程需要确定性状态机而非开放式推理循环。

## 正面影响

- **执行图可视化**：LangGraph 的 `draw_mermaid()` 可生成可视化的图结构，便于审查和文档化。
- **标准化恢复**：checkpointer 提供一致的暂停/恢复语义，消除当前手动恢复代码的边界情况。
- **可测试性**：`InMemorySaver` + 子图封装使每个阶段可独立单元测试。
- **可观测性**：`astream()` 事件可直接映射为 SSE 事件流，减少自定义事件发射代码。
- **社区生态**：LangGraph 有活跃的社区和文档，降低维护成本。

## 风险与代价

1. **框架耦合风险**：LangGraph API 可能在未来版本中发生变化，需要版本锁定。缓解：通过在 `requirements.txt` 中固定 LangGraph 版本，并在 Harness 中抽象图执行接口。
2. **checkpoint 序列化深度**：LangGraph 默认使用 `pickle` 或 `jsonpatch` 序列化 State，需要确保 Domain State 字段均符合序列化约束。缓解：Graph State 只保存最小引用，Domain State 独立管理（见 ADR-002）。
3. **学习曲线**：团队需要理解 LangGraph 的图语义、checkpointer、interrupt 等概念。缓解：L1 阶段先搭建骨架，不实现业务逻辑，给团队学习时间。
4. **checkpointer 对 PostgreSQL 的依赖**：`AsyncPostgresSaver` 需要专用的 checkpoint 表和 write-ahead 表。缓解：这些表在 L1 LangGraph Runtime 骨架阶段通过数据库迁移脚本创建，测试环境使用 `InMemorySaver`。
5. **与 Legacy 双轨运行复杂度**：L0–L8 期间 Legacy 和 LangGraph 需要并行运行。缓解：通过 Feature Flag（L0-3 负责）显式切换，禁止隐式降级（见 migration boundary）。

## 迁移策略

1. **L0**：建立 ADR、兼容矩阵与迁移边界（当前任务）。
2. **L1**：搭建 LangGraph Runtime 骨架（空图 + checkpointer + interrupt + astream → SSE），无业务 Agent 接入。
3. **L2**：Harness 核心与 Domain State 实现，将当前 `XuanhuState` 拆分为 Domain State（临床事实）和 Graph State（执行元数据）。
4. **L3–L8**：逐阶段将 Agent 迁移到 LangGraph 子图。Feature Flag 只决定新会话创建时的运行时身份（`legacy` 或 `langgraph`），既有会话不得在生命周期内混合使用两种执行路径。同一会话的所有阶段统一走创建时确定的运行时路径。
5. **L9**：Legacy 路径下线，`Supervisor` 代码保留为只读参考但不执行，最终可以移除。

迁移期间 Legacy 实现不得删除。两类会话（Legacy/LangGraph）在运行时身份创建后不可隐式切换，恢复路径严格隔离（见 migration boundary）。

## 回滚策略

1. **Feature Flag 回滚**：将 `AGENT_RUNTIME_VERSION` Feature Flag 切回 `legacy`，Feature Flag 只影响新会话创建时的运行时身份。既有 LangGraph 会话不得重建或切换到 Legacy，必须继续使用 LangGraph 路径直到会话结束。两类会话（Legacy 与 LangGraph）恢复路径严格隔离，不得交叉恢复。
2. **数据不丢失**：Domain State 是所有临床事实的唯一权威，存储在 PG 中独立于 LangGraph checkpoint。Feature Flag 回滚只让新会话进入 Legacy；既有 LangGraph 会话继续从原 graph-version namespace 的 checkpoint 恢复并结束，不得从 Domain State 跨运行时重建为 Legacy 会话。
3. **回滚审计**：每次 Feature Flag 切换写入 audit_events（`runtime.switched`），记录操作人、时间、原因和切换前后的 runtime 标识。

## 验证方式

- L0-1 契约测试（`test_agent/test_l0_1_contract.py`）验证本文档章节完整性
- L0-2 Golden E2E 基线测试覆盖 Legacy 所有端点和 SSE 事件
- L1 LangGraph 骨架测试验证空图 + checkpointer + interrupt + astream 链路
- L2 Domain State 测试验证 Domain State 与 Graph State 的边界隔离
- 每阶段质量门禁（`uv run pytest -q -rs && uv run ruff check . && uv run mypy app && uv lock --check`）
