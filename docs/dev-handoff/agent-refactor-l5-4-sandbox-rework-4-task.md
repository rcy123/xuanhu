# L5-4-R4 completion exact-current source 闭合

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 初始～R3 失败 deliveries | `d5b8f0e`、`8b345b9`、`23a561a`、`b7cbbff`；全部保留，不 reset/覆盖 |
| 失败验收 | `ACC-20260722-035`；独立 Reviewer P0=0、P1=0、P2=1、P3=0 |
| 依据 | 原 L5-4/R1/R2/R3 任务书；`DEC-20260722-028` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-4 文件内让 restore current 校验与 `completion_eligibility()` 复用同一个 exact-current source predicate。合法 other-scope 同内容 source 必须既可 append-only 保留，也不能阻断 outer exact current confirm 后的 sandbox eligibility。

不得修改 R3 projection/current predicate；不得逐调用点复制字段子集；不得修改 accepted L5-1/L5-2/L5-3、配置、依赖、任务书、PM 台账或扩大到 Runtime/HTTP/DB/Gateway/Legacy/外部系统/L6。

## 必须先红

production 保持 `b7cbbff` 行为时，新增一个独立回归：

1. 构造 R3 已接受的 `initial -> other-scope -> exact current child` private issue 顺序并 restart；
2. 使用 exact current delivery 完成 stage 与 resume confirm，明确返回 `applied`；
3. 查询 outer exact namespace/session/thread/checkpoint 的 completion，旧实现必须真实得到 `blocked`，测试期望 `eligible` 因而 RED；
4. 同时断言错误 namespace、thread 或 checkpoint 仍为 `blocked`，other-scope source/challenge/current marker 仍 append-only 保留。

记录 exact R3 HEAD、真实 RED 数字与失败断言。不得先改 production，不得用删除 other-scope 记录、放宽 source 基数或只改测试期望得到通过。

## 必须修复

- 提取一个纯 exact source predicate，输入 private source record 与 outer revision；
- predicate 必须完整比较 namespace、test_session_id、thread_id、checkpoint_id、interrupt_id、`safety_subject`、`safety_result`，并要求 `explanation_result is None`；
- restore 的 current `review_required` source 定位与 `completion_eligibility()` 每次重读必须调用同一个 predicate，不保留第二套字段子集；
- completion 仍须对 exact marker/checkpoint/challenge/event 各自精确一个，并继续调用 accepted L5-3 eligibility；other scope 记录既不计入 exact source，也不被删除；
- fixed blocked 失败语义、无真实 complete/export API、R3 projection/current 与 R2/R1 不变量均保持。

## 原不变量与范围

- 新回归加入后完整 L5-4 专项不得少于 `44` 项；R3 `43` 项全部保持；
- accepted L5-3/L5-2/L5-1、Safety、privacy、L0、Ruff、mypy、AST、lock、双全量、diff/scope/tracked/clean 全部重跑；
- forced full 的既有 defaults 差异与任何未稳定复现结果必须原样记录；calibrated full 只移除 `APP_ENV`；
- 全部 fixture 固定虚构/合成、inline、offline、in-memory；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源。

## 允许修改范围

- `app/agent_runtime/sandbox_recheck.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

除此之外全部禁止。不得修改本 R4 任务书、PM 台账、accepted 前序任务、配置、依赖、锁文件或 L6。

## 验收与交付

沿用 R3 完整 fake env、`UV_OFFLINE=1` 和全部门禁。创建单一 R4 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，只含原三个允许文件。handoff 追加 RED/GREEN、shared exact-source predicate、完整门禁、限制与回退。执行者只能声明“L5-4-R4 已交付，申请验收”；独立 Reviewer 必须继续走到 confirm/eligibility，独立 CI 不复用 R3 结果；L5 保持 3/4，L6 未开始，直至最终关闭。
