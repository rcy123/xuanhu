# L5-3/4-R7 fixed review schema restore authority 限定返工任务书

> 状态：已发布 / 待交付
> 发布日期：2026-07-22
> 依据：`ACC-20260722-040`、`DEC-20260722-033`
> 执行起点：包含本任务书与 final R2 失败记录的 clean exact management release HEAD

## 唯一目标

在 L5-3 shared snapshot restore 入口建立唯一 fixed review schema authority：每个恢复 challenge 的 `sandbox_schema_version` 必须等于当前 live issue 路径使用的单一模块常量。L5-4 继续通过构造 L5-3 store 消费该判断，不增加重复 production 特判。

本任务不实现 schema migration、registry、negotiation、多版本兼容或数据升级。当前唯一支持值仍是 `sandbox-review-challenge.v1`。

## 根因与必须先红

R6 只证明 L5-4 outer child 等于 parent。final R2 证明 initial-only 状态可把整个 L5-3 applied snapshot 从固定 v1 协调改为 v2，重派生 challenge、attempt、event、transition refs 与 checkpoint/current 绑定后，L5-3 store 和 L5-4 coordinator 均接受。

修改 production 前必须在 exact R6 代码上留下可重复 RED：

1. **L5-3 pending/applied 矩阵**：对真实 live snapshot 协调改变 challenge schema，并按 canonical authority 重算所有受影响 refs/绑定；旧 store restore 接受，测试期望 fixed `SANDBOX_REVIEW_REJECTED`。
2. **L5-4 initial-only composition**：把真实 `modify_applied` 初始 review snapshot 作同样完整协调重派生；旧 coordinator restore 接受，测试期望 fixed `SANDBOX_RECHECK_REJECTED`。
3. **结构与正例**：证明 fixed schema 检查位于 L5-3 shared challenge restore loop、早于各 challenge state 分支；未修改 v1 的 pending/expired/applied snapshot 与 L5-4 多 revision chain 均可 round-trip。

RED 不得依赖 stale ref、单侧字段修改、删除记录、skip、xfail、动态异常文本或读取外部数据。

## 最小修复合同

- L5-3 live issue 与 snapshot restore 必须共用同一个模块级 fixed schema constant；不得复制另一个字符串来源。
- `_snapshot_is_integral` 对每个 challenge 无条件验证 fixed schema，不能只在 applied/pending/expired 某一状态分支内验证。
- mismatch 必须在构造 store/coordinator 时 fixed、chainless 拒绝；输入 snapshot 字节级不变，不产生 operation 或部分状态。
- L5-4 production 不修改；它必须通过 `SandboxInMemoryReviewStore(snapshot=...)` 自动继承下层判断。
- R6 child==parent guard、L5-3 R1～R5 因果/顺序/current 不变量和全部正常 live 行为保持。

## 允许修改范围

只允许修改以下 5 个文件：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `tests/test_l5_4_sandbox_modify_full_recheck.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`
- `docs/dev-handoff/agent-refactor-l5-4-sandbox.md`

不得修改 `sandbox_recheck.py` production、accepted L5-1/L5-2、配置、依赖、锁文件、PM 六台账或本任务书。五文件外任一改动都必须停止并交回项目经理。

## 门禁与交付

- 新回归加入后 L5-3 专项不得少于 `61` 项，L5-4 专项不得少于 `51` 项；既有 `59/50` 全部保持。
- L5-1/L5-2、Safety、privacy、Runtime/Legacy/public、AST、Ruff、mypy、L0、lock、双全量、diff/scope/tracked/clean 全部重跑。
- 全部 fixture 固定虚构/合成、inline、offline、in-memory；不读取 `.env`、ignored `data/`、`.codex_tmp`，不访问外部资源或启动服务。
- 单一开发提交，exact parent 必须为包含本任务书的 clean management release；提交只含上述 5 个 tracked 文件，handoff 同时记录 RED/GREEN、门禁、限制与回退。
- R7 独立 Reviewer/CI/PM 通过后创建新的 shared acceptance 管理提交；随后仍须从新 clean exact HEAD 执行全新的最终组合 Reviewer/CI/PM，不能复用 final R2 结果；L6 保持未发布、未开始。
