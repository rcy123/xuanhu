# ADR-006 草案：在 Intake 入口进行 PII pseudonym 化与 Gateway 兜底脱敏（已撤回）

> 历史状态：B133 回退前草案，从未成为已采纳 ADR
> 撤回日期：2026-07-21
> 当前替代：L4.5-11-0 基线校准及待验收 v2 方案
> 禁止用途：不得据此恢复 B133 实现或直接发布旧 A～F

## 状态

已撤回。本文只保留回退前的分析和提案，不再作为当前架构决策或实施授权。

## 背景

L4.5-11 / AR-B-031 要求防止患者手机号、身份证号等 PII 泄漏到模型上下文。当前实现把脱敏放在 `app/agent_runtime/context.py` 的**投影边界**：`ContextBuilder.build()` 在已组装、已转义、已跨层混合的自由文本上扫描，通过黑名单正则识别“像不像”身份证/手机号的数字串，并叠加一份临床豁免黑名单（`_CLINICAL_TRUST_CONFLICT_TOKENS`，包含 `not/fake/test/simulation/non-vitals` 等词）。

经过 133 批 red/green 返工，该路径被证明**不可收敛**：

- 投影时刻文本已经历 NFKC 规范化、JSON 序列化、跨消息拼接、临床 marker 包裹，攻击面是“所有 Unicode 变体 × 转义 × 分片组合”的无穷集；
- 黑名单“像不像”范式天然漏报；临床豁免黑名单又容易被新词绕过；
- `context.py` 已膨胀至 10,064 行，多 scanner 共享状态，修一个 family 打坏另一个；
- 跨分片重组依赖 ContextBuilder 内三套独立机制，无法覆盖 3～5 分片的组合盲区。

最新 B133 第七轮 development-red 仍暴露 P1：逐字符 Unicode escape 全角普通数字绕过/误脱敏、partial exponent 后接空格/标点、3～5 分片跨 Context/raw/typed/Gateway 重组、不可信临床前缀骗过豁免。

《L4.5-11收敛性架构重写方案-2026-07-21.md》提出把脱敏点从投影边界**上移到数据入口**，并在**Gateway 拼接边界**加一次兜底，同时把临床豁免反转为冻结白名单。本 ADR 将该架构决策正式化。

## 决策

将 PII 脱敏从“投影边界单点黑名单扫描”改为**三层防御架构**：

1. **层 1：Intake 入口脱敏**（新）
   - 在 `build_intake_context` 调用 `ContextBuilder.build` 之前，对 `current_messages` 逐条处理。
   - 输入为单条原始患者消息，此时无 JSON 转义、无跨层拼接、无临床 marker 包裹。
   - 识别裸串手机号/身份证号，使用 HMAC 进行长度保持的 pseudonym 化。
   - 临床豁免采用冻结白名单：字段名 + 数值范围 + 单位三者交集；未命中白名单的连续数字 token 一律脱敏。
   - 处理后的消息写入 `UserMessageProjection.content`，Domain State 只存假名，原文不进入任何下游。

2. **层 2：ContextBuilder 投影**（大幅瘦身）
   - 保留 allowed_fields 白名单投影和 scalar 边界检查。
   - 删除 semantic number tokenizer、digit corridor、3 个 identity matcher、mobile matcher、clinical measurement verifier、transport artifact mask、boundary piece witness。
   - 由于入口已完成 pseudonym，投影层无需再识别无穷的 PII 形态。

3. **层 3：Gateway 拼接边界兜底**（新）
   - 在 `runtime.run()` 前，对最终发送给模型的 messages 串做一次极简裸串扫描。
   - 只识别入口层 1 应该处理但漏掉的裸 11 位手机号 / 18 位身份证，不处理转义/全角/跨分片。
   - 命中即 `ContextBuildFailed`，绝不让含裸 PII 的消息进入模型 Gateway。

### 辅助决策

- **临床豁免反转为冻结白名单**：删除 `_CLINICAL_TRUST_CONFLICT_TOKENS` 黑名单，改用不可变的临床生命体征字段表（字段名 + 单位 + 数值范围，含 CJK 别名）。只有“字段名命中白名单 AND 数值在范围 AND 单位匹配”才保留数字；其余数字 token 一律脱敏。
- **Domain State 只存假名**：`UserMessageProjection.content` 写入 pseudonym，下游只处理假名。原始消息可逆性仅在审计侧，由 `PseudonymKeyProvider` 提供；密钥缺失时 `PseudonymKeyUnavailable` fail-closed。
- **拆分 `context.py`**：按职责拆分为独立模块（入口、Gateway、白名单、投影、JSON parser、pseudonym），消除同一份文本被多 scanner 重复规范的二次复杂度。

## 决策依据

1. **攻击面从无穷集变为有穷集**：入口处理的是单条原始消息，没有跨层拼接和转义，只需识别裸串；投影层面对的不再是“所有可能的 PII 变形”，而是已经假名化的文本。
2. **白名单范式 fail-closed**：用“字段名 + 数值范围 + 单位”三重要素判断，比“黑名单排除假临床词”更难被新词绕过；攻击者无法用一个不在白名单里的词骗取豁免。
3. **三层防御互为冗余**：入口负责主要脱敏，投影负责字段边界，Gateway 负责最终兜底。任何一层漏掉，下一层仍可拦截。
4. **消除二次复杂度**：当前 `context.py` 中 `_normalized_semantic_view_with_spans`、`_normalized_semantic_text`、`_normalized_folded_prompt_view` 等多份规范叠加，导致 B133 Quality P1 的平方级扫描路径。拆分后每个 scanner 只跑一次。
5. **与 Harness 架构兼容**：入口脱敏是 Harness 的 `ContextBuilder` 上游步骤，不改变 Domain State 与 Graph State 的边界（ADR-002）。`UserMessageProjection.content` 是投影产物，本就是可变的。
6. **符合 L5 安全基线要求**：L5 进入前安全预审要求 G0 工程基线关闭，AR-B-031 必须先收敛。架构重写比逐批增加规则更可能关闭 AR-B-031。

## 明确边界

### 本 ADR 负责的实现范围

- `app/agent_runtime/context/_redaction_entry.py`：层 1 入口脱敏，含裸串识别与 HMAC pseudonym。
- `app/agent_runtime/context/_clinical_whitelist.py`：冻结白名单定义。
- `app/agent_runtime/context/_gateway_guard.py`：层 3 Gateway 兜底扫描。
- `app/agent_runtime/context/_projection.py`：瘦身后的 ContextBuilder 投影。
- `app/agent_runtime/context/_pseudonym.py`：pseudonym 化基础设施（复用并扩展现有）。
- `app/agent_runtime/context/_json_parser.py`：JSON span parser（从现有代码迁移）。
- `app/agent_runtime/context/__init__.py`：公共 API（`ContextBuilder`、`ContextPacket`、异常类）。

### 本 ADR 不触碰的范围

- `triage_precheck.py` 及其规则。
- 临床红旗规则、临床决策阈值、模型输出事实。
- L5～L9 业务实现（本 ADR 只关闭 L5 准入的 G0 工程基线）。
- UI/前端、持久化 schema（除 `UserMessageProjection.content` 内容）。
- `AGENT_RUNTIME_VERSION` 默认开关。
- `.env`、`.agent/`、`.github/copilot-instructions.md`、`auth.json`。
- 网络安全、Trusted Access、渗透、`npm audit`、依赖安全审计。

### 对 Legacy 路径的约束

- pseudonym 化和 Gateway 兜底**仅在 LangGraph v2 路径执行**。
- Legacy 路径继续使用现有 `context.py` 行为，不引入入口脱敏，Domain State 内容不变。
- 两类路径在会话创建时确定，运行期间不得混合切换（与 ADR-001 回滚策略一致）。

### 对原始患者消息的处理

- “原始患者消息不变”在原 AR-B-031 语境中指 Intake DTO schema 与注入文本层级不变。
- 本 ADR 改变的是 `UserMessageProjection.content`（投影产物）的内容，这是允许变更的投影层内部行为。
- 原始消息的可逆性只在审计侧，由 `PseudonymKeyProvider` 保证；Domain 事实语义（如“患者提供了手机号”）不变，只是存储形态为假名。

## 正面影响

- **收敛性**：把不可穷举的 PII 形态识别，转换为确定性的入口 pseudonym + 最终兜底，AR-B-031 可关闭。
- **可维护性**：拆分 `context.py` 为单一职责模块，每个 scanner 只跑一次规范化。
- **安全性**：白名单范式 fail-closed，消除假临床词绕过；三层防御提供冗余。
- **性能**：虽然增加 Gateway 扫描，但投影层大幅瘦身，总体复杂度低于现状。
- **L5 可推进**：G0 工程基线关闭后，L5 准入的人类责任人签署（G1～G6）可以启动。

## 风险与代价

1. **范围变更风险**：本 ADR 超出原 AR-B-031 “只改 ContextBuilder 投影边界”的约束，需要项目经理批准。
   - 缓解：通过 `AR-B-031-scope-change-request-2026-07-21.md` 正式申请；批准前不修改生产代码。
2. **Domain State 内容变化**：原始消息变为假名，Legacy 路径若复用同一入口会受影响。
   - 缓解：pseudonym 化通过 Feature Flag 或运行时路径判断仅作用于 LangGraph v2；审计侧保留可逆性。
3. **临床白名单误拒**：未登记的生命体征数字可能被误脱敏。
   - 缓解：白名单字段来源于已有医学/临床约定；误脱敏影响登记为 P2，不放宽 fail-closed。
4. **Gateway 兜底漏报**：层 3 只处理裸串，若层 1 有 bug 且形态不是裸串，可能漏报。
   - 缓解：层 1 在入口处理原始消息，攻击面已大幅收窄；漏报登记为 P2。
5. **模块拆分引入回归**：`context.py` 拆分可能破坏外部 import 或既有测试。
   - 缓解：保留公共 API 不变；必要时保留 re-export shim；分 6 批实施，每批“先红后绿”。
6. **密钥管理风险**：pseudonym 可逆性依赖运行时密钥。
   - 缓解：密钥缺失时 `PseudonymKeyUnavailable` fail-closed；密钥管理纳入审计。

## 迁移策略

按 6 批实施，每批独立验收，遵循“先红后绿”：

1. **批次 A**：建立临床生命体征冻结白名单（`_clinical_whitelist.py`）。
2. **批次 B**：实现入口 pseudonym 化（`_redaction_entry.py`）。
3. **批次 C**：瘦身 ContextBuilder 投影（删除冗余 scanner，保留白名单投影和 scalar 检查）。
4. **批次 D**：实现 Gateway 拼接边界兜底（`_gateway_guard.py`）。
5. **批次 E**：拆分 `context.py` 为独立模块，公共 API 不变。
6. **批次 F**：冻结统一 manifest，三路独立正式复审，full gates，验收、提交、clean exact-HEAD 复验。

每批结束后：

- 在未改源码上建立失败回归（绑定生产/测试 SHA-256）；
- 修复后回归通过；
- 至少一轮独立开发复审 P0/P1=0；
- 不放宽性能阈值；低配硬件噪声用隔离复跑处理。

## 回滚策略

1. **范围变更不批准**：执行退化路径，继续原 AR-B-031 范围内逐批修复，L5 保持 NO-GO。
2. **实施过程中回滚**：若某一批次引入不可修复的回归，回退该批次改动，保持公共 API 不变；已合并的批次不得回滚到“无入口脱敏”状态。
3. **Feature Flag 回滚**：LangGraph v2 会话可切换回 Legacy 路径创建新会话，但已有 v2 会话继续从原 checkpoint 恢复并结束，不得跨运行时重建（与 ADR-001 一致）。
4. **Domain State 回滚**：pseudonym 化只影响 v2 路径的 `UserMessageProjection.content`，Legacy 路径的 Domain State 不变；回滚后 Legacy 路径可正常读取。

## 验证方式

- 每批“先红后绿”回归测试，绑定生产/测试 SHA-256。
- 最终批次 F：
  - `uv run pytest tests/test_l4_5_11_round10_review_regressions.py -q -rs`
  - `uv run pytest tests/test_l3_1_intake_extraction.py -q -rs`
  - `uv run pytest tests/test_l2_3_context_builder.py -q -rs`
  - `uv run pytest -q -rs`
  - `uv run ruff check .`
  - `uv run mypy app`
  - `uv lock --check`
  - `git diff --check`
- 精确暂存 dry-run：验证 19 个限定路径（6 tracked + 5 untracked + 8 ignored evidence）不含 `.env`、`.agent/`、`.github/copilot-instructions.md`、`auth.json`。
- 三路独立正式复审 P0/P1=0。
- 更新 `L5进入前专业安全预审报告`，把新架构证据纳入 G0 关闭材料。

## 关联文档

- `docs/01_agent部分优化/AR-B-031-scope-change-request-2026-07-21.md`
- `docs/01_agent部分优化/L4.5-11收敛性架构重写方案-2026-07-21.md`
- `docs/01_agent部分优化/Agent优化任务进度表.md`
- `docs/01_agent部分优化/L5进入前专业安全预审报告-2026-07-19.md`
- `docs/01_agent部分优化/Agent整体大修实施计划-LangGraph版.md`
- `docs/01_agent部分优化/adr/ADR-001-adopt-langgraph.md`
- `docs/01_agent部分优化/adr/ADR-002-domain-state-and-graph-state-boundary.md`
