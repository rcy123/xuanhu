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
| 当前阶段 | L3 进行中 |
| 当前任务 | L3-1 已验收通过；下一可发布 L3-2 TriagePolicy 与人工转介 |
| LangGraph 新链路 | L2-1～L2-5 已验收并提交；L3-1 IntakeExtractionAgent、canonical 输入/输出边界、抽取验证与身份信息拒绝已验收通过 |
| Legacy 生产链路 | 保持现状，只允许阻断性修复 |
| 无 RAG 新版问诊里程碑 | 未完成，目标 L3 |
| 无 RAG 全链路里程碑 | 未完成，目标 L6 |
| RAG 增强里程碑 | 未完成，目标 L7 |
| 全量切流与旧实现退役 | 未完成，目标 L9 |

## 3. 阶段进度

| 阶段 | 名称 | 状态 | 完成度 | 进入条件 | 关闭条件 |
|---|---|---|---:|---|---|
| L0 | 大修基线与迁移护栏 | 已完成 | 100% | 架构和计划已确认 | ADR、Golden tests、Feature Flag、基线完成 |
| L1 | LangGraph Runtime 骨架 | 已完成 | 100% | L0 关闭 | MainGraph、checkpointer、恢复和 stream 骨架通过 |
| L2 | Harness 核心与领域 State | 已完成 | 100% | L1 关闭 | AgentRuntime、Context、Verifier、Reducer、outbox 通过 |
| L3 | Intake 问诊子图 | 进行中 | 20% | L2 关闭 | 无 RAG 多轮问诊、Triage、Completeness 和单一下一问通过 |
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

## 7. 最近更新

| 日期 | 更新人 | 内容 |
|---|---|---|
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

## 8. 下一步

1. L3-1 已完成，保持 `AGENT_RUNTIME_VERSION` 默认 `legacy`，未经明确发布不得开始 L3-2 实现。
2. 下一可发布任务为 L3-2：TriagePolicy 与人工转介；仅消费已验证 red-flag candidates，以确定性规则生成权威 GateResult，不允许模型决定转急诊、人工复核或阶段迁移。

## 9. 维护要求

- 发布任务时将状态改为 `已发布`、审核改为 `待验收`。
- 收到交付时改为 `已交付`，但不得在未检查代码和测试前改为 `已完成`。
- 验收失败时登记 `AR-B-xxx`，状态改为 `返工中`，发布限定返工任务。
- 验收通过时记录测试证据、关闭关联阻塞，并更新阶段完成度。
- 不因对话摘要或交接声明覆盖本地代码事实。
- 不提交 `docs/dev-handoff/*`，除非用户明确要求。
- `docs/` 当前被 `.gitignore` 忽略；进度更新仍必须落盘，但不会自动显示在 `git status`。
