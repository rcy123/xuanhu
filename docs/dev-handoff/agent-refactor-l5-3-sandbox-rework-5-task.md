# L5-3-R5 sequenced append-projection 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败 R4 | `1f8503e0c28dd19dcc48b6e63b3e5103ce9adeda`；保留，不 reset/覆盖 |
| R4 验收 | `ACC-20260722-029`；独立 Reviewer P0=0、P1=0、P2=1、P3=0 |
| 依据 | 原 L5-3/R1～R4 任务书；L5 准入包 §7.5、§7.7、§8；`DEC-20260722-022` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-3 文件内关闭 R4 唯一 P2，并完成有显式 sequence 的持久 append collections 到 transition log 的顺序投影：challenge/source/checkpoint 的 issue 顺序必须精确等于 `decided → review_pending` 初始 transition 顺序；R4 已实现的 event 顺序到 `review_applied` transition 顺序必须保持。

没有显式 sequence、按唯一 ref/key 寻址的 attempts 与 current-authorities 不在本轮被重新定义为有序日志；继续使用其既有 uniqueness、cross-reference、cardinality 与 current-marker 不变量。不得实现 L5-4。

## 必须先红

在 production 仍为 `1f8503e0c28dd19dcc48b6e63b3e5103ce9adeda` 行为时新增并运行：

- `test_l5_3_restart_snapshot_rejects_initial_transition_order_opposite_issue_order`
  - 在同一 store 中依次 issue 两个 pending challenges A、B；
  - 保持 sources/challenges/checkpoints 顺序不变，只反转两条 `decided → review_pending` 初始 transitions，按新位置重编号 `transition.sequence` 并重算 `transition_ref`；
  - restore 必须以固定、无 cause/context 的 `SandboxReviewError` 拒绝；R4 应真实 RED 为 DID NOT RAISE。

正例必须证明正常 A/B issue snapshot 可 restore，并允许 B 的 `issued_at` 小于 A（fake clock 跨 challenge 回拨）。禁止 skip、xfail、弱化原 58 项、依赖 stale ref 或引入全局时间单调。

## 必须修复

- `sources`、`challenges`、`checkpoints` 已由连续 `issue_sequence` 和 zip/cross-reference 固定同一 issue 顺序；restore 必须精确比较：

  ```python
  tuple(challenge.challenge_ref for challenge in challenges) == tuple(
      transition.challenge_ref
      for transition in transitions
      if transition.resume_attempt_ref is None
      and transition.from_state == "decided"
      and transition.to_state == "review_pending"
  )
  ```

- 保留 R4 的 `events` → `review_applied` applied-attempt 顺序等式，并保留 transition/event 自身连续 sequence、derived refs、逐 challenge 初始 transition 精确一条、全部 cardinality/authority 校验；
- 明确完成 sequenced collection audit：issue projection 与 apply projection 是全部跨 collection 显式序列映射；attempts/current-authorities 无显式 sequence，顺序不作为 authority，不新增无合同依据的排序；
- 不比较跨 challenge/event/attempt 时间戳；顺序 authority 只来自同一 store lock 的 append sequence；
- 失败继续走 fixed chainless create/restore 边界。

## 允许范围与门禁

只允许修改：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

原任务书/R1～R4 的 fake env、`UV_OFFLINE=1`、L5-3/L5-2/L5-1/Safety/privacy/Ruff/mypy/L0、强制与校准全量、lock/diff/AST/scope/tracked/clean 门禁全部沿用。不得改 PM 文档、任务书、配置、依赖、accepted L5-1/L5-2、Runtime、Legacy 或 L5-4。

## 交付

handoff 追加 R5 真实 RED/GREEN、唯一 P2 closure、sequenced collection audit、原 58 项回归与完整门禁。创建单一 R5 提交，exact parent 为本任务发布后的 clean HEAD 且只含三个允许文件；只能声明“R5 已交付，申请验收”。
