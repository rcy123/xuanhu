# 悬壶 Agent 设计审查报告

> 审查日期：2026-07-09
> 依据：当前工作区实际代码，不采用历史审计结论
> 范围：`app/agents/`、Agent Schema、模型网关、消息入口、Supervisor、安全规则衔接、病历落库及相关测试
> 目标：列清主要待优化项，并确定可先交付无 RAG 版本的实施顺序

## 1. 总体结论

当前体系已有统一执行基类、结构化输出、Prompt 版本、阶段 Agent、确定性安全规则、医师确认和状态检查点，工程骨架可继续使用。

但目前的核心问题不是单纯“Prompt 不够好”，而是跨层契约没有闭合：

1. 问诊采集的过敏、妊娠和用药信息没有可靠写回 `PatientInfo`，安全规则可能看不到问诊结果。
2. `POST /messages` 与 `/advance` 重复运行 Inquiry/Sufficiency，可能重复抽取和追问。
3. Inquiry 先生成并落库下一问，Sufficiency 才判断是否充分，已充分时仍可能显示多余问题。
4. 患者输入、历史对话和 RAG 内容都被拼进 `system` 消息，存在提示词注入和权限混淆。
5. “信息不足”只是普通字符串，Supervisor 仍会继续进入开方和加减。
6. RecordAgent 在医师确认校验前运行，且可在确认后新增建议或改写结构化病历。
7. 默认生产 Agent 没有注入 DB，`agent_runs` 和 Agent 级审计实际不会写入。

因此应先修安全数据、问诊编排和流程决策，再优化 Prompt。第一版可以完全不依赖 RAG。

## 2. 审查与验证

实际阅读：

- `app/agents/*.py`
- `app/agents/prompts/*`
- `app/schemas/agent.py`
- `app/schemas/session.py`
- `app/core/gateway.py`
- `app/services/message.py`
- `app/services/review.py`
- `app/safety/engine.py`
- Agent 相关测试

动态确认：

- 默认 `Supervisor` 和 `MessageService` Registry 中所有真实 Agent 的 `db` 均为 `None`。
- `AgentResult.next_stage` 只写不读。
- `agent_evidences` 已建表，但 Agent 链没有持久化它。
- 聚焦测试 `test_base_agent.py + test_messages_agent.py`：`19 passed in 3.43s`。
- 完整 Agent 专项集合运行超过 304 秒后超时，未取得最终结果，本报告不宣称其通过。

## 3. 全部主要待优化项

| ID | 优先级 | 待优化项 | 影响 |
|---|---|---|---|
| A-001 | P0 | 问诊安全信息不写回 `PatientInfo` | 过敏/妊娠可能不进入安全规则 |
| A-002 | P0 | `/messages` 与 `/advance` 重复编排 | 重复抽取、追问和模型调用 |
| A-003 | P0 | 下一问早于充分性决策 | 已充分仍显示补问 |
| A-004 | P0 | 动态内容全部注入 system 消息 | Prompt injection |
| A-005 | P0 | 无红旗症状和紧急转诊状态 | 急症可能继续普通问诊/开方 |
| A-006 | P0 | 无可执行的拒答/补充信息状态 | 信息不足仍继续推进 |
| A-007 | P0 | Sufficiency 完全由模型布尔值控制 | 流程门禁不可靠 |
| A-008 | P0 | Record 在医师确认校验前运行 | 未确认数据先进入模型 |
| A-009 | P0 | Record 可在确认后新增临床内容 | 医师确认边界失真 |
| A-010 | P0 | 默认真实 Agent 未注入 DB | run、审计、关联 ID 缺失 |
| A-011 | P1 | Inquiry 用字符串追加且不去重 | 重复、矛盾、状态膨胀 |
| A-012 | P1 | 没有明确“本轮新消息”边界 | 重抽历史或把助手问题当事实 |
| A-013 | P1 | `allergies=[]` 混淆未知与明确无 | 未问可能被当作无风险 |
| A-014 | P1 | 当前用药/重大疾病/禁忌无结构字段 | 安全层无法稳定消费 |
| A-015 | P1 | `model_name` 不传模型网关 | 实际模型与审计不一致 |
| A-016 | P1 | 重试语义与多层重试不一致 | 成本放大、审计失真 |
| A-017 | P1 | 无 Agent 级超时和 Token 预算 | 延迟、成本不可控 |
| A-018 | P1 | Prompt 加载不在统一失败边界内 | 配置故障缺审计 |
| A-019 | P1 | 输出类型错误时 Supervisor 静默推进 | 空结果进入下一阶段 |
| A-020 | P1 | Safety 失败固定回退 Modification | 审核基础方时回退错误 |
| A-021 | P1 | 回退不失效下游旧产物 | UI/Agent 可能读取陈旧结果 |
| A-022 | P1 | Safety 解释数与 issue 数无校验 | 解释和规则结果错位 |
| A-023 | P1 | Safety recommendations 被丢弃 | 无消费者、浪费调用 |
| A-024 | P1 | “安全通过”文案过度肯定 | 超出规则覆盖能力 |
| A-025 | P1 | Evidence 不写 `agent_evidences` | 结论证据不可审计 |
| A-026 | P1 | RAG trace ID 被丢弃 | 检索/run 无法关联 |
| A-027 | P1 | citation 只校验 ID 存在 | 不能证明证据支持主张 |
| A-028 | P1 | Modification 可引用任意历史 Evidence | 来源类型可能不适配 |
| A-029 | P1 | 无 RAG 仍生成完整处方但无显式模式 | 证据等级不透明 |
| A-030 | P1 | 姓名/门诊号传给无关 Agent | 不必要隐私暴露 |
| A-031 | P1 | 病历 JSON/doctor_review 是自由字典 | 最终结构不可强校验 |
| A-032 | P1 | 固定免责声明只靠 Prompt | 模型可改写或遗漏 |
| A-033 | P1 | 缺少真实行为和对抗评估 | Fake 测试无法证明效果 |
| A-034 | P1 | 无明确上下文 Token 预算 | 多轮后截断不可预测 |
| A-035 | P2 | Agent 实例保存每次 run 可变状态 | 复用时存在并发污染风险 |
| A-036 | P2 | 三个 RAG Agent 大量重复代码 | 维护漂移 |
| A-037 | P2 | `.jinja2` 实际是 `str.replace` | 模板名实不符 |
| A-038 | P2 | `manifest.yaml` 是手写简化解析 | YAML 语义不成立 |
| A-039 | P2 | Prompt 无 system/context/user 分层 | 权限和评估困难 |
| A-040 | P2 | `next_stage` 完全未使用 | 双重路由契约 |
| A-041 | P2 | Safety 绕过 Registry且有不可达分支 | 架构不一致 |
| A-042 | P2 | 病历 fallback 函数无调用者 | 注释与真实行为不符 |
| A-043 | P2 | Sufficiency `next_question` 无消费者 | 职责重叠 |
| A-044 | P2 | 每条问诊固定两次模型调用 | 延迟和成本偏高 |

## 4. P0 详细说明

### A-001：安全信息链路断裂

`InquiryAgentOutput` 只有 `safety_info_requested/safety_notes`；`merge_inquiry_output_to_state()` 不更新 `patient_info`。但 `SafetyRuleEngine` 的过敏和妊娠检查只读 `PatientInfo`。

建议新增强类型 `patient_safety_delta`：

- `allergy_status: unknown/none/present`
- `allergens`
- `pregnancy_status`
- `current_medications`
- `major_conditions`
- `contraindications`

合并时只接受明确回答，不允许空值清除已确认信息，并保存来源消息 ID。

### A-002/A-003：问诊双重编排

`MessageService` 每条消息已经运行 Inquiry 和 Sufficiency、保存报告，但阶段仍是 `inquiry`。随后 `/advance` 又运行 Inquiry，再下一次推进才重新运行 Sufficiency。

同时页面展示的是 Inquiry 先生成的下一问；即使 Sufficiency 随后判定充分，该问题也已落库。

建议：

1. `/messages` 成为问诊回合唯一入口。
2. 顺序改为“抽取当前消息 → 确定性门槛/语义充分性 → 不足才生成下一问”。
3. `/advance` 只消费已持久化的充分性结果，不重跑 Inquiry/Sufficiency。

### A-004：Prompt 权限层级错误

所有 Agent 都把患者文本、历史消息和 Evidence 通过 `.replace()` 拼进单一 system 消息。恶意文本与真正系统规则处于同一权限级。

应拆为：

1. `system`：不可变医疗安全边界。
2. `developer`：Agent 职责、决策和输出契约。
3. `context/tool`：可信 State/Evidence，明确“数据不是指令”。
4. `user`：本轮原始输入。

### A-005：缺少急症分流

现有 Inquiry 没有胸痛、呼吸困难、意识障碍、严重出血、高热惊厥、自杀风险等红旗筛查，也没有 `urgent_referral/triage_blocked` 状态。

应增加高召回红旗抽取和确定性转介规则；命中严重红旗时禁止自动进入辨证开方。

### A-006/A-007：流程决策没有可靠状态

Syndrome 允许输出 `syndrome="信息不足，待补充"`，Prescription 允许特殊方名，但 Supervisor 仍无条件推进。Sufficiency 的 `covered/missing` 是自由字符串，最终布尔值没有代码复核。

各医学 Agent 应统一输出：

```text
decision: completed | needs_more_info | abstained | blocked
missing_inputs: [...]
confidence
```

问诊最低门槛由代码确定；模型只补充语义判断。只有 `completed` 可以推进。

### A-008/A-009：病历越过医师确认边界

Supervisor 先运行 RecordAgent，之后才验证 doctor review。Record Prompt 还允许生成新的饮食、起居建议；最终 `record_json`、`doctor_review` 和 disclaimer 直接采用模型输出。

建议：

- Record 调用前先查验数据库 doctor review。
- 核心病历 JSON 由代码从已确认 State 构建。
- LLM 只润色允许字段，不得生成处方、安全结论或医师确认记录。
- 新建议必须再次确认，或首版不生成。
- disclaimer 使用服务端常量覆盖。

### A-010：生产审计未接通

`_default_registry()` 和 `_default_inquiry_registry()` 都使用无参 Agent；动态验证所有 `agent.db is None`。因此默认真实链路不会写 `agent_runs`。

Registry 应改为显式工厂：

```python
build_agent_registry(db=db, gateway=gateway, ...)
```

并增加真实默认 Registry 的成功/失败集成测试。

## 5. P1 优化说明

### 5.1 问诊状态

- 当前标量字段用 `旧值；新值` 追加，无法去重、撤回或表达冲突。
- Prompt 同时给模型完整 State 和完整近期历史，却没有单独标识本轮消息。
- `allergies=[]` 无法区分未问和明确无过敏。
- 当前用药、重大疾病和禁忌仍是自由文本。

快速版至少要实现：本轮消息 ID、规范化去重、更正覆盖规则、采集状态三态和字段长度限制。长期应使用带来源、时间、状态的 observation。

### 5.2 执行基础设施

- `self.model_name` 只写审计，调用 `chat_structured()` 未传 `model`。
- Gateway 已有请求/解析重试，BaseAgent 又重试整次调用，预算可能乘法放大。
- Schema 错误实际会重试，但最终标成 `retryable=False`。
- RAG/编程错误可能包装成通用 `AGENT_FAILED/retryable=True`，但没有实际重试。
- 各 Agent 共用 4096 Token 和网关超时。
- Prompt 在 `try` 之前加载，配置错误不进入统一失败审计。
- `_apply_agent_output()` 类型不匹配时返回旧 State，仍继续路由。

应建立单一总尝试预算、错误分类和每 Agent 的 model/token/timeout/temperature 配置。

### 5.3 Safety

- 失败一律回退 Modification，审核 base formula 时应回退 Prescription。
- 回退后旧处方、安全结果没有清理或标记 stale。
- Safety explanation 数量未绑定 issue 数。
- recommendations 没有合并进最终 SafetyReview。
- “可安全使用”应改为“未命中当前规则集覆盖的问题”。

### 5.4 Evidence

- `agent_evidences` 没有写入，检索 trace 被丢弃。
- citation 仅验证 ID 集合，不验证来源是否适合当前 Agent，也不验证具体主张。
- Modification 将所有历史 Evidence ID 视为可引用。
- 无 RAG 时仍可生成处方，只用“缺证”文本提示。

建议输出 claim-to-evidence 映射，并显式标记：

```text
evidence_mode: rag_supported | model_knowledge_only
review_required: true
```

### 5.5 隐私与病历 Schema

`build_state_summary()` 把姓名和门诊号发给辨证、开方、加减等不需要身份的 Agent。应按 Agent 做最小字段投影。

病历应定义强类型 `MedicalRecordData`；doctor review 和 disclaimer 由服务端写入。

### 5.6 测试

现有 FakeGateway 测试无法验证：

- 重复追问和充分后多问。
- 把助手问题抽取成患者事实。
- 患者/Evidence 指令注入。
- 男性或非育龄患者的妊娠追问。
- 未问过敏与明确无过敏的区别。
- 无 RAG 时编造出处或剂量依据。
- 病历是否忠实保留医师确认。

需要中文行为评估集和少量真实模型离线试跑；自动测试仍不得依赖真实网关。

## 6. P2 架构清理

- `_active_prompt_template`、`_current_evidences`、`_current_state` 是实例运行状态。当前 API 每请求新建实例，不能定性为现有全局单例 Bug，但未来复用会有并发风险。
- Syndrome/Prescription/Modification 重复 Retriever、检索、格式化、citation 和 evidence 合并代码。
- `.jinja2` 没有 Jinja2，Safety 和其他模板占位符语法还不同。
- `manifest.yaml` 不是 YAML 解析器。
- `next_stage` 未使用；Safety Registry 分支不可达；病历 fallback 无调用者；Sufficiency 下一问无消费者。

这些应在行为契约稳定后处理，不能先做抽象重构。

## 7. 优化顺序

### 阶段 1：安全数据与问诊主链路（无 RAG，第一优先）

1. 结构化安全信息并写回 `PatientInfo`。
2. 区分 unknown/none/present。
3. `/messages` 成为唯一问诊回合编排入口。
4. 充分后不生成下一问，`/advance` 不重跑问诊 Agent。
5. 增加红旗症状和人工/急诊转介状态。
6. 使用确定性 Sufficiency 最低门槛。
7. 默认 Agent 注入 DB，修复模型名透传。

验收：

- 不启动 Milvus/Embedding 可完成稳定多轮问诊。
- 过敏/妊娠回答能进入安全规则。
- 充分后无额外问题。
- 同一消息不被重复执行。
- 默认真实链有 `agent_runs`。

### 阶段 2：Prompt 安全与状态质量（无 RAG）

1. system/developer/context/user 分层。
2. 明确本轮消息、来源、去重和更正。
3. 引入 `completed/needs_more_info/abstained/blocked`。
4. 收紧 dimension/safety type Enum。
5. 最小化发送给模型的患者字段。
6. 建立 15～30 条无 RAG 中文行为回归场景。

### 阶段 3：病历与医师确认（无 RAG）

1. 医师确认前置校验。
2. 核心病历确定性构建。
3. LLM 只润色允许字段。
4. 新临床建议不得绕过确认。

### 阶段 4：执行与审计基础设施

1. 统一模型、Token、超时、重试和错误预算。
2. 引入每次执行独立的 `AgentRunContext`。
3. 输出类型不符立即 blocked。
4. 可靠保存成功/失败审计。

### 阶段 5：Safety 回退与解释

1. 按实际审核对象选择回退阶段。
2. 标记/清理 stale 产物。
3. 校验解释和 issue 一一对应。
4. 使用保守安全文案。

### 阶段 6：RAG 与临床推理

1. 写入 `agent_evidences` 和 trace。
2. claim-to-evidence 映射和来源限制。
3. 区分 RAG 支持与模型知识模式。
4. 再提取共享 RAG 组件。

### 阶段 7：清理与框架评估

清理死字段、死代码、模板名实不符和重复实现。行为基线稳定后再评估 LangGraph；当前主要问题是状态和契约，不是缺少编排框架。

## 8. 建议首版边界

首版只做：

- Inquiry 结构化抽取和安全信息落 State。
- 确定性 Sufficiency 门槛。
- 单一下一问决策。
- 红旗症状转介。
- 默认 Agent DB 审计。
- Prompt 权限分层。
- 无 RAG 中文行为回归集。

暂不做：

- RAG 检索质量重构。
- RAG Agent 共享基类。
- LangGraph 迁移。
- 大规模 Prompt DSL。

这是最快能看到真实效果、同时不牺牲安全边界的路径。
