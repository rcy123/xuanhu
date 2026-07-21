# Agent 重构 L0-1 交接文件（AR-B-001 + AR-B-002 返修后）

## Codex 最终验收（2026-07-09）

- 结果：验收通过。
- AR-B-001、AR-B-002 已关闭。
- 补充修复：统一 `domain_state_version`；禁止 Graph State 容器夹带临床数据；
  禁止 LangGraph `force=true` 绕过 CompletenessPolicy；禁止既有 LangGraph
  会话跨运行时重建；`thread_id` 使用 graph-version namespace。
- 最终专项证据：`131 passed`；累计 L0 专项为 `149 passed, 1 xfailed`。
- xfail 属于 L0-2 登记的 Legacy 红旗已知缺口，不属于 L0-1 契约失败。

## 任务状态

- **任务编号**：L0-1 / AR-B-001（返修）+ AR-B-002（第 2 轮限定返修）
- **任务名称**：ADR、兼容矩阵与迁移边界 —— 架构合规返修
- **状态**：第 2 轮返修完成，就绪待验收
- **日期**：2026-07-09（原始 + AR-B-001 返修 + AR-B-002 返修同日）
- **执行者**：Claude Code（开发测试 AI）
- **前置任务**：无（L0-1 是首个任务）
- **后续任务**：L0-2（Golden E2E 行为基线）、L0-3（Runtime Feature Flag 与性能基线）

## 返修内容（AR-B-001 + AR-B-002）

以下为两轮返修逐项变更，按任务要求的维度分列。

---

### 1. ADR-003 和兼容矩阵：下一问职责、GapSelector、QuestionComposer

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| 删除 `use_llm_sufficiency` 开关 | ADR-003 回滚策略 | 删除整句，替换为"LangGraph 路径中 `CompletenessPolicy` 始终是确定性 Gate，不提供回退到 LLM SufficiencyAgent 的内部开关" |
| 禁止 LangGraph 路径回退 SufficiencyAgent | ADR-003 回滚策略 | 删除"LangGraph 路径中也可选择使用 LLM SufficiencyAgent"，替换为"Legacy `SufficiencyAgent` 只在 Legacy 会话中运行，LangGraph 会话不得调用" |
| IntakeExtractionAgent 只抽取事实 | ADR-003 明确边界 §LLM 残余角色 | 明确"不生成下一问、不判定完备性" |
| QuestionComposer 基于 GapSelector 生成下一问 | ADR-003 明确边界 §CompletenessPolicy 不负责 | 明确"下一问由模板或 `QuestionComposer` 生成；信息缺口由确定性 `GapSelector` 选择唯一缺口" |
| 兼容矩阵删除"InquiryAgent 生成 next_question"表述 | 兼容矩阵 POST /messages LangGraph 迁移后兼容要求（AR-B-002） | "`agent_message.content` 仍为 InquiryAgent（LLM）的 `next_question`"→"`agent_message.content` 在 LangGraph 路径中由模板或 `QuestionComposer` 基于 `GapSelector` 确定性选择的唯一信息缺口生成，不再由 InquiryAgent（LLM）生成" |

### 2. force=true 收紧：医疗硬前置条件

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| force=true 不得绕过医疗硬前置 | ADR-003 决策依据 §6 + 明确边界 §CompletenessPolicy 不负责 | 明确"不得绕过红旗（red flags）、过敏/妊娠/当前用药采集状态及其他医疗硬前置条件" |
| ManualOverrideRecord 建模 | ADR-003 决策依据 §6 + 明确边界 | 明确"若保留医师人工推进兼容语义，必须建模为独立、可审计的人工覆盖结果（`ManualOverrideRecord`），不得将 `CompletenessPolicy` 改写为通过" |
| 兼容矩阵 force 医疗硬边界（AR-B-002） | 兼容矩阵 POST /advance LangGraph 迁移后兼容要求 | 新增"不得绕过红旗（red flags）、过敏/妊娠/当前用药采集状态及其他医疗硬前置条件"和 ManualOverrideRecord 约束 |

### 3. ADR-002 和迁移边界：Graph State 字段严格对齐 §6.2

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| 删除 `pending_review` 字段 | ADR-002 Graph State 内容列表 | 移除。Doctor Review 以 `interrupt()`/checkpoint 为硬门控 |
| Graph State 字段对齐实施计划 §6.2（AR-B-002） | ADR-002 Graph State 内容列表 | 旧字段（`current_stage`, `state_version`, `rollback_counts`, `blocked_reason`, `recovery_status`, `doctor_review_ref`, `safety_rule_result_ref`）→ 实施计划 §6.2 `XuanhuGraphState` 定义的 12 个字段：`session_id`, `domain_state_version`, `command`, `command_id`, `graph_version`, `run_id`, `route`, `gate_results`, `artifact_refs`, `pending_interrupt`, `budget`, `last_error` |
| 删除 checkpoint 可保存"结构化提取结果"的模糊措辞（AR-B-002） | ADR-002 禁止项 | "完整原始模型输出（只保存结构化提取结果）"→"完整原始模型输出（只保存 `prompt_version` 引用和输出 token 计数，不得保存结构化临床模型输出）" |
| 禁止结构化临床模型输出 | ADR-002 禁止项 | 新增"结构化临床模型输出（如 `SyndromeResult`、`FormulaDraft`、`SafetyRuleResult` — 这些属于 Domain State，Graph State 仅保存引用 `_ref`）" |
| 迁移边界 Graph State 字段对齐 §6.2（AR-B-002） | 迁移边界 §1 规则 2 | 同样替换为 §6.2 字段列表 |
| 迁移边界删除"结构化提取结果"模糊措辞（AR-B-002） | 迁移边界 §1 checkpoint 禁止项 | "只保存已通过 Schema 校验的结构化提取结果"→"只保存 `prompt_version` 引用和输出 token 计数，不得保存结构化临床模型输出" |
| 迁移边界保留 Syndrome 阶段 | 迁移边界 §6 会话生命周期 | LangGraph 路径增加 `syndrome` 阶段 |

### 4. ADR-001 回滚措辞：Feature Flag + 恢复路径隔离

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| 删除渐进回退方案 | ADR-001 回滚策略 | 删除"渐进回滚：可先回滚部分阶段" |
| Feature Flag 只决定新会话 | ADR-001 迁移策略 §4 | 原文"每个阶段通过 Feature Flag 控制是否走 LangGraph 路径"→"Feature Flag 只决定新会话创建时的运行时身份，既有会话不得在生命周期内混合使用两种执行路径" |
| checkpointer 表归入 L1 | ADR-001 风险 §4 | "L0-2 Golden 基线阶段…创建"→"L1 LangGraph Runtime 骨架阶段…创建" |
| 既有会话不得重建或切换（AR-B-002） | ADR-001 回滚策略 §1 | 新增"既有 LangGraph 会话不得重建或切换到 Legacy，必须继续使用 LangGraph 路径直到会话结束。两类会话（Legacy 与 LangGraph）恢复路径严格隔离，不得交叉恢复" |

### 5. 兼容矩阵：IntakeExtractionAgent 职责、Syndrome/Formula 边界、会话隔离

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| IntakeExtractionAgent 只抽取事实 | 兼容矩阵 POST /messages 已知差异 | 新增职责描述 |
| 保留 Syndrome/Formula 边界 | 兼容矩阵 POST /advance 已知差异 | 修正 LangGraph 阶段序列包含 `syndrome` |
| 强化会话隔离 | 兼容矩阵 Feature Flag 行为表 | 增加"Feature Flag 只决定新会话的运行时身份" |

### 6. 迁移边界：L9-4 对齐、pending_review、Syndrome

| 变更项 | 文件 | 变更说明 |
|--------|------|---------|
| 对齐 L9-4 | 迁移边界 L9 阶段边界 | 删除"至少保留一个 release 周期" |
| 删除 pending_review | 迁移边界 Graph State 内容 | 移除，增加 interrupt/checkpoint 说明 |
| 结构化临床输出禁止 | 迁移边界 checkpoint 禁止项 | 引用禁止项 |
| 保留 Syndrome 阶段 | 迁移边界 会话生命周期 | LangGraph 路径增加 `syndrome` |

### 7. 契约测试强化（AR-B-001 + AR-B-002）

| 变更项 | 变更说明 |
|--------|---------|
| 删除空 `pass`（3 处） | `test_no_langgraph_implementation`（替换为实际断言检查 harper/agent_runtime 目录不存在）、`test_target_stage_sequence_correct`（替换为 syndrome + formula 显式断言） |
| 新增 ADR-001 回归断言（测试类 10，3 个测试） | 禁止渐进回滚、Feature Flag 只决定新会话、checkpointer 归入 L1 非 L0-2 |
| 新增 ADR-002 回归断言（测试类 11，5 个测试） | 禁止 `pending_review` 在 Graph State、interrupt 是硬门控、禁止结构化临床模型输出、字段命名对齐实施计划（已更新为 §6.2 字段） |
| 新增 ADR-003 回归断言（测试类 12，5 个测试） | 禁止 `use_llm_sufficiency`、禁止 LangGraph 回退 SufficiencyAgent、CompletenessPolicy 确定性、Legacy SufficiencyAgent 仅 Legacy、禁止模型阶段路由 |
| 新增会话隔离回归断言（测试类 13，5 个测试） | 兼容矩阵/迁移边界一致性、Feature Flag 新会话、禁止交叉恢复、禁止静默降级、ADR-001 与迁移边界一致 |
| 新增阶段归属回归断言（测试类 14，6 个测试） | Syndrome/Formula 边界保留、IntakeExtractionAgent 事实抽取、确定性缺口选择、迁移边界阶段一致、pending_review 非第二真源 |
| 新增 Legacy 删除时机断言（测试类 15，2 个测试） | 禁止 release 周期额外条件、L9 验收后移除 |
| 新增 AR-B-002 下一问职责断言（测试类 16，4 个测试） | 兼容矩阵禁止"InquiryAgent 生成 next_question"表述、必须提及 QuestionComposer、必须提及 GapSelector、ADR-003 IntakeExtractionAgent 不生成下一问 |
| 新增 AR-B-002 force 医疗硬边界断言（测试类 17，5 个测试） | ADR-003 force 不得绕过红旗、不得绕过过敏/妊娠、ManualOverrideRecord 建模、不得改写 CompletenessPolicy、兼容矩阵 force 医疗硬边界 |
| 新增 AR-B-002 Graph State §6.2 精确字段断言（测试类 18，4 个测试） | 迁移边界含 §6.2 字段、ADR-002 禁止项无模糊表述、迁移边界禁止项无模糊表述、ADR-002 Graph State 最小数据原则 |
| 新增 AR-B-002 既有会话禁止跨运行时恢复断言（测试类 19，5 个测试） | ADR-001 不得交叉恢复、Feature Flag 只影响新会话、不得重建/切换、迁移边界不得互相恢复、兼容矩阵不得互相恢复 |
| 新增 AR-B-002 完整阶段顺序断言（测试类 20，4 个测试） | 兼容矩阵阶段序列、迁移边界阶段序列、ADR-001 syndrome 独立阶段、兼容矩阵 sufficiency 非独立阶段 |
| 范围检查强化（测试类 6，7 个测试） | 覆盖 tracked diff（app/、tests/、frontend/）+ untracked 文件（app/、tests/、frontend/）+ test_agent/ 额外文件检查 |

### 8. 交接文件更新

本文件已全面更新，逐项列出 AR-B-001 和 AR-B-002 的全部修改和精确测试结果。

## 逐文件变更

| # | 文件 | 变更类型 | 轮次 | 说明 |
|---|------|------|------|------|
| 1 | `docs/01_agent部分优化/adr/ADR-001-adopt-langgraph.md` | 修改 | AR-B-001 + AR-B-002 | 消除按阶段回退 Legacy、checkpointer 归入 L1、回滚措辞收紧 |
| 2 | `docs/01_agent部分优化/adr/ADR-002-domain-state-and-graph-state-boundary.md` | 修改 | AR-B-001 + AR-B-002 | 删除 pending_review、禁止结构化临床输出、Graph State 字段对齐 §6.2、删除模糊措辞 |
| 3 | `docs/01_agent部分优化/adr/ADR-003-sufficiency-as-policy.md` | 修改 | AR-B-001 + AR-B-002 | 删除 use_llm_sufficiency、禁止 LangGraph 回退 SufficiencyAgent、force 医疗硬边界 |
| 4 | `docs/01_agent部分优化/legacy-api-compatibility-matrix.md` | 修改 | AR-B-001 + AR-B-002 | IntakeExtractionAgent 职责、Syndrome/Formula 边界、会话隔离、InquiryAgent→QuestionComposer、force 医疗硬边界 |
| 5 | `docs/01_agent部分优化/agent-runtime-migration-boundary.md` | 修改 | AR-B-001 + AR-B-002 | Legacy 删除时机对齐 L9-4、pending_review 移除、结构化输出禁止、Syndrome 阶段、Graph State §6.2 对齐、删除模糊措辞 |
| 6 | `test_agent/test_l0_1_contract.py` | 修改 | AR-B-001 + AR-B-002 | 删除 assert True/空 pass（4 处）、新增 AR-B-002 回归测试类 16-20（22 个测试）、范围检查强化（tracked + untracked + frontend） |
| 7 | `docs/dev-handoff/agent-refactor-l0-1.md` | 修改 | AR-B-001 + AR-B-002 | 本文件，返修内容与测试结果 |

**未修改的文件**：`app/`、`tests/`、`frontend/`、数据库迁移、依赖、配置 —— 全部未变更（已通过 git diff + untracked 双重验证）。

注：`docs/` 目录在 `.gitignore` 中，因此 doc 文件变更不出现在 `git diff` 中，但磁盘内容已确认修改。

## 测试命令及精确结果

| # | 命令 | 结果 | 备注 |
|---|------|------|------|
| 1 | `uv run pytest test_agent/test_l0_1_contract.py -q -rs` | **134 passed, 0 failed** (0.36s) | 纯文件契约测试，含 48 个回归断言（AR-B-001: 26 + AR-B-002: 22） |
| 2 | `uv run pytest -q -rs` | **936 passed, 0 failed** (351.13s) | 现有测试套件全部通过，2 个 warning（均为已有问题：test_record_agent.py FakeRecordOutput 字段名、alembic mssql RuntimeWarning） |
| 3 | `uv run ruff check .` | **All checks passed!** | 全项目 lint 通过 |
| 4 | `uv run mypy app` | **Success: no issues found in 72 source files** | 类型检查通过；pyproject.toml 有一个 note（unused module section，已有问题） |
| 5 | `uv lock --check` | **通过**（Resolved 63 packages in 5ms） | 锁文件一致 |
| 6 | `git diff --check` | **通过**（无输出） | 无空白错误 |
| 7 | `git status --short --untracked-files=all` | `?? None` + `?? test_agent/test_l0_1_contract.py` | 预存空文件 `None`（非本任务范围）+ 本任务测试文件 |

## 关键架构决策（返修后确认）

1. **LangGraph 替代手写 Supervisor**：采用 LangGraph StateGraph + checkpointer + interrupt 替代当前的 `Supervisor` Python 状态机。Feature Flag 只决定新会话运行时身份，既有会话生命周期内不可切换到另一条执行路径。恢复路径严格隔离，不得交叉。
2. **Domain State 是唯一权威**：Graph State checkpoint 只保存执行元数据和引用（严格对齐实施计划 §6.2 `XuanhuGraphState` 12 个字段），临床事实全部在 PG Domain State 中。无双真源。Graph State 禁止保存结构化临床模型输出（如 SyndromeResult、FormulaDraft、SafetyRuleResult）。
3. **Sufficiency 是策略而非 AI 决策**：`CompletenessPolicy`（确定性规则）替代 `SufficiencyAgent`（LLM）。Legacy `SufficiencyAgent` 只在 Legacy 会话中运行；LangGraph 路径不提供回退到 LLM SufficiencyAgent 的内部开关。模型不得决定充分性或阶段路由。
4. **下一问由 QuestionComposer 生成**：IntakeExtractionAgent 只抽取结构化事实（observations、safety delta、red flag candidates），不生成下一问、不判定完备性。GapSelector 确定性选择唯一信息缺口，下一问由模板或 QuestionComposer 生成。删除所有"InquiryAgent 生成下一问"的目标架构表述。
5. **force=true 医疗硬边界**：`force=true` 不得绕过红旗（red flags）、过敏/妊娠/当前用药采集状态及其他医疗硬前置条件。医师人工推进必须建模为独立、可审计的 `ManualOverrideRecord`，不得将 `CompletenessPolicy` 改写为通过。
6. **Prescription + Modification 合并**：`FormulaDraftAgent` 一次性输出完整处方草案。
7. **Doctor Review 是框架级 hard gate**：LangGraph `interrupt()` / `Command(resume=...)` 替代 `pending_review` 标志。`pending_review` 不作为 Graph State 字段或第二套控制真源。
8. **checkpointer 表归入 L1**：`AsyncPostgresSaver` 所需的 checkpoint 表和 write-ahead 表在 L1 LangGraph Runtime 骨架阶段创建，不归入 L0-2。
9. **Legacy 删除时机与 L9-4 一致**：不额外增加 release 周期条件。

## 当前行为与目标行为差异

| 维度 | 当前（Legacy） | 目标（LangGraph） | 迁移阶段 |
|------|--------------|-----------------|---------|
| 编排引擎 | `Supervisor` (Python 状态机) | LangGraph `StateGraph` | L1 |
| State 管理 | 单一 `XuanhuState` + PG `state_snapshot` | Domain State (PG) + Graph State (checkpointer, §6.2 12 字段) | L2 |
| Sufficiency | `SufficiencyAgent` (LLM) | `CompletenessPolicy` (确定性规则) | L3 |
| 下一问生成 | `InquiryAgent` (LLM) | `GapSelector` (确定性) → `QuestionComposer` (模板) | L3 |
| 阶段数 | 9 阶段 (inquiry→sufficiency→syndrome→prescription→modification→safety→review→record→done) | 7 阶段 (inquiry→syndrome→formula→safety→review→record→done) | L3–L6 |
| 处方 Agent | PrescriptionAgent + ModificationAgent (2 LLM) | FormulaDraftAgent (1 LLM) | L4 |
| Doctor Review | `pending_review` 标志 + 手动校验 | `interrupt()` / `Command(resume=...)` | L5 |
| 恢复 | `RecoveryService` (PG + Redis) | checkpointer (框架管理) | L1 |
| Feature Flag | 无 | `AGENT_RUNTIME_VERSION` (legacy/langgraph)，只决定新会话 | L3 |
| Legacy 实现 | 运行中 | 保留但 LangGraph 路径不调用，L9-4 验收后移除 | L0–L9 |
| Session isolation | — | 会话创建时确定运行时，不可隐式切换，不可交叉恢复 | L0+ |
| Checkpoint safety | — | 禁止结构化临床模型输出、PII、完整 Prompt/原始输出 | L2+ |
| Graph State 字段 | — | 12 个字段严格对齐 §6.2: session_id, domain_state_version, command, command_id, graph_version, run_id, route, gate_results, artifact_refs, pending_interrupt, budget, last_error | L1+ |
| force=true | — | 绕过 CompletenessPolicy.sufficient 但不绕过医疗硬前置（红旗/过敏/妊娠/用药）；ManualOverrideRecord 可审计 | L3+ |

## 未解决问题

1. **无**。AR-B-001 和 AR-B-002 返修未产生需要下游解决的阻塞问题。
2. 现有测试套件（`tests/`）936 个全部通过，0 失败。
3. 预存空文件 `None` 在仓库根目录（0 字节），不属于本任务范围，按任务要求保留不动。
4. `docs/` 目录在 `.gitignore` 中（line 54: `docs/`），所有 doc 文件变更不出现在 git tracked diff 中。这是项目既有配置，非本任务范围。

## 下游 L0-2/L0-3 注意事项

### L0-2（Golden E2E 行为基线）

- 必须覆盖 ADR-003 定义的 Sufficiency Policy 的确定性输出
- 必须覆盖 ADR-004 定义的合并后 `FormulaDraftAgent` 的输出 Schema
- 必须覆盖 ADR-005 定义的 Doctor Review interrupt 和恢复路径
- 必须覆盖兼容矩阵中全部 9 个端点和 13 种 SSE 事件
- Golden 基线必须基于 Legacy 路径（当前实现）的行为
- 不得将目标架构行为误写为当前行为
- **注意**：checkpointer 表建设已归入 L1，L0-2 不负责此事项

### L0-3（Runtime Feature Flag 与性能基线）

- Feature Flag `AGENT_RUNTIME_VERSION` 的合法值为 `legacy` 和 `langgraph`
- 不得在 L0-3 中切换默认值（默认值仍为 `legacy`，直到 L9）
- Feature Flag 切换必须写入 `audit_events`（`runtime.switched`）
- 性能基线必须分别采集 Legacy 和 LangGraph 路径的数据
- 会话创建时确定运行时身份，此后不可隐式切换
- 两类会话（Legacy/LangGraph）恢复路径不得交叉
- **注意**：Feature Flag 只决定新会话运行时，不提供 per-phase 的细粒度回退

### L1（LangGraph Runtime 骨架）

- 搭建空图 + checkpointer + interrupt + astream 时，不要接入任何业务 Agent
- 不要修改 `Supervisor`、`MessageService`、`ReviewService`、`RecoveryService`
- 不要删除 Legacy 代码
- 新增代码放在 `app/agents/langgraph/` 下
- **注意**：L1 负责创建 checkpointer 表（`checkpoints`、`checkpoint_writes`），此责任已从 L0-2 移交
- **注意**：Graph State 字段以实施计划 §6.2 `XuanhuGraphState` 为准（12 个字段）

## 未提交说明

本任务**未提交、未 push**。所有文件变更保留在工作目录中，等待验收通过后由项目管理者决定提交流程。

注：`docs/` 目录在 `.gitignore` 中，doc 文件（ADR-001/002/003、兼容矩阵、迁移边界、本交接文件）的磁盘变更不会出现在 git index 中。`test_agent/test_l0_1_contract.py` 作为新增未跟踪文件存在。

## 验收自检

- [x] AR-B-001 7 项返修（ADR-001/002/003、兼容矩阵、迁移边界、契约测试强化、交接文件）全部完成
- [x] AR-B-002 7 项返修全部完成：
  - [x] ADR-003 + 兼容矩阵：IntakeExtractionAgent 只抽取事实、GapSelector 确定性选择、QuestionComposer 生成下一问、删除"InquiryAgent 生成 next_question"
  - [x] force=true 收紧：不得绕过红旗/过敏/妊娠/用药、ManualOverrideRecord 可审计
  - [x] ADR-002 + 迁移边界：Graph State 字段严格对齐 §6.2（12 字段）、删除"结构化提取结果"模糊措辞
  - [x] ADR-001 回滚：Feature Flag 只影响新会话、既有 LangGraph 不得重建/切换、恢复路径不得交叉
  - [x] 契约测试强化：删除全部空 pass（4 处）、新增 22 个 AR-B-002 精确断言、范围检查覆盖 tracked + untracked + frontend
  - [x] 交接文件更新：逐项列出真实修改和精确测试结果
  - [x] 全部 7 个命令执行通过
- [x] ADR-001 消除按阶段回退 Legacy，checkpointer 表归入 L1
- [x] ADR-002 删除 `pending_review`，禁止结构化临床模型输出进入 checkpoint，Graph State 字段对齐 §6.2
- [x] ADR-003 删除 `use_llm_sufficiency`，禁止 LangGraph 路径回退 SufficiencyAgent，force 医疗硬边界
- [x] 兼容矩阵：IntakeExtractionAgent 只抽取事实，保留 Syndrome/Formula 边界，InquiryAgent→QuestionComposer，force 医疗硬边界
- [x] 迁移边界：Legacy 删除时机与 L9-4 一致，无额外 release 周期条件，Graph State §6.2 对齐
- [x] 契约测试：134 个测试全部通过（含 AR-B-001 26 个 + AR-B-002 22 个回归断言），无空 pass
- [x] `app/`、`tests/`、`frontend/`、依赖、配置、数据库迁移全部未修改（tracked diff + untracked 双重验证）
- [x] 命令 1–7 全部执行完毕，结果见测试命令表
- [x] 交接文件完整
