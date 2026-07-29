# L5-PROD-1 任务书：LangGraph Safety / Review 产品接线

## 1. 状态与授权

- 任务 ID：`L5-PROD-1`
- 状态：`delivered / engineering gates passed / independent review pending`
- 发布日期：2026-07-28
- 基线：`0a460c1` 加当前未提交 L9 工作树
- 用户授权：启动 L5-PROD/L6-PROD/Recovery 产品接线；允许在排除 `.env*`、
  `.claude/**` 和其他密钥/凭据文件后，将源码交给终端 Claude Code 处理。
- 当前外部复核状态：三个新建子 agent 均实际调用 Claude Code `2.1.220`；
  L5 仅返回不可核验元摘要，后续 L5/L6/Recovery 调用均因 API 403 套餐、额度或
  模型授权失败。该事实不阻止主 agent 的本地工程实现，但阻止最终独立复核门禁关闭。

## 2. 目标

把 L4 的 `formula_draft` 权威产物接入确定性 SafetyRuleEngine，并以真实
LangGraph `interrupt()` 与同一 `thread_id` 的 `Command(resume=...)` 建立不可绕过的
Review 硬门。公开 `/advance` 与 `/review` 按会话固化的 `agent_runtime` 分流；
LangGraph 路径不得调用 Legacy Supervisor、Legacy ReviewService 或 Legacy
`state_snapshot` 作为临床权威。

## 3. 必须实现

1. 从当前、已验证的 `formula_draft` artifact 和 Domain SafetyProfile 构建 Safety 输入；
2. SafetyRuleEngine 是 `passed/issues` 的唯一权威，模型或前端不能覆盖；
3. Safety 运行、版本化 `safety_result` artifact、Gate、兼容 `safety_rule_runs` 投影、
   Outbox 和会话 `review/pending_review` 状态具有原子、幂等的持久化边界；
4. interrupt payload 只包含稳定引用和版本，不包含处方、患者事实、Review 决定或明文令牌；
5. `/review` 仅持久化版本绑定的 `review_submission`，resume payload 只含其引用；
6. `confirm`、`modify`、`reject`、`request_more_info` 均有确定性路由；
7. `modify` 必须创建新的 Formula/Safety revision 并完整重跑 SafetyRuleEngine；
8. 只有同一 checkpoint 的 `Command(resume=review_ref)` 成功应用 Review 后，
   才能清除 `pending_review` 并进入 `record` 或明确回退阶段；
9. 同一 review resume 幂等，并发 resume 只有一个权威结果；进程重启后可恢复；
10. API 成功/错误 envelope、Legacy 三动作行为和现有读接口保持兼容。

## 4. 范围

允许修改：

- `app/agent_runtime/` 的 MainGraph、Runner、L5 产品子图/节点与引用型 Graph State；
- `app/services/`、`app/api/advance.py`、`app/api/review.py`、Review/Safety schema；
- 追加式数据库迁移、Domain Repository 产品投影扩展、相关模型约束；
- L5-PROD 专项 unit/integration/API/checkpoint 测试与项目管理/交付文档；
- 为等待态、四种 Review 动作或错误态所必需的前端适配。

## 5. 非目标与停止条件

- 不在本任务生成 MedicalRecord；Record 由后续 `L6-PROD-1` 完成；
- 不实现 `/recover`；Recovery 由后续独立任务完成；
- 不修改默认 runtime、product-ready 或公开切流开关；
- 不删除 Legacy；不把 LangGraph 会话降级到 Legacy；
- 不读取、输出或修改 `.env*`、`.claude/**`、密钥、凭据或真实数据；
- 不声明临床、患者、公网、商业或机构放行；
- 若必须把 Review 决定或临床正文放入 checkpoint，或必须绕过 interrupt 才能推进，
  任务立即记为 rework/blocked。

## 6. 验收门禁

专项 unit：

```powershell
uv run pytest tests/test_l5_prod_review_subgraph.py tests/test_graph_runner_resume.py -q
```

真实服务（独立 pytest 进程、受保护测试 URL）：

```powershell
uv run pytest tests/test_l5_prod_review_integration.py -m integration -q -rs
```

回归：

```powershell
uv run pytest -q -rs
uv run ruff check .
uv run mypy app scripts
uv lock --check
git diff --check
```

## 7. 交付要求

- handoff：`docs/dev-handoff/agent-refactor-l5-prod-1.md`
- 记录实际测试数、未运行门禁、迁移 head、工作树身份及残余风险；
- 只有专项、回归、真实 PostgreSQL checkpoint restart/resume 和独立 Claude Code
  复核均取得可核验证据后，才允许把 L5-PROD 记为 accepted。
