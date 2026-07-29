# L9-1 交付：LangGraph API / SSE / WebUI v2 适配

## 1. 交付状态

- 基线：`0a460c1`
- 工作树：基线上的 L9 组合未提交工作树；用户排除目录、环境密钥与凭据未纳入。
- 结论：**本地工程 accepted / 真实发布 external_gate**。
- 已完成门禁：专用 PostgreSQL/Redis/checkpoint integration、前后端全量回归与
  三个 Codex 子 agent 独立复验。
- 待补门禁：真实 full 稳定窗口、Legacy 排空、运维回滚与 L7/L8 产品/外部门禁。
- 授权边界：仅工程与非临床测试；不构成临床、患者、公网、商业或机构发布批准。

## 2. 已实现

### API 与 SSE

- `SessionCreateResponse`、`SessionListItem` 明确返回持久化 `agent_runtime`；
- 保持既有成功/错误 envelope 与 13 种 SSE 事件名；
- 所有 EventService 生产的事件 payload 强制写入
  `schema_version=session-event.v2`，调用方伪造版本会被覆盖；
- heartbeat、resync、普通写入与 outbox 去重写入均使用同一版本；
- 未改变会话创建后的 runtime 身份，也未加入跨 runtime fallback。

### WebUI

- 新建会话不再由浏览器端选择或强制发送 Legacy；请求省略
  `agent_runtime`，由后端默认运行时和持久化 `runtime.switched` 审计决定；
- 会话列表明确显示 `Legacy` / `LangGraph v2`；
- LangGraph 步骤条改为“问诊与门禁 → 辨证草案 → 方药草案 →
  Safety → 医师复核 → 病历”，兼容旧阶段别名；
- 对外稳定表达 `ready`、`needs_input`、`triage_hold`、
  `manual_required`，只从持久化 Gate / Read Model 推导，缺失时失败封闭；
- 支持 `intake`、`reasoning`、`syndrome_draft`、`formula_draft` 等
  LangGraph agent/node 运行态；
- 页面刷新后，待复核处方只从完整性校验通过的 current
  `formula_draft` artifact 恢复；不读取 checkpoint 或
  `state_snapshot` 作为临床事实；
- `read_model.review_required` 只恢复“等待硬门禁”展示，不会在
  Safety/Doctor Review 产品接线缺失时错误开放确认按钮。

## 3. L9 切流控制补强

本次同时加入后续 L9-2/L9-3 所需的失败封闭控制面：

- `AGENT_RUNTIME_ROLLOUT_PHASE`：
  `legacy|development|automated_test|internal|canary|full|rollback`；
- `XUANHU_LANGGRAPH_PRODUCT_READY=false` 默认失败封闭；
- `full` 只有在 default=LangGraph、public enabled、product ready 三项均满足时
  才允许新会话，并拒绝显式新建 Legacy；
- `rollback` 要求 default=Legacy，拒绝新建 LangGraph，但不会修改既有
  v2 会话的持久化 runtime；
- `scripts/check_runtime_rollout.py` 只输出配置、审计状态和按 runtime/status
  聚合的数量，不输出患者或会话标识；可作为全量切流和 Legacy 排空门禁。

## 4. 测试证据

### 前端

- `npm run typecheck`：通过；
- `npm run lint`：通过；
- `npm run test`：`24 files / 187 tests passed in 53.61s`；
- `npm run build`：通过；仅有既有的单 chunk 大于 500 kB 警告。

### 后端

- L9 rollout/audit 专项：`35 passed`；
- rollout status 专项：`10 passed`；
- 稳定窗口增量专项：rollout/audit/check/API `22 passed`，PG audit 往返 `3 passed`；
- 全量非 integration：`2381 passed, 398 deselected in 168.87s`；
- `uv run ruff check .`：通过；
- `uv run mypy app scripts`：175 个源码文件，0 错误；
- `uv lock --check`：通过；
- `git diff --check`：通过。

### PostgreSQL / Redis integration

- 使用仅监听 loopback、tmpfs 存储的专用 PostgreSQL/Redis 测试容器；
- L5/L6/Recovery 产品联跑纳入最终全量门禁；
- 全量 integration：`397 passed, 1 xfailed, 2381 deselected in 668.11s`；
- Alembic 单一 head：`20260728_0014`；
- 迁移 downgrade/upgrade、checkpoint restart、API 幂等、Redis Stream 与
  Legacy 回归均在同一全量集合中通过。

## 5. 残余限制

以下不是 L9-1 UI/API 适配可以安全绕过的缺口：

1. L5-PROD Safety/Doctor Review、L6-PROD Record 与 LangGraph Recovery
   已完成本地工程接线、真实服务测试和 Codex 独立复验；
2. L7/L8 产品轨道、真实模型/外部数据和专业准入仍未完成；
3. 尚未执行真实 full 稳定窗口、Legacy open=0 与有权运维回滚；
4. 因此 L9-4 仍未发布，Legacy 保留是迁移正确性要求。

因此不得把 `XUANHU_LANGGRAPH_PRODUCT_READY` 设为 true，不得进入 `full`，
不得删除 Legacy 主路由。
