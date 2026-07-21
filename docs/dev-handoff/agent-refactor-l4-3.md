# L4-3 FormulaConsistencyVerifier 与无 RAG 模式交接

## 变更范围

本任务只增加独立、纯确定性的 FormulaConsistencyVerifier，并为真实 L4-2 成功结果增加进程内对象身份边界。没有模型调用、RAG、Safety、State/DB 写入、Graph 路由、API/UI 修改或 Git commit。

本任务变更文件：

- `app/agent_runtime/formula_consistency.py`
- `app/agents/formula_draft.py`
- `tests/test_l4_3_formula_consistency.py`
- `docs/dev-handoff/agent-refactor-l4-3.md`

工作树中原有 `docs/01_agent部分优化/Agent优化任务进度表.md` 用户修改已完整保留，本任务未编辑该文件。

## 版本

| 契约 | 版本 |
|---|---|
| Formula Schema | `formula-draft.v1`（未修改） |
| Formula input Schema | `formula-draft-input.v1`（未修改） |
| Formula AgentSpec | `formula-draft-agent.v1`（未修改） |
| Formula Prompt | `formula_draft_v1.jinja2`（未修改） |
| Formula no-RAG policy | `formula-draft-policy.no-rag.v1`（未修改） |
| Consistency policy | `formula-consistency-policy.v1` |
| Consistency report | `formula-consistency-report.v1` |
| Herb normalizer | `formula-herb-normalizer.v1` |
| Unit registry | `formula-unit-registry.v1` |

Report、每项 check、canonical formula/item/claim 均为 Pydantic frozen DTO 且 `extra="forbid"`。`passed` 和首个 `failure_code` 只能由固定顺序 checks 推导。相同输入、事实集合和信任模式产生相同 report 与 SHA-256 subject digest。

## 可信 Formula 来源边界

提供两个明确分层的入口：

- `verify_formula_consistency()` 接受不可信裸 `FormulaDraft`，自行 canonicalize 全部输入，只返回结构一致性报告。结构通过不授予路由、Safety、持久化或临床权威；报告明确记录 `trusted_formula_source=false`。
- `verify_trusted_formula_execution()` 只接受真实 `execute_formula_draft()` 成功路径注册的具体 `FormulaExecutionResult` 实例。

L4-2 的公开包装器与 L4-3 消费函数共享工厂闭包内的弱引用身份注册表。注册表保存 canonical RunSpec、RunArtifact、权威 FormulaDraftInput 和输出；没有模块级注册入口。消费时使用对象 `id + weakref + is` 验证具体实例，并复核成功状态、输出与 passed L4-2 report。手工构造 `_TrustedFormulaExecution`、手工 passed report、`model_construct()`、浅/深 `model_copy()` 均不能获得可信身份。消费方只得到深拷贝，不能反向修改注册表权威记录。

## Modification 精确定义

动作严格按 tuple 声明顺序逐项执行：

- `ADD`：目标规范药名必须不存在；dose 必须存在且可精确换算为 g；新 item 追加到当前 composition 尾部，note 为 `None`。
- `REMOVE`：目标必须存在；不得携带 dose；只删除当前位置 item，其他顺序和字段不变。
- `DOSE_ADJUST`：目标必须存在；dose 必须存在且可换算；只替换该 item 的规范 dose，herb、位置和 note 保持不变。
- `REPLACE`：当前 `formula-draft.v1` 只有单一 `herb`，无法无歧义表达旧药和新药。本任务固定返回 `FORMULA_CONSISTENCY_REPLACE_UNSUPPORTED_V1`，要求模型使用 `REMOVE + ADD`；没有无版本修改既有 Schema/AgentSpec/Prompt。

因此重复 REMOVE、REMOVE 后 ADJUST、ADD 已存在药味、REMOVE/ADJUST 不存在药味均按执行到该项时的当前 composition 固定失败。

## 药名规范化

- AR-B-028 返工后统一采用安全顺序：先检查原始字符串的 Unicode category，再执行 NFKC，再次检查 NFKC 结果，最后才允许折叠普通 Unicode 空白和 strip。
- Cc、Cf、Cs 固定拒绝，包括换行、回车、制表、零宽字符和 surrogate；控制字符绝不会被 `\s+` 静默转换成普通空格。
- 同一顺序用于药名、单位以及进入 canonical candidate 的 name、note、rationale、basis claim；modification reason 也复用同一入口。
- 空名称固定拒绝。
- alias registry 由模块内不可变 tuple 构建并以 `MappingProxyType` 公开，调用方不能注入或替换权威 registry。
- v1 包含代码拥有的固定繁简/常用别名，例如 `黃芪/黄耆 -> 黄芪`、`白朮 -> 白术`、`薄荷葉 -> 薄荷`。
- 未知但文本合法的药名采用“规范文本原样保留”策略，不猜测、不静默映射为另一药味，也不查询 RAG。
- 多个原始名称规范化为同一药味时固定返回 duplicate-herb failure。

## 剂量与单位规则

内部使用 `Decimal(str(value))`，统一换算后以 `0.000001g`、ROUND_HALF_EVEN 量化，再输出无冗余零的十进制字符串，避免二进制 float 等值不稳定。

代码拥有、冻结的 v1 unit registry：

- `g / 克 / 公克`：`1g`
- `两 / 市两`：`30g`
- `钱 / 市钱`：`3g`
- `枚 / 个`：`herb_specific`，固定失败并 `requires_human=true`
- `适量 / 少许`（含固定繁体别名）：`unsupported`，固定失败并 `requires_human=true`
- 其他单位：unknown，固定失败并 `requires_human=true`

缺 dose、非有限数、零、负数、换算后超过 `500g` 均固定失败。所有 L4-3 report 的 `requires_human` 恒为 true：失败需要人工处理；即使结构一致性通过，也仍受 `review_required=true` 医师复核契约约束。

## Candidate 重建与精确比较

base、candidate 和 modification 输入均先独立 canonicalize。权威 recomputed candidate 从 canonical base 开始顺序应用动作，不读取 candidate diff 猜测语义。

最终使用 frozen DTO 精确比较：

- NFKC/空白规范后的方名；v1 策略是继承 base 方名，不允许 modification 未授权改名。
- composition 的长度和顺序。
- 规范药名。
- Decimal 规范剂量字符串和统一 `g` 单位。
- note（只做确定性 Unicode/空白规范；动作不授权顺带修改）。
- rationale 和 basis（继承 base，不允许动作顺带修改）。

因此缺药、多药、规范化后重复药味、乱序、未授权 dose/unit/note/name/rationale/basis 变化均固定 candidate mismatch。

## Fact、Syndrome 与无 RAG 边界

base、candidate 和每个 modification basis 的全部 fact ID 必须属于调用边界提供的当前 active fact IDs 或受信 L4-1 Syndrome basis IDs。未知、inactive、superseded、stale、跨 session ID 不在允许集合中，固定失败。每个 modification 的 reason 和 basis claim 必须非空且不是固定占位文本。

再次检查：

- `evidence_mode=model_knowledge_only`
- `claim_evidence_links=()`
- `review_required=true`
- confidence 不超过 `0.65` 且必须有限
- 禁止隐藏或额外的 citation/source/literature/retrieval 字段
- 禁止 route/stage/next_stage/approved/safety_decision/doctor_decision

一致性通过只表示结构自洽，不代表临床有效、安全通过、允许推进阶段或允许跳过医师复核。

## 零模型调用与权限证据

`app/agent_runtime/formula_consistency.py` 只依赖标准库、Pydantic 和 Formula Schema；没有导入或调用 Model Gateway、AgentRuntime、SafetyRuleEngine、Repository、DB、Graph 或 retrieval。纯入口只计算并返回 frozen report；可信入口只读取 L4-2 闭包注册记录的深拷贝。专项测试的所有一致性检查均无模型调用；唯一真实 L4-2 身份正向测试使用既有 FakeGateway 先产生上游结果，L4-3 verifier 本身没有发出额外请求。

## 对抗测试覆盖

专项 67 项覆盖：无修改、ADD、REMOVE、DOSE_ADJUST、REPLACE 固定拒绝、多动作顺序；重复/冲突动作；等价单位和 float 噪声；herb-specific/unsupported/unknown unit；缺失、零、负、NaN、Infinity、超界 dose；别名碰撞和调用方 registry 替换攻击；candidate 缺药/多药/乱序/dose/note/name 篡改；unknown fact；Syndrome basis；no-RAG/review/authority 篡改；report 冻结；真实 L4-2 对象、复制对象与手工对象身份边界；输入不变和 report/digest 稳定性。AR-B-028 新增药名和单位中的换行、回车、制表、零宽字符、surrogate，以及 name/note/rationale 全路径和 `甘草` + `甘\n草` 绕过回归；全部固定失败、`requires_human=true`，且不产生 canonical/recomputed candidate。

## 实际验证结果

按任务要求执行结果：

- `git status --short --untracked-files=all`（开始）→ 仅发现用户原有进度表修改。
- `uv run pytest tests/test_l4_3_formula_consistency.py -q -rs` → **67 passed in 1.80s**（AR-B-028 实现后；最终复跑结果待下方门禁完成后更新）。
- `uv run pytest tests/test_l4_1_syndrome_draft.py tests/test_l4_2_formula_draft.py tests/test_l4_3_formula_consistency.py tests/test_l2_5_repository_outbox.py tests/test_advance_api.py -q -rs` → **138 passed, 4 warnings in 36.79s**（最终复跑）。
- 任务原文的 `uv run pytest tests/test_safety_engine.py tests/test_agent_schemas.py tests/test_prescription_agent.py tests/test_modification_agent.py -q -rs` → **失败：0 tests，`tests/test_safety_engine.py` 不存在**。
- 使用仓库实际文件名复验：`uv run pytest tests/test_safety_rule_engine.py tests/test_agent_schemas.py tests/test_prescription_agent.py tests/test_modification_agent.py -q -rs` → **176 passed in 2.35s**。
- `uv run pytest -q -rs` → **1448 passed, 1 xfailed, 14 warnings in 238.58s**（最终复跑）。
- `uv run ruff check .` → **All checks passed**。
- `uv run mypy app` → **Success: no issues found in 115 source files**。
- `uv lock --check` → **Resolved 83 packages**。
- `git diff --check` → **通过**；只有既有/工作副本 LF→CRLF 提示。

## 已知限制与明确未实现

- v1 alias registry 是冻结的最小代码表，不是完整本草同义词库；未知合法名称保留而不猜测。扩充必须升级 normalizer policy/version 并增加碰撞测试。
- v1 统一量化精度为 `0.000001g`、换算后上限 `500g`；如临床规格改变必须升级 unit policy/version。
- v1 不支持 `REPLACE`，必须使用 `REMOVE + ADD`；未来若增加旧药/新药双字段，必须同步升级 Formula Schema、AgentSpec、Prompt 和 consistency policy。
- 未实现 L4-4 ReasoningSubgraph、revision、条件边、回问和下游失效。
- 未实现 L5 Safety Gate、Doctor Review/HITL；没有产生 Safety passed。
- 未实现 RAG、retrieval、citation 或 evidence source。
- 未写 State、DB、outbox 或 Graph checkpoint；未修改 API/UI、`/advance`、Legacy Agent 或默认 runtime。
