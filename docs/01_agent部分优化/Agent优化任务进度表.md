# 悬壶 Agent 整体大修任务进度表

> 版本：v1.3
> 日期：2026-07-15
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
| 当前阶段 | L0～L4 工程重新验收通过；L4.5 红旗规则人工审定待外部签署 |
| 当前任务 | L4.5-01～L4.5-10 技术闭环完成；等待具名临床专业人员审定 `triage-raw-text-precheck.v1` |
| LangGraph 新链路 | L0～L4 工程完成度为 100%；后端与 WebUI 灰度开关默认关闭，保持非默认、非临床验证 |
| Legacy 生产链路 | 保持现状，只允许阻断性修复 |
| 无 RAG 新版问诊里程碑 | 已完成，L3 关闭 |
| 无 RAG 全链路里程碑 | 未完成，目标 L6 |
| RAG 增强里程碑 | 未完成，目标 L7 |
| 全量切流与旧实现退役 | 未完成，目标 L9 |

## 3. 阶段进度

| 阶段 | 名称 | 状态 | 完成度 | 进入条件 | 关闭条件 |
|---|---|---|---:|---|---|
| L0 | 大修基线与迁移护栏 | 工程已完成 | 100% | 架构和计划已确认 | 契约、真实基线、切换审计及 CI 重新验收通过 |
| L1 | LangGraph Runtime 骨架 | 工程已完成 | 100% | L0 核心设计可用 | 生产生命周期、健康检查、恢复隔离和 stream 接线通过 |
| L2 | Harness 核心与领域 State | 工程已完成 | 100% | L1 核心底座可用 | AgentRuntime、Context、Verifier、Reducer、publisher 与审计闭环通过 |
| L3 | Intake 问诊子图 | 工程已完成 | 100% | L2 核心数据边界可用 | 原文 Triage、初始数据、幂等、SSE、UI 和安全集成测试通过 |
| L4 | 临床推理与方药子图 | 工程已完成 | 100% | L3 核心流程可用 | Read Model、恢复隔离、审计、UI 和全量真实 PG 验收通过 |
| L4.5 | Integration & Safety Hardening | 技术已完成 / 规则审定待签 | 100%（工程） | 中期审查完成 | P0/P1 技术关闭；规则人工审定完成后方可进入后续受控非临床阶段 |
| L5 | Safety 与医师 HITL | 未开始 | 0% | L4.5 关闭且后续范围获批 | Safety Gate、interrupt、review resume 和二次安全审核通过 |
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
| L1-1 | LangGraph 与 PG Checkpointer 兼容性 Spike | L0 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l1-1.md` |
| L1-2 | GraphState、MainGraph 与命令路由 | L1-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l1-2.md` |
| L1-3 | AsyncPostgresSaver 与跨进程恢复 | L1-2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l1-3.md` |
| L1-4 | GraphRunner、超时取消与事件转换 | L1-2/L1-3 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l1-4.md` |

### L2：Harness 核心与领域 State

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L2-1 | Observation/Safety/Artifact Schema 与数据库迁移 | L1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l2-1.md` |
| L2-2 | AgentSpec、RunSpec 与 AgentRuntime | L2-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l2-2.md` |
| L2-3 | ContextBuilder、Prompt 分层与隐私投影 | L2-1/L2-2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l2-3.md` |
| L2-4 | Verifier Chain 与 Domain Reducer | L2-1/L2-2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l2-4.md` |
| L2-5 | Repository、幂等事务与 Outbox | L2-1/L2-4 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l2-5.md` |

### L3：Intake 问诊子图

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L3-1 | IntakeExtractionAgent 与抽取验证 | L2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l3-1.md` |
| L3-2 | TriagePolicy 与人工转介 | L3-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l3-2.md` |
| L3-3 | CompletenessPolicy 与停滞策略 | L3-1/L3-2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l3-3.md` |
| L3-4 | GapSelector 与 Question Composer | L3-3 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l3-4.md` |
| L3-5 | IntakeSubgraph、Messages API 与问诊 E2E | L3-1～L3-4 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l3-5.md` |

### L4：临床推理与方药子图

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L4-1 | SyndromeDraftAgent 与 SyndromeVerifier | L3 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l4-1.md` |
| L4-2 | FormulaDraftAgent 合并基础方与加减 | L4-1 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l4-2.md` |
| L4-3 | FormulaConsistencyVerifier 与无 RAG 模式 | L4-2 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l4-3.md` |
| L4-4 | ReasoningSubgraph、Revision 与回问闭环 | L4-1～L4-3 | 已完成 | 验收通过 | `docs/dev-handoff/agent-refactor-l4-4.md` |

### L4.5：Integration & Safety Hardening

> 范围来源：`L0-L4中期代码审查报告-2026-07-12.md`。L4.5 是 L0～L4 的重新打开与整改关口，不扩展 L5 业务语义。

| 任务 | 名称 | 依赖 | 状态 | 审核 | 验收核心 |
|---|---|---|---|---|---|
| L4.5-01 | 测试数据库安全隔离与破坏性夹具治理 | 无 | 已完成 | 验收通过 | 只接受 `TEST_DATABASE_URL`；测试库名和环境哨兵双保护；迁移必恢复；PG/Redis 用例统一 integration |
| L4.5-02 | 原文确定性红旗预检与模型漏报阻断 | 无 | 技术完成 / 临床待签 | 技术验收通过 | 原文含任一审定高危红旗时，模型空候选仍阻断；模型不得删除或降级确定性候选 |
| L4.5-03 | 创建会话初始 Domain State 与高风险事实 grounded 验证 | L4.5-02 | 已完成 | 验收通过 | 主诉/人口学进入 Observation；结构化安全表单进入权威状态；模型高风险事实必须确认；身份数据不进入模型上下文 |
| L4.5-04 | 公开幂等协议与同会话在途命令控制 | L4.5-01 | 已完成 | 验收通过 | 同 key 跨 trace、断线重试和双进程仅调用/提交一次；保存 request digest 并稳定重放 |
| L4.5-05 | LangGraph Recovery 运行时隔离 | L4.5-01 | 已完成 | 验收通过（fail-closed） | LangGraph 会话绝不读取或修改 Legacy checkpoint；未实现路径固定 501，不宣称具备恢复能力 |
| L4.5-06 | Session Read Model 与版本化 L3/L4 DTO | L4.5-03 | 已完成 | 验收通过 | GET session 从权威 Domain/Gate/current artifact 重建，刷新和进程重建后结果不丢失 |
| L4.5-07 | Outbox Publisher、Redis Stream 与 SSE 闭环 | L4.5-01 | 已完成 | 验收通过 | claim/publish/ack/retry/DLQ/重启接管/有界去重可验证，两个独立 SSE 客户端同步成立 |
| L4.5-08 | 模型运行审计与临床缓存治理 | L4.5-03 | 已完成 | 验收通过 | required PostgreSQL recorder 在模型调用前/后 fail-closed；policy、canonical DTO + 有序消息 digest、actual model/attempt/latency/usage/输出摘要可审计；冲突重放拒绝；10k key 证明缓存有界并清理 |
| L4.5-09 | WebUI LangGraph 灰度入口与 L3/L4 结果闭环 | L4.5-04/L4.5-06/L4.5-07 | 已完成 | 验收通过 | 双 feature flag 默认关闭；启用后可创建/推进并显示 runtime/revision/unresolved，刷新后恢复 |
| L4.5-10 | CI、全量回归、故障注入与 L0～L4 重新验收 | L4.5-01～L4.5-09 | 已完成 | 技术验收通过 | 单命令 locked-dependency 门禁覆盖 Python 3.11/3.12、真实 PG/Redis、串行性能、worker 碰撞、前端、静态、安全、SBOM、promtool 行为、actionlint、Gitleaks 与 detached clean tree；仅在全门禁完成后发布绑定 exact HEAD 的 `reacceptance-result.json`；证据见重新验收报告 |

#### 2026-07-15 终审加固

| 层级 | 终审发现 | 完善结果 | 证据 |
|---|---|---|---|
| L0 | 默认 runtime 只靠环境变量，缺少持久切换事实 | 新增 allowlist-only `runtime.switched` 全局审计、deployment 唯一约束、事务 advisory lock 与部署 CLI；配置和台账不一致时 readiness 503、创建前 fail-closed | migration `20260715_0012`；真实并发/重复集成测试 |
| L1 | 请求级 checkpointer/compiled graph 与 Windows loop 启动风险 | FastAPI lifespan 每 worker 持有一个连接池、saver 和 compiled graph；生产无 request-local fallback；Windows 提供 Selector loop 的 `xuanhu-api` 入口；liveness/503 readiness 分离 | `tests/test_l1_application_lifecycle.py` |
| L2 | 模型审计 provenance、冲突和不可用语义不足 | migration `20260715_0011` 增加 `policy_version` 与 input digest；生产 recorder 必需，started/terminal 写入失败不调用/不返回模型结果；provenance、终态和 terminal→started 冲突 fail-closed | model-audit unit `54 passed`；真实 PG 集成 `15 passed` |
| L0/L3 | 旧性能基线不是生产池同口径 | Legacy/LangGraph 均改为 20 个独立新会话、首轮消息、生产 SQLAlchemy pool；LangGraph 保留真实 PG checkpointer、Domain、公开 claim 与 required audit，模型为确定性本地 gateway | 两次串行复跑均 `2 passed`；LangGraph P95 `1.15s` / `1.82s`，阈值 `<5s` |
| 运维 | Outbox 仅有健康信号，缺少可部署规则和缺指标行为验证 | 新增隐私安全聚合 metrics、5 条 Prometheus 规则、按 target 的 missing-metric 判断及 promtool rule tests | `promtool check rules`：5 rules；`promtool test rules`：SUCCESS |
| 复验 | 旧证据无法证明 exact HEAD/工具链 | 新增 clean-worktree、版本约束、环境证据 manifest、供应链审计与 SBOM 的单命令脚本；PowerShell 5.1 原生命令退出码 fail-closed，已知旧产物先清理，临时产物校验后发布，最终 `passed` 回执只在所有门禁完成后写入；不宣称基础设施镜像/工具完全离线可复现 | `scripts/verify_l0_l4_reacceptance.ps1`、`tests/test_reacceptance_gate_script.py` |

### L5：Safety 与医师 HITL

| 任务 | 名称 | 依赖 | 状态 | 审核 | 交接文件 |
|---|---|---|---|---|---|
| L5-1 | SafetyRuleEngine Graph Adapter 与回退修复 | L4.5 | 未开始 | 未审核 | `docs/dev-handoff/agent-refactor-l5-1.md` |
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
| AR-B-003 | P1 | L1-1 交付不完整：缺少指定交接文件 `docs/dev-handoff/agent-refactor-l1-1.md`；删除了 L0-1 契约测试 `test_agent/test_l0_1_contract.py`，超出 L1-1 范围；PG Checkpointer Spike 失败，`aget_state(config)` 将 `checkpoint_ns` 误解析为 subgraph，7 项集成测试中 5 项失败 | L1-1 | 已关闭 | 已恢复 L0-1 契约测试，补交 L1-1 交接文件；PG Spike 改用版本化 root `thread_id` 验证隔离，专项、全量后端、ruff、mypy、lock、diff check 均通过 |
| AR-B-004 | P1 | L1-2 将 mypy `python_version` 从 `3.11` 改为 `3.12`，但项目仍声明 `requires-python = ">=3.11"` 且 Ruff 目标为 `py311`，导致静态检查不再覆盖仍受支持的 Python 3.11；强制 3.11 检查暴露 NumPy stub 语法失败 | L1-2 | 已关闭 | mypy 恢复 `python_version = "3.11"`，约束 NumPy `>=2.4,<2.5` 并锁定 2.4.6；默认和显式 Python 3.11 无增量 mypy 均通过，未使用全局 import skip |
| AR-B-005 | P1 | L1-3 的 `validate_checkpoint_config` 未接入 MainGraph/checkpoint 执行路径，错配 state 可写入错误 namespace；`close_postgres_checkpointer` 对真实 saver 调用不存在的 `__aexit__` 并吞掉异常，实际不关闭资源；跨进程 helper 通过命令行传递含密码 DB_URL 且原样输出底层异常，可能泄露凭据 | L1-3 | 已关闭 | 校验已置于 checkpointer 实际写入边界；以拥有生命周期的 context manager 管理关闭；DB_URL 由子进程环境读取并统一脱敏错误输出 |
| AR-B-006 | P1 | L1-3 第 1 轮返工不完整：新增 `validated_ainvoke` 仍是调用方可绕过的自由函数，直接 `graph.ainvoke` 继续把错配 state 写入 checkpoint；子进程虽不再通过 argv 传 DB_URL，但仍输出截断的原始 `str(exc)`，真实连接失败暴露目标 IP、端口和底层 psycopg 文本 | L1-3 | 已关闭 | 在 checkpointer 写入边界强制校验；公开 graph API 的错配 state 已有回归测试；子进程失败只返回固定错误码和异常类型 |
| AR-B-007 | P1 | L1-3 第二轮行为和全量测试通过，但 Ruff 门禁失败：`app/agent_runtime/checkpoint.py:26` 导入未使用的 `collections.abc.Sequence` | L1-3 | 已关闭 | 删除未使用导入，Ruff 复验通过 |
| AR-B-008 | P1 | L1-4 的 `_sanitize_runner_error` 仅截断消息并处理 `postgresql://`，仍会把任意异常内的 API key、Bearer token、prompt 片段或患者身份文本带入 `GraphRunnerError`；专项测试只覆盖 DB URL，未覆盖该隐私边界 | L1-4 | 已关闭 | 对外 Runner 错误改为固定文本和错误码，底层异常链不再保留；ainvoke/stream 已覆盖 API key、token、prompt、身份文本和 DB URL 的异常、事件与异常链脱敏回归 |
| AR-B-009 | P1 | L2-1 ORM 与迁移外键删除语义不一致：ORM 声明 CASCADE/RESTRICT/SET NULL，migration `_uuid_column` 却未传 `ondelete`；Safety Pydantic Schema 接受数据库约束拒绝的 pregnancy/lactation 任意字符串；Artifact revision 允许同一 artifact 多个 `current`，且父 revision 可跨 artifact/session，无法保证有效 revision 链 | L2-1 | 已关闭 | Schema/ORM/migration 外键 action 与安全值域已统一；partial unique index 保证单 current，复合自引用 FK 与前序 revision CHECK 保证同 artifact/session 父链；真实 PostgreSQL 约束测试通过 |
| AR-B-010 | P1 | L2-2 缺少专项测试，Runtime attempt 未限制 gateway 内部实际请求，FailurePolicy 未接入执行分支 | L2-2 | 已关闭 | 新增 fake-gateway 专项；每 attempt 限制一个实际请求并接入固定错误码、预算和 deadline 重试策略 |
| AR-B-011 | P1 | L2-2 recorder 可位于完整 run deadline 之外无限阻塞 | L2-2 | 已关闭 | recorder 操作受剩余 deadline/短收尾上限约束，阻塞异步任务可取消并消费 |
| AR-B-012 | P1 | L2-2 同步 recorder 的 `to_thread` 工作可逃逸 run 生命周期 | L2-2 | 已关闭 | recorder 收紧为 async-only；同步实现构造阶段固定拒绝，不创建后台线程 |
| AR-B-013 | P1 | L2-3 裸字符串 context 绕过脱敏，伪名 HMAC 使用源码公开固定 key | L2-3 | 已关闭 | 所有 context 统一递归脱敏；伪名密钥仅运行时注入/密钥提供者，缺失即拒绝 |
| AR-B-014 | P1 | L2-4 Reducer 接受可手工伪造的 passed report，绕过 source/stage/prerequisite | L2-4 | 已关闭 | Reducer 仅接受完整 VerificationContext，并在提交边界重跑 canonical VerifierChain |
| AR-B-015 | P1 | L2-4 安全返工后残留旧 report API 测试和旧交接说明 | L2-4 | 已关闭 | 所有正向调用迁移到 VerificationContext，专项与交接文件同步完成 |
| AR-B-016 | P1 | L3-1 公开执行入口与 L2 Runtime 对已是 `IntakeExtractionInput` 的实例仅做 `isinstance` 判断而不重验；免校验构造的 assistant-only 当前消息会先进入模型请求，然后才被 Intake verifier 拒绝，使“严格输入校验 / assistant 不得作为当前 patient 来源”可被绕过 | L3-1 | 已关闭 | 公开入口对所有输入使用基类 serializer + `model_validate_json()` canonical 重建；assistant-only、重复 ID 和 DTO 子类负向回归均在 gateway 前拒绝且请求数为 0 |
| AR-B-017 | P1 | L3-1 输出边界仍信任已是 `IntakeExtractionOutput` 的实例；Intake verifier 只重验 `model_dump()` 副本，后续检查和成功返回仍使用原对象。`model_construct(decision='abstained', observations=...)` 可跳过 Enum identity/决策一致性并以 `succeeded` 返回；`model_copy(update={'route':'reasoning'})` 的隐藏越权字段被 dump 丢弃却仍存在于成功输出 | L3-1 | 已关闭 | 新增 `canonicalize_intake_output()`，递归拒绝原对象隐藏字段，以 canonical 基类 DTO 替换 artifact 后执行 verifier 并返回；字符串 decision 矛盾固定拒绝，隐藏 `route` 固定拒绝 |
| AR-B-018 | P1 | L3-1 身份信息 verifier 只按 dot 分段匹配少量 fact key，且正文只识别连续手机号/身份证号；普通合法 DTO 中的 `patient.full_name='Alice'`、`{'patient_name':'Alice'}` 嵌套值和 `contact.mobile_number='138-0013-8000'` 均通过验证 | L3-1 | 已关闭 | 统一 canonical 身份语义集，递归检查 fact key/嵌套 JSON key，支持命名空间、下划线/紧凑别名与常见分隔格式号码；所有已证实样例均固定拒绝 |
| AR-B-019 | P1 | L3-1 第 2 轮身份边界返工不完整：`_is_identity_key()` 将 key 全量规范化后只和 `id_card`/`identity_card`/`national_id`/`outpatient_no`/`medical_record_no` 等做整体相等，但对加了命名空间的 `patient.id_card`、`patient.identity_card`、`patient.national_id`、`patient.outpatient_no`、`patient.medical_record_no` 及嵌套 `{'patient.id_card': ...}` 均返回 succeeded | L3-1 | 已关闭 | `_is_identity_key()` 新增完整别名或 `_<identity_alias>` 后缀匹配，指定五个 fact key 与嵌套 key 均固定返回 `INTAKE_IDENTITY_FACT_FORBIDDEN` |
| AR-B-020 | P1 | L3-1 第 3 轮身份边界返工仍不完整：别名集显式支持 `fullname`/`phonenumber`/`mobilenumber` 无下划线形式，但遗漏同类 `idcard`/`identitycard`/`nationalid`/`outpatientno`/`medicalrecordno`；`patient.<alias>` 五个合法 fact key 在公开入口均返回 succeeded | L3-1 | 已关闭 | 别名只维护一份 canonical 语义集，比较时统一紧凑化连续 token 后缀；五个紧凑 fact key、嵌套 key 及全部历史回归均通过 |
| AR-B-021 | P1 | L3-2 声明 Triage 结果严格冻结且规则版本化，但 `TriagePolicyResult` 内嵌的 `GateResultSchema` 及 `details` dict 可原地修改：呼吸困难结果可从 `BLOCKED` 改为 `PASSED`、details 可改为 `continue`；导出的 `TRIAGE_RED_FLAG_RULES` 也是普通 dict，可将呼吸困难从 emergency referral 降级为 manual review | L3-2 | 已关闭 | 权威结果改为 frozen Triage DTO，嵌套 details/rules/source refs 使用 tuple/frozen 子 DTO；公开规则注册表结构不可变，适配副本不反向影响权威结果 |
| AR-B-022 | P1 | L3-2 第 1 轮不可变返工不完整：公开 `TRIAGE_RED_FLAG_RULES` 虽改为 `MappingProxyType`，模块仍保留同一底层 `_TRIAGE_RED_FLAG_RULES` 普通 dict；修改该引用后，呼吸困难的权威处置由 `emergency_referral` 降级为 `manual_review` | L3-2 | 已关闭 | 删除普通 dict backing store，改为 tuple 支撑的 `FrozenTriageRuleRegistry`；独立扫描无可变规则表，高危规则篡改回归及二次 evaluate 均通过 |
| AR-B-023 | P1 | L3-3 完备性权威语义不成立：`chief_complaint.category` 被当作主诉症状，只有类别没有症状即可 `ready/PASSED`；同一维度的不同合法子字段按 value fingerprint 互判冲突，补充真实 `chief_complaint.symptom` 后反而 `conflict`；女性年龄 30 但缺少绝经状态仍直接判妊娠/哺乳 `applicable`，与“必要事实缺失应 unknown”契约冲突；内部携带 candidate 的伪造 `continue/PASSED` Triage Gate 仍可被接受并进入 `ready`；第 1 轮返工后两个不同的当前 `chief_complaint.category` 因不属于覆盖维度而逃逸同 canonical fact key 冲突检测，动态十问类别由排序结果静默决定并可返回 `ready` | L3-3 | 已关闭 | 分类辅助事实与症状覆盖分离；冲突检测消费完整当前事实集并以独立辅助维度审计 category，同值重复幂等、异值乱序稳定 `conflict/FAILED`；缺绝经状态保持 unknown；Triage Gate 内部一致性固定重验；两轮对抗回归及全门禁通过 |
| AR-B-024 | P1 | L3-4 权威边界可绕过：`compose_question()` 只重验裸 `GapSelectionResult` 形状，伪造 `source_completeness_disposition=ready` 的 selected 结果仍生成问题；公开 `select_gap(..., priority_registry=...)` 允许调用方替换权威优先级并把首选从过敏改为主诉；公开模板注入可返回与 selected dimension 不一致的问题；模型 fallback 接受与 selection 不同 `state_version` 的 RunSpec 并成功调用；单问验证遗漏英文 name/phone/ID 等身份索取；第 1 轮返工后 supplied selection 的隐藏 `route/force` 仍被 canonical 序列化静默丢弃并成功生成问题，且 fallback 接受 temperature=2、max_tokens=200000、timeout=86400、空 verifier chain 的宽松 AgentSpec 并请求模型 | L3-4 | 已关闭 | Composer 内部重算 Completeness→Gap 权威选择；生产优先级/模板固定为私有冻结权威引用；模板与 RunSpec/AgentSpec 完整绑定；supplied selection 递归拒绝未声明/授权字段；中英文身份语义拒绝；两轮对抗回归、L3 合并回归及全门禁通过 |
| AR-B-025 | P1 | L3-5 集成未满足 Harness/LangGraph 权威边界：LangGraph 消息路径绕过 `PostgresDomainRepository`，未写 `DomainCommandCommit`/`OutboxEvent`，患者消息与领域结果分两次 commit；模型调用位于持锁数据库事务内；同 command 无数据库级幂等；ready 在 `/messages` 内直接改为 `syndrome`，而 `/advance` 不读取持久化 Completeness Gate、只运行 reasoning 占位并原样返回；所谓 IntakeSubgraph 只是依赖请求级 `ContextVar` executor 的单节点，进程恢复时未绑定即失败；L3-5 仅 2 个适配器单测，未覆盖 E2E、路由、并发、幂等、Outbox、调用次数和恢复；`mypy app` 另有 6 errors | L3-5 | 已关闭 | IntakeSubgraph 已拆为真实节点图并使用条件边路由；领域提交复用 Repository/Outbox 与数据库 claim 幂等，模型调用移出持锁事务；`/advance` 消费当前版本持久化 Gate；恢复元数据不保存临床事实，真实 PostgreSQL/Fake Gateway E2E 覆盖并发单调用、重放、commit 后恢复、路由和隐私，全部门禁通过 |
| AR-B-026 | P1 | L4-1 模型前权威边界可绕过：`validate_syndrome_preflight()` 只检查调用方传入的裸 `GateResultSchema` 名称、版本、state version、decision 和少量 details，不从当前 Domain snapshot 重算或验证持久化来源；仅有主诉和一项十问事实的明显不完整快照配自造 `ready/PASSED` Gate 仍调用模型并成功。`_verify_context()` 只比较 observation ID 集合及 session/status，不比较 fact key、value、normalized value；同一 ID 下篡改症状内容后，篡改文本被直接发送给模型并成功 | L4-1 | 已关闭 | 新增 Repository `ReasoningAuthoritySnapshot`：在同一持锁读取事务中验证 `syndrome/active/langgraph` 当前会话、当前 Domain State `V+1` 与 `/advance` 精确绑定的来源 Gate `V`；source Gate ID/version、同一 completed Intake GraphRun、Triage continue/零候选和 Completeness ready 全部固定重验。模型 Context 仅从权威 Domain State 投影，调用方 State/Context/Gate/stage 不再成为临床真源；缺 authority 固定失败，真实 PG 阶段/版本/跨 run/重复 Gate 与对抗回归全部通过 |
| AR-B-027 | P1 | L4-2 未真正绑定上游已验证 Syndrome 产物：第 0 轮信任调用方裸 Draft；第 1 轮信任调用方成套 Draft/RunSpec/RunArtifact；第 2 轮以可构造 PrivateAttr/内部 DTO 作为 capability，均可把伪造证型送入 Formula 模型 | L4-2 | 已关闭 | L4-1 公开执行包装器与 Formula consumer 共享闭包内弱引用身份注册表，只有真实 `execute_syndrome_draft` 成功返回的具体对象实例登记；Formula 以 id+weakref 双重确认并消费深拷贝权威记录。手工 result/passed report/PrivateAttr/内部 DTO、裸 Artifact/RunSpec、复制对象均不命中且 gateway 0 次；42 项专项、合并回归、全量与静态门禁通过 |
| AR-B-028 | P1 | L4-3 药名控制字符可绕过：`_normalize_text()` 先用 `\s+` 折叠空白，再检查 Unicode `Cc/Cf/Cs`，导致换行、回车、制表等控制字符在检查前消失。`herb="甘\n草"` 被规范为 `"甘 草"` 并得到 `passed=True`；同一药味可通过插入控制字符逃逸 alias/duplicate-herb 检测，污染后续候选方与 Safety 输入。现有 41 项专项未覆盖控制字符、format/surrogate 与规范化后重复组合 | L4-3 | 已关闭 | `_normalize_text()` 在任何空白折叠前依次检查原始字符串与 NFKC 结果，统一拒绝 `Cc/Cf/Cs`；药名、单位、name/note/rationale/basis 等 canonical 文本共享该顺序。新增换行、回车、制表、零宽字符、surrogate、单位与 `甘草 + 甘\n草` 碰撞回归，67 项专项及全门禁通过 |
| AR-B-029 | P1 | L4-4 跨进程恢复可信边界曾可铸造：第 0 轮原始 DTO restore 可直接登记 trusted；第 1 轮调用方可注入伪 `PostgresDomainRepository` 子类返回手工 record/authority | L4-4 | 已关闭 | 恢复入口现只接受 session/artifact/revision/expected digest，并在闭包创建时捕获项目 session-factory 函数；Repository 与当前 authority 均由入口内部从 PostgreSQL 构造/加载。旧原始 restore 不存在，传入 Repository/record/Gate 固定 TypeError；原始 DTO、复制对象、伪 Repository 路径不命中 trusted consumer，真实 PostgreSQL restart 恢复保持通过 |
| AR-B-030 | P1 | L4-4 曾丢弃真实 RunArtifact provenance，对 Syndrome/Formula payload 固定写 `model_actual="fake-model"`、`attempts=1`、`latency_ms=0`，恢复再以伪 provenance 自洽重验 | L4-4 | 已关闭 | Syndrome/Formula commit 现只消费各自 closure trusted execution 的 canonical RunArtifact，完整持久化 output、model、attempts、latency、usage、evidence、trace/run/spec/prompt；消费失败不写临床 artifact。Syndrome 恢复和 Formula replay 均绑定完整 subject digest/canonical payload，任一 provenance 字段篡改固定拒绝；26 项专项及全门禁通过 |

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
| 2026-07-10 | L1-1 | 0 | 验收未通过 | `uv run pytest tests/test_langgraph_compatibility_spike.py -q -rs`：`5 passed`；`$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'; uv run pytest tests/test_langgraph_postgres_checkpoint_spike.py -q -rs`：`5 failed, 2 passed`，失败为 `ValueError: Subgraph l1-1-spike not found` / `ValueError: Subgraph v1 not found`；指定交接文件缺失；`test_agent/test_l0_1_contract.py` 被删除，超出 L1-1 范围 |
| 2026-07-10 | L1-1 | 1 | 验收通过 | L1-1 非 PG Spike `5 passed`；PG Checkpointer Spike `7 passed`；L0-1 契约 `131 passed`；全量后端 `966 passed, 1 xfailed, 2 warnings`；`uv run ruff check .`、`uv run mypy app`、`uv lock --check`、`git diff --check` 均通过；AR-B-003 关闭 |
| 2026-07-10 | L1-2 | 0 | 验收未通过 | 功能与回归门禁通过：L1-2 专项 `44 passed`、L1-1 Spike `12 passed`、L0-1 契约 `131 passed`、全量后端 `1010 passed, 1 xfailed, 2 warnings`，ruff、配置为 3.12 的 mypy、lock、diff check 通过；静态配置审查发现 AR-B-004：项目仍支持 Python 3.11，但 mypy 目标被改为 3.12，`uv run mypy app --no-incremental --python-version 3.11` 失败于 NumPy stub 的 PEP 695 `type` 语句 |
| 2026-07-10 | L1-2 | 1 | 验收通过 | AR-B-004 关闭：`uv run mypy app --no-incremental` 与显式 `--python-version 3.11` 均为 `Success: no issues found in 79 source files`；L1-2 专项 `44 passed`、L1-1/PG Spike `12 passed`、L0-1 契约 `131 passed`、全量后端 `1010 passed, 1 xfailed, 2 warnings`；ruff、lock、diff check 通过；`.gitignore` 的 `.workbuddy/` 改动未申报且不属于 L1-2，验收与后续提交均应排除 |
| 2026-07-10 | L1-3 | 0 | 验收未通过 | L1-3 PG 专项 `30 passed`，L1-2/L1-1/L0 回归 `187 passed`，ruff、Python 3.11 mypy、lock、diff check 通过；未继续跑全量门禁。诊断证明错配 config/state 被 MainGraph 成功写入 checkpoint（thread `v1:config-session` 内保存 state `session_id=state-session, graph_version=v2`），且 `close_postgres_checkpointer` 调用后 saver 仍可 `setup()`；静态审查发现子进程 argv/错误输出凭据泄漏风险，登记 AR-B-005 |
| 2026-07-10 | L1-3 | 1 | 验收未通过 | L1-3 专项 `33 passed`、L1-2/L1-1/L0 回归 `187 passed`，ruff、Python 3.11 mypy、lock、diff check 通过；未跑全量门禁。资源生命周期和 DB_URL argv 已修复，但诊断再次证明直接 `graph.ainvoke` 可绕过 `validated_ainvoke` 并写入错配 state；使用伪造 DB_URL 触发真实子进程连接失败时，stdout 暴露目标 IP、端口和底层连接文本。登记 AR-B-006，AR-B-005 未关闭 |
| 2026-07-10 | L1-3 | 2 | 验收未通过 | 独立诊断确认公开 `graph.ainvoke` 错配拒绝、子进程固定错误码输出；L1-3 专项 `33 passed`；L1-2/L1-1/L0 回归 `187 passed`；定向可写临时目录全量 `1043 passed, 1 xfailed, 2 warnings`；mypy（含 Python 3.11）、lock、diff check 通过，但 Ruff 因 `checkpoint.py:26` 未使用 `Sequence` 失败，登记 AR-B-007 |
| 2026-07-10 | L1-3 | 3 | 验收通过 | 删除未使用 `Sequence` 后 Ruff 通过；L1-3 专项 `33 passed`；L1-2/L1-1/L0 合并回归 `220 passed`；全量后端 `1043 passed, 1 xfailed, 2 warnings`；mypy 默认与显式 Python 3.11、uv lock、diff check 均通过；AR-B-005/006/007 关闭 |
| 2026-07-10 | L1-4 | 0 | 验收未通过 | L1-4 专项 `32 passed`，但独立诊断输入 `api_key=demo-secret-value`、`prompt=patient name demo-user`、`authorization: Bearer demo-token` 均可经 `_sanitize_runner_error` 原样进入 `GraphRunnerError`。违反错误、事件和日志不得泄露密钥、prompt 或患者身份的隐私门禁；登记 AR-B-008，未继续执行全量门禁 |
| 2026-07-10 | L1-4 | 1 | 验收通过 | AR-B-008 已关闭：Runner 对外错误仅保留固定文本和错误码，异常链不保留底层文本；L1-4 专项 `34 passed`；L1-3/L1-2/L1-1/L0 合并回归 `254 passed`；隔离临时目录全量后端 `1077 passed, 1 xfailed, 2 warnings`；Ruff、mypy（默认及 Python 3.11）、uv lock、diff check 通过 |
| 2026-07-10 | L1 | 阶段门禁 | 验收通过 | L1-1～L1-4 全部完成且 AR-B-003～008 均关闭；复核 L1-4 专项 `34 passed`；隔离临时目录全量后端 `1077 passed, 1 xfailed, 3 warnings`；Ruff、mypy（默认及 Python 3.11）、uv lock、git diff check 通过；保持 `AGENT_RUNTIME_VERSION=legacy` |
| 2026-07-10 | L2-1 | 0 | 验收未通过 | 专项 `5 passed`、models/migrations 回归 `59 passed`、Ruff、Python 3.11 mypy、uv lock、diff check 通过；独立真实 PostgreSQL `0001 → 0002 → 0001 → 0002` 迁移循环通过。静态契约审查发现迁移未实现 ORM 的 FK ondelete、Safety Schema/DB 值域不一致、artifact revision 缺少单 current 与同 artifact 父链约束，登记 AR-B-009；未继续全量门禁 |
| 2026-07-10 | L2-1 | 1 | 验收通过 | AR-B-009 关闭：真实 PostgreSQL 专项 `8 passed`，覆盖 `0001 → 0002 → 0001 → 0002`、FK action、安全值域、单 current、同 artifact/session 前序父链；额外 session 级联删除诊断通过；models/migrations `59 passed`；全量后端 `1085 passed, 1 xfailed, 6 warnings`；Ruff、mypy（默认及 Python 3.11）、uv lock、diff check 通过 |

| 2026-07-10 | L2-2 | 3 | 验收通过 | 专项 `23 passed`；Gateway/BaseAgent 回归 `36 passed`；全量 `1108 passed, 1 xfailed, 6 warnings`；Ruff、mypy、lock、diff check 通过；AR-B-010～012 关闭 |
| 2026-07-10 | L2-3 | 1 | 验收通过 | 专项 `9 passed`；L2-2 回归 `23 passed`；全量 `1117 passed, 1 xfailed, 6 warnings`；Ruff、mypy、lock、diff check 通过；AR-B-013 关闭 |
| 2026-07-11 | L2-4 | 2 | 验收通过 | 专项 `16 passed`；L2-1～L2-3 回归 `40 passed, 4 warnings`；全量 `1133 passed, 1 xfailed, 6 warnings`；Ruff、mypy（91 files）、lock、diff check 通过；AR-B-014/015 关闭 |
| 2026-07-11 | L2-5 | 0 | 验收通过 | 真实 PostgreSQL 专项 `12 passed, 4 warnings`；L2-1～L2-5 合并回归 `68 passed, 8 warnings`；全量 `1145 passed, 1 xfailed, 10 warnings`；Ruff、mypy（93 files）、lock、diff check 通过；确认原子事务、数据库级幂等、并发版本冲突、Outbox claim/lease/ack/retry、故障回滚与隐私边界，无新增阻塞 |
| 2026-07-11 | L2 | 阶段门禁 | 验收通过 | L2-1～L2-5 全部完成且无打开 P0/P1 阻塞；真实 PostgreSQL L2 合并回归 `68 passed, 8 warnings`；全量 `1145 passed, 1 xfailed, 10 warnings`；Ruff、mypy（93 files）、uv lock、git diff check 通过；保持 `AGENT_RUNTIME_VERSION=legacy` |
| 2026-07-11 | L3-1 | 0 | 验收未通过 | 专项 `25 passed`；`ruff check .`、`mypy app`（96 files）、`uv lock --check`、`git diff --check` 通过。独立诊断以 `model_construct` 构造 assistant-only `IntakeExtractionInput`，公开入口实际调用 gateway 1 次并发送 assistant 文本，随后才返回 `INTAKE_SOURCE_NOT_ALLOWED`；登记 AR-B-016，未继续跑全量门禁 |
| 2026-07-11 | L3-1 | 1 | 验收未通过 | AR-B-016 修复确认：专项 `28 passed`，原 assistant-only 诊断返回 `INTAKE_INPUT_SCHEMA_INVALID` 且 gateway `0` 次；L2 合并回归 `68 passed, 8 warnings`，Legacy 兼容回归 `78 passed`，全量后端 `1173 passed, 1 xfailed, 10 warnings`；Ruff、mypy（96 files）、lock、diff check 通过。独立诊断仍证明已构造输出可以字符串 decision/隐藏 `.route` 通过并成功返回，三类身份候选也均 `passed=True`；关闭 AR-B-016，登记 AR-B-017/018 |
| 2026-07-11 | L3-1 | 2 | 验收未通过 | 专项 `35 passed`；Ruff、mypy（96 files）、lock、diff check 通过。原 constructed decision、隐藏 `route`、`patient.full_name`、嵌套 `patient_name`和格式化手机号诊断已固定拒绝，AR-B-017 关闭；但独立诊断证明 `patient.id_card`、`patient.identity_card`、`patient.national_id`、`patient.outpatient_no`、`patient.medical_record_no` 和嵌套 `patient.id_card` 均 succeeded，AR-B-018 未关闭，登记 AR-B-019，未继续跑全量门禁 |
| 2026-07-11 | L3-1 | 3 | 验收未通过 | 专项 `41 passed`；Ruff、mypy（96 files）、lock、diff check 通过。AR-B-019 指定的五个命名空间复合键与嵌套 key 均已拒绝，AR-B-019 关闭；但独立诊断证明 `patient.idcard`、`patient.identitycard`、`patient.nationalid`、`patient.outpatientno`、`patient.medicalrecordno` 均 succeeded，AR-B-018 未关闭，登记 AR-B-020，未继续跑全量门禁 |
| 2026-07-11 | L3-1 | 4 | 验收通过 | 专项 `47 passed`；独立诊断确认 `idcard`/`identitycard`/`nationalid`/`outpatientno`/`medicalrecordno` 紧凑别名及合法分隔变体均固定拒绝；L2 合并回归 `68 passed, 8 warnings`，Legacy 兼容回归 `78 passed`，全量后端 `1192 passed, 1 xfailed, 10 warnings`；Ruff、mypy（96 files）、lock、diff check 全部通过；关闭 AR-B-018/020，L3-1 完成 |
| 2026-07-11 | L3-2 | 0 | 验收未通过 | 专项 `19 passed`；Ruff、mypy（98 files）、lock、diff check 通过。独立诊断证明 emergency referral 结果的 `gate_result.decision` 可原地改为 `PASSED`、`details.disposition` 可改为 `continue`，且导出规则 dict 可将呼吸困难降级为 manual review；登记 AR-B-021，未继续跑全量门禁 |
| 2026-07-11 | L3-2 | 1 | 验收未通过 | 专项 `22 passed`，定向 Ruff 与 mypy 通过；权威结果 DTO、嵌套 details/rules/source refs 及公开规则映射的直接篡改已被拒绝。但独立子进程诊断通过模块保留的 `_TRIAGE_RED_FLAG_RULES` backing dict 替换呼吸困难规则后，evaluate 结果由 `emergency_referral` 降级为 `manual_review`；AR-B-021 未关闭，登记 AR-B-022，未继续跑全量门禁 |
| 2026-07-11 | L3-2 | 2 | 验收通过 | L3-1/L3-2 合并回归 `70 passed`；独立子进程确认模块内无包含 `TriageRule` 的可变 dict，低置信度呼吸困难仍为 `emergency_referral/BLOCKED`；全量后端 `1215 passed, 1 xfailed, 10 warnings`；Ruff、mypy（98 files）、lock、diff check 全部通过；关闭 AR-B-021/022，L3-2 完成 |
| 2026-07-11 | L3-3 | 0 | 验收未通过 | 专项 `27 passed`、L3-1～L3-3 合并回归 `97 passed`，定向 Ruff/mypy、lock、diff check 通过；独立诊断确认无症状仅类别即可 `ready`、类别与真实症状并存反而 `conflict`、缺绝经状态的 30 岁女性提前判 applicable、携带 candidate_count 的伪造 continue/PASSED Triage Gate 仍进入 `ready`；登记 AR-B-023，未继续跑全量门禁 |
| 2026-07-11 | L3-3 | 1 | 验收未通过 | 专项 `38 passed`、L3-1～L3-3 合并回归 `108 passed`，定向 Ruff/mypy、lock、diff check 通过；原四项诊断均已修复，但独立诊断构造两个不同的当前 `chief_complaint.category` 后仍得到 `ready` 且无 conflict，证明辅助分类 fact 逃逸同 canonical key 冲突规则；AR-B-023 保持打开，未继续跑全量门禁 |
| 2026-07-11 | L3-3 | 2 | 验收通过 | 专项 `43 passed`、L3-1～L3-3 合并回归 `113 passed`；独立诊断确认 category 异值为稳定 `conflict/FAILED`、乱序一致、同值重复不冲突且输出不含 category/value fingerprint，原症状/互补字段/menopause/Triage 四项修复保持；全量后端 `1258 passed, 1 xfailed, 10 warnings`；Ruff、mypy（100 files）、lock、diff check 全部通过；关闭 AR-B-023，L3-3 完成 |
| 2026-07-11 | L3-4 | 0 | 验收未通过 | 专项 `23 passed`、L3-1～L3-4 合并回归 `136 passed`，定向 Ruff/mypy、lock、diff check 通过；独立诊断确认伪造 ready-selected 仍成功生成问题、注入替代优先级可改变唯一缺口、错配模板可为症状缺口生成睡眠问题、state version 7 的 selection 可用 version 106 的 RunSpec 成功调用模型，英文 phone/full name/ID 问句均通过验证；登记 AR-B-024，未继续跑全量门禁 |
| 2026-07-11 | L3-4 | 1 | 验收未通过 | 专项 `45 passed`、L3-1～L3-4 合并回归 `158 passed`，定向 Ruff/mypy、lock、diff check 通过；原五项诊断均已修复，但独立诊断确认权威 selection 的 `model_copy(update={'route':'ready','force':true})` 被静默接受并成功生成问题，且高温度、20 万 token、24 小时 timeout、空 verifier chain 的 AgentSpec 仍通过 fallback 前置检查并发起 1 次模型请求；AR-B-024 保持打开，未继续跑全量门禁 |
| 2026-07-11 | L3-4 | 2 | 验收通过 | 专项 `55 passed`、L3-1～L3-4 合并回归 `168 passed`；独立诊断确认 supplied selection 隐藏 `route/force` 固定拒绝、宽松 AgentSpec 在 gateway 前拒绝且请求数 0、ready 路径无问题、生产注册表不可公开替换及英文身份请求保持拒绝；全量后端 `1313 passed, 1 xfailed, 10 warnings`；Ruff、mypy（103 files）、lock、diff check 全部通过；关闭 AR-B-024，L3-4 完成 |
| 2026-07-11 | L3-5 | 0 | 验收未通过 | L3-1～L3-5 合并回归 `170 passed`；Messages/Advance/Session API 回归 `58 passed`；真实 PostgreSQL migration、Repository、checkpoint 与 runner 回归 `87 passed, 8 warnings`；Ruff、lock、diff check 通过，但 `mypy app` 报 6 errors。静态审查确认消息/领域分段 commit、事务内模型调用、缺少 DomainCommandCommit/Outbox、`/advance` 不消费 Completeness Gate、ContextVar 单节点不可跨进程恢复且专项仅 2 个弱测试；登记 AR-B-025，未继续跑全量后端 |
| 2026-07-12 | L3-5 | 3 | 验收通过 | 专项 `15 passed`；L3-1～L3-5 合并回归 `183 passed, 4 warnings`；全量后端 `1328 passed, 1 xfailed, 14 warnings`；Ruff、mypy（108 files）、lock、diff check 全部通过。真实节点图、条件路由、Repository/Outbox 原子提交、数据库 claim 幂等、当前 Gate 推进、并发单调用、commit 后恢复和 checkpoint/Outbox 隐私负向检查均通过；关闭 AR-B-025，L3 完成 |
| 2026-07-12 | L4-1 | 0 | 验收未通过 | 专项 `13 passed`，定向 Ruff、mypy（111 files）、lock、diff check 通过；但独立诊断确认明显不完整 Domain snapshot 配调用方自造的当前版本 `ready/PASSED` Gate 仍 `succeeded` 且模型调用 1 次，同 observation ID 下篡改 fact 内容后篡改值进入模型并 `succeeded`。现有测试只覆盖错误 policy version/过期 Gate，且 inactive/superseded 用允许 `RUN_PROVENANCE_MISMATCH` 的弱断言掩盖 fact-link 验证；登记 AR-B-026，未继续跑合并与全量后端门禁 |
| 2026-07-12 | L4-1 | 4 | 验收通过 | L4-1、Repository、Advance 专项 `55 passed, 4 warnings`；L3-1～L4-1 合并回归 `205 passed, 4 warnings`；Repository/checkpoint/API 回归 `130 passed, 4 warnings`；全量后端 `1365 passed, 1 xfailed, 14 warnings`；Ruff、mypy（111 files）、lock、diff check 全部通过。独立审查确认 `/advance` 后当前 Domain State `V+1` 与来源 Gate `V` 分离绑定，推进前 inquiry、错误 stage/status/runtime/recovery、伪 Gate/State/Context、跨 run、重复或非 completed Gate 均在 gateway 前拒绝；关闭 AR-B-026，L4-1 完成 |
| 2026-07-12 | L4-2 | 0 | 验收未通过 | 专项 `34 passed`，Ruff、mypy（114 files）、lock、diff check 通过；范围审查未发现提前实现 L4-3～L5。独立对抗诊断构造结构合法、引用全部真实 active fact IDs、但证型/治法/basis 文本由调用方凭空编造的 completed SyndromeDraft，公开 `execute_formula_draft()` 仍 `succeeded`、gateway 调用 1 次，伪造证型进入模型 Context。现有测试把“伪造报告”缩减为未知 fact ID 场景，未验证上游 L4-1 产物来源；登记 AR-B-027，未继续跑合并与全量后端门禁 |
| 2026-07-12 | L4-2 | 1 | 验收未通过 | 独立对抗诊断同时伪造结构合法且引用真实 active facts 的 SyndromeDraft、与其自洽的 L4-1 RunSpec/RunArtifact，公开 Formula 入口仍 `succeeded`、gateway 调用 1 次且伪造证型进入 Context，证明调用方三件套重验/digest 并未建立可信来源。专项 `8 failed, 32 passed`：non-completed、缺 treatment principle、伪 State/context、同 ID 篡改、PII 和缺 artifact 等回归失败；其中 helper 对显式 `None` artifact 自动补默认 artifact，使缺 artifact 路径实际成功。交接文件仍是第 0 轮内容且未更新。AR-B-027 保持打开，未运行后续门禁 |
| 2026-07-12 | L4-2 | 2 | 验收未通过 | 专项 `40 passed`，Ruff、mypy（114 files）、lock、diff check 通过，前轮 8 个测试失败和 None sentinel 已修复，交接文件已更新；但独立诊断直接导入 `_TrustedSyndromeExecution`，为手工构造且 canonical report=passed 的伪造 SyndromeExecutionResult 赋值 `_trusted_execution` 后，公开 Formula 入口仍 `succeeded`、gateway 调用 1 次且伪造证型进入 Context。PrivateAttr/下划线命名不是安全边界；AR-B-027 保持打开，未继续跑合并与全量后端门禁 |
| 2026-07-12 | L4-2 | 3 | 验收通过 | 独立复现第 2 轮手工 canonical passed report、内部 DTO 与伪私有字段攻击，现固定返回 `FORMULA_SYNDROME_DRAFT_INVALID` 且 gateway 0 次；专项 `42 passed`，L4-1/L4-2/Repository/Advance 合并回归 `97 passed, 4 warnings`，全量后端 `1407 passed, 1 xfailed, 14 warnings`；Ruff、mypy（114 files）、lock、diff check 全部通过。身份注册只接受真实 L4-1 成功结果具体实例，复制/手工对象与裸 Artifact/RunSpec 均拒绝；关闭 AR-B-027，L4-2 完成 |
| 2026-07-12 | L4-3 | 0 | 验收未通过 | 专项 `41 passed`，Ruff、mypy（115 files）、lock、diff check 通过；确定性动作、Decimal 单位换算、候选重建、无 RAG 和可信 Formula 身份边界已落地，范围未扩展到 L4-4/L5。但独立诊断确认 `herb="甘\n草"` 的换行先被 `\s+` 折叠，最终一致性报告 `passed=True`，可逃逸药名别名和重复检测；登记 AR-B-028，未继续跑合并与全量后端门禁 |
| 2026-07-12 | L4-3 | 1 | 验收通过 | 独立复测 `甘草 + 甘\n草` 已固定返回 `SCHEMA_INVALID`、`passed=false`、`requires_human=true`；专项 `67 passed`，L4/Repository/Advance 合并回归 `164 passed, 4 warnings`，Safety/Legacy `176 passed`，全量后端 `1474 passed, 1 xfailed, 14 warnings`；Ruff、mypy（115 files）、lock、diff check 全部通过。原始/NFKC `Cc/Cf/Cs` 在空白折叠前统一拒绝，药名、单位及 canonical 文本回归覆盖成立；关闭 AR-B-028，L4-3 完成 |
| 2026-07-12 | L4-4 | 0 | 验收未通过 | PostgreSQL 专项 `6 passed, 3 warnings`，L1/L2/L3/L4/Advance 组合回归 `296 passed, 11 warnings`，Ruff、mypy（118 files）、lock、diff check 通过；真实子图、artifact payload、回问失效和 ready-for-safety 基础路径已落地，未提前执行 Safety。但独立对抗直接调用 `_restore_trusted_syndrome_execution()`，用手工 RunSpec/Artifact/Input/Gate 获得 `succeeded` 且命中 Formula trusted consumer，证明恢复未绑定 Repository revision/digest 并回归 AR-B-027 类权威铸造；登记 AR-B-029。专项仅 6 项，亦未覆盖发布要求中的完整 decision/verifier、幂等并发与版本冲突矩阵；未继续跑 Safety/Legacy 和全量门禁 |
| 2026-07-12 | L4-4 | 1 | 验收未通过 | 旧原始 DTO restore 已删除，分支/恢复/并发回归扩充至 PostgreSQL 专项 `18 passed, 3 warnings`；Ruff、mypy（118 files）、lock、diff check 通过。但独立对抗以无数据库的 `ForgedRepository(PostgresDomainRepository)` 覆写三个读取方法，`isinstance` 校验仍通过，手工 record/authority 再次得到 `restored=True`、`trusted_capability_granted=True`。AR-B-029 保持打开；一次并行测试批次因 120 秒超时无可靠汇总，确认 P1 后未运行组合、Safety/Legacy 和全量门禁 |
| 2026-07-12 | L4-4 | 2 | 验收未通过 | AR-B-029 历史原始 DTO 与伪 Repository 注入攻击已固定拒绝，PostgreSQL 专项增至 `21 passed, 3 warnings`，Ruff、mypy（118 files）、lock、diff check 通过，可关闭 AR-B-029。但源码和独立诊断确认持久化 helper 对实际 `mimo-v2.5` 运行统一写入 `model_actual=fake-model`，attempts/latency 亦为固定值，恢复重验使用伪 provenance 自洽通过；登记 AR-B-030。确认 P1 后未运行组合、Safety/Legacy 和全量门禁 |
| 2026-07-12 | L4-4 | 3 | 验收通过 | 独立重放原始 DTO 与伪 Repository 注入均固定拒绝；Syndrome/Formula payload 精确来自 closure trusted RunArtifact，非默认模型及 model/attempts/latency/usage/evidence/trace/run/spec/prompt/output 篡改回归成立。PostgreSQL 专项 `26 passed, 3 warnings`，L1～L4/Repository/Advance 组合 `316 passed, 11 warnings`，Safety/Legacy `176 passed`，全量后端 `1500 passed, 1 xfailed, 17 warnings`；Ruff、mypy（118 files）、lock、diff check 全部通过。关闭 AR-B-030，L4-4 完成并关闭 L4 |

## 7. 最近更新

| 日期 | 更新人 | 内容 |
|---|---|---|
| 2026-07-12 | Codex | L4-4 第 3 轮复验通过并关闭 L4：Syndrome/Formula 产物只从真实 closure trusted execution 持久化完整 RunArtifact provenance，恢复/replay 对所有 provenance 字段、payload digest、当前 authority 和 exact revision 精确校验；AR-B-029/030 对抗、全部 decision 分支、回问失效、revision、并发重放、state conflict 与 PostgreSQL restart 均通过。专项 26、组合 316、Safety/Legacy 176、全量 1500 passed，静态门禁全部通过；L4 完成度 100%，下一可提交 L4-4 并发布 L5-1。 |
| 2026-07-12 | Codex | L4-4 第 2 轮复验仍未通过：恢复入口已取消 Repository/factory/record 注入，前两轮 capability 铸造攻击均拒绝，关闭 AR-B-029；但 Syndrome/Formula artifact payload 丢弃真实 AgentRuntime RunArtifact，将模型、attempts、latency 固定伪造为 `fake-model/1/0`。当前实际模型 `mimo-v2.5` 与持久化 provenance 明确不一致；登记 AR-B-030，下一轮仅改为从真实闭包 trusted execution 持久化并精确恢复 provenance。 |
| 2026-07-12 | Codex | L4-4 第 1 轮复验仍未通过：旧 `_restore_trusted_syndrome_execution` 已移除，专项增至 18 项并覆盖主要 decision、恢复、并发和版本冲突；但新恢复函数仍接收调用方 Repository，`isinstance` 无法阻止伪造的 `PostgresDomainRepository` 子类覆写权威读取。独立攻击无需数据库即再次铸造 trusted Syndrome。AR-B-029 继续打开；下一轮仅取消 Repository/factory/record 注入并将真实持久化读取封入恢复边界。 |
| 2026-07-12 | Codex | L4-4 第 0 轮验收未通过并发布限定返工：真实 ReasoningSubgraph、持久化 artifact payload、回问失效及 ready-for-safety 基础链路通过现有测试，但跨进程恢复新增的 `_restore_trusted_syndrome_execution()` 可被任意调用方用手工自洽 DTO 直接铸造 trusted Syndrome，绕过 Repository revision/digest 与真实 L4-1 执行来源。登记 AR-B-029；返工只收口持久化恢复 attestation 与缺失的分支/幂等/恢复回归，不得扩展 L5、RAG、Review、Record 或 UI。 |
| 2026-07-12 | Codex | 提交已验收的 L4-3（`cbed449`）；发布 L4-4 ReasoningSubgraph、Revision 与回问闭环。L4-4 用真实版本化子图替换 `reasoning_placeholder`，串联权威 precheck、L4-1 Syndrome、L4-2 Formula 与 L4-3 consistency；确定性处理 completed/needs_more_info/abstained，持久化 syndrome/formula revision，并在回问时将下游产物标为 stale。只允许输出 ready-for-safety 边界，不得执行或伪造 L5 Safety/HITL、RAG、Legacy Prescription/Modification 或 UI。 |
| 2026-07-12 | Codex | L4-3 第 1 轮复验通过：原始与 NFKC 文本均在空白折叠前拒绝 Unicode `Cc/Cf/Cs`，药名、单位和 canonical 文本共享确定性安全顺序；独立控制字符碰撞攻击已固定失败。专项 67、L4 合并 164、Safety/Legacy 176、全量 1474 passed，静态门禁全部通过；关闭 AR-B-028，L4 完成度 75%，下一可提交 L4-3 并发布 L4-4。 |
| 2026-07-12 | Codex | L4-3 第 0 轮验收未通过并发布限定返工：核心确定性重建、单位/动作语义、可信 L4-2 来源与无副作用边界已完成，专项和静态门禁通过；但药名控制字符在安全检查前被空白折叠，`甘\n草` 可作为合法未知药名通过并逃逸 duplicate/alias 检测。登记 AR-B-028，返工仅调整原始文本控制字符检查顺序及药名/单位/碰撞回归，不得扩展 L4-4 或 L5。 |
| 2026-07-12 | Codex | 提交已验收的 L4-2（`3e677f1`）；发布 L4-3 FormulaConsistencyVerifier 与无 RAG 模式。L4-3 只新增纯确定性、无模型调用的方药一致性验证：规范化药味/单位、按 base + modifications 重算 candidate、校验 action 与 composition 对应、剂量及 fact/证型依据，并再次固定 `model_knowledge_only`/医师复核边界；不得接 L4-4 ReasoningSubgraph/revision/回问、Safety/HITL、RAG、DB 或 API/UI。 |
| 2026-07-12 | Codex | L4-2 第 3 轮复验通过：L4-1 成功结果改由闭包内弱引用身份注册表登记，Formula 仅消费真实执行返回的具体对象；手工 passed report、内部 DTO、伪 PrivateAttr、裸 Artifact/RunSpec 和复制对象均固定拒绝且零模型调用。专项 42、合并 97、全量 1407 passed，静态门禁全部通过；关闭 AR-B-027，L4 完成度 50%，下一可发布 L4-3。 |
| 2026-07-12 | Codex | L4-2 第 2 轮复验仍未通过：专项恢复 40 passed，静态门禁通过且交接已同步，但进程内“密封”仅依赖可导入 `_TrustedSyndromeExecution` 和可赋值 Pydantic PrivateAttr。独立对抗可手工铸造 passed result/private payload 并再次把伪造证型送入 Formula 模型。AR-B-027 继续打开；下一轮必须使用只有真实 L4-1 执行路径才能登记/解析的对象身份注册或受信存储引用，不得把 DTO 私有字段当 capability。 |
| 2026-07-12 | Codex | L4-2 第 1 轮复验仍未通过：新增 L4-1 RunArtifact/RunSpec 重验与 digest，但这些输入仍可由调用方成套伪造，不构成可信产物来源；独立诊断再次成功把伪造证型送入 Formula 模型。专项同时出现 8 failures，缺 artifact 测试的 helper 会把显式 None 替换为默认 artifact，交接文件也未同步。AR-B-027 继续打开，返工必须改为受信执行边界或受信存储引用，并先修复专项回归。 |
| 2026-07-12 | Codex | L4-2 第 0 轮验收未通过并发布限定返工：Formula Schema、单次 base/modifications/candidate 输出、无 RAG/只读/隐私边界和专项测试已落地，但 Formula 入口只校验调用方 SyndromeDraft 的形状与 fact ID 集合，未绑定 L4-1 已验证 RunArtifact/产物引用；任意伪造证型和治法只要引用真实 active facts 即可进入模型。登记 AR-B-027，返工仅收口上游 Syndrome 产物来源与对应零调用回归，不得扩展 L4-3～L5。 |
| 2026-07-12 | Codex | 提交已验收的 L4-1（`73ba735`）；发布 L4-2 FormulaDraftAgent 合并基础方与加减。L4-2 只新增强类型 Formula Draft、固定 AgentSpec/RunSpec、以前置已验证 completed Syndrome Draft 为输入，并在一次模型输出中同时给出 base formula、modifications 与 candidate formula；保持 `model_knowledge_only`、`review_required=true`、只读无路由权，不得提前实现 L4-3 确定性一致性 Verifier、L4-4 ReasoningSubgraph/revision/回问、Safety/HITL、RAG 或切流。 |
| 2026-07-12 | Codex | L4-1 第 4 轮复验通过：Repository authority 在同一读取边界锁定持久化会话阶段、当前 Domain State 与 `/advance` 来源 Gate，正确区分当前 `V+1` 和 Gate `V`；Syndrome Context 仅由权威 active observations 构建，缺 authority 与所有伪造/跨 run/重复 Gate 路径固定零模型调用。专项、合并、受影响回归、全量后端和静态门禁全部通过，关闭 AR-B-026；L4 完成度 25%，下一可发布 L4-2。 |
| 2026-07-12 | Codex | L4-1 第 0 轮验收未通过并发布限定返工：强类型草案、无 RAG 契约和基础 Verifier 已落地，但模型前 Gate 与 Context 仍是调用方可伪造的第二输入源；当前版本伪 ready 可让不完整快照进入模型，同 observation ID 的事实内容也可被替换。登记 AR-B-026，返工仅收口 Gate 来源/重算、Context 精确投影和对应零调用回归，不得扩展到 L4-2～L4-4。 |
| 2026-07-12 | Codex | 提交已验收的 L3-5 并关闭 L3；发布 L4-1 SyndromeDraftAgent 与 SyndromeVerifier。L4-1 只实现强类型 Syndrome 草案、固定 AgentSpec/RunSpec、READY_FOR_REASONING 前置检查与确定性 Verifier；明确无 RAG `model_knowledge_only`、置信度上限和医师复核要求，不得提前实现 Formula、ReasoningSubgraph、Safety/HITL 或 RAG。 |
| 2026-07-12 | Codex | L3-5 第 3 轮复验通过：真实 Intake 节点图和条件边、Repository/Outbox 原子幂等、持久化 Gate 推进、并发单调用、commit 后恢复及中间载荷隐私边界均成立；专项、L3 合并回归、全量后端和全部静态门禁通过，关闭 AR-B-025，L3 阶段完成。 |
| 2026-07-11 | Codex | L3-5 第 0 轮验收未通过并发布限定返工：现实现绕过 L2 Repository/Reducer 的原子幂等与 Outbox，模型运行于持锁事务，`/advance` 不消费持久化 Completeness Gate，ContextVar 单节点无法跨进程恢复，且缺少真实 E2E/幂等/并发/恢复测试并有 6 个 mypy 错误；登记 AR-B-025。返工仅收口 L3-5 集成，不得扩展到 L4～L9。 |
| 2026-07-11 | Codex | 提交 L3-4（`5a4b117`）并发布 L3-5：将 L3-1～L3-4 集成为 IntakeSubgraph，替换 MainGraph 的 intake 占位路径；为创建时固化为 LangGraph 的会话接入 `/messages` 与 `/advance`，完成 Domain State/Gate/Question/Outbox、command 幂等、并发版本、checkpoint 恢复和无 RAG 问诊 E2E。Legacy 会话保持原路径，禁止运行时混跑、静默降级、Legacy Sufficiency 双调用及提前实现 L4～L9。 |
| 2026-07-11 | Codex | L3-4 第 2 轮复验通过：supplied selection 增加递归未声明/授权字段拒绝，Question Composer 的 model policy、verifier chain、failure policy 与只读权限锁定为单一固定契约；原五项返工、独立诊断、L3 合并回归、全量后端和静态门禁全部通过，关闭 AR-B-024，L3-4 完成，下一可发布 L3-5 |
| 2026-07-11 | Codex | L3-4 第 1 轮复验仍未通过：Completeness→Gap 重算、生产注册表收口、模板绑定、RunSpec state/version 和中英文身份语义五项原问题已修复；但 supplied selection 隐藏 authority 字段仍被 serializer 丢弃后接受，AgentSpec 也未固定低温/短输出/短超时及 verifier chain。AR-B-024 保持打开，第二轮仅收口这两项及 gateway 前对抗回归 |
| 2026-07-11 | Codex | L3-4 第 0 轮验收未通过：专项、L3 合并回归与静态门禁通过，但 GapSelection 可伪造、生产优先级可注入替换、模板与 selected dimension 未绑定、fallback RunSpec state version 未绑定，且英文身份索取绕过单问验证；登记 AR-B-024，限定返工于权威绑定、注册表收口、RunSpec 一致性和中英文身份隐私回归，不得扩展到 L3-5 |
| 2026-07-11 | Codex | 提交 L3-3（`4c00ea7`）并发布 L3-4：实现纯确定性、版本化 GapSelector，只消费经过 canonical 重验且内部一致的 Completeness 权威结果，在 incomplete/conflict 路径按不可变优先级选择唯一缺口；Question Composer 对已选缺口模板优先，模板缺失时才允许通过 L2 AgentRuntime 使用受限只读模型兜底，输出必须是单一、无身份信息索取且不可携带路由/充分性权威的问题。不得让模型自选缺口、追加第二问或改变 Triage/Completeness，不接入 Graph、阶段迁移、API、Repository/DB/Outbox，也不实现 L3-5 |
| 2026-07-11 | Codex | L3-3 第 2 轮复验通过：辅助策略事实纳入完整当前事实集冲突检测，`chief_complaint.category` 使用独立辅助维度审计，异值乱序稳定 conflict、同值重复幂等且不泄露原值；原 AR-B-023 四项修复、L3 合并回归、全量后端和静态门禁全部通过，关闭 AR-B-023，L3-3 完成，下一可发布 L3-4 |
| 2026-07-11 | Codex | L3-3 第 1 轮复验仍未通过：主诉覆盖/互补字段、menopause unknown 与 Triage 内部一致性四项原问题已修复；但两个冲突的当前 `chief_complaint.category` 不进入任何维度分组，动态十问类别仍由排序结果静默决定并可 `ready`。AR-B-023 保持打开，第二轮返工仅补齐策略辅助 fact 的同 key 冲突检测与回归 |
| 2026-07-11 | Codex | L3-3 第 0 轮验收未通过：专项与静态门禁通过，但主诉覆盖、聚合维度冲突、妊娠/哺乳适用性和 Triage 前置权威存在四项确定性绕过/误判；登记 AR-B-023，限定返工于语义分离、冲突键收口、unknown 适用性和 Triage continue 内部一致性重验及其对抗回归，不得扩展到 L3-4/L3-5 |
| 2026-07-11 | Codex | 提交 L3-2（`d0c25aa`）并发布 L3-3：实现纯确定性、版本化 CompletenessPolicy，只消费已验证的 Domain State 结构化事实；定义必需/可选问诊维度、主诉相关动态门槛、安全信息三态、性别/年龄/绝经适用性，以及连续无新增事实和最大补问轮次的停滞判定；输出可审计 GateResult、缺失/冲突维度与人工接管信号。不得调用模型或 Legacy SufficiencyAgent，不生成/选择下一问，不允许 `force`、模型或外部输入改写 Gate，不接入 Graph、阶段迁移、API、Repository/DB/Outbox，也不实现 L3-4～L3-5 |
| 2026-07-11 | Codex | L3-2 第 2 轮复验通过：规则注册表改为 tuple 支撑的冻结 Mapping，模块内不再保留可变 backing dict；深度冻结、篡改对抗、L3-1/L3-2 回归、全量后端及全部静态门禁通过，关闭 AR-B-021/022，L3-2 完成，下一可发布 L3-3 |
| 2026-07-11 | Codex | L3-2 第 1 轮复验仍未通过：22 个专项用例及定向 Ruff/mypy 通过，权威结果的深度冻结已生效；但 `MappingProxyType` 仍共享模块内可变 backing dict，可从模块属性替换高危规则并改变权威决策，登记 AR-B-022，限定第 2 轮返工于彻底移除可变权威引用及对应对抗回归 |
| 2026-07-11 | Codex | L3-2 第 0 轮验收未通过：确定性映射、红旗阻断、去重/顺序无关和隐私专项均通过，但权威 GateResult/details 与导出规则表均可在返回/导入后篡改，可把 BLOCKED 改为 PASSED 或降级 emergency 规则；登记 AR-B-021，限定返工于深度不可变契约和回归 |
| 2026-07-11 | Codex | 提交 L3-1（`b3b182b`）并发布 L3-2：实现纯确定性、版本化 TriagePolicy，仅消费已验证的 red-flag candidates，生成权威 GateResult 与 continue/emergency referral/manual review 处置；任何红旗候选都不得自动放行，高危类别不得被模型 severity/confidence 降级；不实现 Graph/interrupt、阶段迁移、API、Repository 或 L3-3～L3-5 |
| 2026-07-11 | Codex | L3-1 第 4 轮复验通过：复合身份别名改为单一 canonical 语义集与紧凑连续 token 后缀匹配，紧凑/下划线/命名空间/嵌套样例与 AR-B-016～020 回归全部通过；专项、L2/Legacy 回归、全量后端和静态门禁均通过，关闭 AR-B-018/020，L3-1 完成，下一可发布 L3-2 |
| 2026-07-11 | Codex | L3-1 第 3 轮复验仍未通过：指定的命名空间复合身份键后缀已修复，关闭 AR-B-019；但无下划线复合别名策略不一致，`idcard`/`identitycard`/`nationalid`/`outpatientno`/`medicalrecordno` 仍可作为合法 fact key 输出，登记 AR-B-020，限定第 4 轮返工于别名规范化与回归 |
| 2026-07-11 | Codex | L3-1 第 2 轮复验仍未通过：输出 canonical 重建、原隐藏越权与原三类身份样例已修复，关闭 AR-B-017；但身份键匹配对命名空间后的 `id_card`/`identity_card`/`national_id`/`outpatient_no`/`medical_record_no` 复合后缀失效，登记 AR-B-019，限定第 3 轮返工于该确定性匹配与回归 |
| 2026-07-11 | Codex | L3-1 第 1 轮复验仍未通过：AR-B-016 的 canonical 输入重验已修复并关闭，专项、L2/Legacy 回归、全量后端和静态门禁均通过；但已构造输出可绕过 Schema/decision/authority 并返回原对象，fact-key 别名、嵌套 key 和格式化手机号也可绕过身份 verifier；登记 AR-B-017/018，限定第 2 轮返工于 L3-1 输出验证边界 |
| 2026-07-11 | Codex | L3-1 第 0 轮验收未通过：专项和静态门禁通过，但公开入口对已构造 Intake DTO 跳过重验，assistant-only 消息会先发送给模型再被 verifier 拒绝；登记 AR-B-016，限定返工为输入边界不可绕过重验与负向回归，不扩展到 L3-2～L3-5 |
| 2026-07-11 | Codex | 提交 L2-5（`3d525b5`）并发布 L3-1：实现版本化严格 IntakeExtraction 输出契约、IntakeExtractionAgent/AgentSpec/Prompt 与抽取验证，输出仅限 observations、safety delta、red-flag candidates、ambiguities、extraction decision；必须复用 L2 Runtime/Context/Verifier 边界并使用 fake model 测试；不得实现 TriagePolicy、CompletenessPolicy、下一问、Subgraph/API、Repository 编排、Legacy 切换或 L3-2～L3-5 |
| 2026-07-11 | Codex | L2-5 验收通过并关闭 L2 阶段：真实 PostgreSQL migration 往返、事务原子性、幂等重放、并发冲突、Outbox 多 worker/lease/retry/recovery 和隐私专项通过；L2 合并回归、全量后端及静态门禁全部通过，无新增阻塞；L2-5 待提交，下一可发布 L3-1 |
| 2026-07-11 | Codex | L2-5 已交付待验收：新增真实 PostgreSQL Domain Repository、session 行锁与复合唯一键幂等、同事务 run/step/gate/outbox、SKIP LOCKED claim/lease/ack/retry，并覆盖并发、故障触发器回滚、重复请求、父 revision、进程恢复和隐私负向测试；未接入 Redis/SSE publisher、API 或业务 Agent |
| 2026-07-11 | Codex | 根据本地提交 `c2b3f9a`、`fd492b5`、交接文件和验收门禁恢复曾回退的 L2-2～L2-4 看板事实；发布 L2-5，限定为真实 PostgreSQL Domain Repository、乐观版本事务、命令幂等和同事务 Outbox，不接入 Redis/SSE 发布、API、业务 Agent 或 L3 |
| 2026-07-10 | Codex | 提交 L2-1 并发布 L2-2：定义版本化 AgentSpec/RunSpec/RunArtifact 等 Harness 协议，基于现有 ModelGatewayClient 实现受预算约束的 AgentRuntime，透传每 Agent 的 model/token/timeout/temperature 并注入最小 run/audit 记录；不得实现 ContextBuilder、Verifier/Reducer、Repository/outbox、业务 Agent、API 或切流 |
| 2026-07-10 | Codex | L2-1 第 1 轮复验通过：统一 Schema/ORM/migration 外键与安全值域，以 partial unique index 和复合父链 FK/CHECK 固化 artifact revision 约束；真实 PG 专项、额外级联诊断、全量后端及静态门禁通过，关闭 AR-B-009，下一可发布 L2-2 |
| 2026-07-10 | Codex | L2-1 第 0 轮验收未通过：专项、既有迁移回归、静态门禁及真实 PostgreSQL upgrade/downgrade 循环通过，但 ORM/migration 外键删除语义、Safety Schema/DB 值域和 artifact revision 链约束不一致；登记 AR-B-009，限定返工不扩展到 L2-2～L2-5 |
| 2026-07-10 | Codex | L1 阶段关口通过并发布 L2-1：L1 四项任务全部完成、无开放 P0/P1/AR-B，专项、全量后端与静态门禁通过；L2-1 限定为 Observation/Safety/Artifact Schema、对应数据库迁移与测试，不实现 AgentRuntime、Reducer、Repository/outbox、业务 Agent、API 或切流 |
| 2026-07-10 | Codex | L1-4 第 1 轮复验通过：固定 Runner 对外错误并断开底层异常链，新增 API key、Bearer token、prompt、身份文本和 DB URL 的 ainvoke/stream 隐私回归；专项、合并回归、全量后端及静态门禁通过，关闭 AR-B-008，L1-4 完成 |
| 2026-07-10 | Codex | L1-4 第 0 轮验收未通过：专项 `32 passed`，但 Runner 错误归一化将任意 `str(exc)` 前缀带入对外错误，API key、Bearer token、prompt 和身份类文本均可泄露；登记 AR-B-008，限定返工为固定错误输出与隐私回归测试，不扩大到 API、业务 Agent 或 L2 |
| 2026-07-10 | Codex | 提交 L1-3 并发布 L1-4：实现 GraphRunner 的 `ainvoke/astream` 包装、总超时与取消语义、错误归一化、state 版本校验，以及 LangGraph 事件到版本化业务事件的转换；不得接入 API/SSE 路由、业务 Agent、领域 Schema、RAG 或 Legacy 改造 |
| 2026-07-10 | Codex | L1-3 第 3 轮复验通过：删除未使用 `Sequence` 导入，Ruff、L1-3 专项、合并回归、全量后端和静态门禁均通过；AR-B-005/006/007 关闭，L1-3 完成，下一任务 L1-4 |
| 2026-07-10 | Codex | L1-3 第 2 轮复验：不可绕过 checkpointer 校验、资源关闭和子进程错误脱敏均通过，定向全量 `1043 passed, 1 xfailed, 2 warnings`；仅 Ruff 报 `Sequence` 未使用，登记 AR-B-007，限定删除导入并复跑 Ruff |
| 2026-07-10 | Codex | L1-3 第 1 轮复验未通过：关闭语义和 DB_URL argv 已修复，但 `validated_ainvoke` 仍可被公开 `graph.ainvoke` 绕过，子进程仍返回原始连接异常详情；新增 AR-B-006，限定第二轮返工只收口不可绕过校验和失败输出脱敏 |
| 2026-07-10 | Codex | L1-3 第 0 轮验收未通过：PG setup、持久化、重建实例读取和真实跨进程 interrupt/resume 测试通过，但 config/state 校验未接入执行路径、公开关闭 helper 实际不关闭 saver、子进程通过 argv 传递 DB URL且错误未脱敏；登记 AR-B-005，限定返工不得扩展至 L1-4 |
| 2026-07-10 | Codex | 提交 L1-2（`d5f0c64`）并发布 L1-3：接入 AsyncPostgresSaver 初始化/健康检查，基于版本化 root `thread_id` 验证 PG 持久化、重建 graph/checkpointer 实例读取、进程边界恢复、thread/graph-version 隔离和错误归一化边界；不得实现 Runner/stream、业务 Agent、生产 API 或 Legacy 恢复改造 |
| 2026-07-10 | Codex | L1-2 第 1 轮复验通过：恢复 Python 3.11 mypy 契约并以 NumPy 2.4.6 兼容约束解决 PEP 695 stub 问题，专项、PG 回归、L0 契约、全量后端和静态门禁通过；关闭 AR-B-004，L1 完成度更新为 50%，下一可发布任务 L1-3 |
| 2026-07-10 | Codex | L1-2 第 0 轮验收未通过：图骨架、InMemorySaver、全量回归和常规静态门禁均通过，但交付通过把 mypy 目标从 3.11 改为 3.12 绕开 NumPy stub 失败，与项目 `requires-python >=3.11` 契约不一致；登记 AR-B-004，限定返工仅修复 Python 3.11 静态检查兼容性 |
| 2026-07-10 | Codex | 发布 L1-2：定义最小 `XuanhuGraphState`、MainGraph 和 command router，使用 InMemorySaver 验证命令路由、状态序列化、thread/graph version 隔离和边界占位；不得接入业务 Agent、生产 API 路由或 PG checkpointer |
| 2026-07-10 | Codex | 完成并验收 L1-1：引入 LangGraph/PG checkpointer 依赖和隔离 Spike 测试，验证 async graph、FastAPI async、InMemorySaver、AsyncPostgresSaver、PG 持久化、重建实例读取、版本化 thread 隔离和 interrupt/resume；关闭 AR-B-003，下一任务 L1-2 |
| 2026-07-10 | Codex | L1-1 第 0 轮验收未通过：缺少交接文件，删除 L0-1 契约测试超出范围，PG Checkpointer Spike 集成测试 5/7 失败；登记 AR-B-003 并改为返工中 |
| 2026-07-09 | Codex | 发布 L1-1：验证 LangGraph 与异步 PostgreSQL checkpointer 在当前 Python、Pydantic、FastAPI async、PostgreSQL/连接池和 Windows 开发环境中的兼容性；仅允许依赖、锁文件和隔离 Spike 测试，不接入业务 Agent、MainGraph 或生产路由 |
| 2026-07-09 | Codex | L0 阶段关闭：L0-1/L0-2/L0-3 全部完成并验收通过，无打开 AR-B/P0/P1；L1 进入条件满足，下一可发布任务 L1-1 |
| 2026-07-09 | Codex | 完成 L0-3：Runtime Feature Flag 默认 legacy；建立 fake-model 性能基线和 Token 可观测缺口记录 |
| 2026-07-09 | Codex | 完成 L0-2：建立 Golden E2E 与 Legacy 行为/测试分类基线；红旗缺口以 strict xfail 固定并明确禁止迁移复制 |
| 2026-07-09 | Codex | L0-1 第 1 轮复验未通过：AR-B-001 返修不完整，新增 AR-B-002；文档仍存在下一问职责、确定性 Gate、Graph State/checkpoint 边界冲突，契约测试存在空 `pass` 和弱断言；生产代码未修改，全量门禁复核通过 |
| 2026-07-09 | Codex | L0-1 第 0 轮验收未通过：登记 AR-B-001，任务改为 `返工中 / 验收未通过`；生产代码未修改，全量门禁通过但文档架构契约冲突且契约测试存在无效断言 |
| 2026-07-09 | Codex | 恢复发布 L0-1：确认任务仍为 `已发布 / 待验收`、无依赖且无打开阻塞，交接文件尚未生成；保持原任务登记和文件边界，不重复登记 |
| 2026-07-09 | Codex | 发布 L0-1：ADR、兼容矩阵与迁移边界；任务新增测试统一隔离到仓库根目录 `test_agent/`，不得写入现有 `tests/` |
| 2026-07-09 | Codex | 建立 LangGraph Agent 整体大修任务看板，拆分 L0～L9 共 41 个可独立发布和验收的任务；下一任务为 L0-1 |

| 2026-07-13 | Codex | 根据 L0～L4 中期代码审查重新打开阶段验收，创建 `codex/l4-5-integration-safety-hardening` 分支并发布 L4.5-01～L4.5-03；LangGraph 保持非默认、非临床试用，暂停 L5 业务叠加，直至 P0/P1 与重新验收门禁关闭。 |
| 2026-07-14 | Codex | 依次发布并执行 L4.5-04～L4.5-09：完成公开耐久幂等与真实双进程验收、Recovery 501 隔离、PG 权威 Read Model、Outbox/Redis/SSE/DLQ、模型审计、有界缓存及前后端默认关闭的非临床灰度入口。 |
| 2026-07-14 | Codex | 发布并执行 L4.5-10，修复最终全量门禁发现的迁移 teardown、GraphRun 排序和共享 worker Outbox 顺序污染；最终 Python 3.11/3.12 各 1469 passed，integration 344 passed/1 个既定 Legacy xfail，前端 171 passed，静态、安全、密钥扫描和双 SBOM 全绿；技术提交为 `3a92faa`。 |
| 2026-07-14 | Codex | L0～L4 工程重新验收通过；L4.5-02 仍等待具名临床专业人员按 `临床红旗规则人工审定签署单-2026-07-14.md` 签署。签署前两个 LangGraph 公共开关保持默认关闭，不得进入真实临床或患者试点。 |
| 2026-07-15 | Codex | 终审重新打开 L0/L1/L2/L4.5-07/L4.5-10，关闭 runtime 切换台账、进程级 LangGraph 生命周期、required 模型审计、Outbox 可部署告警和 exact-HEAD 复验的证据缺口；技术实现提交 `c9148c2`，唯一 Alembic head 为 `20260715_0012`。 |
| 2026-07-15 | Codex | 当前门禁证据：Python 3.11/3.12 各 `1549 passed, 362 deselected`；真实服务（排除串行性能）`359 passed, 1 xfailed`；碰撞 `2 passed`；串行双基线连续两次 `2 passed`；前端 `23 files / 171 tests`；Ruff、mypy（154 source files）、lock、pip/npm audit、双 SBOM、promtool、actionlint 和 Gitleaks 全绿。工程种子 `29/29` 只记为 `not_for_clinical_signoff`。 |
| 2026-07-15 | Codex | 首次 exact-HEAD 一键执行发现 Windows PowerShell 5.1 会剥离多行 Python `-c` 中的双引号，环境探针在测试开始前 fail-closed；提交 `f37e758` 改为 Windows-safe 单引号字面量并增加真实 PowerShell native argument smoke test，安全目标与 PG/Redis 版本探针复测通过。 |
| 2026-07-15 | Codex | 后续一键执行发现 PowerShell 5.1 函数局部 `$LASTEXITCODE` 会遮蔽原生命令写入的全局退出码，导致 export/audit/SBOM 失败被延迟暴露；现已改为读取真实全局退出码，增加真实 `exit 23` 回归测试、非空/格式校验、临时发布和最终成功回执，禁止将半成品 manifest 误作整轮通过证据。 |

## 8. 下一步

1. 由具名临床专业人员审定 `triage-raw-text-precheck.v1` 的规则、同义词、否定/时态、数值阈值和去标识化召回评估集，填写并签署 `临床红旗规则人工审定签署单-2026-07-14.md`。
2. 签署完成且规则摘要与技术实现提交 `c9148c2` 一致后，只关闭 L4.5-02 红旗规则人工审定门禁；任何规则变更都必须重新审定。该签署不是临床上线许可，真实患者服务或临床试点仍须完成 L5～L9 和机构正式审批。在此之前保持 Legacy 默认和两个 LangGraph 公共开关关闭。
3. L5 只能在产品/医疗安全负责人明确确认临床门禁与后续范围后重新发布；不得把本次 501 Recovery 隔离、健康端点或 L0～L4 工程完成误写成 L5～L9 已完成。

## 9. 维护要求

- 发布任务时将状态改为 `已发布`、审核改为 `待验收`。
- 收到交付时改为 `已交付`，但不得在未检查代码和测试前改为 `已完成`。
- 验收失败时登记 `AR-B-xxx`，状态改为 `返工中`，发布限定返工任务。
- 验收通过时记录测试证据、关闭关联阻塞，并更新阶段完成度。
- 不因对话摘要或交接声明覆盖本地代码事实。
- 正式验收引用的 `docs/dev-handoff/*` 必须显式纳入对应交付提交，不能只留在被忽略的工作树。
- `docs/` 默认被 `.gitignore` 忽略；正式进度/交接证据须使用显式 pathspec 纳入版本控制。
