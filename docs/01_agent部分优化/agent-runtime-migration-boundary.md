# Agent Runtime 迁移边界

## 文档说明

本文档定义 Legacy（当前 `Supervisor` + `AgentRegistry`）与 LangGraph（目标 `StateGraph` + `checkpointer`）之间的迁移边界，包括会话隔离、恢复边界、状态边界、安全边界、阶段边界和回滚边界。L0-2 负责 Golden 基线测试，L0-3 负责 Feature Flag 与性能基线，本任务只定义边界。

所有边界约束均为不可变架构规则，不得在 L1–L9 的任何阶段被绕过或削弱。

---

## 1. Domain State 与 Graph State 边界

### 不可变规则

1. **Domain State 是临床事实的唯一权威（Single Source of Truth）**。
   - 存储于 PostgreSQL `consult_sessions.state_snapshot`（JSONB）及业务表（`consult_messages`、`medical_records`、`doctor_reviews`、`safety_rule_runs`）。
   - 所有临床决策（辨证结果、处方、安全审核结论、病历）必须以 Domain State 为准。
   - Graph State checkpoint 中的数据不可直接用于临床决策。

2. **Graph State / checkpoint 只保存最小可序列化执行数据和引用**，严格对齐《Agent整体大修实施计划-LangGraph版.md》§6.2 `XuanhuGraphState` 定义。
   - 内容：`session_id`（对应 `thread_id`）、`domain_state_version`（整型引用）、`command`（当前待执行命令）、`command_id`（命令幂等键）、`graph_version`、`run_id`、`route`（路由目标）、`gate_results`（Policy Gate 判定结果引用）、`artifact_refs`（Domain artifact UUID 引用集合，如 `doctor_review_ref`、`safety_rule_result_ref`、`syndrome_result_ref` 等）、`pending_interrupt`（挂起中断信息）、`budget`（执行预算追踪）、`last_error`（最近错误信息）。
   - Doctor Review 以 `interrupt()`/checkpoint 为硬门控，不通过 `pending_review` 字段作为第二套控制真源。
   - Graph State 序列化后大小应恒定 < 1KB。

3. **checkpoint 禁止保存以下内容**：
   - SQLAlchemy `AsyncSession` 或任何 ORM 对象
   - 模型客户端（如 `openai.AsyncOpenAI`）
   - Python 函数、方法、类或任何可调用对象
   - 完整 Prompt 文本（只保存 `prompt_version` 引用字符串）
   - 完整原始模型输出（只保存 `prompt_version` 引用和输出 token 计数，不得保存结构化临床模型输出）
   - 结构化临床模型输出（如 `SyndromeResult`、`FormulaDraft`、`SafetyRuleResult` 等 — 这些属于 Domain State，Graph State 仅保存 UUID 引用）
   - 患者身份信息（`patient_info.name` 或任何 PII 字段）

4. **禁止 Domain State / Graph State 双真源（Dual Source of Truth）**：
   - 同一临床事实不得同时存在于 Domain State 和 Graph State 中。
   - 例如：`syndrome_result` 只存储于 Domain State，Graph State 中不存储完整的 `syndrome_result`，只通过 `artifact_refs` 和 `domain_state_version` 引用。
   - 恢复时从 Graph State 获取 `domain_state_version` → 从 Domain State 加载完整临床数据 → 重建执行上下文。

---

## 2. 会话隔离边界

### 不可变规则

1. **Legacy 与 LangGraph 会话运行时隔离**。
   - 会话在创建时确定运行时身份（通过 `AGENT_RUNTIME_VERSION` Feature Flag 或请求参数），身份一旦确定后不可隐式切换。
   - 创建时未指定运行时的会话默认为 `legacy`。
   - 在 L0–L8 期间，两类会话同时存在，各自走独立的执行路径。

2. **两类会话不得互相恢复**：
   - Legacy 会话的恢复由 `RecoveryService` 处理（PG `state_snapshot` + Redis `xuanhu:checkpoint:` + 最近审计事件一致性检查）。
   - LangGraph 会话的恢复由 LangGraph checkpointer 处理（`thread_id` = `session_id` + graph-version namespace + `Command(resume=...)`）。
   - **严禁** Legacy 会话通过 LangGraph checkpointer 恢复，或 LangGraph 会话通过 Legacy `RecoveryService` 恢复。
   - `POST /recover` API 处理器必须检查会话的运行时身份，路由到正确的恢复路径。

3. **执行路径隔离**：
   - Legacy 会话：`MessageService` + `Supervisor.advance()` + `ReviewService.review()` + `RecoveryService.recover()`
   - LangGraph 会话：`IntakeSubgraph` + `ReasoningSubgraph` + `ReviewRecordSubgraph` + LangGraph `Command(resume=...)` + checkpointer 恢复
   - 两类路径不得在同一个会话的生命周期中混合使用。

---

## 3. 恢复边界

### Legacy 恢复（当前实现，保持不变）

- 处理组件：`RecoveryService`（`app/services/recovery.py`）
- 支持四种动作：`resume_from_pg_snapshot`、`retry_current_stage`、`rollback_to_stage`、`terminate`
- 一致性检查：Redis checkpoint vs PG state_snapshot vs 最近审计事件（B-005）
- checkpoint 缺失时降级为 PG snapshot 恢复，记录降级事实到审计

### LangGraph 恢复（L1 L2 实现）

- 处理组件：LangGraph checkpointer（`AsyncPostgresSaver`）+ `graph.update_state()` + `Command(resume=...)`
- `thread_id` 使用 `{graph_version}:{session_id}` 的稳定命名空间：每个图版本、每个会话一个 thread，旧图版本 checkpoint 不得被新图版本恢复
- graph-version namespace：checkpoint 按图版本隔离（tuple 形式的 `config["configurable"]`），图版本变更时旧 checkpoint 不可用于恢复
- 恢复步骤：
  1. 从 `thread_id` 加载 checkpoint
  2. 获取 `domain_state_version` 引用
  3. 从 PG 加载 Domain State
  4. 重建 Graph State 执行上下文
  5. 通过 `Command(resume=...)` 从暂停点继续执行
- `retry_current_stage`：通过 `graph.update_state(config, state)` 重置当前节点状态后 `Command(resume=...)` 重新执行
- `rollback_to_stage`：通过 `graph.update_state(config, state)` 回退到目标节点的 checkpoint，然后 `Command(resume=...)`

### 恢复隔离保证

- `POST /recover` 在处理 LangGraph 会话时，不得调用 `RecoveryService._do_resume_from_snapshot`、`_do_retry_current_stage`、`_do_rollback_to_stage` 或 `_do_terminate` 中的 PG 直接操作。
- `POST /recover` 在处理 Legacy 会话时，不得调用 LangGraph checkpointer 的任何方法。

---

## 4. 降级与切换边界

### 不可变规则

1. **禁止因 LangGraph 错误静默降级到 Legacy**：
   - LangGraph 路径在执行中遇到异常（Agent 失败、checkpointer 异常、图执行错误）时，必须按 LangGraph 的错误处理机制响应（重试、blocked、返回错误），**不得** 静默切换到 Legacy 路径重试。
   - 唯一允许的全局回滚动作：人工将 Feature Flag 的“新会话默认运行时”从 `langgraph` 改为 `legacy`，且变更必须写入 `audit_events`（`runtime.switched`），记录操作人、时间、原因。该动作不改变任何既有会话的运行时身份。

2. **运行时切换必须显式、可审计**：
   - 切换必须通过管理 API 或配置变更（手动操作），不得通过代码自动触发。
   - 每次切换写入一条 `audit_events` 记录，`event_type="runtime.switched"`，`payload` 包含 `from_runtime`、`to_runtime`、`operator`、`reason`、`timestamp`。
   - 切换不影响已创建的会话——已有会话继续使用创建时的运行时身份，直到会话结束。

3. **错误响应一致性**：
   - 无论 Legacy 还是 LangGraph 路径，同一错误场景（如 `SESSION_BUSY`、`INVALID_STATE_VERSION`）的 HTTP 状态码、错误码和响应 Schema 必须一致。

---

## 5. L0 / L1 / L9 阶段边界

### L0：基线与护栏

- **L0-1 允许**：建立 ADR、兼容矩阵、迁移边界和文档契约测试
- **L0-2 允许**：在 `tests/golden/` 建立 Legacy Golden 行为基线和测试分类
- **L0-3 允许**：增加受校验的 `AGENT_RUNTIME_VERSION=legacy|langgraph` 配置和性能基线；默认必须为 `legacy`
- **禁止**：实现 LangGraph、checkpointer、Harness、运行时路由或数据库迁移
- **禁止**：因 Feature Flag 改变既有 Legacy API/Agent 行为；`langgraph` 值在 L1 Runtime 实现前只完成配置校验，不得进入未实现路径
- **交付**：5 份 ADR、兼容矩阵、迁移边界、契约测试、Golden 测试、Feature Flag、基线报告和交接文件

### L1：LangGraph Runtime 骨架

- **允许**：搭建空图 + checkpointer + `interrupt()` + `astream()` → SSE 映射
- **禁止**：接入业务 Agent、修改现有 API 行为、实现 Domain State 拆分
- **允许**：新增 `app/agents/langgraph/` 目录下的骨架代码
- **禁止**：修改 `Supervisor`、`MessageService`、`ReviewService`、`RecoveryService`

### L2：Harness 核心与 Domain State

- **允许**：实现 `DomainStateStore` + `GraphState` TypedDict
- **禁止**：在图中执行业务逻辑
- **允许**：新增 `app/harness/` 目录
- **禁止**：修改现有 `XuanhuState` Schema（Legacy 路径仍需它）

### L3–L8：阶段 Agent 迁移

- **允许**：逐阶段将 Agent 从 Legacy 迁移到 LangGraph 子图
- **禁止**：删除任何 Legacy Agent 实现
- **禁止**：在同一个阶段混合 Legacy 和 LangGraph 路径
- **必须**：每个阶段通过 L0-2 Golden E2E 基线测试验证行为等价

### L9：Legacy 下线

- **允许**：将 `AGENT_RUNTIME_VERSION` 默认值改为 `langgraph`
- **禁止**：在 L9 验收通过前删除 Legacy 代码（保留为只读参考）
- **必须**：所有会话通过 LangGraph 路径执行，Legacy 路径不再被调用
- **允许**：L9-4 验收通过后最终移除 Legacy 代码，移除时机与实施计划 L9-4 一致

---

## 6. 数据库、Redis、事件、会话和回滚边界

### 数据库边界

- **PG 表 Schema**：`consult_sessions`、`consult_messages`、`doctor_reviews`、`medical_records`、`safety_rule_runs`、`audit_events` 在 L0–L9 期间不可有破坏性变更（不可删除列/表、不可改变已有列类型、不可改变已有约束语义）。
- **可扩展**：允许新增列（nullable）、新增表（如 LangGraph checkpoint 表 `checkpoints`、`checkpoint_writes`），不允许破坏性变更。
- **事务边界**：Domain State 写入与业务表写入在同一事务中。checkpoint 写入由 LangGraph 管理独立事务。

### Redis 边界

- **现有键名约定不变**：`xuanhu:events:{session_id}`（事件流）、`xuanhu:checkpoint:{session_id}`（Legacy checkpoint）、会话锁键格式。
- **不可变**：`EVENT_STREAM_MAXLEN=1000`、会话锁 TTL=90s（由 `session_lock_ttl_seconds` 配置控制）。
- **可扩展**：允许新增 Redis 键用于 LangGraph 特定功能（如缓存、限流），不允许移除或重命名现有键。

### 事件边界

- **13 种 SSE 事件类型不可移除**：`stage.changed`、`message.created`、`agent.started`、`agent.finished`、`agent.failed`、`review.required`、`safety.blocked`、`session.blocked`、`session.done`、`session.terminated`、`doctor.reviewed`、`heartbeat`、`resync`。
- **事件 payload 敏感字段过滤**：`_FORBIDDEN_PAYLOAD_KEYS` 约束在所有路径（Legacy + LangGraph）中强制执行。
- **`review.required` 契约**：必须包含 `modified_formula`，禁止包含 `base_formula`（P3-3 契约）。

### 会话边界

- **会话生命周期**：CREATED → inquiry → sufficiency → syndrome → prescription → modification → safety → review → record → done（Legacy 路径）/ CREATED → inquiry → syndrome → formula → safety → review → record → done（LangGraph 路径，SUFFICIENCY 合并为 CompletenessPolicy 确定性 Gate，PRESCRIPTION + MODIFICATION 合并为 FORMULA，SYNDROME 保留为独立阶段边界）。
- **终态**：`done`（病历已生成）、`terminated`（人工终止）、`blocked`（异常阻塞）。三类终态不可逆转为非终态（除 `blocked` 可通过恢复操作解除外）。
- **会话所有关系**：会话归属于创建时的运行时身份。会话结束后其运行时身份不再改变。

### 回滚边界

- **Legacy 会话回滚**：通过 `RecoveryService._do_rollback_to_stage`，合法目标阶段为 `VALID_ROLLBACK_TARGETS` = {inquiry, sufficiency, syndrome, prescription, modification, safety, review, record}。
- **LangGraph 会话内部恢复**：只允许在同一 `{graph_version}:{session_id}` namespace 内，通过受验证的恢复命令和 checkpoint 历史恢复；不得把既有 LangGraph 会话重建、转换或转交给 Legacy `RecoveryService`。
- **Feature Flag 回滚**：将 `AGENT_RUNTIME_VERSION` 切回 `legacy`，所有新会话走 Legacy 路径。已有 LangGraph 会话不受影响（继续使用 LangGraph 直到会话结束）。切换写入 `audit_events` 记录。
- **数据不丢失保证**：Domain State 是所有临床事实的唯一权威，存储在 PG 中独立于 LangGraph checkpoint。Feature Flag 回滚后只有新会话使用 Legacy；既有 LangGraph 会话继续在原 graph-version namespace 内恢复和结束，不得用 Domain State 将其跨运行时重建为 Legacy 会话。

---

## 7. 安全硬边界

### SafetyRuleEngine 不可绕过的规则

1. **`SafetyRuleEngine` 始终是安全结论的权威**：`passed` / `issues` / `severity` / `rollback_target` 等字段由 `SafetyRuleEngine.check()` 确定性输出，模型不得覆盖。
2. **`SafetyAgent`（LLM）仅生成解释文本**：其输出（`SafetyExplanation`）仅用于补充 `explanation` / `explanation_issues` / `recommendations` 字段，**不得**修改 `safety_review.passed`、`safety_review.issues`、`safety_review.severity` 或 `safety_review.rollback_target`。
3. **模型不得绕过安全规则**：在任何状态下（inquiry、formula、review、record 等），模型不得生成允许绕过安全审核的回复或建议。模型不得建议医师"忽略"或"接受"安全审核问题（MVP 不支持 `SAFETY_ACCEPT_RISK`）。
4. **安全审核的执行顺序固定**（与设计文档 §10.1 一致）：normalize → convert_dose → unknown_herb → eighteen_incompatibilities → nineteen_fears → pregnancy → combination → dose_limit → allergy → deduplicate → sort。执行顺序不可在运行时由模型或上下文改变。
5. **安全规则执行必须写入不可变记录**：每次 `SafetyRuleEngine.check()` 写入一条 `safety_rule_runs` 记录，包含 `rule_version`、`execution_order`、`formula_snapshot`、`normalized_formula`、`patient_snapshot`（已脱敏，`exclude={"name"}`）。

### 额外安全约束

- **妊娠状态未知时的保守处理**：`pregnancy_status ∈ {unknown, null, ""}` 时生成 `WARNING` 级别的 `CAUTION` issue，医师必须确认。
- **`pregnancy_status ∈ {pregnant, possible}` 时严格处理**：两种状态同等严格，触发 `BLOCKER`（禁用）和 `HIGH`（慎用）的妊娠禁忌检查。
- **未知药名的保守假设**：无法在 `herbs` 知识库中查到的药材无法执行后续检查，按 `HIGH` 级别 `CAUTION` 阻断后续流程（§1.1 保守假设原则）。

---

## 8. Doctor Review 硬边界

### 不可绕过规则

1. **Doctor Review 是不可绕过的 hard gate**：有效复核前（`action ∈ {confirm, modify}` + 可追溯的 `review_id` + `action="modify"` 时二次安全审核通过），**不得** 执行以下任何操作：
   - 写入 `medical_records` 表
   - 将 `session.status` 置为 `done`
   - 发射 `session.done` SSE 事件
   - 将 `current_stage` 置为 `done`

2. **医师修改处方后必须重新执行安全审核**：`action=modify` 时，医师提交的 `formula_override` 必须通过 `SafetyRuleEngine.check(formula_source="doctor_override")` 完整审核。审核不通过时，图必须再次暂停（`interrupt()`），告知医师具体的安全问题（`SafetyIssue` 列表），**不得**直接推进到 record。

3. **模型不得代表医师做 Decision**：
   - LLM 不得生成 `confirm` / `modify` / `reject` 动作。
   - 只有来自 `POST /review` API（真人医师操作）的 `Command(resume=...)` 才能解除 `interrupt()`。
   - 模型不得在自然语言回复中建议绕过医师复核。

4. **review_id 可追溯性**：
   - 每次 Doctor Review 必须写入一条 `doctor_reviews` 记录（不可变）。
   - `review_id` 必须可追溯到 `doctor_reviews.id`。
   - `medical_records.doctor_review_id` 必须关联到对应的 `doctor_reviews.id`（B-014 校验）。

5. **病历生成前置复核校验**：RECORD → DONE 前必须验证 `doctor_review` 存在、`review_id` 有效且 action 为 `confirm` 或 `modify`（与 Legacy 路径的 B-014 等价）。

---

## 9. 禁止删除 Legacy 实现

在 L0–L8 的所有阶段中，**禁止删除任何 Legacy 实现代码**。具体包括：

- `app/agents/supervisor.py`（`Supervisor` 类）
- `app/services/message.py`（`MessageService` 类）
- `app/services/review.py`（`ReviewService` 类）
- `app/services/recovery.py`（`RecoveryService` 类）
- `app/services/events.py`（`EventService` 类）
- `app/agents/registry.py`（`AgentRegistry` 类）
- 所有现有 Agent 实现：`InquiryAgent`、`SufficiencyAgent`、`SyndromeAgent`、`PrescriptionAgent`、`ModificationAgent`、`RecordAgent`、`SafetyAgent`
- `app/safety/engine.py`（`SafetyRuleEngine` 类）
- `app/schemas/agent.py`（`XuanhuState`、所有 Agent 输出 Schema）

Legacy 代码可以：
- 被标记为 deprecated（docstring 或注释）
- 被条件编译跳过（Feature Flag 为 `langgraph` 时不调用）
- 在 L9 验收通过后最终移除

Legacy 代码不得被：
- 删除
- 修改行为逻辑（允许 bugfix，不允许重构）
- 重命名（会影响 Legacy 路径的导入）

---

## 10. 契约测试验证

本文档的所有不可变边界通过 `tests/test_l0_1_contract.py` 进行纯文件契约测试验证。测试不调用数据库、Redis 或真实模型。

测试覆盖：
- ADR 文件存在性和章节完整性
- 兼容矩阵的端点覆盖（9 个端点全部覆盖）
- SSE 事件类型覆盖（13 种事件全部覆盖）
- 迁移边界的不可变规则（Domain State 权威、checkpoint 约束、会话隔离、禁止静默降级、SafetyRuleEngine 硬边界、Doctor Review 硬边界、禁止删除 Legacy）
- 文档内部一致性（ADR 之间无冲突、ADR 与兼容矩阵之间的引用一致、ADR 与迁移边界之间的引用一致）

---

## 参考资料

- [ADR-001：采用 LangGraph 作为 Agent 编排框架](adr/ADR-001-adopt-langgraph.md)
- [ADR-002：Domain State 与 Graph State 双层状态边界](adr/ADR-002-domain-state-and-graph-state-boundary.md)
- [ADR-003：Sufficiency Policy 由确定性规则控制而非模型](adr/ADR-003-sufficiency-as-policy.md)
- [ADR-004：合并 PrescriptionAgent 和 ModificationAgent 为 FormulaDraftAgent](adr/ADR-004-merge-prescription-and-modification.md)
- [ADR-005：Doctor Review 作为 LangGraph interrupt 硬门控](adr/ADR-005-doctor-review-interrupt.md)
- [Legacy API / SSE 兼容矩阵](legacy-api-compatibility-matrix.md)
- [Agent整体大修实施计划-LangGraph版.md](../Agent整体大修实施计划-LangGraph版.md) 第 5 节（L0 基线与护栏）、第 15 节（统一质量门禁）、第 17 节（关键风险与控制）
- [多Agent架构设计-Harness版.md](../多Agent架构设计-Harness版.md) 第 2 节（设计原则）、第 4 节（Model Agent 与确定性组件）、第 5 节（新状态机）、第 20 节（架构决策摘要）
