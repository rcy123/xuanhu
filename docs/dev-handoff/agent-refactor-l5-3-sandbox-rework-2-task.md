# L5-3-R2 sealed-attempt、single-use 与 causal restore 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败 R1 | `f5b7211a51418f2cd09348fc60c993576568a5b2`；保留，不 reset/覆盖 |
| R1 验收 | `ACC-20260722-026`；独立 Reviewer P0=0、P1=3、P2=1、P3=0 |
| 依据 | 原 L5-3/R1 任务书；L5 准入包 §7.5、§7.7、§8；`DEC-20260722-019` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-3 文件内关闭 R1 独立复审留下的四项 finding：sealed attempt ref 必须绑定完整 attempt body；每个 single-use challenge 最多一个 applied attempt/event；复用 checkpoint ID 时 eligibility 必须经 current marker 定位当前 interrupt；restore history 必须满足 issued/staged/applied 的 exclusive-expiry 时间因果。

R1 已关闭的 exact expiry、原 event-only tamper、distinct-checkpoint current marker 与 chainless error 回归必须保持。不实现 L5-4 stale 写入或 full recheck。

## 必须先红

在 production 仍为 `f5b7211` 行为时新增并运行以下四项，记录真实 RED：

- `test_l5_3_restart_snapshot_rejects_coordinated_attempt_and_event_action_change`
  - 从 applied `reject` snapshot 同时把 sealed attempt/event action 改为 `confirm`，只重算 event ref；restore 必须 fixed chainless reject；旧 R1 错误接受并令 eligibility 变 eligible。
- `test_l5_3_restart_snapshot_rejects_two_applied_attempts_for_one_challenge`
  - 同一 challenge 合法 stage 两个不同 action attempt、应用一个；构造第二个 coherently derived applied attempt/event/transitions；restore 必须拒绝，不得因 set 去重而接受两个 applied。
- `test_l5_3_reused_checkpoint_id_resolves_current_interrupt_eligibility`
  - 同 scope 的 v7/cp-001/int-001 confirm 后，再发布并应用 v8/cp-001/int-002；eligibility(cp-001) 必须通过 current marker 定位 v8 并返回 eligible；旧 R1 因先得到两个 checkpoint row 而错误 blocked。
- `test_l5_3_restart_snapshot_rejects_noncausal_stage_and_apply_times`
  - 至少覆盖 staged `< issued_at`、staged `>= expires_at`、applied `< staged`、applied `>= expires_at`；同步重算 event/transition refs 后 restore 仍必须拒绝。

禁止先改 production、skip、xfail、弱化原 48 项或只测试 stale ref。

## 必须修复

### 1. sealed attempt body binding

- `resume_attempt_ref` 必须由 `_SealedAttemptV1` 除 ref/state 外的完整 canonical authority body派生，至少直接绑定 challenge/source/scope/session/action、全部 test identity 与 signed payload digest；
- state 可从 sealed 原子转为 applied 而不改变 authority ref；除 state 外任一字段变化且 ref 未同步必须 schema/integrity reject；
- event 与 attempt 的跨记录 action/identity/digest 一致性继续验证；不得保存 plaintext nonce/signature。

### 2. single-use cardinality

- snapshot integrity 必须按 challenge 计数而非只比较 set；每个 challenge 精确允许零或一个 applied attempt；
- challenge/checkpoint 为 applied 时必须精确一个 applied attempt、一个 event、每类 applied transition 各一条；issued/expired 时不得存在 applied attempt/event；
- 可以在 applied 前存在多个合法 sealed attempts，但一旦一个成功，其余永远 sealed/replayed，不得在 restore 中表示为第二个 applied；
- 原 32 并发继续精确 `1 applied + 31 replayed_or_conflict`。

### 3. current-marker-first eligibility

- eligibility 先按 `(namespace, test_session_id, thread_id)` 取得唯一 current marker，再验证 caller `checkpoint_id` 等于 marker，最后用 marker 的 issue sequence/challenge ref 定位精确当前 checkpoint；
- 不得先按 checkpoint ID 搜索并要求全历史唯一；相同 checkpoint ID、不同 interrupt 的 append-only 历史合法；
- current applied confirm eligible；旧 interrupt 即使同 checkpoint ID 也不得被选中；current pending/reject/modify 仍 blocked。

### 4. restore temporal causality

- initial transition 必须等于 `issued_at`；每个 staged transition 必须满足 `issued_at <= staged_at < expires_at`；
- applied event 与三条 applied transition 必须同一时间，且 `staged_at <= applied_at < expires_at`；
- 时间变化即使重算全部 derived refs 也不得通过；fake clock 不要求单调全局，只验证每个 challenge/attempt 内因果；
- live stage/apply 的 `now >= expires_at` tombstone 行为保持。

## 允许范围与门禁

只允许修改：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

原任务书/R1 的 fake env、`UV_OFFLINE=1`、L5-3/L5-2/L5-1/Safety/privacy/Ruff/mypy/L0、强制与校准全量、lock/diff/scope/tracked/clean 门禁全部沿用。不得改 PM 文档、任务书、配置、依赖、accepted L5-1/L5-2、Runtime、Legacy 或 L5-4。

## 交付

handoff 追加 R2 RED/GREEN、四项 closure、原 48 项回归与完整门禁。创建单一 R2 提交，exact parent 为本任务发布后的 clean HEAD且只含三个允许文件；只能声明“R2 已交付，申请验收”。
