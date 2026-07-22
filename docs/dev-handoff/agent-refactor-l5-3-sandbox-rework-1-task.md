# L5-3-R1 expiry、restart integrity 与 current authority 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 原 release | `5c54b038dff9eefc060efbdbe8a5356279b4ea7f` |
| 失败 delivery | `99a1fb822a3963a9f324232e3be465c6835694b9`；保留，不 reset/覆盖 |
| 失败验收 | `ACC-20260722-025`；独立 Reviewer P0=0、P1=3、P2=1、P3=0 |
| 依据 | 原 L5-3 任务书；L5 准入包 §7.5、§7.7、§8、§8.1；`DEC-20260722-018` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-3 文件内关闭四项已复现 finding：到期时间必须为 exclusive boundary；restart snapshot 必须验证 derived refs 与跨记录一致性；同 namespace/session/thread 的新 authority 发布后旧 checkpoint 不再 eligible；任何进入边界的异常必须归一化为 cause/context 均为空的固定错误。

不得重写状态机、放宽原测试、删除失败历史、扩大到 L5-4 full recheck、真实 Runtime/DB/identity 或外部系统。

## 必须先红

在生产代码仍为 `99a1fb8` 时先新增并运行以下四个测试；必须观察对应错误行为，禁止先改生产代码、skip 或 xfail：

- `test_l5_3_exact_expiry_is_rejected_during_stage_and_resume`
  - stage 分支：fake clock 恰好等于 `expires_at`，必须预期 reject/tombstone；旧代码错误 staged；
  - resume 分支：在到期前 stage，随后 fake clock 恰好等于 `expires_at`，必须预期 reject/tombstone/零 event；旧代码错误 applied。
- `test_l5_3_restart_snapshot_rejects_changed_event_action_and_derived_refs`
  - applied `reject` 后仅改 snapshot event action 为 `confirm`，保留旧 `event_ref`；restore 必须 fixed chainless reject；旧代码错误接受并把 eligibility 从 blocked 变 eligible；
  - 至少再分别改变 event/attempt/transition/source 的 derived ref 或关键跨引用，restore 全部拒绝，不能靠单一 event 特判。
- `test_l5_3_new_current_authority_blocks_prior_checkpoint_eligibility`
  - 同 namespace/session/thread 先 applied v7/checkpoint-1，再发布 v8/checkpoint-2；旧 checkpoint 必须 blocked，新 checkpoint pending 时也 blocked；旧代码错误保留旧 eligible。
- `test_l5_3_injected_review_error_is_normalized_without_cause_or_context`
  - fake dependency 抛出带嵌套 cause/context 的 `SandboxReviewError`；边界最终异常必须为新固定 `SANDBOX_REVIEW_REJECTED`，`__cause__ is None` 且 `__context__ is None`，消息不含嵌套原文；旧代码 bare re-raise 泄露两条链。

真实 RED 数字、每项失败摘要和当时 exact HEAD 必须写入 handoff。

## 必须修复

### 1. exclusive expiry

- `expires_at` 是 exclusive upper bound；`now >= expires_at` 在 stage 与 resume 两处都原子写 expired tombstone 并 fixed reject；
- exact boundary、`+1`、fake clock 回拨均不得恢复；checkpoint 不推进、event 不追加；
- 不改变精确 900 秒 TTL，不等待 wall clock。

### 2. restart snapshot integrity

- restore 前 strict 深重建并验证所有 derived refs；至少覆盖 source、attempt、event、transition，ref 必须由各自 canonical body 重算一致；
- snapshot 必须验证集合内 ref 唯一、checkpoint/challenge/source/attempt/event 引用存在且一对一、event action/identity/signed payload digest 与 sealed attempt 一致、event 的全部 challenge authority 字段与其 challenge 一致；
- 任一缺失、extra、重复、改序导致的 append-only 不一致或跨记录 mismatch 必须在 store 可用前 fixed chainless reject；不得以重新计算一个新 ref 自动修补输入；
- canonical bytes 必须诚实；不通过隐藏字段伪造完整性。

### 3. current authority eligibility

- store 必须在同一 lock/transaction 内维护每个 `(namespace, test_session_id, thread_id)` 的当前 challenge/checkpoint authority；新 challenge 成功 issue 时原子成为 current；
- eligibility 除原 exact binding/confirm 条件外，必须要求请求 checkpoint 正是该 scope 的 current authority；旧 checkpoint 即使曾 applied confirm 也返回 blocked；
- current marker 必须可被 fake restart 精确恢复并纳入 snapshot integrity；missing/duplicate/mismatch 时 fail closed；
- 本 R1 只更新 completion eligibility，不执行 L5-4 的 stale 写标记、formula 修改或规则重检。

### 4. fixed chainless normalization

- 所有公开 boundary 均不得 bare re-raise 外部/注入的 `SandboxReviewError`；必须在离开 active exception context 后创建新的固定异常；
- create/restore 路径的最终 `SandboxReviewError` 必须同时满足 cause/context 为空；stage/resume/eligibility 继续只返回固定 DTO；
- 不记录或返回异常原文、fixture、nonce、signature 或 payload。

## 不变量与原回归

- `ALLOW` 才可 issue；`BLOCK` 在 nonce/store write 前拒绝；
- challenge 全绑定、32-byte nonce only-once、secret-free、ref-only command、64 KiB、32 并发恰好一次、fake restart 不重发、missing records 不重建、deep-copy isolation、append-only、non-confirm blocked 全部保持；
- 原 35 项必须全绿；新增四项不得合并成弱参数断言；
- 生产 imports/外部边界、三文件 scope、fake env 与 full-suite 校准规则完全沿用原任务书。

## 允许修改范围

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

除此之外全部禁止。不得修改本任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy 或 L5-4。

## 验收门禁

沿用原任务书完整 fixed fake env 与 `UV_OFFLINE=1`：

```powershell
uv run pytest tests/test_l5_3_sandbox_reviewer_interrupt_resume.py -q -rs
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q -rs
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run pytest tests/test_l4_5_11_1_intake_privacy_projection.py tests/test_l4_5_11_2_runtime_privacy_guard.py -q -rs
uv run ruff check app/agent_runtime/sandbox_review.py tests/test_l5_3_sandbox_reviewer_interrupt_resume.py
uv run mypy app/agent_runtime/sandbox_review.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv run pytest -q -rs
uv lock --check
git diff --check
```

强制 `APP_ENV=sandbox-test` 的唯一既有 defaults 冲突原样记录；另运行只移除 `APP_ENV` 且保留所有 fake endpoints/`UV_OFFLINE=1` 的校准全量。

## 交付

创建单一 R1 开发提交，exact parent 必须为本任务书发布后的 clean management HEAD，提交只含原三个允许文件。handoff 追加 R1 RED/GREEN、四 finding 关闭、原回归、双全量、scope/tracked/clean 与限制。只能声明“R1 已交付，申请验收”，不得自称 accepted；L5-4 仍未发布。
