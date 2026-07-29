# RECOVERY-PROD-1 任务书：LangGraph checkpoint 恢复产品接线

## 状态与授权

- 发布/实施日期：2026-07-28
- 状态：`delivered / engineering gates passed / independent review pending`
- 基线：`0a460c1` 加当前 L9 组合工作树
- 用户授权：实施 L5-PROD/L6-PROD/Recovery；子 agent 只能在终端使用
  Claude Code，且不得发送 `.env*`、`.claude/**`、密钥、凭据或真实数据。

## 目标

为持久化为 LangGraph 的会话提供失败封闭、幂等、可审计的产品恢复路径。
PostgreSQL Domain State 始终是业务权威；checkpoint 仅是控制面游标。LangGraph
会话不得调用 Legacy RecoveryService 或读取 Legacy Redis checkpoint。

## 必须实现

1. MainGraph 使用真实嵌套 Recovery 子图，Graph State 只保存 command 引用。
2. `/recover` 先读取会话持久化 runtime，再按 runtime 分派；禁止 caller override。
3. checkpoint 必须绑定 exact session/graph/domain version，且只能含允许的引用型字段。
4. 支持 `retry_current_stage`、`resume_from_pg_snapshot`、受限
   `rollback_to_stage` 与 `terminate`。
5. pending Doctor Review 必须回到 `/review`；triage hold 不得由 Recovery 解锁。
6. rollback/retry 必须按目标阶段失效下游 artifact，不能伪造 Safety/Review authority。
7. recovery control artifact、session、audit、Outbox、GraphRun/steps 与 Domain commit
   原子提交；内层 claim 可幂等重放。
8. 覆盖“Domain 已提交、claim 尚未完成”的进程崩溃窗口。
9. 前端仅在可恢复的 blocked/manual 状态展示恢复动作；terminated/triage hold 不展示。
10. 保持 Legacy RecoveryService 行为不变。

## 非目标

- 不从 checkpoint 恢复临床事实或正文。
- 不把 LangGraph 会话降级到 Legacy。
- 不开启 public/product-ready/full，不删除 Legacy。
- 不声明临床、患者、公网、商业或机构放行。

## 验收

- Recovery unit/dispatcher/路由与 API 合同通过。
- 独立 PG/Redis 上覆盖真实 Safety block、checkpoint restart、恢复/重放、缺失或
  非法 checkpoint、rollback invalidation、terminate 与 crash-window repair。
- L5/L6/Recovery 联跑、全量 integration、全量非 integration、前端与静态门禁通过。
- 终端 Claude Code 独立复核取得可核验结论。
