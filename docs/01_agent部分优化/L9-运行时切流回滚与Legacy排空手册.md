# L9 运行时切流、回滚与 Legacy 排空手册

## 1. 适用范围

本手册只描述工程发布控制。它不替代临床、安全、隐私、机构或生产批准。
所有命令只应在已授权的部署环境执行；不得把真实患者数据、连接串或密钥
写入命令行参数、日志或交付文档。

## 2. 不变量

- `agent_runtime` 在会话创建后不可改变；
- default/phase 只决定新会话；
- 既有 Legacy 会话继续 Legacy，既有 LangGraph 会话继续 LangGraph；
- 回滚不得把 v2 checkpoint、artifact 或 Domain State 写入 Legacy
  `state_snapshot`；
- LangGraph 故障不得调用 Legacy Supervisor/Review/Recovery；
- 未通过产品门禁时 `XUANHU_LANGGRAPH_PRODUCT_READY` 必须保持 false。

## 3. 发布前检查

```powershell
uv run python -m scripts.check_runtime_rollout --require-phase development
uv run pytest -q -rs
uv run ruff check .
uv run mypy app scripts
uv lock --check
```

涉及 PostgreSQL/Redis/checkpointer 的阶段必须配置独立、安全、可销毁的
`TEST_DATABASE_URL` 与 `TEST_REDIS_URL` 并执行 integration 测试。不得复用
开发或生产数据库。

## 4. 分阶段切流

按以下顺序逐阶段部署，每阶段都记录独立 deployment id、指标、故障和结论：

1. `development`
2. `automated_test`
3. `internal`
4. `canary`
5. `full`

这里存在两类 deployment id，不得混用：

- runtime deployment id：传给 `scripts.audit_runtime_switch`，标识一次
  `legacy <-> langgraph` default runtime 变更；
- phase deployment id：传给 `scripts.audit_runtime_rollout_phase`，标识一次
  rollout phase 变更，例如 `phase-full-20260728-001`。

每个新变更必须生成新的、8～64 位 allowlisted deployment id。只有在重试
**完全相同**的命令时才复用原 id；重试会返回原审计记录，不会刷新稳定窗口
起点。同一个 id 不得用于另一 runtime、phase、from/to 或发布原因。

从 Legacy 切到 LangGraph 时，部署环境先使用目标
`AGENT_RUNTIME_VERSION=langgraph` 运行审计命令，但在审计与 readiness
完成前不得接收创建流量：

```powershell
uv run python -m scripts.audit_runtime_switch `
  --from-runtime legacy `
  --to-runtime langgraph `
  --operator release-bot `
  --reason "approved staged LangGraph rollout" `
  --deployment-id release-YYYYMMDD-NNN
```

每次 phase 变化都必须在目标配置已经部署、但创建流量仍暂停时记录。例如
`internal -> canary`：

```powershell
$CanaryPhaseDeploymentId = "phase-canary-YYYYMMDD-NNN"
uv run python -m scripts.audit_runtime_rollout_phase `
  --from-phase internal `
  --to-phase canary `
  --operator release-bot `
  --reason "approved canary rollout" `
  --deployment-id $CanaryPhaseDeploymentId

uv run python -m scripts.check_runtime_rollout `
  --require-phase canary `
  --deployment-id $CanaryPhaseDeploymentId
```

phase 审计命令会把记录绑定到当时最新的 durable runtime-switch deployment。
runtime 审计缺失、配置不一致、phase 链缺失/错序或 deployment id 冲突时均
失败封闭；不得先放量再补审计。

只有 L5-PROD～L8-PROD、真实 DB/Redis、恢复、Review/Record E2E 与外部发布
门禁全部完成后，才能设置：

```text
AGENT_RUNTIME_VERSION=langgraph
AGENT_RUNTIME_ROLLOUT_PHASE=full
XUANHU_LANGGRAPH_PUBLIC_ENABLED=true
XUANHU_LANGGRAPH_PRODUCT_READY=true
```

并执行：

```powershell
$FullPhaseDeploymentId = "phase-full-YYYYMMDD-NNN"
uv run python -m scripts.audit_runtime_rollout_phase `
  --from-phase canary `
  --to-phase full `
  --operator release-bot `
  --reason "approved full LangGraph rollout" `
  --deployment-id $FullPhaseDeploymentId

uv run python -m scripts.check_runtime_rollout `
  --require-phase full `
  --deployment-id $FullPhaseDeploymentId
```

若从其他合法阶段进入 `full`，`--from-phase` 必须填写 durable phase 链的
真实当前值，不得固定照抄 `canary`。

## 5. 回滚

回滚只停止新建 v2，不迁移、不删除现有 v2 会话：

```text
AGENT_RUNTIME_VERSION=legacy
AGENT_RUNTIME_ROLLOUT_PHASE=rollback
```

记录反向审计：

```powershell
$RollbackRuntimeDeploymentId = "runtime-rollback-YYYYMMDD-NNN"
uv run python -m scripts.audit_runtime_switch `
  --from-runtime langgraph `
  --to-runtime legacy `
  --operator release-bot `
  --reason "approved rollback; preserve existing v2 sessions" `
  --deployment-id $RollbackRuntimeDeploymentId

$RollbackPhaseDeploymentId = "phase-rollback-YYYYMMDD-NNN"
uv run python -m scripts.audit_runtime_rollout_phase `
  --from-phase full `
  --to-phase rollback `
  --operator release-bot `
  --reason "approved rollback; stop new v2 sessions" `
  --deployment-id $RollbackPhaseDeploymentId
```

验证：

```powershell
uv run python -m scripts.check_runtime_rollout `
  --require-phase rollback `
  --deployment-id $RollbackPhaseDeploymentId
uv run pytest tests/test_runtime_switch_audit.py `
  tests/test_runtime_rollout.py `
  tests/test_runtime_rollout_api.py `
  tests/test_recovery_dispatcher.py -q -rs
```

顺序必须是：暂停创建流量 → 部署目标 runtime/phase 配置 → 记录 runtime
switch（仅 runtime 确实变化时）→ 记录 phase transition → readiness 返回 0
→ 恢复创建流量。离开 `full` 但 runtime 仍为 LangGraph 时，不得伪造一次
同 runtime switch；只需用新的 phase deployment id 记录
`full -> <目标 phase>`。任何离开 `full` 的 durable phase 记录都会立即清空
连续 full 窗口；之后重新进入 `full` 必须再用新的 phase deployment id
记录，稳定窗口从这条新记录重新起算。

必须抽验：

- rollback 后新会话为 Legacy；
- rollback 前的 v2 会话仍为 LangGraph；
- v2 artifact/revision/digest 未改变；
- v2 recovery 不进入 Legacy；
- 恢复后能继续原 runtime 并完成。

本地专用 PG/Redis 已覆盖最后一项：既有 v2 会话可经 LangGraph Recovery
恢复并继续原 runtime 完成 Review、Record 与 done。真实部署环境仍须由有权运维
按本节命令独立演练，不能用本地证据代替。

## 6. Legacy 排空与删除

全量稳定窗口结束后先执行：

```powershell
uv run python -m scripts.check_runtime_rollout `
  --require-phase full `
  --require-stable-minutes $ApprovedStableWindowMinutes `
  --deployment-id $FullPhaseDeploymentId `
  --require-legacy-drained
```

稳定窗口分钟数必须来自已批准的发布策略，不得由脚本或执行者临时缩短。返回 0
只证明配置/审计一致、指定 phase deployment 对应的**当前连续 full** 已达到
该时长且没有 open Legacy 会话。`--require-stable-minutes` 必须同时提供
`--deployment-id`；缺失 phase 审计、链错序、runtime-switch 绑定不一致、
phase/deployment 不匹配、已经离开 full 或时长不足均失败封闭。canary 时长、
更早一次 full 以及 rollback 前的 full 时长都不会被计入。仍需单独核对：

- 旧会话不会被业务重新打开；
- 审计/归档/病历读取不依赖将删除的执行代码；
- Legacy Supervisor、SufficiencyAgent、Prescription/Modification v1、
  旧 prompt loader/manifest、死 `next_stage` 和不可达 Safety 分支的消费者
  已归零；
- 删除后后端、前端、E2E、integration、恢复和行为门禁全通过。

删除必须是独立、可审查的 L9-4 变更，不得与切流同批执行。
