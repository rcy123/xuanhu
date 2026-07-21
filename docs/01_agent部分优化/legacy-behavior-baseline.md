# Legacy Agent 行为基线

> 任务：L0-2
> 日期：2026-07-09
> 运行时：Legacy `Supervisor + AgentRegistry`
> Golden：`tests/golden/test_legacy_golden.py`

## 1. 基线目的

本基线记录迁移前可观察行为，供 L1–L9 做新旧对比。Golden 测试使用 fake
Agent，不调用真实模型；确定性安全规则使用真实实现。基线不是对 Legacy
内部结构的永久兼容承诺，目标契约以 ADR、兼容矩阵和迁移边界为准。

## 2. Golden 场景

| 场景 | 固定行为 | 迁移要求 |
|---|---|---|
| 正常问诊到病历 | 消息、阶段、医师确认、病历及关键 SSE/审计可串联 | 保持外部 API/SSE 语义 |
| 信息不足 | `INSUFFICIENT_INQUIRY`，保持 inquiry | LangGraph 改由确定性 CompletenessPolicy |
| 过敏 | 确定性规则产生 `ALLERGY/BLOCKER` | 不得由模型覆盖 |
| 妊娠/可能妊娠 | 两者对禁用药均产生 `PREGNANCY/BLOCKER` | 保持同等严格 |
| 红旗症状 | Legacy 缺少确定性 red-flag gate，Golden 严格 xfail | L3 必须修复，禁止复制为兼容行为 |
| 安全失败与修改 | 安全失败回退；医师修改后重新执行安全审核 | 保持硬门禁 |
| 医师确认/修改/拒绝 | review 阶段不可被 advance 绕过 | L5 改为 interrupt 硬门禁 |
| 病历生成 | 无有效 Doctor Review 时进入 blocked 且不落病历 | 保持硬门禁 |

专项证据：`9 passed, 1 xfailed`。唯一 xfail 是已知 Legacy 红旗缺口，使用
`strict=True`；若 Legacy 行为意外变化，测试会失败并要求重新分类。

## 3. API、数据库、Redis 与前端基线

- API 请求/响应、错误码、版本语义和 13 类 SSE 事件以
  `legacy-api-compatibility-matrix.md` 为精确清单。
- PostgreSQL 的临床事实权威表、Legacy `state_snapshot/state_version` 和
  不可破坏 Schema 边界以 `agent-runtime-migration-boundary.md` 为准。
- Redis 继续使用 `xuanhu:events:{session_id}`、`xuanhu:checkpoint:{session_id}`
  和既有会话锁；Golden 校验关键事件和恢复所依赖的现有路径。
- 前端继续依赖稳定 API DTO 和业务 SSE；`ChatPanel` 收到事件后以 GET 刷新
  为权威，`review.required` 使用 `modified_formula`，SSE 不可用时回退轮询。
- L0 不改变任何上述 Legacy 行为。

## 4. 现有测试分类

| 分类 | 范围 | 迁移处理 |
|---|---|---|
| 继续保留 | SafetyRuleEngine、Schema、API envelope/error、DB/Redis、审计、隐私、锁、Doctor Review/Record gate、前端 DTO/SSE | 作为跨运行时契约 |
| 迁移后改写 | `Supervisor` 阶段路由、Inquiry/Sufficiency、手写 checkpoint/recovery、Legacy Agent 单阶段测试 | 对应 L1–L6 子图/Harness 测试 |
| 验证旧错误行为、应删除 | LLM Sufficiency、InquiryAgent 生成下一问、`force=true` 绕过充分性、同会话 Legacy 阶段名细节 | 目标实现上线后删除，不作为兼容要求 |
| 已知缺口 | 确定性 red-flag gate | L3 修复；当前以 strict xfail 记录 |

L0-1 文档契约测试现位于 `tests/test_l0_1_contract.py`，由默认
`testpaths` 自动发现，也可在核对架构文档时单独运行。

## 5. 变更规则

后续阶段若改变 Golden 可观察结果，必须同时提供：

1. 变更对应的 ADR/任务编号；
2. Legacy 与 LangGraph 的差异说明；
3. 新回归测试；
4. 回滚路径；
5. 对安全、医师复核和病历门禁无削弱的证据。
