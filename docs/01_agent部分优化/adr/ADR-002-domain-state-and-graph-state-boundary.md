# ADR-002：Domain State 与 Graph State 双层状态边界

## 状态

已采纳（2026-07-09）

## 背景

当前 `XuanhuState`（`app/schemas/agent.py`）是一个单一 Pydantic BaseModel，混合了以下三类数据：

1. **临床事实**：`patient_info`、`chief_complaint`、`ten_questions`、`syndrome_result`、`base_formula`、`modified_formula` 等——这些是诊疗过程的权威记录，必须持久化到 PostgreSQL 并支持审计。
2. **执行元数据**：`current_stage`、`pending_review`、`rollback_counts`、`state_version`、`recovery_status`、`blocked_reason`——这些是状态机控制数据，用于决定下一步执行路径。
3. **对话历史引用**：`inquiry_messages`、`evidences`——Agent 运行时需要的上下文，但完整数据在 `consult_messages` 表中。

LangGraph 的 checkpointer 会对整个 State 序列化（pickle 或 jsonpatch），如果直接将 `XuanhuState` 作为 LangGraph 的图 State（`TypedDict`），会导致以下问题：

- **checkpoint 膨胀**：`inquiry_messages` 包含所有对话轮次的完整消息，随着会话增长 checkpoints 会迅速变大。
- **隐私泄露**：`patient_info` 包含 `name` 等可识别字段，序列化到 LangGraph checkpoint 表（`checkpoints` / `checkpoint_writes`）后增加数据泄露面。
- **序列化脆弱性**：`XuanhuState` 包含 Pydantic 模型、`datetime` 等复杂类型，LangGraph 的默认序列化器可能无法正确处理。
- **双真源风险**：如果 clinical fact 同时存在于 Domain State（PG）和 Graph State（checkpoint），更新不一致会导致临床事实分歧。

《Agent整体大修实施计划-LangGraph版.md》第 2.3 节定义了两种 State 的边界，并明确 Domain State 是临床事实唯一权威，Graph State 只保存最小可序列化执行数据和引用。

## 决策

将当前单一 `XuanhuState` 拆分为 **Domain State** 和 **Graph State** 两层：

### Domain State（领域状态）——临床事实唯一权威

- **存储位置**：PostgreSQL `consult_sessions.state_snapshot`（JSONB），以及各个业务表（`consult_messages`、`medical_records`、`doctor_reviews`、`safety_rule_runs` 等）。
- **内容**：所有临床事实字段——`patient_info`、`chief_complaint`、`present_illness`、`ten_questions`、`syndrome_result`、`base_formula`、`modified_formula`、`safety_rule_result`、`safety_review`、`doctor_review`、`medical_record`。
- **访问方式**：通过 Harness 的 Domain State Store 读写，使用 SQLAlchemy 的 `AsyncSession` 管理事务边界。
- **序列化**：JSONB（PG 原生支持，不需要 pickle）。
- **权威性**：Domain State 是临床事实的唯一权威。Graph State checkpoint 中的数据不可直接用于临床决策；所有临床决策必须基于 Domain State 的最新值。

### Graph State（图状态）——最小可序列化执行数据

- **存储位置**：LangGraph checkpointer（`AsyncPostgresSaver` 管理的 `checkpoints` 和 `checkpoint_writes` 表）。
- **内容**：仅包含图执行所需的最小数据，严格对齐《Agent整体大修实施计划-LangGraph版.md》§6.2 `XuanhuGraphState` 定义：
  - `session_id`：会话标识，对应 `thread_id`
  - `domain_state_version`：Domain State 版本指针（整型引用，不是完整 Domain State）
  - `command`：当前待执行命令类型（如 `advance`、`review`、`recover`），不包含患者输入或临床载荷
  - `command_id`：命令幂等键
  - `graph_version`：图版本标识（用于版本化 checkpoint namespace）
  - `run_id`：本次图执行运行的唯一标识
  - `route`：当前路由目标（节点或子图标识）
  - `gate_results`：Policy Gate 结果的标识符引用，不保存完整 Gate 输出或临床字段
  - `artifact_refs`：Domain artifact 引用集合（如 `doctor_review_ref`、`safety_rule_result_ref`、`syndrome_result_ref` 等 UUID 字符串引用）
  - `pending_interrupt`：当前挂起中断的类型、ID 和恢复令牌引用，不保存医师决定、处方或患者数据
  - `budget`：执行预算追踪（剩余步数/时间）
  - `last_error`：脱敏错误码和 trace 引用，不保存异常堆栈、Prompt、模型输出或患者数据
- **访问方式**：通过 LangGraph 的 `StateGraph` 自动管理，checkpointer 在每个 super-step 后自动保存。
- **序列化**：LangGraph 默认序列化 + JSON-safe 类型（str、int、bool、list、dict）。
- **权威性**：Graph State 仅用于图执行流程控制，不是临床事实的权威来源。

### 明确禁止放入 Graph State 的内容

- SQLAlchemy `AsyncSession` 或任何 ORM 对象
- 模型客户端（如 `openai.AsyncOpenAI`）
- Python 函数、方法或可调用对象
- 完整 Prompt 文本（只保存 `prompt_version` 引用）
- 完整原始模型输出（只保存 `prompt_version` 引用和输出 token 计数，不得保存结构化临床模型输出）
- 结构化临床模型输出（如 `SyndromeResult`、`FormulaDraft`、`SafetyRuleResult` — 这些属于 Domain State，Graph State 仅保存引用 `_ref`）
- `patient_info.name` 或任何可直接识别患者的字段（PII）
- 完整的 `inquiry_messages` 列表（只保存最新 N 轮的摘要引用）

## 决策依据

1. **单一真源原则**：临床事实只存储在 Domain State（PG）中，Graph State checkpoint 只是执行快照。恢复时从 Domain State 重建 Graph State，而非直接从 checkpoint 反序列化所有临床数据。
2. **隐私合规**：患者身份信息不进入 LangGraph checkpoint 表，减少 PII（Personally Identifiable Information）分布面。`patient_info` 中的 `name` 字段在写入 checkpoint 前由 Harness 剥离（写入 `patient_info` 时自动 `exclude={"name"}`，与现有 `SafetyRuleRun.patient_snapshot` 一致）。
3. **性能**：Graph State 保持极小（< 1KB），checkpoint 序列化/反序列化开销可控，支持频繁的 super-step 保存。
4. **可恢复性**：从 Domain State 可以完整重建会话状态；从 Graph State 只能恢复执行流程。恢复流程为：从 `thread_id` 加载 checkpoint → 从 `domain_state_version` 引用加载 Domain State → 重建完整图执行上下文。
5. **审计完整性**：所有临床变更通过 Domain State 的版本化（`state_version`）和审计事件（`audit_events`）可追溯，不依赖 LangGraph checkpoint 的变更历史。
6. **框架解耦**：Domain State 的 Schema 和存储不依赖 LangGraph 的序列化机制，未来更换编排框架时 Domain State 无需迁移。

## 明确边界

### 写入边界

- **Domain State 写入**：仅在节点执行完成后，通过 Harness 的事务边界写入 PG。写入 Domain State 的操作与写入业务表（`consult_messages`、`safety_rule_runs` 等）在同一事务中。
- **Graph State 写入**：由 LangGraph checkpointer 在每个 super-step（节点执行 + 条件边求值）后自动写入。Harness 不直接写入 checkpoint 表。
- **一致性保证**：Domain State 写入成功后更新 Graph State 中的 `domain_state_version` 引用。恢复或继续执行前必须比较该版本与数据库当前版本；旧版本输出一律拒绝并重建上下文，不允许覆盖较新的 Domain State。

### 读取边界

- **节点执行时**：从 Graph State 获取 `domain_state_version` 和 `session_id`，通过 Domain State Store 加载对应版本并校验当前版本，重建完整执行上下文。
- **interrupt 恢复时**：从 checkpoint 获取 `route`、`pending_interrupt` 和 `domain_state_version`，校验 Domain State 版本后通过 `Command(resume=...)` 恢复执行。
- **审计查询时**：仅读取 Domain State（PG），不读取 checkpoint 表。

### 禁止的模式

- **双写**：同一临床事实不得同时写入 Domain State 和 Graph State。如 `syndrome_result` 只写入 Domain State，Graph State 仅保存执行流程状态。
- **checkpoint 直接读取**：任何业务逻辑不得直接读取 LangGraph checkpoint 表中的序列化数据作为临床决策依据。
- **跨状态引用断裂**：Graph State 中的引用（`domain_state_version`、`artifact_refs` 内的 `doctor_review_ref`）必须能回溯到 Domain State 中的有效记录。如果引用断裂（如 `doctor_reviews` 记录被物理删除），恢复时必须进入 `blocked` 状态并记录审计。

## 正面影响

- **隐私面收窄**：患者身份信息只出现在 PG 特定表中（`consult_sessions` JSONB `state_snapshot` 的 `patient_info`），不扩散到 LangGraph checkpoint 表。
- **性能优化**：checkpoint 大小恒定（~1KB），不受对话轮次或 RAG 证据数量影响。
- **恢复可靠性**：恢复时从权威 Domain State 加载临床数据，不依赖 checkpoint 数据的完整性。
- **测试简化**：Domain State 可独立于 LangGraph 测试（纯 PG CRUD + Schema 校验）；Graph State 可通过 `InMemorySaver` 测试图逻辑。

## 风险与代价

1. **读取延迟**：每个节点执行时需从 PG 加载 Domain State，增加一次 DB 查询（相比从 Graph State 直接读取）。缓解：Domain State 加载是单次 `SELECT state_snapshot FROM consult_sessions WHERE id=$1`，延迟可忽略。
2. **一致性窗口**：Graph State 中的 `domain_state_version` 可能落后于 Domain State 当前版本（checkpoint 写入在领域事务提交后进行）。缓解：恢复或继续执行前比较版本；不一致时丢弃旧运行产物并基于最新 Domain State 重建上下文，禁止用 checkpoint 覆盖领域状态。
3. **State 拆分复杂度**：开发时需区分"哪些字段进 Domain State"和"哪些字段进 Graph State"，增加思考负担。缓解：在 Harness 中提供 `DomainStateProtocol` 和 `GraphStateProtocol` 两个明确的 TypedDict，字段分配在 protocol 定义中一目了然。

## 迁移策略

1. **L0**（当前任务）：本文档定义 Domain State 与 Graph State 边界。
2. **L2**：在 Harness 中实现 `DomainStateStore`（封装 PG JSONB 读写）和 `GraphState`（TypedDict），将当前 `XuanhuState` 的字段按边界分配到两层。
3. **L3–L8**：所有图节点通过 `DomainStateStore` 读写临床事实，Graph State 仅通过 LangGraph StateGraph 管理。
4. **Legacy 路径**：当前 `XuanhuState` + `state_snapshot` 保持不变，不拆分。Legacy 路径继续使用单一 `XuanhuState`，直到 L9 下线。

## 回滚策略

- Domain State 和 Graph State 的拆分为 Harness 内部实现细节，不影响 API 契约和 SSE 事件。回滚只需将 Harness 内部实现切回单一 `XuanhuState` 模式。
- PG `state_snapshot` 的 JSONB 结构与当前一致，回滚后 Legacy 路径可正常读取。

## 验证方式

- L0-1 契约测试验证本文档章节和边界约束
- L2 单元测试验证 `DomainStateStore` 的正确读写 + `GraphState` 与 LangGraph `InMemorySaver` 的集成
- L2 集成测试验证 Domain State 写入→Graph State `domain_state_version` 引用→恢复重建的完整链路
- 隐私审计：验证 checkpoint 表中无 `name`、`api_key`、`prompt`、`raw_response` 等敏感字段
