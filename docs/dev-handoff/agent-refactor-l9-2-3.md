# L9-2 / L9-3 交付：分阶段切流、回滚与排空控制面

## 1. 结论

控制面代码、本地基础设施回归与 Codex 独立复验已 accepted；**真实切流和回滚
演练未通过前置门禁，不得执行**。当前不是 L9-4 删除授权。

## 2. 分阶段策略

| phase | 新会话策略 | 既有会话 |
|---|---|---|
| `legacy` | 使用当前 default；LangGraph 仍受 public flag 保护 | runtime 不变 |
| `development` | 开发环境显式验证 | runtime 不变 |
| `automated_test` | 自动测试环境验证 | runtime 不变 |
| `internal` | 内部会话验证 | runtime 不变 |
| `canary` | 小比例入口可显式选 v2 | runtime 不变 |
| `full` | 只允许新 LangGraph；显式 Legacy 返回 409 | runtime 不变 |
| `rollback` | 只允许新 Legacy；显式 LangGraph 失败封闭 | 已有 v2 继续 v2 |

`full` 额外要求：

1. `AGENT_RUNTIME_VERSION=langgraph`；
2. `XUANHU_LANGGRAPH_PUBLIC_ENABLED=true`；
3. `XUANHU_LANGGRAPH_PRODUCT_READY=true`；
4. durable `runtime.switched` 最新审计与配置一致。
5. 运维 readiness 使用 durable `runtime.rollout_phase_changed` 线性审计，
   并校验当前 phase deployment id。

缺一项均返回稳定的 `RUNTIME_ROLLOUT_NOT_READY`；不会静默改选 Legacy。

## 3. 自动化证据

- full phase 未满足产品授权时失败封闭；
- full phase 拒绝新 Legacy；
- rollback phase 拒绝新 LangGraph；
- runtime switch audit 线性、幂等，可从 LangGraph 审计切回 Legacy；
- rollout phase audit 线性、幂等，并与最新 runtime-switch deployment 绑定；
- rollout status CLI 在 Legacy 尚有 open session 时拒绝 removal；
- `--require-stable-minutes` 只以 durable rollout phase audit 的当前
  `full_entered_at` 计算连续 full 时长，并强制同时提供当前 phase
  `--deployment-id`；canary 时长和旧 full 窗口不计入，离开 full/rollback/
  重新 full 会重置。phase 链缺失或错序、runtime-switch 绑定、phase/deployment、
  默认 runtime 或窗口任一不符均失败封闭；
- API 错误保持既有 envelope；
- phase audit/check scoped 合同 `17 passed`，PostgreSQL phase audit 往返
  `1 passed`，Recovery 跨边界 `5 passed`；
- Alembic 当前单一 head 为 `20260728_0014`；
- 全量非 integration `2381 passed, 398 deselected in 168.87s`；
- 全量 integration `397 passed, 1 xfailed, 2381 deselected in 668.11s`；
- 三个 Codex 子 agent 最终复验均 `ACCEPT`，P0/P1/P2/P3=`0/0/0/0`；
- 真实 Safety interrupt、Doctor Review resume、Record done 与 LangGraph
  Recovery/checkpoint restart 的本地隔离演练通过。

## 4. 回滚与恢复演练状态

本地隔离 PG/Redis 已证明：

- rollback 控制面只影响新会话，既有 v2 runtime 身份不被重写；
- v2 请求不会落入 Legacy RecoveryService；
- 真实 Safety interrupt 后可通过 LangGraph Recovery 恢复，随后继续原
  LangGraph runtime 完成 Doctor Review、Record 与 session done；
- 缺失/非法 checkpoint、triage hold、pending review、stale authority 与
  crash-window claim 均失败封闭或按专用路径处理。

尚未完成的是生产/真实环境 full phase 稳定窗口、Legacy open=0 排空证明、
运维回滚演练与 L7/L8 产品/外部门禁。这些仍阻断 L9-4。

## 5. L9-4 删除条件

只有同时满足以下条件才允许发布 Legacy removal 任务：

1. L5-PROD～L8-PROD 已验收；
2. 真实 PostgreSQL/Redis、checkpoint restart、Doctor Review resume、
   Record 和 `session.done` E2E 通过；
3. `check_runtime_rollout --require-phase full --require-legacy-drained`
   加上发布策略批准的 `--require-stable-minutes` 和当前 full phase
   `--deployment-id` 后返回 0；
4. full phase 稳定窗口与回滚演练通过；
5. 旧会话全部终态，且运维确认无需重新打开；
6. 独立审查与全量门禁通过。

当前仍不满足 1、3～5；Legacy 代码保留是迁移正确性要求，不是清理遗漏。
