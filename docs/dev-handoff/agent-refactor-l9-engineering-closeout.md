# L9 工程收口：API/UI、产品闭环与回滚控制面

## 当前结论

L9 的本地工程范围已经形成完整闭环：

- L9-1 API/SSE/WebUI v2 适配；
- L9-2/L9-3 staged rollout、rollback 与排空控制面；
- L5-PROD Safety/Doctor Review；
- L6-PROD deterministic Record；
- LangGraph checkpoint Recovery；
- 独立 PostgreSQL/Redis/checkpoint 与全量回归。

状态为 **本地工程 accepted / 真实发布 external_gate**。三个 Codex 子 agent
分别覆盖 L5、L6 与 Recovery/rollout，经历初审、R1 与最终复验后均
`ACCEPT`，P0/P1/P2/P3=`0/0/0/0`。剩余阻塞不是已知本地代码失败，而是真实
full 稳定窗口、Legacy 排空、运维回滚，以及 L7/L8 产品/外部专业准入。
默认开关不变，L9-4 未发布。

## 最终工程证据

- 产品联跑、Recovery API、phase audit/check 与 PostgreSQL 往返均纳入最终全量门禁。
- 全量非 integration：`2381 passed, 398 deselected in 168.87s`。
- 全量 integration：`397 passed, 1 xfailed, 2381 deselected in 668.11s`。
- 前端：`24 files / 187 tests passed in 53.61s`；typecheck、lint、build 通过。
- 静态：全仓 Ruff；`mypy app scripts` 175 files；lock、diff 通过。
- 数据库：Alembic 单一 head `20260728_0014`；request_more_info downgrade/upgrade
  与已有数据组合通过。
- 门禁校准史：先后排除 `D:\tmp` CWD/env 噪声与 performance 环境泄漏；一个真实
  `advance` preflight-order 问题经一行修复和定向 `4 passed` 后，随后
  `final2` 非 integration 与 integration 全绿。
- 环境清理：精确 inspect 后删除 `xuanhu-l9-rework-pg` 与
  `xuanhu-l9-rework-redis`；`docker ps -a` 精确过滤均为 0；两容器无 bind/volume，
  tmpfs 数据不可恢复、容器可重建，未触碰其他容器或卷。

## 发布边界

- `AGENT_RUNTIME_VERSION` 默认 `legacy`。
- `XUANHU_LANGGRAPH_PUBLIC_ENABLED=false`。
- `XUANHU_LANGGRAPH_PRODUCT_READY=false`。
- `AGENT_RUNTIME_ROLLOUT_PHASE=legacy`。
- 不删除 Legacy，不修改既有会话的 runtime，不把 Recovery 作为跨 runtime fallback。
- 不构成真实患者、临床、公网、商业、机构或人体研究批准。
- `check_runtime_rollout --require-stable-minutes` 现可机器校验受审计 full 切流的
  最短存续时间；具体分钟数必须来自有权发布策略，本地实现不会自定或缩短。
