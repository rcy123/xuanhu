# 悬壶 Agent 整体大修任务进度表

> 版本：v1.0
> 日期：2026-07-09
> 目标架构：Harness + LangGraph
> 总计划：`docs/01_agent部分优化/Agent整体大修实施计划-LangGraph版.md`
> 状态说明：本文档是任务发布、验收、返工和阶段关闭的唯一进度看板

## 1. 状态规则

任务状态：

- `未开始`：尚未发布。
- `已发布`：已给开发测试 AI 下达任务，等待交付。
- `已交付`：开发测试 AI 已提交结果，等待验收。
- `返工中`：验收未通过，已发布限定返工。
- `已完成`：本地代码和测试经项目经理验收通过。
- `阻塞`：存在无法在当前授权或环境内解决的外部阻塞。

审核状态：

- `未审核`
- `待验收`
- `验收通过`
- `验收未通过`

阶段只有在所属必需任务全部 `已完成`、阶段门禁通过且无打开的 P0/P1 阻塞时才能关闭。

## 2. 当前总览

| 项目 | 当前状态 |
|---|---|
| 当前阶段 | L0 已关闭；L1 进入条件已满足 |
| 当前任务 | 无；下一可发布任务为 L1-1 |
| LangGraph 新链路 | 未开始 |
| Legacy 生产链路 | 保持现状，只允许阻断性修复 |
| 无 RAG 新版问诊里程碑 | 未完成，目标 L3 |
| 无 RAG 全链路里程碑 | 未完成，目标 L6 |
| RAG 增强里程碑 | 未完成，目标 L7 |
| 全量切流与旧实现退役 | 未完成，目标 L9 |

## 3. 阶段进度

| 阶段 | 名称 | 状态 | 完成度 | 进入条件 | 关闭条件 |
|---|---|---|---:|---|---|
| L0 | 大修基线与迁移护栏 | 已完成 | 100% | 架构和计划已确认 | ADR、Golden tests、Feature Flag、基线完成 |
| L1 | LangGraph Runtime 骨架 | 未开始 | 0% | L0 关闭 | MainGraph、checkpointer、恢复和 stream 骨架通过 |
| L2 | Harness 核心与领域 State | 未开始 | 0% | L1 关闭 | AgentRuntime、Context、Verifier、Reducer、outbox 通过 |
| L3 | Intake 问诊子图 | 未开始 | 0% | L2 关闭 | 无 RAG 多轮问诊、Triage、Completeness 和单一下一问通过 |
| L4 | 临床推理与方药子图 | 未开始 | 0% | L3 关闭 | Syndrome/Formula Draft、回问、revision 和一致性验证通过 |
| L5 | Safety 与医师 HITL | 未开始 | 0% | L4 关闭 | Safety Gate、interrupt、review resume 和二次安全审核通过 |
| L6 | 病历子图 | 未开始 | 0% | L5 关闭 | RecordAssembler、Narration 限权、落库和无 RAG E2E 通过 |
| L7 | Evidence/RAG 增强 | 未开始 | 0% | L6 关闭 | Evidence policy、claim links、trace 和 RAG 评估通过 |
| L8 | 可观测性、评估与安全加固 | 未开始 | 0% | L7 关闭 | Episode、故障注入、隐私、行为评估和 Shadow 对比通过 |
| L9 | API/UI 切换与旧实现退役 | 未开始 | 0% | L8 关闭 | 全量新会话切流、回滚演练、Legacy 删除和全门禁通过 |

## 4. 任务明细

### L0：大修基线与迁移护栏

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L0-1 | ADR、兼容矩阵与迁移边界 | 无 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l0-1.md` |
| L0-2 | Golden E2E 行为基线 | L0-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l0-2.md` |
| L0-3 | Runtime Feature Flag 与性能基线 | L0-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l0-3.md` |

### L1：LangGraph Runtime 骨架

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L1-1 | LangGraph 与 PG Checkpointer 兼容性 Spike | L0 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l1-1.md` |
| L1-2 | GraphState、MainGraph 与命令路由 | L1-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l1-2.md` |
| L1-3 | AsyncPostgresSaver 与跨进程恢复 | L1-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l1-3.md` |
| L1-4 | GraphRunner、超时取消与事件转换 | L1-2/L1-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l1-4.md` |

### L2：Harness 核心与领域 State

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L2-1 | Observation/Safety/Artifact Schema 与数据库迁移 | L1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l2-1.md` |
| L2-2 | AgentSpec、RunSpec 与 AgentRuntime | L2-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l2-2.md` |
| L2-3 | ContextBuilder、Prompt 分层与隐私投影 | L2-1/L2-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l2-3.md` |
| L2-4 | Verifier Chain 与 Domain Reducer | L2-1/L2-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l2-4.md` |
| L2-5 | Repository、幂等事务与 Outbox | L2-1/L2-4 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l2-5.md` |

### L3：Intake 问诊子图

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L3-1 | IntakeExtractionAgent 与抽取验证 | L2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l3-1.md` |
| L3-2 | TriagePolicy 与人工转介 | L3-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l3-2.md` |
| L3-3 | CompletenessPolicy 与停滞策略 | L3-1/L3-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l3-3.md` |
| L3-4 | GapSelector 与 Question Composer | L3-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l3-4.md` |
| L3-5 | IntakeSubgraph、Messages API 与问诊 E2E | L3-1～L3-4 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l3-5.md` |

### L4：临床推理与方药子图

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L4-1 | SyndromeDraftAgent 与 SyndromeVerifier | L3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l4-1.md` |
| L4-2 | FormulaDraftAgent 合并基础方与加减 | L4-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l4-2.md` |
| L4-3 | FormulaConsistencyVerifier 与无 RAG 模式 | L4-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l4-3.md` |
| L4-4 | ReasoningSubgraph、Revision 与回问闭环 | L4-1～L4-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l4-4.md` |

### L5：Safety 与医师 HITL

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L5-1 | SafetyRuleEngine Graph Adapter 与回退修复 | L4 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l5-1.md` |
| L5-2 | SafetyExplanationAgent 限权与一致性验证 | L5-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l5-2.md` |
| L5-3 | Doctor Review Interrupt 与持久化恢复 | L5-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l5-3.md` |
| L5-4 | Review Resume、医师修改与二次安全审核 | L5-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l5-4.md` |

### L6：病历子图

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L6-1 | Doctor Review 前置 Gate 与 RecordAssembler | L5 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l6-1.md` |
| L6-2 | RecordNarrationAgent 限权重构 | L6-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l6-2.md` |
| L6-3 | RecordConsistencyVerifier 与幂等落库 | L6-1/L6-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l6-3.md` |
| L6-4 | ReviewRecordSubgraph、API 与无 RAG E2E | L6-1～L6-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l6-4.md` |

### L7：Evidence/RAG 增强

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L7-1 | EvidencePacket、SourcePolicy 与数据库关联 | L6 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l7-1.md` |
| L7-2 | Syndrome/Formula Retrieval Nodes 与 Trace | L7-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l7-2.md` |
| L7-3 | Claim-to-Evidence 与 Citation Verifier | L7-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l7-3.md` |
| L7-4 | RAG 降级策略、质量评估与 E2E | L7-1～L7-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l7-4.md` |

### L8：可观测性、评估与安全加固

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L8-1 | Episode Package、Metrics 与业务事件 | L7 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l8-1.md` |
| L8-2 | 隐私、权限、预算与 Prompt Injection 加固 | L8-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l8-2.md` |
| L8-3 | 故障注入、恢复与幂等验证 | L8-1/L8-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l8-3.md` |
| L8-4 | 行为评估集、真实模型试跑与 Shadow 对比 | L8-1～L8-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l8-4.md` |

### L9：API/UI 切换与旧实现退役

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L9-1 | API DTO、SSE 与前端 v2 适配 | L8 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l9-1.md` |
| L9-2 | 分阶段切流与新旧会话隔离 | L9-1 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l9-2.md` |
| L9-3 | 回滚演练与生产验收 | L9-2 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l9-3.md` |
| L9-4 | Legacy Agent 删除与文档收尾 | L9-3 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l9-4.md` |

## 5. 阻塞项

| 编号 | 严重度 | 阻塞项 | 关联任务 | 状态 | 处理方案 |
|---|---|---|---|---|---|
| AR-B-001 | P1 | L0-1 文档契约互相冲突：允许 LangGraph 会话阶段级回退到 Legacy、允许 LLM Sufficiency 回归、Graph State 可保存结构化模型结果且沿用 `pending_review`、下一问职责仍归 InquiryAgent；契约测试未能捕获这些冲突 | L0-1 | 已关闭 | ADR、兼容矩阵、迁移边界和精确负向契约测试已统一 |
| AR-B-002 | P1 | L0-1 第 1 轮返修不完整：下一问职责、`force=true`、Graph State 字段和弱测试仍有冲突 | L0-1 | 已关闭 | 统一 §6.2 字段、禁止 Gate 绕过与跨运行时重建，最终专项 131 passed |

新增阻塞编号使用 `AR-B-001` 递增。

## 6. 验收证据

| 日期 | 任务 | 轮次 | 结果 | 证据 |
|---|---|---:|---|---|
| 2026-07-09 | L0-1 | 0 | 验收未通过 | 专项 `81 passed`；全量后端 `936 passed, 2 warnings`；`ruff check .`、`mypy app`、`uv lock --check`、`git diff --check` 均通过。静态契约审查发现 AR-B-001；工作树另有无关未跟踪文件 `.claude/settings.local.json`，已保留未动 |
| 2026-07-09 | L0-1 | 1 | 验收未通过 | 专项 `107 passed`，但测试含空 `pass`/弱断言且未捕获文档冲突；全量后端在隔离 `--basetemp` 下 `936 passed, 2 warnings`；`.venv\Scripts\ruff.exe check .`、`.venv\Scripts\mypy.exe app`、临时可写 `UV_CACHE_DIR` 下 `uv lock --check --offline`、`git diff --check` 均通过。默认 uv/pytest 缓存目录权限异常已通过隔离目录复核，不构成代码失败。真实工作树无 tracked diff，未跟踪文件为 `.claude/settings.local.json`、空文件 `None` 与 `test_agent/test_l0_1_contract.py`；前两者为无关文件，已保留未动。登记 AR-B-002 |
| 2026-07-09 | L0-1 | 2 | 验收通过 | 最终契约专项 `131 passed`；统一 Graph State、Completeness、下一问和会话隔离边界；AR-B-001/002 关闭 |
| 2026-07-09 | L0-2 | 0 | 验收通过 | Golden `9 passed, 1 xfailed`；覆盖 8 类场景；strict xfail 记录 Legacy 红旗缺口且禁止迁移复制 |
| 2026-07-09 | L0-3 | 0 | 验收通过 | Feature Flag `8 passed`；性能 `1 passed`；20 回合 P50 54.48 ms、P95 91.67 ms、失败率 0%，默认 runtime=legacy |
| 2026-07-09 | L0 | 阶段门禁 | 验收通过 | L0 专项 `149 passed, 1 xfailed`；全量后端 `954 passed, 1 xfailed, 2 warnings`；ruff、mypy、uv lock、git diff check 通过；前端 22 files/161 tests、typecheck、lint、build 通过 |

## 7. 最近更新

| 日期 | 更新人 | 内容 |
|---|---|---|
| 2026-07-09 | Codex | L0 阶段关闭：L0-1/L0-2/L0-3 全部完成并验收通过，无打开 AR-B/P0/P1；L1 进入条件满足，下一可发布任务 L1-1 |
| 2026-07-09 | Codex | 完成 L0-3：Runtime Feature Flag 默认 legacy；建立 fake-model 性能基线和 Token 可观测缺口记录 |
| 2026-07-09 | Codex | 完成 L0-2：建立 Golden E2E 与 Legacy 行为/测试分类基线；红旗缺口以 strict xfail 固定并明确禁止迁移复制 |
| 2026-07-09 | Codex | L0-1 第 1 轮复验未通过：AR-B-001 返修不完整，新增 AR-B-002；文档仍存在下一问职责、确定性 Gate、Graph State/checkpoint 边界冲突，契约测试存在空 `pass` 和弱断言；生产代码未修改，全量门禁复核通过 |
| 2026-07-09 | Codex | L0-1 第 0 轮验收未通过：登记 AR-B-001，任务改为 `返工中 / 验收未通过`；生产代码未修改，全量门禁通过但文档架构契约冲突且契约测试存在无效断言 |
| 2026-07-09 | Codex | 恢复发布 L0-1：确认任务仍为 `已发布 / 待验收`、无依赖且无打开阻塞，交接文件尚未生成；保持原任务登记和文件边界，不重复登记 |
| 2026-07-09 | Codex | 发布 L0-1：ADR、兼容矩阵与迁移边界；任务新增测试统一隔离到仓库根目录 `test_agent/`，不得写入现有 `tests/` |
| 2026-07-09 | Codex | 建立 LangGraph Agent 整体大修任务看板，拆分 L0～L9 共 41 个可独立发布和验收的任务；下一任务为 L0-1 |

## 8. 下一步

1. 发布 L1-1：LangGraph 与 PostgreSQL Checkpointer 兼容性 Spike。
2. L1-1 仅验证依赖兼容、async checkpointer、Windows/FastAPI/Pydantic 组合，不接入业务 Agent。
3. 保持 `AGENT_RUNTIME_VERSION` 默认 `legacy`，直到后续切流任务通过独立验收。

## 9. 维护要求

- 发布任务时将状态改为 `已发布`、审核改为 `待验收`。
- 收到交付时改为 `已交付`，但不得在未检查代码和测试前改为 `已完成`。
- 验收失败时登记 `AR-B-xxx`，状态改为 `返工中`，发布限定返工任务。
- 验收通过时记录测试证据、关闭关联阻塞，并更新阶段完成度。
- 不因对话摘要或交接声明覆盖本地代码事实。
- 不提交 `docs/dev-handoff/*`，除非用户明确要求。
- `docs/` 当前被 `.gitignore` 忽略；进度更新仍必须落盘，但不会自动显示在 `git status`。
