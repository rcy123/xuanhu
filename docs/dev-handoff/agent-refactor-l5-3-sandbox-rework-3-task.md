# L5-3-R3 live/restore causal predicate 限定返工

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付 |
| 发布人 | Codex（工程项目经理） |
| 失败 R2 | `03ba9104a9d79924fb4d1241429161a8d80f989e`；保留，不 reset/覆盖 |
| R2 验收 | `ACC-20260722-027`；独立 Reviewer P0=0、P1=1、P2=1、P3=0 |
| 依据 | 原 L5-3/R1/R2 任务书；L5 准入包 §7.5、§7.7、§8；`DEC-20260722-020` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |

## 唯一目标

在原三个 L5-3 文件内，让 live stage/apply 与 restart restore 共用同一组可达历史因果不变量：live 操作不得生成自身 snapshot validator 会拒绝的历史；restart 不得接受 live 原子状态机不可能生成的“挑战已 applied 后才 stage 另一个 sealed attempt”历史。

R2 已关闭的 full sealed-attempt binding、single-use cardinality、current-marker-first eligibility 与单 attempt 时间因果回归必须保持。不实现 L5-4 stale 写入或 full safety recheck。

## 必须先红

在 production 仍为 `03ba9104a9d79924fb4d1241429161a8d80f989e` 行为时新增并运行以下回归，记录真实 RED：

- `test_l5_3_live_stage_and_apply_reject_predecessor_clock_without_mutation`
  - challenge 在 `T` issued 后把 fake clock 回拨到 `T-1`；stage 必须 fixed chainless reject，且 challenge/checkpoint/attempt/event/transitions 不变，`snapshot()` 仍可 restore；
  - attempt 在 `T+10` staged 后把 fake clock 回拨到 `T+9`；apply 必须 fixed chainless reject，attempt 仍 sealed、challenge issued、checkpoint pending、event 为空，`snapshot()` 仍可 restore。
- `test_l5_3_restart_snapshot_rejects_attempt_staged_after_challenge_applied`
  - 合法 stage 两个 attempts、apply 其中一个；把 loser 的 staged transition 移到 winner 的 `review_applied` transition 之后，并同步重排 sequence/重算全部 derived refs；restore 必须 fixed chainless reject。

可增加一项精确回归证明成功 apply 的三条 transition 顺序连续、且同 challenge 的所有 stage transition 都位于第一个 applied transition 之前。禁止先改 production、skip、xfail、弱化原 55 项或只依赖 stale-ref 拒绝。

## 必须修复

### 1. live lower-bound causality

- stage 在任何 mutation 前同时验证 `issued_at <= now < expires_at`；`now < issued_at` 返回固定、无 cause/context 的失败，不得改变 attempt、challenge、checkpoint、event 或 transitions；
- apply 在任何 mutation 前从权威 append-only history 定位该 attempt 唯一的 staged transition，并验证 `staged_at <= now < expires_at`；`now < staged_at` 同样不得产生任何部分 mutation；
- 每次成功 live stage/apply 后产生的 snapshot 必须通过同一实现版本的 restore validator；不得依靠 fake clock 全局单调假设。

### 2. cross-attempt sequence causality

- 对一个 applied challenge，所有 attempts 的 staged transition sequence 都必须早于 winner 的第一个 applied transition；否则该历史不是 live store 可达历史，restore 必须拒绝；
- winner 的 `attempt_applied`、`checkpoint_applied`、`review_applied` transition 必须保持 live 原子提交的固定顺序与连续 sequence，且与 event/action/binding/cardinality 不变量同时成立；
- 跨 attempt 不要求时间戳全局单调：fake clock 可回拨；这里使用 append-only sequence 表达“challenge applied 后不再允许 stage”的状态机因果。winner 自身仍满足 `issued_at <= staged_at <= applied_at < expires_at`。

### 3. 单一因果谓词

- 把 live mutation 前置校验与 snapshot restore 校验收敛到命名清楚的共享 helper/谓词或等价单一实现，避免两套条件再次漂移；
- 所有公开失败继续使用既有 fixed chainless `SandboxReviewError` 边界；不泄漏动态时间、对象 repr、nonce、签名或内部异常。

## 允许范围与门禁

只允许修改：

- `app/agent_runtime/sandbox_review.py`
- `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

原任务书/R1/R2 的 fake env、`UV_OFFLINE=1`、L5-3/L5-2/L5-1/Safety/privacy/Ruff/mypy/L0、强制与校准全量、lock/diff/scope/tracked/clean 门禁全部沿用。不得改 PM 文档、任务书、配置、依赖、accepted L5-1/L5-2、Runtime、Legacy 或 L5-4。

## 交付

handoff 追加 R3 RED/GREEN、两项根因 closure、原 55 项回归与完整门禁。创建单一 R3 提交，exact parent 为本任务发布后的 clean HEAD 且只含三个允许文件；只能声明“R3 已交付，申请验收”。
