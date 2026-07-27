# 悬壶 Agent 整体大修实施计划（LangGraph 版）

> 版本：v1.0-draft
> 日期：2026-07-09
> 上游设计：`docs/01_agent部分优化/多Agent架构设计-Harness版.md`
> 问题基线：`docs/01_agent部分优化/agent-audit-report.md`
> 决策：本轮大修直接采用 LangGraph，不再单独设置“是否迁移 LangGraph”的评估阶段

## 1. 大修目标

本轮不是在现有 Agent 上逐个修补 Prompt，而是建立新的 Harness + LangGraph v2 执行体系，并逐步切换现有 API。

目标：

1. 使用 LangGraph 明确定义节点、条件边、循环、暂停和恢复。
2. 将模型 Agent 收缩为无状态、无写权限、无路由权的语义工作单元。
3. 将完整性、分流、安全、验证和医师批准变成确定性 Gate。
4. 消除 Inquiry/Sufficiency 双重编排。
5. 合并 Prescription/Modification 为可验证的 Formula Draft。
6. 先交付无 RAG 的稳定全流程，再增强 Evidence/RAG。
7. 保持现有 FastAPI、模型网关、安全规则、PostgreSQL、Redis、SSE 和前端接口可渐进迁移。
8. 建立可恢复、可审计、可回放、可评估的 Agent Episode。

## 2. LangGraph 使用原则

### 2.1 使用的核心能力

| LangGraph 能力 | 本项目用途 |
|---|---|
| `StateGraph` | 定义主图和问诊/推理/病历子图 |
| Conditional edges | 根据确定性 GateResult 路由 |
| `Command` | 节点返回 State 更新和下一跳 |
| Checkpointer | 保存图执行游标、支持失败恢复 |
| `interrupt()` | 医师确认、人工补充和高风险处理 |
| `Command(resume=...)` | 恢复医师审核后的图执行 |
| Subgraphs | 隔离 Intake、Reasoning、Review/Record |
| `astream()` | 转换为现有 SSE/Redis 业务事件 |
| State history | 调试和回放图步骤 |

LangGraph 是低层编排框架，可继续使用现有 `ModelGatewayClient`，不要求改用 LangChain 模型封装。

官方参考：

- Overview：
  https://docs.langchain.com/oss/python/langgraph/overview
- Graph API：
  https://docs.langchain.com/oss/python/langgraph/graph-api
- Persistence：
  https://docs.langchain.com/oss/python/langgraph/persistence
- Interrupts：
  https://docs.langchain.com/oss/python/langgraph/interrupts
- Subgraphs：
  https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- Streaming：
  https://docs.langchain.com/oss/python/langgraph/streaming
- Testing：
  https://docs.langchain.com/oss/python/langgraph/test

### 2.2 不采用的做法

- 不使用自由自主的 ReAct Agent 控制医疗主流程。
- 不让模型通过 tool call 任意修改数据库。
- 不把所有临床数据堆进 `MessagesState`。
- 不把 LangGraph checkpoint 当作唯一临床业务数据库。
- 不让节点通过隐式 Prompt 决定路由。
- 不在第一阶段同时引入 LangSmith 生产依赖。
- 不做一次性 Big Bang 上线。

### 2.3 两类 State 的边界

必须区分：

1. **Domain State**：临床事实和业务记录的权威来源，保存在 PostgreSQL 业务表/State Snapshot。
2. **Graph State**：LangGraph 的执行游标和本轮工件引用，只保存运行所需的最小数据。

```text
Domain State (source of truth)
  observations / safety data / artifacts / review / record

Graph State (execution state)
  session_id / domain_state_version / current command
  gate results / artifact IDs / pending interrupt / run budget
```

Graph 节点通过 Repository 读取 Domain State，并通过 Reducer/Repository 提交经过验证的 Delta。Checkpoint 不得成为第二套业务事实真源。

### 2.4 Checkpoint 策略

- 生产使用 `langgraph-checkpoint-postgres` 的异步 PostgreSQL checkpointer。
- root `thread_id = {graph_version}:{session_id}`，以 graph major version 与会话 ID 组成不可混用的持久化命名空间。
- root config 不写 `checkpoint_ns`：LangGraph 1.2.x 将该字段解释为子图 namespace；根图版本隔离由上述 `thread_id` 完成，子图 namespace 由框架管理。
- 测试使用 `InMemorySaver`。
- Checkpoint 中不保存姓名、门诊号、完整 Prompt 和完整原始模型输出。
- Domain 写入与 checkpoint 无法天然形成同一业务事务时，通过幂等键、版本校验和 outbox 保证可恢复。
- 所有 `interrupt()` 前的副作用必须幂等；LangGraph 恢复时会从节点开头重新执行。

## 3. 目标图结构

```text
MainGraph
  START
    │
    ├─ command=message ─► IntakeSubgraph
    │                       ├─ persist_message
    │                       ├─ triage_precheck
    │                       ├─ extract_intake
    │                       ├─ verify_intake
    │                       ├─ reduce_observations
    │                       ├─ triage_gate
    │                       ├─ completeness_gate
    │                       └─ compose_question / ready
    │
    ├─ command=advance ─► ReasoningSubgraph
    │                       ├─ reasoning_precheck
    │                       ├─ draft_syndrome
    │                       ├─ verify_syndrome
    │                       ├─ draft_formula
    │                       ├─ verify_formula
    │                       ├─ safety_check
    │                       ├─ explain_safety(optional)
    │                       └─ doctor_review_interrupt
    │
    ├─ command=review ───► ReviewRecordSubgraph
    │                       ├─ validate_review
    │                       ├─ safety_recheck(if modify)
    │                       ├─ assemble_record
    │                       ├─ narrate_record(optional)
    │                       ├─ verify_record
    │                       └─ persist_record
    │
    └─ command=recover ──► Recovery Router
```

## 4. 阶段总览

| 阶段 | 名称 | 核心产物 | 里程碑 |
|---|---|---|---|
| L0 | 大修基线与迁移护栏 | ADR、兼容矩阵、Golden tests、Feature Flag | 可安全开始重构 |
| L1 | LangGraph Runtime 骨架 | 依赖、GraphState、主图、checkpointer、runner | 图可运行/恢复 |
| L2 | Harness 核心与领域 State | Observation、AgentSpec、Context、Verifier、Reducer | 模型输出先验后写 |
| L3 | Intake 问诊子图 | Extraction、Triage、Completeness、单一下一问 | 无 RAG 问诊版 |
| L4 | 临床推理与方药子图 | Syndrome Draft、Formula Draft、Verifier | 无 RAG 草案链 |
| L5 | Safety 与医师 HITL | Safety Gate、interrupt/review/resume | 等待医师复核闭环 |
| L6 | 病历子图 | RecordAssembler、Narration、Verifier | 无 RAG 全链路 |
| L7 | Evidence/RAG 增强 | EvidencePolicy、claim links、持久化 | RAG 支持链 |
| L8 | 可观测性、评估与安全加固 | Episode、评估集、预算、隐私与故障注入 | 生产质量门禁 |
| L9 | API/UI 切换与旧实现退役 | v2 切流、回滚、清理旧 Supervisor/Agent | 大修完成 |

依赖关系：

```text
L0 → L1 → L2 → L3 → L4 → L5 → L6 → L7 → L8 → L9
                       └──────── 无 RAG 快速版 ────────┘
```

L3 完成即可体验新版问诊；L6 完成形成无 RAG 全流程；L7 后再宣称 RAG 增强完成。

## 5. L0：大修基线与迁移护栏

### 5.1 目标

冻结当前可用行为，建立新旧系统可对比、可切换、可回滚的基础。

### 5.2 工作项

1. 编写架构决策记录：
   - ADR-001：采用 LangGraph。
   - ADR-002：Domain State 与 Graph State 双层边界。
   - ADR-003：Sufficiency 改为 Policy。
   - ADR-004：Prescription/Modification 合并。
   - ADR-005：医师复核使用 interrupt。
2. 建立旧 API 兼容矩阵：
   - `/messages`
   - `/advance`
   - `/review`
   - `/record`
   - `/recover`
   - SSE 事件类型
3. 增加 Feature Flag：
   - `AGENT_RUNTIME_VERSION=legacy|langgraph`
   - 支持按环境和测试切换。
4. 记录当前数据库、Redis、API 和前端行为基线。
5. 建立 Golden E2E 场景：
   - 正常多轮问诊。
   - 信息不足。
   - 过敏。
   - 妊娠/可能妊娠。
   - 红旗症状。
   - 安全失败与修改。
   - 医师确认/修改/拒绝。
   - 病历生成。
6. 给现有 Agent 测试分类：
   - 继续保留。
   - 迁移后改写。
   - 验证旧错误行为、应删除。
7. 记录性能基线：
   - 每回合模型调用数。
   - P50/P95 延迟。
   - Token。
   - 失败率。

### 5.3 产物

- `docs/01_agent部分优化/adr/*.md`
- `tests/golden/`
- Runtime feature flag。
- 新旧兼容矩阵。
- 基线报告。

### 5.4 验收

- Legacy 路径无行为变化。
- Golden 场景能在 Legacy 路径运行。
- Feature Flag 默认仍指向 Legacy。
- 所有后续阶段都有明确回滚点。

## 6. L1：LangGraph Runtime 骨架

### 6.1 目标

建立不包含临床逻辑的 LangGraph 运行底座。

### 6.2 工作项

1. 增加并锁定依赖：
   - `langgraph`
   - `langgraph-checkpoint-postgres`
2. 先做兼容性 Spike：
   - 当前 Python 版本。
   - Pydantic 2.13。
   - FastAPI async。
   - PostgreSQL/连接池。
   - Windows 开发环境。
3. 定义 `XuanhuGraphState`：
   - `session_id`
   - `domain_state_version`
   - `command`
   - `command_id`
   - `graph_version`
   - `run_id`
   - `route`
   - `gate_results`
   - `artifact_refs`
   - `pending_interrupt`
   - `budget`
   - `last_error`
4. 创建目录：

```text
app/agent_runtime/
  graph.py
  state.py
  commands.py
  runner.py
  routing.py
  checkpoint.py
  config.py
  errors.py
  events.py
```

5. 创建 MainGraph：
   - START。
   - command router。
   - 空 Intake/Reasoning/Review 子图占位。
   - END/blocked/manual terminal。
6. 接入 `AsyncPostgresSaver`：
   - 初始化/健康检查。
   - `thread_id=session_id`。
   - graph version namespace。
7. 实现 `GraphRunner`：
   - `ainvoke/astream` 包装。
   - 总超时。
   - 取消。
   - 错误归一化。
   - state version 检查。
8. 建立 LangGraph event → 业务事件转换层。
9. 增加 InMemory checkpointer 单测。
10. 增加 PG checkpointer 集成测试和进程重启恢复测试。

### 6.3 关键约束

- 不把 SQLAlchemy Session 放进 Graph State。
- 不把模型客户端、函数或复杂对象放进 checkpoint。
- 节点通过 Runtime Context/依赖工厂获取资源。
- Graph State 必须 JSON/标准序列化友好。

### 6.4 验收

- MainGraph 可按 command 路由到占位子图。
- 相同 thread ID 可从 checkpoint 恢复。
- 进程重启后可继续未完成图。
- Graph v1/v2 namespace 不互相读取。
- SSE 转换层不暴露 LangGraph 内部事件格式。

## 7. L2：Harness 核心与领域 State

### 7.1 目标

建立模型调用、上下文、验证和 State 提交的统一协议。

### 7.2 工作项

1. 定义 Observation Ledger：
   - `observation_id`
   - `fact_key`
   - `value`
   - `normalized_value`
   - `source_message_id`
   - `status=active/corrected/retracted`
   - `confidence`
   - `created_at`
2. 定义安全信息：
   - allergy collection status。
   - allergens。
   - pregnancy/lactation。
   - medications。
   - major conditions。
   - contraindications。
3. 数据库迁移：
   - observations。
   - artifact revisions。
   - gate results。
   - graph run/step metadata（或扩展 `agent_runs`）。
   - outbox。
4. 定义 Harness 协议：
   - `AgentSpec`
   - `RunSpec`
   - `ContextPacket`
   - `RunArtifact`
   - `VerificationReport`
   - `GateResult`
5. 实现 `AgentRuntime`：
   - 使用现有 `ModelGatewayClient`。
   - 实际透传 model。
   - 每 Agent token/timeout/temperature。
   - 统一总重试预算。
   - DB run/audit 注入。
6. 实现 `ContextBuilder`：
   - 字段白名单。
   - 去身份化。
   - system/developer/context/user 分层。
   - Token 预算。
   - Prompt 严格变量校验。
7. 实现 Verifier Chain：
   - Schema。
   - Output type。
   - Source/provenance。
   - stage prerequisite。
   - stale state version。
8. 实现 Domain Reducer：
   - 去重。
   - 更正。
   - 空值保护。
   - 下游产物失效。
9. 实现 Repository 和事务边界。
10. 建立 outbox publisher。

### 7.3 产物目录

```text
app/agent_runtime/
  specs/
  context/
  verifiers/
  reducers/
  policies/
  repositories/
  observability/
```

### 7.4 验收

- Agent 无法直接写 DB/State。
- 输出类型错误不会推进。
- 旧 State 版本结果不能覆盖新版本。
- 姓名、门诊号默认不进入模型上下文。
- 每个 run 记录实际模型、Prompt、Schema、Policy 版本。
- 重复执行同一 command 不重复提交。

## 8. L3：Intake 问诊子图

### 8.1 目标

交付第一版可体验的无 RAG 问诊流程，移除独立 Sufficiency LLM。

### 8.2 图节点

```text
persist_message
  → triage_precheck
  → build_intake_context
  → extract_intake
  → verify_intake
  → reduce_observations
  → triage_gate
  → completeness_gate
       ├─ ready → mark_ready
       ├─ incomplete → select_gap → compose_question
       ├─ red_flag → triage_interrupt/manual
       └─ conflict → clarification_question
```

### 8.3 工作项

1. 新建 `IntakeExtractionAgent`。
2. 输出只包含：
   - observations。
   - safety delta。
   - red flag candidates。
   - ambiguities。
   - extraction decision。
3. 删除 Inquiry 输出中的路由权和下一问权。
4. 实现 `TriagePolicy`：
   - 红旗规则集。
   - 严重度。
   - 转急诊/人工复核。
5. 实现 `CompletenessPolicy`：
   - 必需/可选维度 Enum。
   - 主诉相关动态门槛。
   - 安全信息三态。
   - 性别/年龄/绝经适用性。
6. 实现 `GapSelector`：
   - 只选择一个最高优先级缺口。
7. 实现 Question Composer：
   - 常见问题模板优先。
   - 模板不足时才调用模型。
8. 改造 `/messages`：
   - 每条消息只运行一次 Intake 子图。
   - 返回 ready 或唯一下一问。
9. 改造 `/advance` 预检查：
   - 只消费持久化 Completeness Gate。
   - 不重跑 Inquiry/Sufficiency。
10. 增加停滞策略：
   - 连续无新增事实。
   - 最大补问轮次。
   - 人工接管。
11. 保留 Legacy 路径用于对比。

### 8.4 测试

- 主诉缺失。
- 一次输入多个事实。
- 历史事实不重复抽取。
- 患者更正信息。
- 助手问题不作为患者事实。
- 明确无过敏 vs 未询问。
- 妊娠适用性。
- 当前用药。
- 红旗阻断。
- Prompt injection。
- 信息充分后不生成问题。
- 并发消息/state version。
- 同 command 幂等。

### 8.5 验收

- 每回合最多一次 Intake 模型调用。
- 简单下一问不调用模型。
- 无 SufficiencyAgent 模型调用。
- 安全信息进入权威 Domain State。
- ready 后不再追问。
- 红旗不能进入 Reasoning。
- L3 可独立作为无 RAG 问诊版本试用。

## 9. L4：临床推理与方药子图

### 9.1 目标

形成无 RAG 的辨证和方药草案链，所有不足情况可拒答或回问。

### 9.2 图节点

```text
reasoning_precheck
  → build_syndrome_context
  → draft_syndrome
  → verify_syndrome
       ├─ needs_more_info → invalidate_downstream → Intake
       ├─ abstained → manual_required
       └─ completed
            → build_formula_context
            → draft_formula
            → verify_formula
                 ├─ needs_more_info → Intake
                 ├─ abstained → manual_required
                 └─ completed → Safety
```

### 9.3 工作项

1. 新建强类型 `SyndromeDraft`：
   - decision。
   - syndrome。
   - fact-linked basis。
   - differential。
   - treatment principle。
   - confidence。
   - evidence mode。
   - missing inputs。
2. 新建 `SyndromeVerifier`：
   - fact IDs 必须 active。
   - 冲突事实阻断。
   - 无 RAG 置信度上限。
   - needs_more_info 不允许伪正常结果。
3. 合并 Prescription/Modification：
   - 新建 `FormulaDraftAgent`。
   - 一次输出 base formula、modifications、candidate formula。
4. 新建 `FormulaConsistencyVerifier`：
   - 根据 base + actions 重算 candidate。
   - 药味规范化。
   - 剂量/单位。
   - modification 与 composition 对应。
   - 症状/证型依据引用。
5. 显式无 RAG 模式：
   - `evidence_mode=model_knowledge_only`。
   - `review_required=true`。
   - 禁止伪造出处/citation。
6. 实现产物 revision：
   - syndrome revision。
   - formula revision。
   - superseded/stale。
7. 回问时使后续产物失效。
8. 兼容现有前端结果卡片或增加 v2 DTO 转换。

### 9.4 验收

- 信息不足不会用特殊字符串伪装 completed。
- 不足时图正确返回 Intake。
- Formula 可以由 base + modifications 确定性重建。
- 无 RAG 输出明确标记。
- Agent 不直接决定下一阶段。
- Legacy 的 Prescription/Modification 不再进入 v2 图。

## 10. L5：Safety 与医师 HITL

> 2026-07-22 沙盒范围注记：本节原医疗产品/医师目标只保留为未来临床轨道背景。当前个人学习、非临床、仅合成数据范围的 L5-1～L5-4 必须以 [L5 个人学习工程沙盒准入包](L5个人学习工程沙盒准入包-2026-07-22.md) 为准，使用 `Sandbox Reviewer` 和测试身份字段；不得将工程测试解释为医师复核、临床批准或真实处方授权。

### 10.1 目标

将确定性安全审核和医师复核变成 LangGraph 的硬 Gate/interrupt。

### 10.2 图节点

```text
safety_precheck
  → safety_rule_engine
  → safety_gate
       ├─ failed_revisable → formula_revision_loop
       ├─ blocker/limit → manual_required
       └─ passed → safety_explanation(optional)
                    → doctor_review_interrupt
```

### 10.3 工作项

1. 保留并适配现有 `SafetyRuleEngine`。
2. 修复安全输入：
   - 使用问诊更新后的 allergies/pregnancy/medications。
3. 按实际审核对象和 issue 类型决定回退。
4. Safety 失败创建新 Formula revision，不原地覆盖。
5. 实现 rollback/revision 次数限制。
6. SafetyExplanationAgent：
   - 只读固定 issue IDs。
   - 数量一致性验证。
   - 保守文案。
   - recommendations 有明确消费者。
7. 实现 `doctor_review_interrupt`：
   - interrupt payload 只含可序列化 review DTO。
   - 不在 interrupt 前执行非幂等副作用。
8. 使用同一 `thread_id` 和 `Command(resume=...)` 恢复。
9. 支持：
   - confirm。
   - modify。
   - reject。
   - request_more_info。
10. 医师修改后重新运行 SafetyRuleEngine。
11. 修改 review API 作为 LangGraph resume 适配层。
12. 增加中断期间进程重启恢复测试。

### 10.4 验收

- SafetyRuleEngine 是 passed/issues 的唯一权威。
- 未通过不能进入医师确认。
- 未确认不能进入 Record。
- 医师修改后必须二次安全检查。
- interrupt 可跨进程恢复且不产生重复 DB 写入。
- 同一 review resume 具有幂等性。

## 11. L6：病历子图

### 11.1 目标

完成无 RAG 的问诊到最终病历全链路。

### 11.2 图节点

```text
validate_doctor_review
  → assemble_record_json
  → narrate_record(optional)
  → verify_record
  → persist_record
  → done
```

### 11.3 工作项

1. Doctor Review DB 验证移到 Record 前。
2. 新建强类型 `MedicalRecordData`。
3. 实现 `RecordAssembler`：
   - 从已确认 State 确定性构建 JSON。
   - 使用医师确认的 formula revision。
   - 使用权威 Safety result。
   - 服务端固定 disclaimer。
4. 重构 `RecordNarrationAgent`：
   - 只输出允许润色的文本段。
   - 不输出 doctor review、formula、安全结论和新建议。
5. 实现 `RecordConsistencyVerifier`：
   - 文本关键字段和 JSON 一致。
   - 无新症状/诊断/方药。
   - 医师确认 ID 一致。
6. 落库幂等：
   - session + reviewed revision + version。
7. 保持现有病历读取、编辑和导出 API。
8. 完成 `session.done` 事件适配。

### 11.4 验收

- 无医师确认时 Record 节点不调用模型。
- 最终 formula、安全结论和 review 不能被 LLM 改写。
- disclaimer 不依赖模型。
- 重放/恢复不生成重复病历。
- L3～L6 形成完整无 RAG 版本。

## 12. L7：Evidence/RAG 增强

### 12.1 目标

在新 Harness 上接入可审计 Evidence，不改变无 RAG 流程正确性。

### 12.2 工作项

1. 定义 `EvidencePacket`：
   - evidence ID。
   - source type。
   - source/chunk ID。
   - score/rank。
   - content digest。
   - retrieval trace。
2. 定义 Evidence source policy：
   - Syndrome：theory/case。
   - Formula：formula/herb。
3. RAG 检索节点独立于模型节点。
4. 检索 trace 继承 graph run trace。
5. 写入 `agent_evidences`。
6. 实现 claim-to-evidence：
   - 每条辨证依据。
   - 方名。
   - 药味。
   - 剂量。
   - 加减理由。
7. Verifier 检查：
   - ID。
   - source type。
   - 本 run 可见性。
   - claim link 完整性。
8. 定义 RAG 不可用策略：
   - configured fallback to model knowledge。
   - configured hard block。
9. 无 Evidence 时不得输出伪 citation/出处。
10. RAG 结果进入 Context 的 data/tool 层，不进入 system。
11. 建立 RAG 回归和质量评估集。

### 12.3 验收

- 每个 citation 可追溯到 retrieval run。
- 不同 Agent 不能引用错误 source type。
- 无 RAG/有 RAG 模式结果明确区分。
- RAG 故障不会破坏 checkpoint 恢复。
- 无 RAG 路径仍能独立运行。

## 13. L8：可观测性、评估与安全加固

### 13.1 目标

建立上线前生产门禁。

### 13.2 工作项

1. Episode Package：
   - command。
   - State hash。
   - node trajectory。
   - Agent/Prompt/Schema/Policy/Graph versions。
   - model actual/usage。
   - Evidence。
   - Verification。
   - Gate decisions。
   - human interventions。
   - failure attribution。
2. LangGraph stream → SSE 事件：
   - node.started。
   - node.completed。
   - gate.failed。
   - interrupt.required。
   - graph.completed/failed。
3. 敏感数据策略：
   - 日志脱敏。
   - checkpoint 最小化。
   - Prompt/输出受控保存。
   - 数据保留与删除。
4. 总预算：
   - 模型调用数。
   - Token。
   - deadline。
   - retry。
5. 故障注入：
   - Gateway timeout。
   - RAG unavailable。
   - PostgreSQL transient failure。
   - Redis failure。
   - checkpoint failure。
   - duplicate resume。
   - state conflict。
6. 评估集：
   - Intake。
   - Triage。
   - Completeness。
   - Prompt injection。
   - Syndrome consistency。
   - Formula consistency。
   - Safety。
   - Review。
   - Record。
7. 新旧 Shadow 对比：
   - 相同去标识输入。
   - 不将 v2 输出写入生产业务结果。
   - 比较质量、延迟、Token、失败率。
8. 可选接入 LangSmith 仅用于非敏感开发/测试；生产接入另行做隐私审查。

### 13.3 验收

- 每次失败能归因到 node/model/tool/policy/verifier/persistence。
- 所有 P0/P1 审查问题有回归测试或明确关闭记录。
- 隐私检查通过。
- 故障恢复和重复执行测试通过。
- v2 关键指标不低于设定门槛。

## 14. L9：API/UI 切换与旧实现退役

### 14.1 目标

逐步将生产流量切到 LangGraph，并移除双重实现。

### 14.2 工作项

1. API Adapter：
   - 保持现有响应 envelope。
   - Graph DTO → API DTO。
   - Interrupt → review/人工处理响应。
2. 前端适配：
   - 新 Stage 映射。
   - ready/needs_input/triage_hold/manual_required。
   - node 运行状态。
   - interrupt/review 恢复。
3. SSE 兼容：
   - 保留已有事件。
   - 新增 v2 事件版本字段。
4. 切流顺序：
   - 开发环境。
   - 自动测试。
   - 内部试用。
   - 小比例会话。
   - 全量新会话。
5. 会话迁移策略：
   - 迁移期间旧会话继续 Legacy。
   - 新会话按 flag 进入 v2。
   - 不在运行中强行迁移旧 thread。
6. 回滚策略：
   - 未完成 v2 会话保持 checkpoint。
   - 新会话可切回 Legacy。
   - 已产生 v2 artifact 不覆盖 Legacy 数据。
7. 全量稳定后删除：
   - Legacy Supervisor 主路由。
   - SufficiencyAgent。
   - PrescriptionAgent/ModificationAgent v1。
   - 旧 Prompt Loader/manifest（若无消费者）。
   - 死 `next_stage`。
   - 不可达 Safety 分支。
8. 更新部署、运维和故障恢复文档。

### 14.3 验收

- 新会话全部使用 LangGraph。
- 旧会话按原 runtime 完成，不混图。
- 回滚演练通过。
- Legacy 代码删除后全量测试通过。
- 无双写、双路由、双 State 真源。

## 15. 每阶段统一质量门禁

每阶段至少执行：

```powershell
uv run pytest <阶段专项> -q -rs
uv run pytest -q -rs
uv run ruff check .
uv run mypy app
uv lock --check
```

涉及 PostgreSQL checkpointer、Domain Repository、Safety 或 API 时必须跑真实 PostgreSQL 集成测试。

涉及前端时：

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test
npm run build
```

额外门禁：

- 无真实模型依赖的自动化测试。
- 所有真实模型试跑单独标记，不进入普通 CI。
- 不记录完整 Prompt、完整原始模型输出、API Key 或患者身份。
- 每个新节点必须有正常、拒绝、失败、重放/幂等测试。
- 每个 conditional edge 必须有路由测试。
- 每个 interrupt 必须有暂停、恢复、重复恢复和进程重启测试。

## 16. 建议任务拆分

每个阶段继续拆成小任务，建议：

| 阶段 | 建议任务 |
|---|---|
| L0 | L0-1 ADR/兼容矩阵；L0-2 Golden tests；L0-3 Feature Flag/基线 |
| L1 | L1-1 依赖与 Spike；L1-2 StateGraph；L1-3 PG checkpointer；L1-4 Runner/stream |
| L2 | L2-1 Domain schemas/migration；L2-2 AgentRuntime；L2-3 Context；L2-4 Verifier/Reducer；L2-5 outbox |
| L3 | L3-1 Intake Agent；L3-2 Triage；L3-3 Completeness；L3-4 Question；L3-5 API 集成 |
| L4 | L4-1 Syndrome；L4-2 Formula；L4-3 consistency verifier；L4-4 revision/回问 |
| L5 | L5-1 Safety adapter；L5-2 Safety explanation；L5-3 interrupt；L5-4 review resume |
| L6 | L6-1 RecordAssembler；L6-2 Narration；L6-3 consistency/落库；L6-4 API/E2E |
| L7 | L7-1 Evidence schema/policy；L7-2 retrieval nodes；L7-3 claim links；L7-4 eval |
| L8 | L8-1 Episode/metrics；L8-2 security；L8-3 failure injection；L8-4 behavior eval/shadow |
| L9 | L9-1 API/UI adapter；L9-2 staged rollout；L9-3 rollback drill；L9-4 legacy removal |

每个任务应只覆盖一个可独立验收的边界，避免单个任务同时修改 Graph、Schema、API、前端和 RAG。

## 17. 关键风险与控制

| 风险 | 控制 |
|---|---|
| LangGraph checkpoint 与业务 State 双真源 | Graph State 只存执行引用，Domain State 权威 |
| interrupt 恢复重复副作用 | interrupt 前副作用幂等，command/review 使用幂等键 |
| 框架迁移同时改临床逻辑难定位 | L1/L2 先建空图和 Harness，再逐子图迁移 |
| 大修周期内 Legacy 持续变化 | L0 后限制 Legacy Agent 新功能，只修阻断 Bug |
| Checkpoint 含敏感医疗数据 | 最小 Graph State、去身份化、保留策略 |
| StateGraph 变更导致旧 checkpoint 不兼容 | graph version namespace，新会话切换，不强迁旧 thread |
| 自动循环失控 | 总节点步数、调用次数、Token、deadline 上限 |
| 模型输出合法但临床不一致 | 独立 Verifier、Policy、Safety、医师复核 |
| RAG 延迟整体交付 | L3～L6 明确支持 no-RAG 模式 |
| LangGraph 内部事件污染 API | 事件转换层，外部只暴露版本化业务事件 |

## 18. 大修完成定义

同时满足以下条件才视为完成：

1. 新会话统一进入 LangGraph v2。
2. Inquiry/Sufficiency 双调用消失。
3. Sufficiency 已改为确定性 Policy。
4. Triage/Completeness/Safety/Doctor Review 都是硬 Gate。
5. Prescription/Modification 已合并为 Formula Draft + Verifier。
6. 所有模型输出均先验证后提交。
7. 医师确认使用可恢复 interrupt。
8. Record 核心 JSON 确定性构建。
9. Domain State 是唯一临床事实真源。
10. Checkpoint 可跨进程恢复。
11. run、node、gate、evidence、human intervention 可审计。
12. no-RAG 与 RAG 模式明确可区分。
13. Legacy Agent 主路由和死代码已删除。
14. 后端、前端、E2E、故障恢复和行为评估门禁通过。

## 19. 近期执行顺序

建议立即按以下顺序启动：

1. L0-1：ADR、兼容矩阵和迁移边界。
2. L0-2：Golden E2E 基线。
3. L0-3：Runtime Feature Flag。
4. L1-1：LangGraph/PG checkpointer 兼容性 Spike。
5. L1-2：最小 MainGraph + InMemorySaver。
6. L1-3：AsyncPostgresSaver 和恢复测试。

在 L1 骨架验收前，不开始批量重写业务 Agent；否则会把框架问题和业务问题混在一起。

## 20. 2026-07-27 双轨状态覆盖说明

本节只覆盖当前完成状态，不改写上述目标路线和产品完成定义。

- L5-SBX、L6-SBX：`ACC-20260727-056` / `45acf54` 曾完成第一轮重新验收，后因实时撤权缺口由 `ACC-20260727-057` 追加式重新打开；当前以 `e8e0973` / `ACC-20260727-058` 的 offline/fixed-synthetic/in-memory reference composition 为准。它证明权限重放、实时 authorizer、sealed review/recheck、active v2 record projection、canonical in-memory persistence 和 deterministic narration 的参考合同，不代表应用已接线。
- L5-PROD、L6-PROD：未完成。Doctor Review 硬 Gate、narration 后 verifier/allowlist agent、产品 RecordSubgraph/API、数据库幂等、`session.done`、no-RAG E2E 和可信 bootstrap 配置仍须另行设计、实现与验收。
- L7：旧发布 `e8f0666` 与实现 `c5b7152` 已由 `21004b9` 撤回。当前只允许以 `e8e0973` 为 clean 基线重新发布新的 L7-SBX bounded task；L7-PROD 不获授权。
- 第 15 节所要求的数据库集成门禁仍适用于产品实现。本轮因 `TEST_DATABASE_URL`、`DATABASE_URL` 未配置而未执行 DB integration，因此不得用 SBX 验收替代该门禁。
- 第 18 节“大修完成定义”保持不变；当前项目尚未达到整体大修完成。
