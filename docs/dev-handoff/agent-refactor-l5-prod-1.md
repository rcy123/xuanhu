# L5-PROD-1 交付：LangGraph Safety / Doctor Review 产品接线

## 结论

- 基线：`0a460c1e88f52323b471f20e77db8eca3ee0bf3f`
- 工作树：基线上未提交的 L9 组合工作树；用户排除目录、环境密钥与凭据未纳入。
- 工程状态：**本地工程 accepted**；三个 Codex 子 agent 独立复验的 L5 路径最终 `ACCEPT`。
- 发布状态：默认 runtime 仍为 Legacy，LangGraph public/product-ready 仍为 false；未切流、未删除 Legacy。

## 产品合同

- MainGraph 的历史 `review_placeholder` 已替换为真实嵌套 Safety/Review 子图。
- Safety 只消费 PostgreSQL Domain State 中当前且已验证的 `formula_draft` 与
  `SafetyProfile`；`SafetyRuleEngine` 是 passed/issues 唯一权威。
- Safety artifact、Gate、`SafetyRuleRun` 兼容投影、session、audit、Outbox 和
  Domain command commit 在同一事务提交。
- Review 使用真实 LangGraph `interrupt()`；checkpoint payload 仅含稳定引用与版本，
  不包含处方、患者事实或 Review 决定。
- `/review` 对 LangGraph 会话只写版本绑定的 `review_submission` 与兼容
  `DoctorReview` 投影，再以同一 thread 的 `Command(resume=...)` 应用决定。
- `confirm`、`modify`、`reject`、`request_more_info` 均为确定性路由；`modify`
  创建新处方/安全 revision 并重跑 Safety，失败尝试也保留审计 artifact。
- Legacy 会话继续只调用原 ReviewService；LangGraph 路径没有 Legacy fallback。

## 数据与迁移

- 追加迁移 `20260728_0013` 扩展 `doctor_reviews.action`。
- downgrade 将旧版本无法表达的 `request_more_info` 兼容投影保守映射为
  `reject`；append-only Domain/audit 权威记录不被改写。
- 当前迁移头：`20260728_0014 (head)`。

## 验证证据

- L5 单元：`tests/test_l5_prod_review_subgraph.py` 与
  `tests/test_graph_runner_resume.py` 纳入非 integration 全量并通过。
- L5/L6/Recovery 产品联跑与 `request_more_info` downgrade/upgrade 均纳入最终
  non-integration/integration 门禁。
- 最终 integration：`397 passed, 1 xfailed, 2381 deselected in 668.11s`。
- 最终非 integration：`2381 passed, 398 deselected in 168.87s`。
- 全仓 Ruff、`mypy app scripts`（175 files）、lock、diff check 通过。
- 前端 `24 files / 187 tests passed in 53.61s`，typecheck、lint、build 通过。

## 独立复验

- 初审：**REWORK**，P0/P1/P2/P3=`0/1/3/1`；发现 interrupt 被 END 消耗，
  coverage matrix、安全确定性、原子审计与 docstring 缺口。
- R1：**REWORK**，P0/P1/P2/P3=`0/1/1/1`；限定为 HTTP durable outcome
  resolver、provenance 与文档闭环。
- 最终：**ACCEPT**，P0/P1/P2/P3=`0/0/0/0`；上述 findings 全部关闭。

## 残余边界

- 不构成真实患者、临床、公开、商业或机构发布批准。
- 不得据此把 product-ready 设为 true、进入 full 或删除 Legacy。
