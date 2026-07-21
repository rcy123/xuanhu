# L4.5-11-0 回退后基线重置与方案校准任务（v2 重新发布）

## 1. 发布信息

| 项目 | 内容 |
|---|---|
| 任务编号 | `L4.5-11-0` |
| 合同版本 | v2.0（重新发布） |
| 发布日期 | 2026-07-21 |
| 发布人 | Codex（工程项目经理，依据项目 owner 指令） |
| 执行负责人 | 领取本任务的开发测试执行者 |
| 状态 | 已发布 / 待交付 |
| 审核 | 待项目经理验收 |
| 实施分支 | `codex/l4-5-11-context-privacy-hardening` |
| 事实核对基线 | `a0790c6` |
| 生产代码基线 | `b97c9f9`（截至事实核对基线，其后两个提交仅修改受控文档） |
| 原发布合同 | `6a7b8c4` 中的 v1 任务合同；由本合同替代，不视为交付或验收失败 |
| 关联阻塞 | `AR-B-031`（P1，处理中） |
| 唯一方案交付 | `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md` |
| 交付与验收载体 | `docs/dev-handoff/agent-refactor-l4-5-11-0.md` |

执行者开始工作时必须记录包含本任务合同的 clean exact HEAD，作为本轮“执行起点”；事实核对仍以 `a0790c6` 为准。若代码事实已经变化，按停止条件处理，不得静默套用本合同。

## 2. 重新发布原因与权威关系

用户已回退 B1～B133 打地鼠式 Context 返工。原任务在 `6a7b8c4` 发布后尚未形成交付或验收结果，项目 owner 现要求重新发布。为避免旧合同、旧 v1 方案与当前管理台账形成多个事实源，本合同作如下限定：

1. 本文件是 `L4.5-11-0` 唯一有效任务合同；
2. `项目管理/00-当前状态.md` 是当前动作的唯一事实源；
3. `项目管理/01-任务台账.md` 是任务状态的唯一事实源；
4. `L4.5-11收敛性架构重写方案-2026-07-21.md`、`AR-B-031-scope-change-request-2026-07-21.md` 和 `项目管理/历史/ADR-006-pii-redaction-at-intake-entry-B133草案.md` 只提供回退前历史与失败模式，**不授权实施其中 A～F、Domain 假名化、临床白名单或文件拆分**；
5. 只有本任务验收通过后，项目经理才可以发布一个 v2 实现任务。

本次重新发布不抹除 `ACC-20260721-002` 的第一次发布记录，也不把“重新发布”伪装成新的工程验收。

## 3. 已核对的当前事实

截至事实基线 `a0790c6`：

- `app/agent_runtime/context.py` 为 210 行；
- 仓库不存在 `UserMessageProjection`；
- `tests/test_l2_3_context_builder.py` 收集 9 项测试；
- `app/services/langgraph_intake.py::_build_intake_input()` 将 `ConsultMessage.content` 写入 `IntakeExtractionInput.current_messages`；
- `app/agents/intake_extraction.py::build_intake_context()` 把 `current_messages` 的 `content` 原文 JSON 序列化到 USER 层；
- `app/agents/intake_extraction.py::execute_intake_extraction()` 将 `ContextPacket.messages` 转为字典后调用 `AgentRuntime.run()`；
- `app/agent_runtime/runtime.py::AgentRuntime._call_gateway()` 将 messages 直接交给 `ModelGatewayClient.chat_structured_observed()` 或 `chat_structured()`，当前没有独立的最终模型输入隐私门禁；
- `ContextBuilder` 对 Mapping/string 投影中的自由文本应用两个简单裸号码模式，但 `build_intake_context()` 的 USER 参数不经过 Mapping 投影，因此当前 Intake USER 原文缺口成立；
- 原始患者消息还承担 grounding 与 `EvidenceSpan` quote/offset 验证职责，不能把“修改 Domain 原文”当成无影响的默认方案。

当前真实调用链为：

```text
ConsultMessage.content
  -> app.services.langgraph_intake._build_intake_input
  -> IntakeExtractionInput.current_messages
  -> app.agents.intake_extraction.build_intake_context
  -> ContextPacket.messages（USER 为 current_messages JSON）
  -> app.agents.intake_extraction.execute_intake_extraction
  -> AgentRuntime.run
  -> AgentRuntime._call_gateway
  -> ModelGatewayClient.chat_structured_observed / chat_structured
```

交付方案必须重新核对这条链路，不得引用回退前的类型、10,000+ 行文件或约 180 项测试。

## 4. 任务目标

在不修改生产代码和测试代码的前提下，形成一个与当前小基线一致、有限、可验证、可分步回退的 L4.5-11 v2 工程方案。

必须同时完成：

1. 画出从原始消息到最终 Gateway 请求的当前数据流和所有权边界；
2. 定义有限威胁模型、明确支持集合、非目标和停止条件；
3. 比较并选定模型输入独立投影策略，不得预设 Domain State 改存假名；
4. 给出 EvidenceSpan quote/offset 在脱敏投影下仍可验证的具体机制；
5. 定义 Runtime/Gateway 最终不可绕过门禁的位置、失败码、审计内容和零请求失败语义；
6. 把后续工程拆成 2～4 个互斥小任务，每个都能先红后绿、独立验收和回退；
7. 只推荐其中一个作为验收后的下一发布任务；其余保持候选，不能提前实施。

## 5. 非目标

本任务不执行任何产品实现，不关闭 AR-B-031，也不恢复或批准旧 v1 A～F。

明确不做：

- 不编写或修改生产代码、测试代码、数据库迁移、依赖、CI 或部署配置；
- 不改变持久化消息、Domain State、审计存储或前端显示语义；
- 不设计或修改临床红旗规则、临床阈值、诊疗逻辑或 L5～L9；
- 不改变 Legacy 默认路径、RAG、两个 LangGraph 公共开关或切流策略；
- 不宣称解决“所有 PII”“所有 Unicode 变体”或无限攻击面；
- 不把 AI 工程审查写成隐私、法务、临床、伦理或机构签署。

## 6. 允许修改范围

执行者只允许新增或修改以下两个交付文件：

- `docs/01_agent部分优化/L4.5-11模型输入隐私收敛方案-v2-2026-07-21.md`
- `docs/dev-handoff/agent-refactor-l4-5-11-0.md`

本任务书若存在事实性错误，只在 handoff 登记并停止，由项目经理修订合同。执行者不得自行修改项目经理台账或任务合同。

## 7. 禁止修改范围

- `app/**`
- `tests/**`
- `frontend/**`
- `scripts/**`
- `.github/**`
- `pyproject.toml`、`uv.lock`、前端依赖和锁文件
- 数据库迁移、运行配置、部署文件和环境文件
- `docs/01_agent部分优化/项目管理/**`
- `docs/01_agent部分优化/Agent优化任务进度表.md`
- 回退前历史方案、历史批准单和历史 ADR 草案
- 任何既有交付、验收和专业签署记录

发现必须修改禁区时立即停止，在 handoff 写明触发原因、所需新权限和建议的新任务边界。

## 8. v2 方案强制内容

### 8.1 当前事实与数据所有权

逐项列出并引用当前代码：

| 数据层 | 必须回答的问题 |
|---|---|
| 原始持久消息 | 谁创建、谁读取、审计和 grounding 为何需要原文 |
| Domain State | 哪些结构化事实来自原文，是否允许被投影结果反向覆盖 |
| `IntakeExtractionInput` | 当前包含哪些原始或结构化字段，权威来源是什么 |
| 模型输入投影 | 生命周期、可丢弃性、密钥依赖和与原文的映射关系 |
| `ContextPacket.messages` | 每层允许和禁止的数据类别 |
| 模型审计记录 | 允许保存的摘要、digest、计数和失败码；禁止记录的原始 PII |
| 最终 Gateway 请求 | 最后一处不可绕过检查及失败时的零网络请求保证 |

模型输入投影必须是派生数据，不得成为 Domain 或持久化原文的新事实源。

### 8.2 有限威胁模型

用有限矩阵定义完成条件，至少包含：

- 保护对象：结构化身份字段、连续手机号、连续身份证号；
- 明确支持的变换：原始 ASCII、NFKC 可归一化全角形式、方案明确列出的常见分隔形式、JSON 序列化、同一最终请求内的消息边界；
- 保持对象：体温、血压、心率、血糖、日期、剂量等非身份临床数字，不得用“所有长数字都删除”代替分类策略；
- 失败状态：密钥不可用、投影失败、映射失败、最终门禁命中时 fail-closed，并证明 Gateway 零请求；
- 明确非目标：未列入支持集合的同形字、任意编码层数、任意跨请求拼接等进入风险台账，不通过无限追加正则暗示已解决。

每个威胁族必须绑定一个不变量和一组参数化测试维度。B1～B133 的单个样例可以作为回归输入，但不能成为完成定义。

### 8.3 EvidenceSpan 与 grounding

必须比较至少两种策略：

1. 等长替换，使模型可返回与原文坐标一致的 offset；
2. 每条消息维护显式 projection-to-source offset map，在 verifier 中映射后核对原始 quote。

方案必须选定一种，说明：

- 映射对象的稳定标识；
- NFKC、分隔符和替换长度变化如何处理；
- 模型返回 span 时在哪一层完成映射和拒绝；
- 无映射、越界、quote 不一致或跨消息 span 的固定失败行为；
- 如何防止模型看到的假名/占位符反向写成患者事实。

“保持现有 E2E 通过”或“后续再处理 offset”不能满足此项。

### 8.4 最终不可绕过门禁

方案必须以当前真实调用路径为依据，明确：

- 门禁位于所有 Intake 模型请求必经的哪一层；
- structured-output fallback 或重试是否仍经过同一门禁；
- 门禁扫描的是最终序列化请求还是上游中间对象；
- 命中时使用哪个固定、脱敏、可测试的失败码；
- recorder、日志和异常不得包含命中的原始值；
- 测试如何证明 Gateway 调用次数为 0；
- 其他 Agent 是否复用该边界，若不复用必须明确非目标和后续任务。

### 8.5 后续任务拆分

拆为 2～4 个任务。每个候选任务必须包含：

- 稳定候选编号和单一结果；
- 依赖与前置验收；
- 先红证据和期望失败原因；
- 允许文件、禁止文件和公共 API 影响；
- 专项测试与比例适当的回归门禁；
- 回退方式；
- 停止条件；
- 交付文件路径；
- 是否需要独立 reviewer 或外部专业批准。

任务范围必须互斥。不得先发布“拆分 `context.py`”；当前文件仅 210 行，只有未来职责和依赖证明需要新模块时，才能在对应实现任务中提出最小模块边界。

## 9. 交付文件要求

`docs/dev-handoff/agent-refactor-l4-5-11-0.md` 必须包含：

1. 执行起点 exact HEAD、分支和开始/结束工作树状态；
2. 本任务第 3 节每项事实的代码路径、命令和结果；
3. 新增/修改文件清单；
4. 与原 v1 方案逐项比较：保留、拒绝、待决；
5. v2 方案各不变量与对应测试维度；
6. 2～4 个候选任务及依赖图；
7. P0/P1/P2/P3 风险、owner、缓解和触发条件；
8. 明确声明是否修改生产代码、测试、依赖、配置和项目经理台账；
9. 只推荐一个下一任务，并说明其先红边界；
10. 未决问题和停止条件触发情况。

开发交付只能把任务状态声明为“已交付、申请验收”，不能自行写成“已完成”或“已验收”。

## 10. 最低核对命令与证据

执行者至少记录以下只读或文档门禁；可以按操作系统等价改写，但必须保留完整命令、退出码和摘要：

```powershell
git rev-parse HEAD
git status --short --branch

uv run python -c "from pathlib import Path; print(len(Path('app/agent_runtime/context.py').read_text(encoding='utf-8').splitlines()))"
git grep -n "UserMessageProjection" -- app tests
uv run pytest --override-ini addopts= --collect-only -q tests/test_l2_3_context_builder.py

rg -n "def _build_intake_input|def build_intake_context|def execute_intake_extraction|async def run\(|def _call_gateway|chat_structured" app
uv run pytest --override-ini addopts= -q tests/test_l0_1_contract.py
git diff --check
git status --short
```

`git grep` 无命中时退出码 1 是预期事实，不得伪写为测试失败。禁止为了让核对命令“变绿”而修改生产或测试文件。

## 11. 验收标准

项目经理仅在以下条件全部满足时接受 `L4.5-11-0`：

- 交付只包含本合同允许的两份文档；
- 所有代码路径、类型、测试数量和调用链与交付 exact HEAD 一致；
- v1 仅作为历史输入，没有被恢复为当前授权；
- 原始消息、Domain State、模型输入投影、审计摘要和最终请求的所有权互不混淆；
- 有限威胁模型具有明确边界、不变量、参数维度、非目标和停止条件；
- EvidenceSpan 选择了可执行的等长或 offset-map 策略，并定义 fail-closed 行为；
- 最终门禁绑定实际调用路径，覆盖 fallback/retry，且命中时零 Gateway 请求；
- 2～4 个候选任务范围互斥、可独立先红后绿、验收和回退；
- 只推荐一个下一任务，没有提前实现任何候选任务；
- 没有把工程验收写成专业签署；
- L0 文档契约通过，`git diff --check` 通过，交付文件显式纳入 Git；
- 项目经理完成独立事实核对并在 handoff 写入验收结论。

验收通过只表示可以发布一个 v2 实现任务。它不关闭 AR-B-031，不恢复 L4.5 总验收，不满足 L5 准入，也不替代具名隐私、法务、临床、伦理或机构批准。

## 12. 停止条件

出现任一情况，执行者立即停止扩写方案，在 handoff 记录证据并申请项目经理决策：

- 执行起点的生产代码、调用链、测试数量与本合同基线不一致；
- 需要修改本合同禁区才能证明设计成立；
- 方案要求改变原始消息、Domain State、Legacy、UI、临床规则或专业审批边界；
- EvidenceSpan 无法在所选投影方案下确定性映射和校验；
- 威胁模型再次依赖无限 Unicode/转义/跨分片样例追加；
- 候选任务超过 4 个、范围重叠或无法得到单一验收结论；
- 发现新的 P0/P1，而现有 AR-B-031 无法准确承载；
- 工作区包含无法归属的改动，或正式交付文件仍被 Git 忽略。

## 13. 发布后的唯一下一动作

开发测试执行者领取本合同，创建 v2 方案和 handoff，完成只读核对后提交“已交付、申请验收”。在项目经理验收前，不得开始任何生产代码或测试实现。
