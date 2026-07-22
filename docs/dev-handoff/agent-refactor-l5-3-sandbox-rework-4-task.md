# L5-3-R4 event/transition append-order 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败 R3 | `605f32466b71c6f6a0f1a41ece5fd3eb3d0ac12b`；保留，不 reset/覆盖 |
| R3 验收 | `ACC-20260722-028`；独立 Reviewer P0=0、P1=0、P2=1、P3=0 |
| 依据 | 原 L5-3/R1/R2/R3 任务书；L5 准入包 §7.5、§7.7、§8；`DEC-20260722-021` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-3 文件内关闭 R3 唯一 P2：snapshot restore 必须证明 append-only review event 的全局应用顺序与 `review_applied` transition 的全局应用顺序一致，拒绝由反转 events、重编号 sequence 并重算 refs 构造的 live-unreachable 历史。

R3 已关闭的 live/restore causal predicate、cross-attempt stage/apply sequence、非全局单调 fake clock，以及 R2/R1/初始全部 finding 回归必须保持。不实现 L5-4。

## 必须先红

在 production 仍为 `605f32466b71c6f6a0f1a41ece5fd3eb3d0ac12b` 行为时新增并运行：

- `test_l5_3_restart_snapshot_rejects_event_order_opposite_review_applied_order`
  - 在同一 store 中依次成功 apply 两个合法 challenges A、B；
  - 保持 transitions 原样，把 events 反转，按新位置重编号 `event.sequence` 并重算各自 `event_ref`；
  - restore 必须以固定、无 cause/context 的 `SandboxReviewError` 拒绝；R3 应真实 RED 为 DID NOT RAISE。

同时增加或扩展正例，证明两个 apply 的正常 live snapshot 可 restore，且两个 challenges 使用跨 attempt 回拨时间仍合法；禁止 skip、xfail、弱化原 57 项或依赖 stale event ref 拒绝。

## 必须修复

- `events` 已按连续 `event.sequence` 排序，`transitions` 已按连续 `transition.sequence` 排序；restore 必须精确比较两侧的 applied attempt 顺序：

  ```python
  tuple(event.resume_attempt_ref for event in events) == tuple(
      transition.resume_attempt_ref
      for transition in transitions
      if transition.to_state == "review_applied"
  )
  ```

- 现有 cardinality、逐 event authority/ref、每 challenge 精确一个 event 与 winner 连续三 transition 校验必须先保持；顺序比较不能替代这些不变量；
- 不比较不同 events、challenges 或 attempts 的时间戳，不引入 fake clock 全局单调；顺序 authority 只来自同一 store lock 下的 append-only sequence；
- 失败继续走既有 fixed chainless create/restore 边界，不泄漏动态对象、时间、nonce、签名或内部异常。

## 允许范围与门禁

只允许修改：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

原任务书/R1/R2/R3 的 fake env、`UV_OFFLINE=1`、L5-3/L5-2/L5-1/Safety/privacy/Ruff/mypy/L0、强制与校准全量、lock/diff/AST/scope/tracked/clean 门禁全部沿用。不得改 PM 文档、任务书、配置、依赖、accepted L5-1/L5-2、Runtime、Legacy 或 L5-4。

## 交付

handoff 追加 R4 真实 RED/GREEN、唯一 P2 closure、原 57 项回归与完整门禁。创建单一 R4 提交，exact parent 为本任务发布后的 clean HEAD 且只含三个允许文件；只能声明“R4 已交付，申请验收”。
