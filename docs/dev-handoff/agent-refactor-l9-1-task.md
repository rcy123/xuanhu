# L9-1 任务书：LangGraph API / SSE / WebUI v2 适配

## 1. 状态与基线

- 任务 ID：`L9-1`
- 状态：`delivered / acceptance pending`
- 发布日期：2026-07-28
- 基线：`0a460c1`
- 依赖：`L8-SBX` 已由 `ACC-20260728-062` 关闭；本任务不把 SBX 结论扩大为真实临床或公网生产批准。
- 用户授权：完成 L9 整体工程任务；允许修改前端以适配 LangGraph 后端。

## 2. 目标

建立稳定、版本化、向后兼容的 LangGraph 公共读写契约，使 WebUI 可以按会话持久化的
`agent_runtime` 正确展示与操作 LangGraph 会话，同时继续只读展示迁移期间尚未结束的
Legacy 会话。

## 3. 范围

允许修改：

- `app/api/`、`app/services/`、`app/schemas/` 中与会话 DTO、SSE 和 LangGraph 分流直接相关的文件；
- `frontend/` 中 API 类型、Stage/Agent 映射、会话创建、SSE、状态卡、review 恢复与测试；
- L9 专项测试、任务/交付文档和必要的非敏感示例配置。

必须完成：

1. 现有成功/错误 envelope 保持兼容；
2. `formula` Stage、LangGraph gate disposition、graph/node 运行态和 unresolved 原因可被前端表达；
3. 13 种既有 SSE 事件继续保留，payload 增加稳定的 v2 schema version；
4. WebUI 新建会话不再强制显式选择 Legacy，而由后端经审计的默认运行时决定；
5. 刷新后从权威 read model 恢复 LangGraph 状态、未解决项和 review 等待态；
6. 既有 Legacy 会话的展示合同不被破坏。

## 4. 明确非目标

- 本任务不部署到真实临床、患者、公网、商业或机构环境；
- 本任务不宣称 L5-PROD～L8-PROD 已完成；
- 本任务不删除 Legacy 实现；删除只允许在 L9-2/L9-3 的切流、恢复和回滚证据通过后进入 L9-4；
- 不读取、输出或修改 `.env`、`.claude/`、本地数据、密钥或真实患者信息；
- 不以 LangGraph 会话回退到 Legacy 作为错误恢复手段。

## 5. 不变量

- 会话创建后 `agent_runtime` 不变；
- 既有 Legacy 与 LangGraph 会话不得交叉恢复；
- Domain State 仍是临床事实真源，checkpoint 只保存执行游标；
- SSE 不暴露 Prompt、原始模型输出、密钥、身份信息或内部 checkpoint；
- 医师 review 和 Safety 硬 Gate 不得由前端或模型绕过。

## 6. 验收门禁

专项：

```powershell
uv run pytest tests/test_sessions_api.py tests/test_session_read_model.py tests/test_sse_stream.py tests/test_outbox_publisher.py -q -rs
```

前端：

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test
npm run build
```

回归：

```powershell
uv run pytest -q -rs
uv run ruff check .
uv run mypy app scripts
uv lock --check
git diff --check
```

## 7. 停止条件

出现以下任一情况立即停止并记录为 rework / blocked：

- 需要让 LangGraph 会话调用 Legacy Supervisor、ReviewService 或 RecoveryService；
- 需要破坏既有数据库列、SSE 事件名或成功/错误 envelope；
- 需要真实密钥、真实患者数据、外部临床批准或生产发布权限；
- 无法在不绕过 Safety / Doctor Review 硬 Gate 的前提下继续。

## 8. 交付

- 交付文档：`docs/dev-handoff/agent-refactor-l9-1.md`
- 必须记录实际变更、专项/全量门禁、残余限制和 exact revision 或工作树身份。
