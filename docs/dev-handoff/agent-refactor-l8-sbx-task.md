# L8-SBX 可观测性、评估与安全加固整体任务书

## 1. 任务身份与授权

- 稳定 ID：`L8-SBX`
- 发布基线：`ac6e9c2`（L7-SBX accepted 管理记录；实现基线 `da604d75a758f1b8941e849735453472208aff6f`）
- 本任务由用户于 2026-07-28 明确授权，范围是个人学习沙盒中的固定合成数据、离线 unit/in-memory reference composition。
- 子 agent 的实质工作必须通过终端 Claude Code 执行；Claude Code 外发范围限于 L8 相关代码、测试、文档和 Git 元数据，不得读取或发送 `.env`、密钥、凭据、ignored data 或真实业务数据。

## 2. 目标

在不接入产品 Runtime 的前提下，建立 L8 的可复现工程门禁：

1. Episode Package、版本与业务事件、指标和失败归因。
2. 隐私/权限/预算/Prompt Injection 的 fail-closed 参考实现。
3. 有限故障注入、恢复、重复 resume、状态冲突和幂等验证。
4. 固定行为评估集、离线 v2 评估和不写生产结果的 Shadow 对比；真实模型试跑仅保留显式 external gate。

## 3. 明确非目标

- 不修改或接线 `app/api`、`app/server.py`、HTTP/SSE 生产入口、MainGraph、GraphRunner、部署、容器、数据库迁移、Redis、Milvus、模型 gateway 或前端。
- 不修改已 accepted 的 L0-L7 生产/沙盒合同；L7 Evidence/RAG 只通过公开的只读 DTO/fixture 复用。
- 不读取 `.env`、ignored `data/`、stash、未跟踪 `.claude/`、患者/医师/机构/临床/商业/公开真实数据。
- 不把规则扫描宣称为全面 PII/隐私合规证明；不把固定 fixture 或真实模型试跑宣称为临床批准。
- 不自动启用真实模型、LangSmith、生产 shadow write、外部服务、真实 checkpoint 或持久化。
- 不推进 L9，不删除 Legacy，不改变默认 runtime 开关。

## 4. 允许修改范围

允许新增或修改以下范围：

- `app/agent_runtime/sandbox_observability.py`
- `app/agent_runtime/sandbox_security.py`
- `app/agent_runtime/sandbox_faults.py`
- `app/agent_runtime/sandbox_evaluation.py`
- `tests/test_sandbox_observability_l8.py`
- `tests/test_sandbox_security_l8.py`
- `tests/test_sandbox_faults_l8.py`
- `tests/test_sandbox_evaluation_l8.py`
- 本任务 handoff 与项目管理记录

除非先发布新的范围变更，不得修改其他源码、schema、migration、依赖或锁文件。

## 5. 分阶段任务与依赖

| 子任务 | 内容 | 依赖 | 交付 |
|---|---|---|---|
| L8-1 | Episode Package、Metrics、业务事件和失败归因 | L7-SBX | `sandbox_observability.py` + 专项测试 |
| L8-2 | 隐私、权限、预算、Prompt Injection | L8-1 | `sandbox_security.py` + 专项测试 |
| L8-3 | 故障注入、恢复、重复 resume、状态冲突和幂等 | L8-1/L8-2 | `sandbox_faults.py` + 专项测试 |
| L8-4 | 行为评估集、离线评估、Shadow 对比、真实模型 external gate | L8-1/L8-2/L8-3 | `sandbox_evaluation.py` + 专项测试 |

默认一次只发布一个 active execution task；前一阶段的测试与管理证据通过后才进入下一阶段。

## 6. 必须实现的公共合同

### L8-1

- 严格、冻结、版本化的 `EpisodePackageV1`、`NodeTrajectoryEventV1`、`ModelUsageV1`、`FailureAttributionV1`、`BusinessEventV1`。
- Episode 必须绑定 `state_hash`、`graph_version`、`agent_spec_version`、`prompt_version`、`schema_version`、`policy_version`、model actual/usage、evidence/verification/gate/human-intervention 引用。
- 不得保存原始 prompt、模型原文、临床文本、身份字段或异常堆栈。
- 事件类型闭集：`node.started`、`node.completed`、`gate.failed`、`interrupt.required`、`graph.completed`、`graph.failed`。
- append-only in-memory store；同 key 同 canonical bytes 幂等，同 key 异 bytes 拒绝；snapshot/restore 必须 canonical、可重放且拒绝篡改。
- 指标使用固定名称和固定标签集合；不允许动态 label、原始内容或任意 key。

### L8-2

- `PrivacyPolicy` 在写入前进行有限、可审计的 redaction，并在不确定时 fail-closed。
- `CapabilityScope` 为操作建立闭集 allowlist；未授权能力和权限回调漂移必须拒绝。
- `BudgetLedger` 原子 reserve/consume/release，限制 model calls、tokens、deadline 和 retries；同一幂等键不可重复扣费。
- `PromptInjectionGuard` 对 untrusted text 做有限分类，禁止把 untrusted 指令提升为 system/policy；命中高风险时返回固定无 payload 错误。

### L8-3

- `FaultKind` 闭集覆盖 gateway timeout、RAG unavailable、PostgreSQL transient、Redis failure、checkpoint failure、duplicate resume、state conflict。
- 注入由显式 `FaultPlan` 控制，默认关闭；每次 fault 可归因到 node/model/tool/policy/verifier/persistence。
- 恢复必须有 bounded retry、deadline、single-use resume、state-version precondition 和 idempotent side effect ledger。
- checkpoint restore 只接受 canonical snapshot；恢复失败不产生部分成功或重复业务结果。

### L8-4

- 固定、版本化、无真实数据的 `BehaviorCaseV1`/`BehaviorDatasetV1`，覆盖 Intake/Triage/Completeness/Prompt Injection/Syndrome/Formula/Safety/Review/Record。
- 评估输出必须包含每维度 pass/fail、固定阈值、失败归因和 aggregate metrics。
- `ShadowComparator` 使用同一去标识输入比较 legacy/v2 的质量、延迟、token、失败率；v2 输出只能进入隔离报告，禁止写业务结果。
- `RealModelTrialGate` 默认关闭；无具名外部批准、预算和数据策略时必须返回 `external_gate_required`，不得发起网络模型调用。

## 7. 验收门禁

每个子任务必须提供真实 RED/GREEN 证据，并绑定 exact implementation commit：

```text
uv run pytest tests/test_sandbox_observability_l8.py -q
uv run pytest tests/test_sandbox_security_l8.py -q
uv run pytest tests/test_sandbox_faults_l8.py -q
uv run pytest tests/test_sandbox_evaluation_l8.py -q
uv run pytest tests/test_sandbox_observability_l8.py tests/test_sandbox_security_l8.py tests/test_sandbox_faults_l8.py tests/test_sandbox_evaluation_l8.py -q
uv run pytest -m "not integration" -q
uv run ruff check app/agent_runtime/sandbox_*.py tests/test_sandbox_*_l8.py
uv run ruff format --check app/agent_runtime/sandbox_observability.py app/agent_runtime/sandbox_security.py app/agent_runtime/sandbox_faults.py app/agent_runtime/sandbox_evaluation.py tests/test_sandbox_*_l8.py
uv run mypy app/agent_runtime/sandbox_observability.py app/agent_runtime/sandbox_security.py app/agent_runtime/sandbox_faults.py app/agent_runtime/sandbox_evaluation.py
uv lock --check
git diff --check
```

PM 必须额外探针：PII/secret leak、动态指标标签、跨 episode 读取、snapshot tamper、budget overspend、prompt injection downgrade、每类 fault attribution、duplicate resume、state conflict、shadow write prohibition、real-model gate no-call。

门禁勘误（2026-07-28）：原格式命令中的 `app/agent_runtime/sandbox_*.py`
会同时选中 5 个已验收的 L5～L7 模块，超出第 4 节允许修改范围；首次执行因此报告
8 个文件待格式化，其中 3 个属于 L8、5 个属于既有模块。格式门禁现按上面的 4 个
L8 源模块白名单执行；3 个 L8 文件已格式化并复检通过，既有模块未被修改。Ruff
检查仍保留更宽的只读通配符门禁并已通过。

## 8. 停止条件

- 需要修改允许列表之外的文件、引入依赖或锁文件。
- 需要读取或发送 `.env`、密钥、真实数据、stash 或未跟踪 `.claude/`。
- 需要网络、数据库、Redis、Milvus、模型 gateway、产品 Runtime 或部署。
- 出现 P0/P1，或同一 authority/integrity 缺陷连续出现并开始扩张例外表。
- L7/L6 回归失败且不能证明与 L8 隔离。

## 9. 状态边界

L8-SBX engineering complete 不等于 L8-PROD、L9、临床、公开、商业或机构授权。真实模型试跑、LangSmith、生产 shadow、外部数据和专业批准均属于 `external_gate`，不得由工程验收替代。
