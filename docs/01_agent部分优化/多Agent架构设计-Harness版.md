# 悬壶多 Agent 架构设计（Harness 版）

> 版本：v1.0-draft
> 日期：2026-07-09
> 适用范围：悬壶中医辅助问诊、辨证、方药草案、安全审核、医师复核与病历生成
> 设计目标：以 LangGraph 承载 Harness 编排，先支持无 RAG 的可靠快速版，再平滑增强 Evidence/RAG

## 1. 设计背景

当前实现把较多责任放在 LLM Agent 内部：组装上下文、判断充分性、生成下一问、给出医学结果，并通过 Supervisor 串接。这造成以下问题：

- 模型既生成内容又影响流程，确定性门禁不足。
- Inquiry 与 Sufficiency 重复调用并争夺下一问决策权。
- 患者输入和 Evidence 被拼入 system prompt。
- 安全信息、Evidence、医师确认等跨层契约不闭合。
- 模型输出一旦“结构合法”，容易被直接写入长期 State。
- Prompt 调优、模型升级与流程正确性耦合。

本设计采用 Harness Engineering 思路：模型是概率型工作单元，Harness 是围绕模型的确定性运行与治理层。系统能力来自：

```text
System Capability = Model Workers + Harness + Governed Data/Tools
```

这里的 Harness 不是一个“总控 Agent”，也不是 LangGraph 的同义词。它是可执行的软件层，负责：

- 任务规格
- 上下文选择
- 状态与记忆
- 工具权限
- Agent 调用
- 输出验证
- 路由和循环
- 持久化
- 可观测性
- 失败归因
- 人工介入
- 评估反馈

### 1.1 概念来源

本设计参考并迁移以下 Harness 工程原则：

- OpenAI：把人的主要工作转向设计环境、明确意图和构建反馈循环；通过机械化架构约束保证 Agent 自主性不导致漂移。
  https://openai.com/index/harness-engineering/
- OpenAI Codex Harness：核心 Agent loop 之外还包括线程生命周期、持久化、配置、认证、工具执行和统一策略。
  https://openai.com/index/unlocking-the-codex-harness/
- Anthropic：长任务应拆成可处理单元，通过结构化工件交接；Planner/Generator/Evaluator 的价值在于明确职责与验证反馈，而不是增加角色数量。
  https://www.anthropic.com/engineering/harness-design-long-running-apps

上述资料主要讨论软件工程 Agent。本文件将其推导到医疗辅助场景：把测试/编译门禁替换为 Schema、临床完整性、安全规则、Evidence、一致性和医师批准门禁。

## 2. 设计原则

### 2.1 Harness 掌握控制权

- Agent 不直接修改持久化 State。
- Agent 不决定阶段跳转。
- Agent 不覆盖安全规则结果。
- Agent 不确认最终处方或病历。
- Harness 先验证输出，再由 Reducer 原子提交。

### 2.2 确定性规则优先

可以由代码明确判断的事项，不交给模型：

- Schema 和 Enum
- 状态版本与幂等
- 最低问诊门槛
- 红旗症状阻断规则
- 过敏/妊娠采集状态
- citation ID 和 source type
- 剂量、单位、十八反、十九畏等安全规则
- 医师确认存在性
- 固定免责声明
- 阶段允许边

模型只处理需要语义理解或自然语言生成的任务。

### 2.3 生成与验证分离

每次模型输出必须经过至少一层独立验证：

```text
Generate → Parse → Verify → Reduce → Commit
```

验证可以是确定性 Verifier，也可以增加只负责“发现问题”的 Critic Agent。Critic Agent 只能提出 flags，不能替代确定性门禁或医师批准。

### 2.4 State 是事实账本，不是对话摘要

- 原始消息不可变保存。
- 抽取事实带来源消息 ID。
- 更正和撤回显式记录。
- 阶段产物带输入 State 版本。
- 下游输入只使用 current 产物。
- 回退时旧产物标记 `superseded/stale`。

### 2.5 最小上下文与最小权限

每个 Agent 只看到完成任务所需字段：

- 辨证/开方不需要患者姓名、门诊号。
- Question Agent 不需要 RAG。
- Record Narrator 不可写医师确认和最终处方结构。
- Safety Explainer 不可调用处方修改工具。

### 2.6 人工批准是状态机边界

医师确认不是 Prompt 文案，而是 Harness 的硬边界：

- 未确认不得生成最终病历。
- 医师确认之后，模型不得新增未经确认的临床建议。
- 医师修改处方后必须重新执行确定性安全检查。

### 2.7 最简单的 Agent 集合

不把每个业务名词都做成 LLM Agent。优先使用 Policy、Verifier 和模板；只有语义任务才使用模型。

## 3. 总体架构

```text
┌──────────────────── Client / API ────────────────────┐
│ messages | advance | review | recover | event stream │
└──────────────────────────┬────────────────────────────┘
                           │ Command
┌──────────────────────────▼────────────────────────────┐
│                   Xuanhu Harness                      │
│                                                     │
│  Command Router ── Run Controller ── State Machine  │
│       │               │                  │           │
│       │          Context Builder         │           │
│       │               │                  │           │
│  Policy Gates ◄── Agent Runtime ──► Tool Registry   │
│       │               │                  │           │
│  Verifier Chain ── State Reducer ── Event/Outbox    │
│       │               │                  │           │
│  Audit / Trace / Metrics / Episode Package / Evals  │
└───────────────┬───────────────────────┬───────────────┘
                │                       │
       ┌────────▼────────┐     ┌────────▼─────────┐
       │ Model Workers   │     │ Governed Tools   │
       │ Extract/Reason/ │     │ DB/RAG/Safety/   │
       │ Draft/Narrate   │     │ Prompt/Gateway   │
       └─────────────────┘     └──────────────────┘
```

### 3.1 Harness 核心组件

| 组件 | 职责 |
|---|---|
| Command Router | 接收 message/advance/review 等命令，验证权限、幂等键和 State 版本 |
| Run Controller | 执行单次 Harness loop，维护调用预算、截止时间、取消和恢复 |
| State Machine | 只允许声明过的阶段边，模型不能修改 |
| Context Builder | 按 Agent 白名单构造结构化上下文，去身份化并控制 Token |
| Agent Runtime | 加载版本化 AgentSpec，调用模型并产生 RunArtifact |
| Tool Registry | 提供受权限约束的 RAG、安全规则、模板和数据库工具 |
| Policy Gates | 执行分流、完整性、Evidence、安全、医师批准等确定性策略 |
| Verifier Chain | 验证 Schema、事实来源、引用、前置条件和临床一致性 |
| State Reducer | 将通过验证的 Delta 应用到 State，处理去重、更正和产物失效 |
| Event/Outbox | 事务提交后可靠发布 SSE/Redis 事件 |
| Observability | 保存 run、step、evidence、gate、失败归因、模型与 Prompt 版本 |
| Eval Loop | 把线上失败归入评估集，验证 Harness/Prompt 修改是否改善 |

## 4. Model Agent 与确定性组件

### 4.1 保留的模型 Agent

| Agent | 模型职责 | 明确禁止 |
|---|---|---|
| IntakeExtractionAgent | 从“本轮新消息”抽取事实、安全信息和候选红旗信号 | 判断充分、路由、辨证、开方 |
| QuestionComposerAgent | 根据 Harness 指定的一个缺失维度生成单一问题；优先模板，必要时调用 | 自选缺失维度、一次多问 |
| SyndromeDraftAgent | 根据已验证事实和可用 Evidence 生成辨证草案 | 开方、流程跳转、最终诊断 |
| FormulaDraftAgent | 一次输出基础方、加减后候选方和明确加减差异 | 安全通过、医师确认、最终处方 |
| SafetyExplanationAgent | 将确定性安全结果转成保守说明 | 改写 passed/issues/rollback |
| RecordNarrationAgent | 润色已确定性构建的病历叙述部分 | 新增事实、建议、处方、确认信息 |
| SemanticCriticAgent（可选） | 查找矛盾、缺证、表述越界并输出 flags | 批准结果或直接修改 State |

### 4.2 不再作为模型 Agent 的组件

| 原组件 | 新设计 |
|---|---|
| SufficiencyAgent | `CompletenessPolicy`，确定性最低门槛 + 可选语义 flag |
| Supervisor 中的模型式路由 | `StateMachine + Policy Gates` |
| Safety 决策 | 继续由 `SafetyRuleEngine` 权威执行 |
| Record 核心 JSON 生成 | `RecordAssembler` 确定性构建 |

### 4.3 为什么合并 Prescription 与 Modification

当前两次模型调用分别生成基础方和加减方，容易出现：

- 第二次重写第一份处方而非可追踪修改。
- 两次检索和上下文重复。
- 药味/剂量漂移难以验证。
- 安全失败回退目标复杂。

新 `FormulaDraftAgent` 一次返回：

```text
base_formula
modifications[]
candidate_formula
decision
confidence
evidence_mode
claim_evidence_links[]
```

Harness 的 `FormulaConsistencyVerifier` 根据 base + modifications 重算 candidate，验证药味和剂量差异。需要医师重新拟方时，创建新的 Formula Draft revision，而不是复用旧阶段。

## 5. 新状态机

```text
CREATED
   │
   ▼
INTAKE ───────────────► TRIAGE_HOLD ─► HUMAN_DECISION
   │                         ▲
   ├─ incomplete ─► NEEDS_INPUT ─┐
   │              ▲              │ new message
   └─ ready ──────┴──────────────┘
                  │
                  ▼
          READY_FOR_REASONING
                  │ advance/run
                  ▼
           SYNDROME_DRAFT
          ┌───────┴────────┐
 needs info/abstain      completed
          │                ▼
          └────► NEEDS_INPUT / FORMULA_DRAFT
                                  │
                     needs info   │ completed
                          ┌───────┴───────┐
                          ▼               ▼
                    NEEDS_INPUT      SAFETY_CHECK
                                         │
                       fail/revise ┌──────┴──────┐ pass
                                   ▼             ▼
                            FORMULA_DRAFT   DOCTOR_REVIEW
                                                 │
                          reject ┌───────┬────────┘ confirm/modify
                                 ▼       ▼
                            TERMINATED  SAFETY_RECHECK(if modified)
                                             │ pass
                                             ▼
                                       RECORD_BUILD
                                             │
                                             ▼
                                            DONE
```

全局旁路状态：

- `BLOCKED_SYSTEM`：网关、DB、Schema、不可恢复配置错误。
- `MANUAL_REQUIRED`：回退超限、临床矛盾或 Critic 高风险 flag。
- `CANCELLED`：用户/医师取消。

### 5.1 阶段迁移原则

- Agent 输出只提供 `decision` 建议，不直接写 `current_stage`。
- GateResult 是迁移的唯一依据。
- 每个产物记录 `input_state_version`。
- 版本不一致时丢弃结果并重跑，不允许覆盖新 State。
- 回退创建新 revision，旧 revision 标为 superseded。

## 6. 问诊回合 Harness

### 6.1 单轮流程

```text
1. Persist raw message
2. Triage pre-check
3. Build IntakeContext(current_message + minimal active facts)
4. Run IntakeExtractionAgent
5. Schema/Provenance/SafetyField verifiers
6. Reduce observations into State
7. Run TriagePolicy again on normalized observations
8. Run CompletenessPolicy
9a. ready      → READY_FOR_REASONING; no question
9b. incomplete → select one gap → template/QuestionComposer → NEEDS_INPUT
9c. red flag   → TRIAGE_HOLD
10. Atomic commit + outbox events
```

### 6.2 Inquiry 输出

```python
class IntakeExtractionOutput:
    decision: Literal["extracted", "needs_clarification", "abstained"]
    observations: list[ObservationDelta]
    patient_safety_delta: PatientSafetyDelta
    red_flag_candidates: list[RedFlagCandidate]
    ambiguities: list[Ambiguity]
```

Inquiry 不再输出 `next_question`，避免和完整性策略争夺决策权。

### 6.3 CompletenessPolicy

输入是结构化事实，不读取原始 Prompt 输出：

```python
class CompletenessResult:
    ready: bool
    missing_required: list[InquiryDimension]
    missing_optional: list[InquiryDimension]
    conflicting: list[FactKey]
    next_gap: InquiryDimension | None
    policy_version: str
```

建议最低门槛：

- 主诉：症状 + 基本病程。
- 现病史：主要变化或伴随症状。
- 主诉相关十问关键项。
- 过敏采集状态不是 unknown。
- 对适用患者，妊娠/哺乳状态不是 unknown。
- 当前用药和重大疾病已确认或明确无。
- 无未处理的高风险红旗。

“适用患者”由代码根据性别、年龄、绝经状态和医师标记判断，不能让模型机械追问所有患者。

## 7. 临床推理 Harness

### 7.1 Syndrome Draft

Harness 前置检查：

- State 是 `READY_FOR_REASONING`。
- Completeness policy version 有效。
- 无未处理 red flag。
- Context 只含 active observations。

输出：

```python
class SyndromeDraft:
    decision: Literal["completed", "needs_more_info", "abstained"]
    syndrome: str | None
    syndrome_basis: list[FactClaim]
    differential: list[FactClaim]
    treatment_principle: str | None
    confidence: float
    evidence_mode: Literal["rag_supported", "model_knowledge_only"]
    claim_evidence_links: list[ClaimEvidenceLink]
    missing_inputs: list[InquiryDimension]
```

无 RAG 模式：

- 允许生成“草案”，不允许声称有文献支持。
- `evidence_mode=model_knowledge_only`。
- Harness 限制最大置信度。
- 必须进入医师复核，不能自动成为诊断。

Verifier：

- basis 中 fact ID 必须存在且 active。
- RAG citation 必须属于本 run。
- `needs_more_info` 不能携带处方。
- 低完整性/高矛盾时不能 `completed`。

### 7.2 Formula Draft

前置条件：

- Syndrome decision 为 completed。
- Treatment principle 非空。
- 无未解决的高风险 clinical flag。

输出基础方、修改动作、候选完整方及证据模式。

Verifier：

- base + modifications 能重算出 candidate。
- 药味规范化成功。
- 剂量和单位可解析。
- 每个修改动作有 fact/证型/证据依据。
- citation source 只允许 formula/herb。
- 无 RAG 时明确 `model_knowledge_only`。
- 通过后才能进入 SafetyRuleEngine。

### 7.3 Safety

```text
Candidate Formula
  → normalize
  → deterministic SafetyRuleEngine
  → Safety Gate
      ├─ passed: doctor review
      ├─ revisable: new formula revision
      └─ blocker/limit exceeded: manual required
  → optional SafetyExplanationAgent
```

SafetyExplanationAgent 只能解释固定 issue ID。Harness 校验解释条数和 issue ID 一致，并使用保守文案：“未命中当前规则集覆盖的阻断项”，不使用“绝对安全”。

## 8. 医师复核与病历

### 8.1 Doctor Review Gate

医师动作：

- `confirm`
- `modify`
- `reject`
- `request_more_info`

`modify` 后 Harness 创建新 Formula revision，并强制重新运行 SafetyRuleEngine。

### 8.2 Record Build

```text
Validated DoctorReview
       │
       ▼
RecordAssembler (deterministic JSON)
       │
       ├─ fixed disclaimer
       ├─ confirmed formula revision
       ├─ authoritative safety result
       └─ doctor review from DB
       │
       ▼
RecordNarrationAgent (optional prose only)
       │
       ▼
RecordConsistencyVerifier
       │
       ▼
Persist final record
```

RecordNarrationAgent 不输出：

- doctor_review
- formula JSON
- safety passed/issues
- 新的调护/用药建议
- disclaimer

如果需要 AI 调护建议，应在 Doctor Review 前生成并纳入复核对象。

## 9. Harness 执行协议

### 9.1 AgentSpec

```python
class AgentSpec:
    name: str
    version: str
    input_schema: type
    output_schema: type
    model_policy: ModelPolicy
    context_policy: ContextPolicy
    tool_permissions: set[Capability]
    verifier_chain: list[str]
    failure_policy: FailurePolicy
```

### 9.2 RunSpec

```python
class RunSpec:
    run_id: UUID
    session_id: UUID
    state_version: int
    stage: str
    agent_spec_version: str
    prompt_version: str
    deadline_at: datetime
    total_attempt_budget: int
    idempotency_key: str
```

### 9.3 ContextPacket

```python
class ContextPacket:
    trusted_state: dict
    current_user_input: str | None
    evidence: list[EvidencePacket]
    allowed_fact_ids: list[str]
    token_budget: int
    redaction_report: dict
```

动态数据永远不与不可变 system policy 拼接为同一语义区块。

### 9.4 RunArtifact

```python
class RunArtifact:
    output: BaseModel
    model_actual: str
    usage: TokenUsage
    attempts: int
    latency_ms: int
    evidence_ids: list[str]
    trace_id: str
```

### 9.5 VerificationReport

```python
class VerificationReport:
    passed: bool
    checks: list[CheckResult]
    failure_class: str | None
    retry_allowed: bool
    requires_human: bool
```

只有 VerificationReport 通过，State Reducer 才能提交。

## 10. Prompt 与上下文设计

### 10.1 固定消息层次

| 层次 | 内容 | 是否允许患者/RAG 文本 |
|---|---|---|
| system | 全局医疗安全、禁止越权 | 否 |
| developer | 当前 AgentSpec、输出契约 | 否 |
| context/tool | 去身份化 State、Evidence 数据 | 是，明确为数据 |
| user | 本轮患者/医师输入 | 是 |

### 10.2 Context Policy

每个 Agent 显式声明：

- 可见字段
- 最大历史消息数
- 最大 Token
- 是否可见身份字段
- Evidence source 白名单
- 缺失字段表达
- 冲突事实表达

### 10.3 Prompt 版本

Prompt、AgentSpec、Schema、Policy 分别版本化，并在 run 中同时记录。禁止只改 Prompt 文件而无法知道当时使用的 Schema/Policy。

## 11. 工具与权限矩阵

| 能力 | Intake | Question | Syndrome | Formula | Safety Explain | Record Narrate |
|---|---:|---:|---:|---:|---:|---:|
| 读取去身份 State | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 读取原始本轮消息 | ✓ | 条件 | 否 | 否 | 否 | 否 |
| RAG theory/case | 否 | 否 | ✓ | 条件 | 否 | 否 |
| RAG formula/herb | 否 | 否 | 条件 | ✓ | 否 | 否 |
| SafetyRuleEngine | 否 | 否 | 否 | 否 | 只读结果 | 否 |
| 直接写 State/DB | 否 | 否 | 否 | 否 | 否 | 否 |
| 改变阶段 | 否 | 否 | 否 | 否 | 否 | 否 |
| 修改医师确认 | 否 | 否 | 否 | 否 | 否 | 否 |

所有写入由 Harness Reducer/Repository 完成。

## 12. 事务、幂等与事件

### 12.1 单次提交

每个 Harness step：

1. 读取 State + version。
2. 运行 Agent/Policy。
3. 验证。
4. 使用 expected version 提交 State、run、evidence、gate result。
5. 同事务写 outbox。
6. 事务后发布 SSE/Redis 事件。

### 12.2 幂等键

建议：

```text
session_id + command_id + input_state_version + agent_spec_version
```

重复请求返回已有 RunArtifact，不重复调用模型。

### 12.3 产物版本

所有临床产物使用：

- `artifact_id`
- `revision`
- `input_state_version`
- `status=current/superseded/stale`
- `produced_by_run_id`

## 13. 失败、重试与人工介入

### 13.1 失败分类

| 类型 | 策略 |
|---|---|
| gateway transient/timeout | 在总预算内重试 |
| schema parse | 一次结构修复或重试 |
| invalid clinical prerequisite | 不重试，回到 needs input |
| RAG unavailable | 降级到显式 no-RAG 模式或 blocked，由配置决定 |
| verifier rejected | 不直接提交；可修复重试一次 |
| safety failed | 新 Formula revision 或人工处理 |
| programming/persistence error | system blocked，保留失败归因 |
| state version conflict | 丢弃旧结果，基于新版本重跑 |

### 13.2 总预算

Harness 管理一次 command 的总调用次数、总 Token 和总截止时间，避免 Gateway 与 Agent 各自重试形成乘法。

### 13.3 人工边界

以下情况进入 `MANUAL_REQUIRED`：

- 红旗规则命中且无法自动分级。
- 事实存在未解决矛盾。
- 多次 `needs_more_info` 无新增信息。
- Formula/Safety 回退超过限制。
- Critic 输出高风险 flag。
- 无 RAG 模式下配置要求必须有 Evidence。

## 14. 可观测性与 Episode Package

每次 command 形成可审计 Episode：

```text
command
state_before_hash
selected_context_manifest
agent_spec/prompt/schema/policy versions
model actual + usage
tool calls
evidence refs
raw-output hash (受控保存)
parsed artifact
verification report
gate decisions
state delta
state_after_hash
events
failure attribution
human interventions
```

医疗数据按最小必要原则存储。普通日志不写 Prompt 原文、完整模型输出、患者姓名或门诊号。

## 15. 评估与反馈循环

### 15.1 分层评估

1. Schema：字段、Enum、长度、互斥关系。
2. Reducer：去重、更正、来源和版本。
3. Policy：完整性、红旗、安全和阶段边。
4. Behavioral：重复追问、单一问题、拒答、注入抵抗。
5. Clinical consistency：事实→证型→治法→方药链条。
6. Evidence：source、citation、claim 支持。
7. End-to-end：问诊到医师确认和病历。

### 15.2 Harness 修改闭环

每次 Harness/Prompt 修改必须声明：

- 预期改善哪个失败类别。
- 影响哪些 Episode 指标。
- 新增什么回归样例。
- 是否增加调用成本或延迟。
- 如何回滚。

线上失败先归因到 Context、Model、Tool、Policy、Verifier、State 或 UI，不能默认归咎于模型。

## 16. API 目标契约

### 16.1 `POST /messages`

一次请求完成一个问诊回合：

```text
persist message
extract
verify/reduce
triage/completeness
optional next question
commit
```

返回：

- 当前 Harness 状态
- 抽取摘要
- completeness result
- 一个下一问或 ready 标志
- state version
- run/trace ID

### 16.2 `POST /advance`

只允许从 `READY_FOR_REASONING` 启动。Harness 可在调用预算内自动运行：

```text
Syndrome → Formula → Safety
```

遇到 `NEEDS_INPUT`、`MANUAL_REQUIRED`、`DOCTOR_REVIEW` 或系统错误立即停止。实现初期可按 step 异步执行，但外部契约不允许重复执行已完成阶段。

### 16.3 `POST /review`

- confirm：进入 Record Build。
- modify：生成新 Formula revision → Safety recheck。
- reject：终止或返回指定阶段。
- request_more_info：回到 NEEDS_INPUT，并失效下游产物。

## 17. 与当前实现的映射

| 当前实现 | 目标实现 |
|---|---|
| `BaseAgentImpl` | `AgentRuntime + AgentSpec + RunContext` |
| `AgentRegistry` 实例映射 | 权限化 AgentSpec/Factory Registry |
| `Supervisor` | `RunController + StateMachine + Gates + Reducer` |
| `InquiryAgent` | `IntakeExtractionAgent` |
| `SufficiencyAgent` | `CompletenessPolicy` |
| `PrescriptionAgent + ModificationAgent` | `FormulaDraftAgent + FormulaConsistencyVerifier` |
| `SafetyRuleEngine` | 保留为权威 Safety Gate |
| `SafetyAgent` | 可选 SafetyExplanationAgent |
| `RecordAgent` | `RecordAssembler + RecordNarrationAgent` |
| Prompt `str.replace` | 分层 Prompt Builder + 严格变量验证 |
| State 字符串追加 | Observation Ledger + Reducer |
| `agent_runs` | Episode/Run/Step 观测主线 |
| `agent_evidences` 未使用 | 每 run 强制持久化 Evidence |

## 18. 实施顺序

### H1：无 RAG 问诊 Harness

- Observation/Safety Delta。
- IntakeExtractionAgent。
- TriagePolicy。
- CompletenessPolicy。
- Question template/composer。
- `/messages` 唯一编排。
- DB run/audit 接通。

### H2：Harness Runtime 基线

- AgentSpec、RunSpec、ContextPacket。
- Verifier Chain、Reducer。
- 总重试/Token/超时预算。
- 幂等与 outbox。
- Prompt 权限分层。

### H3：无 RAG 临床草案

- Syndrome Draft decision 契约。
- FormulaDraftAgent 合并基础方与加减。
- `model_knowledge_only` 模式。
- FormulaConsistencyVerifier。
- Safety Gate 与医师复核。

### H4：病历边界

- Doctor Review 前置 Gate。
- RecordAssembler。
- Record Narration 限权。
- RecordConsistencyVerifier。

### H5：Evidence/RAG

- Evidence source policy。
- claim-to-evidence。
- `agent_evidences` 和 trace。
- RAG 不可用降级策略。

### H6：评估与演进

- Episode package。
- 行为/临床/Evidence 评估集。
- 失败归因报表。
- Harness 变更预测、验证和回滚。

## 19. 第一版完成定义

无 RAG 第一版必须满足：

- 每条用户消息最多一次 Intake 模型调用；简单下一问不调用模型。
- Sufficiency 不再是独立 LLM Agent。
- 已充分时不生成下一问。
- 过敏、妊娠、当前用药等信息有明确采集状态并进入 Safety。
- 红旗命中不能自动进入辨证开方。
- 所有模型输出先验证后提交。
- Agent 无权修改阶段和最终安全结论。
- 无 RAG 临床输出显式标记 `model_knowledge_only`。
- 未经医师确认不能生成最终病历。
- 默认生产链完整保存 run、gate、evidence 和 trace。

## 20. 架构决策摘要

1. Inquiry 与 Sufficiency 保持概念分离，但 Sufficiency 改为确定性 Policy，不再单独调用模型。
2. Prescription 与 Modification 合并为一个 FormulaDraftAgent，由 Harness 验证差异。
3. Safety 决策保持确定性，LLM 解释可选。
4. Record 核心数据确定性组装，LLM 只做受限叙述。
5. Agent 是无状态、无写权限、无路由权的模型工作单元。
6. Harness 是系统的控制面和可信边界。
7. 直接使用 LangGraph 承载 Harness 的状态图、持久化与人工中断；先完成无 RAG 子图，再增强 RAG。
