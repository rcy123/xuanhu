# RECOVERY-PROD-1 交付：LangGraph checkpoint 恢复

## 结论

- 基线：`0a460c1e88f52323b471f20e77db8eca3ee0bf3f`
- 工程状态：**本地工程 accepted**；三个 Codex 子 agent 独立复验的 Recovery 路径最终 `ACCEPT`。
- Legacy RecoveryService 保留且仅服务持久化为 Legacy 的会话。

## 实现

- `recovery_placeholder` 现在是嵌套 Recovery 子图，内部节点为
  `recovery.execute`。
- `RecoveryDispatcher` 只按数据库中的 `agent_runtime` 选实现；LangGraph 使用
  `LangGraphRecoveryService`，Legacy 继续使用原服务。
- checkpoint 仅校验 session、graph version、Domain state version、route 与
  interrupt 引用；未知字段或临床载荷 fail closed。
- 内层 durable command claim 使用稳定 `recover:<digest>` 键；原始 recovery
  reason 只持久化摘要，不进入 checkpoint。
- retry/resume 从 PostgreSQL Domain authority 重建控制状态；review interrupt
  指向 `/review`，triage hold 不被解锁。
- rollback 只允许不向前移动的 `inquiry/safety/record`，并按阶段失效下游权威。
- terminate 不依赖 checkpoint，但保留原 runtime 身份。
- Domain commit 与 recovery artifact、session、audit、Outbox、GraphRun/steps
  原子提交；提交后 claim 未完成可由 commit/artifact 引用修复。
- 公共 API 在创建 request-local saver 前关闭只读事务，避免
  `CREATE INDEX CONCURRENTLY` 等待自身 virtual transaction。
- JSONB claim 重置显式写 SQL `NULL`，避免 JSON 字面量 `null` 违反对象约束；
  同类 advance/HTTP idempotency 路径同步修正。
- 前端只对可恢复 blocked/manual 状态展示“尝试恢复”，恢复后刷新权威详情与消息。

## 测试

- Recovery unit/dispatcher/路由聚焦：`87 passed`（实现阶段证据）。
- 受影响幂等/Recovery 聚焦：`31 passed, 14 deselected`。
- 既有 Recovery API：`27 passed in 16.97s`。
- Recovery 产品集成：`5 passed in 25.12s`。
- L5/L6/Recovery 产品联跑纳入最终 non-integration/integration 门禁。
- 最终 integration：`397 passed, 1 xfailed, 2381 deselected in 668.11s`。
- 最终非 integration：`2381 passed, 398 deselected in 168.87s`。
- 前端 `24 files / 187 tests passed in 53.61s`，typecheck、lint、build 通过。
- 全仓 Ruff、`mypy app scripts`（175 files）、lock、diff check 通过。
- Alembic 单一 head：`20260728_0014`。

## 独立复验

- 初审：**REWORK**，P0/P1/P2/P3=`0/1/1/1`；发现稳定窗错误复用
  runtime-switch age、nested checkpoint schema 与文档缺口。
- R1：**REWORK**，P0/P1/P2/P3=`0/0/2/2`；限定为 scoped Ruff format、
  可执行 phase runbook 与当前 handoff/治理文档。
- 最终：**ACCEPT**，P0/P1/P2/P3=`0/0/0/0`；稳定窗改用 durable phase audit
  的 `full_entered_at` 和当前 `--deployment-id`，checkpoint schema 严格失败封闭。

## 残余边界

- 尚未执行真实 full/canary 稳定窗口，也未证明真实环境 Legacy open=0。
- product-ready/public/full 保持关闭；L9-4 Legacy removal 不在本交付内。
