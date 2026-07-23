# L5-3 Sandbox Reviewer interrupt/resume 开发交付

## 1. 交付状态

| 项目 | 内容 |
|---|---|
| 状态 | **已交付，申请验收**；不得据此自称 accepted、clinical approved 或 production ready |
| 管理发布 exact HEAD | `5c54b038dff9eefc060efbdbe8a5356279b4ea7f` |
| exact parent 的 parent | `7662915df0468e14924c2b3a979df445575a2bbd` |
| 已接受前置 | L5-2 delivery `1957ad311b3997499e4f9a0e3f2dd95aa652fa9e`；`ACC-20260722-023` |
| 任务依据 | `agent-refactor-l5-3-sandbox-task.md`；L5 准入包 §7.1、§7.2、§7.5、§7.7、§8、§8.1；`DEC-20260722-017` |
| 交付类型 | 纯离线、strict、immutable、single-use、restartable reviewer interrupt/resume domain state machine |

本交付只实现隔离的 synthetic sandbox review transport、in-memory domain store 与 eligibility probe。它不接入真实 LangGraph `Command`、MainGraph、HTTP、数据库、身份系统、Runtime、Legacy 或外部服务，也不执行 completion、record、export 或不可逆业务效果。

## 2. 冻结范围与实际 diff

相对 release exact HEAD 只新增三个允许文件：

```text
A app/agent_runtime/sandbox_review.py
A tests/test_l5_3_sandbox_reviewer_interrupt_resume.py
A docs/dev-handoff/agent-refactor-l5-3-sandbox.md
```

没有修改 accepted L5-1/L5-2、任务书、PM 台账、配置、依赖、migration、feature flag、应用/HTTP/容器/部署/数据库、Gateway、Runtime、Legacy 或现有 tests。

## 3. 测试先行与真实 RED/GREEN

在 clean exact release HEAD 上先新增专项测试，此时生产模块不存在。完整 fake 环境下首次运行：

```powershell
uv run pytest tests/test_l5_3_sandbox_reviewer_interrupt_resume.py -q -rs
```

真实 RED 为退出码 `2`，`collected 0 items / 1 error`：

```text
ModuleNotFoundError: No module named 'app.agent_runtime.sandbox_review'
```

没有 skip、xfail、动态替身或先写生产代码。实现阶段保留了以下真实迭代：

1. 首次生产实现运行：`4 passed, 30 failed`；统一根因是构造 challenge ref 时 provisional DTO 被最终 ref validator 拒绝。修正为只在内部以未验证 draft 计算 ref，再构造并 strict 校验最终 challenge。
2. 第二次运行：`33 passed, 1 failed`；唯一失败是测试把名为 `before` 的 caller snapshot 原地绕过 frozen 标志修改后，又要求 store 的未修改深拷贝等于该已修改对象。测试改为保留原 `before` 并单独篡改 `caller_copy`，没有弱化事件字段相等性、frozen 或 store isolation。
3. 修正后：`34 passed`；Ruff 的测试 import 顺序与 mypy 的 typed constructor 展开问题均在允许文件内修正。
4. 主流程 pre-delivery 复核发现正常 fixture 原为 L5-1 `BLOCK`，违反 blocker 后 review 不可达。正常 authority 改为 `ALLOW + empty issues/rules/allowlist + digest-bound explanation_unavailable`；生产 create boundary 在 nonce factory、store read/write 前 strict 拒绝非 `ALLOW` result；新增指定 BLOCK zero-side-effect 回归。最终专项为 `35 passed`。

## 4. Authority、challenge 与 ref-only transport schema

所有 DTO 均为 Pydantic strict/frozen、`extra="forbid"`；nested collection 使用 tuple；传入 source、submission、command 与 store snapshot 均经 canonical JSON strict 重解析，store 保存和返回独立重建对象。

### 4.1 Accepted source

`SandboxReviewSourceV1` 精确绑定：

- accepted `SandboxSafetySubjectV1` 与 `SandboxSafetyResultV1`；可选 accepted `SandboxExplanationResultV1`；
- subject 完整 canonical SHA-256 `input_digest`，并要求等于 result `decision_subject_digest`；
- L5-1 `result_digest`、完整 result canonical digest、可选完整 explanation canonical digest；
- explanation `source_result_digest` 必须等于 L5-1 `result_digest`；
- adapter version 必须一致；`review_render_digest` 绑定 input/result canonical digest、explanation digest 与固定 synthetic technical summary；
- create boundary 只接纳 `SandboxSafetyDecision.ALLOW`。即使 BLOCK source 自身 schema/digest 全部有效，也固定、无 cause 地拒绝，nonce factory 与 store operation count 均为 0，challenge/checkpoint/event 为空，eligibility 为 blocked。

### 4.2 Challenge authority

`SandboxReviewChallengeV1` 的 SHA-256 authority/ref 绑定：

- review schema、adapter、graph version；namespace、test session、thread、checkpoint、interrupt ID；
- domain state version、formula revision；
- input、result、rule bundle、synthetic dataset、review render digest；
- 精确 actions `confirm/reject/modify_fixture`；
- injected integer fake-clock `issued_at/expires_at`，固定 TTL 900 秒；
- injected exactly 32-byte nonce 的 digest；
- 固定 `synthetic_safety_review_pending` technical summary。

challenge 不含 formula/profile/issue/explanation 原文、Prompt、凭据或真实/个人属性。plaintext nonce 只在首次 issue delivery 返回一次，`repr` 隐藏；重试与复用同一 store 的 fake restart 只恢复原 challenge/checkpoint 且返回 `None` nonce，不再次调用 nonce factory。31/33-byte factory 在任何 store mutation 前 fixed chainless error。

### 4.3 Submission、proof、sealed attempt 与 command

- `SandboxResumeSubmissionV1` 包含 namespace/session、完整 challenge、action、隐藏 plaintext nonce 与 strict proof；完整 canonical bytes 最大 `65,536`，`65,537` bytes 在 verifier/store/CAS 零调用前 fixed reject，不截断或部分接纳。
- `SandboxTestReviewProofV1` 字段集合精确为 sandbox test reviewer ID、固定 test role、固定 synthetic organization label、固定 non-credential qualification label、signature scheme/key ID、signed payload digest 与 signature；不存在 doctor/physician/clinician/license/credential-approved authority。
- signed payload digest 绑定完整 challenge、action、plaintext nonce 与全部 test identity/signature metadata。fake verifier 只接收 digest/scheme/key/signature；异常只得到 fixed payload-free rejection。
- store 内 sealed attempt 只保存 ref、challenge/source ref、namespace/session、action、test identity、scheme/key、signed payload digest 与 state；不保存 plaintext nonce 或 signature。
- `SandboxResumeCommandV1` strict/frozen 字段集合精确等于 `{resume_attempt_ref}`。coordinator 只按 ref 从 store 重读 attempt、challenge、checkpoint、source 与全部 authority；command/caller 不能自报 action、reviewer、digest、nonce 或 signature authority。

## 5. 原子状态机、CAS、restart 与 append-only event

`SandboxInMemoryReviewStore` 明确为 sandbox-only、thread-safe in-memory reference store，所有 CAS、checkpoint/challenge/attempt state replacement、transition 与 event append 都在同一 `RLock` 临界区完成：

```text
decided -> review_pending
issued -> claimed -> applied
review_pending -> review_applied
```

- 32 个线程并发 resume 同一 attempt，精确 `1 applied + 31 replayed_or_conflict`；只有一个 review event 与一个 `review_applied` transition。
- applied/replayed/missing ref、missing challenge/checkpoint、stale graph/state/formula/input/result/rule/dataset/render binding、wrong namespace/session/action/nonce/signature 全部 fail closed；checkpoint/event 不被错误推进，也不构造等价 challenge。
- fake clock 首次观察 `now > expires_at` 后在 lock 内写入 `expired` tombstone；clock 回拨也不能复活 nonce。
- fake restart 只需复用同一 store，不依赖 coordinator-local cache；原首次 delivery 的有效 nonce/proof 可从 exact recovered checkpoint 成功 stage/resume。
- events 是 immutable append-only tuple records，绑定全部 challenge authority/version/digest、safe action、attempt/challenge ref 与 test identity，但不含 nonce、signature、fixture 原文或 explanation 原文。caller 即使用 `object.__setattr__` 篡改 snapshot event/source，也不能改变 store 内 authority。

Canonical encoding 是完整、诚实、稳定的 sorted JSON，不做伪装式全局 redaction：store snapshot 合法保存 accepted source，因此包含 synthetic fixture/explanation 内容；whole-store secrecy 断言只排除 plaintext nonce 与 signature。更严格的 event secrecy 另外排除 nonce、signature、fixture 与 explanation 原文。

## 6. Eligibility、不可达能力与隔离限制

eligibility 是无副作用 probe。只有当前 exact authority 上已 applied 的 `confirm` event 返回 `eligible`；无 review、pending/expired/replayed/stale、`reject`、`modify_fixture` 或 BLOCK safety result 均为 fixed `blocked`。`modify_fixture` 只追加一个 test review event，保留原 formula revision，不创建新 revision 或调用规则。

模块没有定义或调用 `complete`、`record`、`export`；没有处方/医疗建议、真实签署、网络、文件持久化、DB、queue/outbox、LangGraph、Runtime 或 Legacy side effect。production import 仅为 Python 标准库、Pydantic、accepted `sandbox_safety` 与 `sandbox_explanation`。

## 7. 最终门禁证据

除 calibrated full suite 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake 环境与 `UV_OFFLINE=1`。

| 门禁 | 最终结果 |
|---|---|
| L5-3 专项 | `35 passed in 3.30s` |
| accepted L5-2 回归 | `18 passed in 9.09s` |
| accepted L5-1 回归 | `14 passed in 16.08s` |
| Safety 回归 | `71 passed, 3 deselected in 2.87s` |
| L4.5-11 privacy 回归 | `76 passed in 5.21s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.25s` |
| `uv lock --check` | `Resolved 84 packages in 3ms` |
| diff/scope/tracked | `git diff --cached --check` 无错误；cached name-status 精确为三个允许文件且均为 `A`；`git ls-files --error-unmatch` 对三者全部成功 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1691 passed, 362 deselected in 131.09s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1692 passed, 362 deselected in 130.04s` |

专项覆盖 challenge/ref/action/identity exact schema、每一 binding stale、nonce 单次交付、31/33-byte nonce、wrong nonce/signature/cross-session/namespace、expired tombstone、32-thread CAS、restart、missing store records、completion/export absent、modify_fixture blocked、65,537-byte early rejection、nested copy isolation、append-only/secret-free event、fixed chainless errors、forbidden imports，以及 BLOCK result 后 challenge/review 不可达。

## 8. 受控 fake 环境与禁止访问证明

```powershell
$env:APP_ENV='sandbox-test'
$env:DB_URL='postgresql://sandbox:sandbox@127.0.0.1:9/sandbox'
$env:REDIS_URL='redis://127.0.0.1:9/0'
$env:MODEL_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:MODEL_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:EMBEDDING_GATEWAY_BASE_URL='http://127.0.0.1:9/v1'
$env:EMBEDDING_GATEWAY_API_KEY='sandbox-test-key-not-a-secret'
$env:CHAT_MODEL='sandbox-test-model'
$env:EMBEDDING_MODEL='sandbox-test-embedding'
$env:EMBEDDING_DIM='8'
$env:AGENT_RUNTIME_VERSION='legacy'
$env:XUANHU_LANGGRAPH_PUBLIC_ENABLED='false'
$env:UV_OFFLINE='1'
```

没有读取或显示本地 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动应用、HTTP/E2E、容器、数据库、Redis、Milvus、模型/embedding Gateway、网络或真实 wall-clock wait。全部 review source 来自测试内 fixed-fictitious admitted synthetic fixture。

## 9. 限制、提交与回退

- 本交付仅用于个人学习、非临床、固定虚构 synthetic offline sandbox；不是临床、法律、隐私、伦理、监管或生产批准，不适用于真实个体、患者服务、商业/公开生产或人体研究。
- in-memory store 只模拟 restart，不提供进程外 durability；clock、nonce factory 与 signature verifier 都是 injected fake boundary。
- `modify_fixture` 不修改 fixture；L5-4 修改后全量重检、真实 completion/export、应用集成与外部身份/签署均不在范围内。
- 强制 `APP_ENV=sandbox-test` 的既有 defaults 冲突按合同原样保留；校准全量只移除该变量，没有修改范围外 test/config 制造通过。
- 单一开发提交消息为 `feat: add L5-3 offline sandbox review state machine`，exact parent 必须为 `5c54b038dff9eefc060efbdbe8a5356279b4ea7f`，提交只含第 2 节三个文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 exact parent、唯一提交消息、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- 若独立验收失败，使用 `git revert <delivery-exact-head>` 保留历史，不 reset 或覆盖 accepted L5-1/L5-2。

---

**已交付，申请验收。**

## 10. 项目经理第 1 轮独立验收（2026-07-22）

- 冻结交付：`99a1fb822a3963a9f324232e3be465c6835694b9`；exact parent `5c54b038dff9eefc060efbdbe8a5356279b4ea7f`；只含原合同三个文件；Review/CI 前后 clean。
- 独立 CI：专项 `35 passed`；L5-2 `18`、L5-1 `14`、Safety `71 passed, 3 deselected`、privacy `76`、L0 `131`、Ruff/mypy/lock/scope/tracked/clean 通过；校准全量 `1692 passed, 362 deselected`；强制 fake env 仅既有 APP_ENV defaults 冲突。
- 独立 Reviewer：P0=0、P1=3、P2=1、P3=0，结论 `rework required`。
- P1：`now == expires_at` 仍可 staged/applied；restart snapshot 的 event action 可在旧 event ref 下从 reject 改 confirm；新 authority 发布后旧 checkpoint 仍 eligible。
- P2：注入的 `SandboxReviewError` 可经 bare re-raise 保留 cause/context，不满足 fixed chainless contract。
- PM 固定 synthetic 探针逐项复现；自动化全绿不能替代未覆盖的边界语义。
- PM 结论：**未接受 / 发布 L5-3-R1 限定返工**（`ACC-20260722-025`、`DEC-20260722-018`）。保留本提交与全部证据；L5-4 不得发布。

R1 合同见 [agent-refactor-l5-3-sandbox-rework-1-task.md](agent-refactor-l5-3-sandbox-rework-1-task.md)。

## 11. L5-3-R1 开发交付（2026-07-22）

### 11.1 状态、基线与限定范围

- 状态：**R1 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R1 clean release / exact parent：`e3f1472d2956aa9a1e350938d28adc00e6b8d41f`；其 parent 为失败 delivery `99a1fb822a3963a9f324232e3be465c6835694b9`。原交付、`ACC-20260722-025`、`DEC-20260722-018`、第 1 轮 finding 与 R1 任务书全部保留，没有 reset、覆盖或删除失败历史。
- R1 只修改 `app/agent_runtime/sandbox_review.py`、`tests/test_l5_3_sandbox_reviewer_interrupt_resume.py` 与本文；没有修改 R1 任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy、HTTP/DB/Gateway 或 L5-4。

### 11.2 四项先行回归与真实 RED

在 production 仍为 `99a1fb8` 行为、worktree 唯一修改为四项指定 regression（restart integrity 使用 10 个明确参数子例）时，以第 8 节完整 fake env 与 `UV_OFFLINE=1` 运行专项。exact HEAD 仍为 `e3f1472d2956aa9a1e350938d28adc00e6b8d41f`；结果为退出码 `1`，`13 failed, 35 passed in 2.91s`，48 项全部收集：

1. `test_l5_3_exact_expiry_is_rejected_during_stage_and_resume`：exact `expires_at` 的实际结果为 `staged/applied`，而非两次 fixed rejection；
2. `test_l5_3_restart_snapshot_rejects_changed_event_action_and_derived_refs`：event action/ref、attempt ref、transition ref、source ref、duplicate event、transition reorder、missing challenge、event identity 与 missing current marker 共 10 个 tamper 全部 `DID NOT RAISE`；
3. `test_l5_3_new_current_authority_blocks_prior_checkpoint_eligibility`：发布同 scope v8/checkpoint-2 后，旧 v7/checkpoint-1 实际仍为 `eligible`；
4. `test_l5_3_injected_review_error_is_normalized_without_cause_or_context`：fake nonce/store dependency 抛出的 fixed error 保留 nested `ValueError` 作为 `__context__`。

RED 前没有修改 production、handoff 或范围外文件，没有 skip、xfail、条件分支或弱化原 35 项。实现后的首次专项为 `47 passed, 1 failed`；唯一失败是原 missing-record test 仍预期损坏 snapshot 可以先构造 store。R1 合同要求 restore 立即 fail closed，因此该原测试被加强为 constructor fixed chainless rejection；最终专项 `48 passed`。

### 11.3 Exclusive expiry 与 atomic tombstone

- stage/apply 均在 store lock 内使用 exclusive upper bound `now >= expires_at`；exact boundary 与 `+1` 都先替换为 `expired` tombstone，再 fixed reject；
- exact-expiry stage 不创建 attempt/event、不推进 checkpoint；到期前 staged、exact-expiry resume 保持 attempt sealed、checkpoint pending、event 为空；
- fake clock 回拨仍读取 persisted expired state，不能恢复 nonce；TTL 继续精确 900 秒，没有 wall-clock read 或 wait。

### 11.4 Restart integrity 与 derived refs

restore 先 canonical strict 深重建，再验证完整 snapshot；任一失败在离开 exception context 后创建新的 fixed `SandboxReviewError`，cause/context 均为空，不自动修补输入。R1 integrity 包括：

- stored source ref 绑定 issue sequence、scope、checkpoint/interrupt 与完整 accepted source；attempt ref 绑定 challenge ref 与 signed payload digest；event/transition ref 绑定各自除 ref 外的完整 canonical body；
- source/challenge/checkpoint 按连续 `issue_sequence` 一对一同序；event/transition 具有连续 append sequence，duplicate ref、missing/extra record、改序或断链均拒绝；
- checkpoint/challenge/source/attempt/event/transition 引用必须存在；attempt scope/source/challenge 必须一致；
- event 的 action、全部 test identity、signed payload digest 必须与 sealed attempt 一致；event 的全部 version/scope/state/formula/digest authority 必须与 challenge 一致；applied/sealed attempt、challenge/checkpoint/event/transition state 必须形成唯一一致历史；
- 每个 staged attempt 追加 bound `attempt_unstaged -> attempt_staged` transition；每个 applied attempt 仍只有一组 issued/claimed/applied 与 review_applied transition、一个 event。原 32-thread CAS 继续为精确 `1 applied + 31 replayed_or_conflict`。

Canonical JSON 保持完整诚实：没有通过隐藏 action、identity、source 或其他字段伪造 integrity。store 仍只排除 plaintext nonce/signature；event 继续额外排除 fixture/explanation 原文。

### 11.5 Current authority 与 fixed chainless boundary

- store 在 issue 的同一 lock/transaction 内维护 `(namespace, test_session_id, thread_id)` 唯一 `_CurrentAuthorityV1`，绑定 issue sequence、checkpoint 与 challenge；同 scope 新 issue 原子替换 current marker；
- snapshot integrity 要求每个有 checkpoint 的 scope 恰有一个 marker，且必须指向该 scope 最大 issue sequence；missing、duplicate、旧 marker 或 cross-ref mismatch 不能 restore；
- eligibility 除原 exact applied-confirm authority 外，还要求请求 checkpoint 与 current marker 精确一致；新 checkpoint pending 时新旧 checkpoint 都 blocked；fake restart 后 marker 语义不变；
- coordinator create 与 store restore 均吞并 injected/internal exception，仅在 active exception context 退出后抛出新的 `SANDBOX_REVIEW_REJECTED`；测试对 nonce factory 与 store dependency 的 nested cause/context 同时证明最终 `__cause__ is None`、`__context__ is None`，无嵌套原文；stage/resume/eligibility 继续只返回 fixed DTO。

本修复不改变 ALLOW-only admission、BLOCK zero mutation、32-byte nonce only-once、ref-only command、65,536-byte limit、secret-free、fake restart 不重发、non-confirm blocked 或 complete/record/export absent。

### 11.6 R1 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R1 结果 |
|---|---|
| L5-3/R1 专项 | 最终交付树 `48 passed in 2.50s`；前次 `48 passed in 2.43s` |
| accepted L5-2 回归 | `18 passed in 8.21s` |
| accepted L5-1 回归 | `14 passed in 15.75s` |
| Safety 回归 | `71 passed, 3 deselected in 2.29s` |
| L4.5-11 privacy 回归 | `76 passed in 5.88s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.20s` |
| `uv lock --check` | `Resolved 84 packages in 4ms` |
| diff/scope/tracked | working/cached diff check 均无错误；name-status 精确为三个原允许文件且均为 `M`；`git ls-files --error-unmatch` 对三者全部成功 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1704 passed, 362 deselected in 132.36s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1705 passed, 362 deselected in 130.79s` |

全部执行没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway、网络或外部服务。

### 11.7 R1 提交、限制与回退

- 单一 R1 开发提交消息为 `fix: close L5-3 review state invariants`，exact parent 必须为 `e3f1472d2956aa9a1e350938d28adc00e6b8d41f`，提交只含 11.1 节三个原允许文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 parent/message、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- in-memory snapshot integrity 是本地 reference-store structural integrity，不是外部持久化、加密签名、数据库 transaction 或生产 durability；真实 Runtime/identity/DB/HTTP 与 L5-4 full recheck 仍未实现、未发布。
- 若 R1 独立验收失败，使用 `git revert <r1-delivery-exact-head>` 保留历史，不 reset 或覆盖失败交付与 accepted L5-1/L5-2。

---

**R1 已交付，申请验收。**

## 12. 项目经理 R1 独立验收（2026-07-22）

- 冻结 R1：`f5b7211a51418f2cd09348fc60c993576568a5b2`；exact parent `e3f1472d2956aa9a1e350938d28adc00e6b8d41f`；三文件 scope 与 clean 正确。
- 独立 CI：专项 `48 passed`、校准全量 `1705 passed, 362 deselected`；强制 fake env 仅既有 defaults 冲突；回归、Ruff/mypy/L0/lock/AST/scope/tracked 全通过。
- 原四项复验：exact expiry、event-only stale ref、distinct-checkpoint current marker、nested cause/context 原复现 resolved。
- 独立 Reviewer：P0=0、P1=3、P2=1、P3=0；结论 `rework required`。attempt ref 未绑定完整 body；single-use restore 可有两个 applied；复用 checkpoint ID 的 current eligibility 误拒；restore 未验证 exclusive-expiry 时间因果。
- PM 结论：**R1 未接受 / 发布 L5-3-R2**（`ACC-20260722-026`、`DEC-20260722-019`）。保留第 1 次与 R1 全部失败/CI/Review 证据；L5-4 不得发布。

R2 合同见 [agent-refactor-l5-3-sandbox-rework-2-task.md](agent-refactor-l5-3-sandbox-rework-2-task.md)。

## 13. L5-3-R2 开发交付（2026-07-22）

### 13.1 状态、基线与限定范围

- 状态：**R2 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R2 clean release / exact parent：`aa9661200ec3d55c230ef32ec8c242c050990cf9`；其 parent 为失败 R1 delivery `f5b7211a51418f2cd09348fc60c993576568a5b2`。原 delivery、R1、两轮 finding、`ACC-20260722-025/026`、`DEC-20260722-018/019` 与 R1/R2 任务书全部保留，没有 reset、覆盖或删除失败历史。
- R2 只修改原三个文件：`app/agent_runtime/sandbox_review.py`、`tests/test_l5_3_sandbox_reviewer_interrupt_resume.py` 与本文；没有修改 R2 任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy、HTTP/DB/Gateway 或 L5-4。

### 13.2 四项先行回归与真实 RED

在 production 仍为 `f5b7211` 行为、worktree 唯一修改为四项指定 regression（causal restore 使用四个明确参数子例）时，以第 8 节完整 fake env 与 `UV_OFFLINE=1` 运行专项。exact HEAD 仍为 `aa9661200ec3d55c230ef32ec8c242c050990cf9`；结果为退出码 `1`，`7 failed, 48 passed in 2.84s`，55 项全部收集：

1. `test_l5_3_restart_snapshot_rejects_coordinated_attempt_and_event_action_change`：同时把 applied reject attempt/event action 改为 confirm 并重算 event ref 后，restore `DID NOT RAISE`；
2. `test_l5_3_restart_snapshot_rejects_two_applied_attempts_for_one_challenge`：同 challenge 两个不同 action attempt 被构造成各自 derived event/transition 的双 applied history 后，restore `DID NOT RAISE`；
3. `test_l5_3_reused_checkpoint_id_resolves_current_interrupt_eligibility`：同 scope 复用 checkpoint ID、以不同 interrupt 发布并 applied v8 后，live/restart eligibility 实际均为 `blocked`；
4. `test_l5_3_restart_snapshot_rejects_noncausal_stage_and_apply_times`：`staged < issued`、`staged >= expires`、`applied < staged`、`applied >= expires` 四个 tamper 在同步重算 event/transition refs 后全部 `DID NOT RAISE`。

RED 前没有修改 production、handoff 或范围外文件，没有 skip、xfail、条件绕过、只测 stale ref 或弱化原 48 项。实现后的首次专项即为 `55 passed in 2.33s`。

### 13.3 Full sealed-attempt authority binding

- `resume_attempt_ref` 现在覆盖 `_SealedAttemptV1` 除 `resume_attempt_ref/state` 外的完整 canonical body：challenge/source ref、namespace/session、action、reviewer ID、固定 test role/organization/qualification、signature scheme/key ID 与 signed payload digest；
- coordinator 先从 authoritative store snapshot 定位 exact source ref，构造内部 provisional sealed body，再派生 ref 并 strict 构造最终 attempt；caller 不参与 ref authority；
- sealed 到 applied 只改变 state，因此 ref 保持稳定；除 ref/state 外任一 authority 字段改变而不重算 ref会在 DTO validator 拒绝。即使同时改 attempt/event action并重算 event ref，旧 attempt ref也不再匹配；
- attempt/event 跨记录 action、全部 test identity 与 signed payload digest 一致性继续验证；store 仍不保存 plaintext nonce 或 signature。

### 13.4 Single-use cardinality 与 exact applied history

snapshot integrity 现在对每个 challenge 直接计数：

- 最多一个 applied attempt；applied challenge/checkpoint 必须精确一个 applied attempt与一个 event；issued/expired challenge 必须为零 applied attempt/event；
- 每个 attempt 精确一条 staged transition；applied attempt 精确一条 `issued->claimed`、一条 `claimed->applied` 与一条 `review_pending->review_applied`，且绑定唯一 event；
- applied 前允许多个合法 sealed attempts；一个成功后其余保持 sealed，live resume 继续返回 replay/conflict，restore 不能表示第二个 applied；
- 原 32-thread 回归继续证明精确 `1 applied + 31 replayed_or_conflict`、一个 event 与一个 review_applied transition。R1 的 ref uniqueness、cross-record、append sequence 与 current marker integrity 全部保留。

### 13.5 Current-marker-first eligibility

eligibility 在 lock 内先按 `(namespace, test_session_id, thread_id)` 取得唯一 current marker：

1. caller checkpoint ID 必须等于 marker checkpoint ID；
2. 再以 marker 的 `issue_sequence + challenge_ref + scope + checkpoint_id` 定位精确 current checkpoint row；
3. 最后验证 current challenge/source/event exact applied-confirm authority。

因此相同 checkpoint ID、不同 interrupt 的 append-only history 合法；旧 interrupt 即使 checkpoint ID 相同也不会被先选中或造成全历史 uniqueness 误拒。R2 regression 同时证明 live store 与 fake restart 对 current v8 applied confirm 都返回 eligible；current pending/reject/modify 与旧 authority仍 blocked。

### 13.6 Restore temporal causality

- initial transition 继续精确等于 challenge `issued_at`；
- 每个 staged transition 现在必须满足 `issued_at <= staged_at < expires_at`，且 append sequence 晚于 initial；
- applied event 与三条 applied transition 必须时间逐字一致，并满足 `staged_at <= applied_at < expires_at`；
- integrity 校验在 ref validator 之后独立执行，因此攻击者同步重算 event/transition refs 也不能绕过因果；只验证每个 challenge/attempt 内因果，不要求全局 fake clock 单调；
- live stage/apply 的 R1 exclusive `now >= expires_at` atomic tombstone、clock 回拨不可复活、900 秒 TTL 与 fixed chainless boundary不变。

本修复不改变 ALLOW-only/BLOCK zero mutation、32-byte nonce only-once、challenge 全绑定、ref-only command、65,536-byte limit、secret-free、fake restart 不重发、current marker atomic issue/restore、non-confirm blocked 或 complete/record/export absent。

### 13.7 R2 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R2 结果 |
|---|---|
| L5-3/R2 专项 | 最终交付树 `55 passed in 2.59s`；前次 `55 passed in 2.33s` |
| accepted L5-2 回归 | `18 passed in 8.46s` |
| accepted L5-1 回归 | `14 passed in 15.69s` |
| Safety 回归 | `71 passed, 3 deselected in 2.44s` |
| L4.5-11 privacy 回归 | `76 passed in 6.14s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.34s` |
| `uv lock --check` | `Resolved 84 packages in 3ms` |
| diff/scope/tracked | working/cached diff check 均无错误；name-status 精确为三个原允许文件且均为 `M`；`git ls-files --error-unmatch` 对三者全部成功 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1711 passed, 362 deselected in 130.97s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1712 passed, 362 deselected in 133.08s` |

全部执行没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway、网络或外部服务。

### 13.8 R2 提交、限制与回退

- 单一 R2 开发提交消息为 `fix: close L5-3 restored history gaps`，exact parent 必须为 `aa9661200ec3d55c230ef32ec8c242c050990cf9`，提交只含 13.1 节三个原允许文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 parent/message、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- in-memory snapshot integrity 仍是本地 reference-store structural/causal integrity，不是外部持久化、加密签名、数据库 transaction 或生产 durability；真实 Runtime/identity/DB/HTTP 与 L5-4 full recheck 仍未实现、未发布。
- 若 R2 独立验收失败，使用 `git revert <r2-delivery-exact-head>` 保留历史，不 reset 或覆盖前两轮失败交付与 accepted L5-1/L5-2。

---

**R2 已交付，申请验收。**

## 14. 项目经理 R2 独立验收（2026-07-22）

- delivery `03ba9104a9d79924fb4d1241429161a8d80f989e` 的 exact parent、三个文件 scope、tracked 与 clean 均正确。
- 独立 CI 通过合同校准：L5-3 `55 passed`；L5-2 `18`；L5-1 `14`；Safety `71/3 deselected`；privacy `76`；L0 `131`；Ruff/mypy/lock/AST/diff 全绿。强制全量 `1 failed, 1711 passed, 362 deselected` 且唯一为既有 `APP_ENV` defaults 冲突；校准全量 `1712 passed, 362 deselected`。
- 独立 Reviewer 确认 R1 四项与原 expiry/event/current/chainless findings 均关闭，但发现 P1=1、P2=1：live stage/apply 缺 predecessor lower bound，可生成自身 restore 拒绝的历史；restore 仍接受 challenge applied 后才 stage loser attempt 的不可达 sequence。
- PM 结论：**R2 未接受 / 发布 L5-3-R3**（`ACC-20260722-027`、`DEC-20260722-020`）。保留前三次 delivery、全部 RED/GREEN、CI 与 Review 证据；L5-4 不得发布。

R3 合同见 [agent-refactor-l5-3-sandbox-rework-3-task.md](agent-refactor-l5-3-sandbox-rework-3-task.md)。

## 15. L5-3-R3 开发交付（2026-07-22）

### 15.1 状态、基线与限定范围

- 状态：**R3 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R3 clean release / exact parent：`80e989f6b6cd3d663a8a94bd92e508ef47628748`；其 parent 为失败 R2 delivery `03ba9104a9d79924fb4d1241429161a8d80f989e`。原 delivery、R1/R2、三轮 finding、`ACC-20260722-025/026/027`、`DEC-20260722-018/019/020` 与 R1/R2/R3 任务书全部保留，没有 reset、覆盖或删除失败历史。
- R3 只修改原三个文件：`app/agent_runtime/sandbox_review.py`、`tests/test_l5_3_sandbox_reviewer_interrupt_resume.py` 与本文；没有修改 R3 任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy、HTTP/DB/Gateway 或 L5-4。

### 15.2 两项先行回归与真实 RED

在 production 仍为 `03ba9104a9d79924fb4d1241429161a8d80f989e` 行为、worktree 唯一修改为两项指定 regression 时，以第 8 节完整 fake env 与 `UV_OFFLINE=1` 运行专项。exact HEAD 仍为 `80e989f6b6cd3d663a8a94bd92e508ef47628748`，production diff 为空；结果为退出码 `1`，`2 failed, 55 passed in 2.76s`，57 项全部收集：

1. `test_l5_3_live_stage_and_apply_reject_predecessor_clock_without_mutation`：issued 后把 fake clock 回拨到 predecessor 之前，stage/apply 实际返回 `staged/applied`，而非两次 fixed rejection；
2. `test_l5_3_restart_snapshot_rejects_attempt_staged_after_challenge_applied`：把 loser staged transition 移到 winner `review_applied` 之后、同步重排 sequence 并重算所有 transition refs 后，restore `DID NOT RAISE`。

RED 前没有修改 production、handoff 或范围外文件，没有 skip、xfail、条件绕过、只依赖 stale ref 或弱化原 55 项。实现后的首次专项为 `57 passed in 2.46s`。

### 15.3 Live/restore 共享 causal predicate

- 新增单一 `_causal_observation_is_reachable` 谓词，统一表达每个 attempt 的 `issued_at <= predecessor_at <= observed_at < expires_at`；live mutation 前置校验与 snapshot restore 使用同一实现，避免两套 lower-bound 条件漂移；
- stage 在 mutation 前以 challenge `issued_at` 为 predecessor；`now < issued_at` 返回既有 fixed rejection，attempt/challenge/checkpoint/event/transitions 与完整 snapshot 均不变，原 exclusive expiry tombstone 语义不变；
- apply 从权威 append-only transitions 定位该 attempt 精确一条 staged transition，以其 `observed_at` 为 predecessor；缺失、重复或 `now < staged_at` 都返回既有 fixed `resume_rejected`，attempt 仍 sealed、challenge issued、checkpoint pending、event 为空且 snapshot 可 restore；
- restore 对 staged 与 applied 使用同一 per-attempt predicate；固定 `SandboxReviewError` 边界继续无 cause/context，不泄漏动态时间、nonce、签名、对象 repr 或内部异常。

### 15.4 Cross-attempt append-sequence causality

- 对 applied challenge，winner 的 `attempt_applied`、`checkpoint_applied`、`review_applied` 三条 transition 必须保持固定顺序且 sequence 连续；
- 同 challenge 的全部 `attempt_staged` transition 必须早于 winner 第一条 applied transition；即使攻击者移动 loser transition、重排全部 sequence 并重算 derived refs，restore 仍 fixed chainless reject；
- cross-attempt 因果只使用 append-only sequence 表达“challenge applied 后不可再 stage”。不比较不同 attempt 的时间戳，也不要求 fake clock 全局单调；winner 自身仍满足 per-attempt lower bounds。

R2 已关闭的 full sealed-attempt authority binding、single-use cardinality、current-marker-first eligibility、单 attempt restore temporal causality，以及 R1 的 expiry/event/current/chainless invariants 全部保留。本修复不实现 L5-4 stale 写入、full safety recheck 或任何生产 Runtime/DB/HTTP 集成。

### 15.5 R3 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R3 结果 |
|---|---|
| L5-3/R3 专项 | 最终交付树 `57 passed in 3.20s`；实现后前次 `57 passed in 2.46s` |
| 独立 AST 边界 | `1 passed, 56 deselected in 2.04s` |
| accepted L5-2 回归 | `18 passed in 8.83s` |
| accepted L5-1 回归 | `14 passed in 15.38s` |
| Safety 回归 | `71 passed, 3 deselected in 2.35s` |
| L4.5-11 privacy 回归 | `76 passed in 5.88s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.14s` |
| `uv lock --check` | `Resolved 84 packages in 5ms` |
| diff/scope/tracked | 提交前最终审计精确限定为 15.1 节三个 tracked 文件，working/cached diff check 均须无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | 有效完整运行 `1 failed, 1713 passed, 362 deselected in 136.29s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1714 passed, 362 deselected in 133.38s` |

首次强制全量在 124.5 秒达到命令时限，未形成 pytest 结论；保持源码与环境不变、提高命令时限后的完整重跑给出上表有效结果。全部执行没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway、网络或外部服务。

### 15.6 R3 提交、限制与回退

- 单一 R3 开发提交消息为 `fix: align L5-3 live and restored causality`，exact parent 必须为 `80e989f6b6cd3d663a8a94bd92e508ef47628748`，提交只含 15.1 节三个原允许文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 parent/message、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- in-memory snapshot integrity 仍是本地 reference-store structural/causal integrity，不是外部持久化、加密签名、数据库 transaction 或生产 durability；真实 Runtime/identity/DB/HTTP 与 L5-4 full recheck 仍未实现、未发布。
- 若 R3 独立验收失败，使用 `git revert <r3-delivery-exact-head>` 保留历史，不 reset 或覆盖前三轮失败交付与 accepted L5-1/L5-2。

---

**R3 已交付，申请验收。**

## 16. 项目经理 R3 独立验收（2026-07-22）

- delivery `605f32466b71c6f6a0f1a41ece5fd3eb3d0ac12b` 的 exact parent、三个文件 scope、tracked 与 clean 均正确。
- 独立 CI 通过合同校准：L5-3 `57 passed`；L5-2 `18`；L5-1 `14`；Safety `71/3 deselected`；privacy `76`；L0 `131`；Ruff/mypy/lock/AST/diff 全绿。强制全量 `1 failed, 1713 passed, 362 deselected` 且唯一为既有 `APP_ENV` defaults 冲突；校准全量 `1714 passed, 362 deselected`。
- 独立 Reviewer 确认 R2 两项、R1/初始 findings 与跨 attempt 非单调时间正例全部通过，但发现 P2=1：两个已应用 challenges 的 events 可被反转、重编号并重算 refs，而 `review_applied` transitions 保持原序，restore 仍接受该 live-unreachable 双日志历史。
- PM 结论：**R3 未接受 / 发布 L5-3-R4**（`ACC-20260722-028`、`DEC-20260722-021`）。保留前四次 delivery、全部 RED/GREEN、CI 与 Review 证据；L5-4 不得发布。

R4 合同见 [agent-refactor-l5-3-sandbox-rework-4-task.md](agent-refactor-l5-3-sandbox-rework-4-task.md)。

## 17. L5-3-R4 开发交付（2026-07-22）

### 17.1 状态、基线与限定范围

- 状态：**R4 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R4 clean release / exact parent：`b557a36fbf130073aa63e14ba53e984e6dac5a0b`；其 parent 为失败 R3 delivery `605f32466b71c6f6a0f1a41ece5fd3eb3d0ac12b`。原 delivery、R1/R2/R3、四轮 finding、`ACC-20260722-025/026/027/028`、`DEC-20260722-018/019/020/021` 与 R1/R2/R3/R4 任务书全部保留，没有 reset、覆盖或删除失败历史。
- R4 只修改原三个文件：`app/agent_runtime/sandbox_review.py`、`tests/test_l5_3_sandbox_reviewer_interrupt_resume.py` 与本文；没有修改 R4 任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy、HTTP/DB/Gateway 或 L5-4。

### 17.2 指定先行回归与真实 RED

在 production 仍为 `605f32466b71c6f6a0f1a41ece5fd3eb3d0ac12b` 行为、worktree 唯一修改为指定 regression 时，以第 8 节完整 fake env 与 `UV_OFFLINE=1` 运行专项。exact HEAD 仍为 `b557a36fbf130073aa63e14ba53e984e6dac5a0b`，production diff 为空；结果为退出码 `1`，`1 failed, 57 passed in 2.99s`，58 项全部收集：

- `test_l5_3_restart_snapshot_rejects_event_order_opposite_review_applied_order` 先在同一 store 依次成功 apply 两个合法 challenges，再保持 transitions 原样、反转 events、按新位置重编号 sequence 并重算两个 event refs；R3 restore 实际 `DID NOT RAISE SandboxReviewError`。

RED 前没有修改 production、handoff 或范围外文件，没有 skip、xfail、条件绕过、依赖 stale event ref 或弱化原 57 项。实现后的首次专项为 `58 passed in 2.50s`。

### 17.3 Event/transition append-order P2 closure

- snapshot 继续先验证 event/transition sequence 分别从零连续、ref 唯一与 derived ref 完整，并逐 event 验证 attempt/challenge authority；每 challenge 精确一个 event、winner 精确连续三条 applied transitions、single-use cardinality、状态与时间因果不变量全部保留；
- 上述逐记录和逐 challenge 校验完成后，restore 精确比较 `tuple(event.resume_attempt_ref for event in events)` 与全部 `to_state == "review_applied"` transitions 的 `resume_attempt_ref` tuple；两侧顺序或成员有任何差异都 fixed reject；
- 因为 event 与 transition 都在同一 store lock 内 append，其连续 sequence 是唯一顺序 authority。同步反转 events、重编号 sequence 并重算 refs 仍无法把 live-unreachable 双日志历史恢复为合法 snapshot；
- 失败继续由既有 create/restore fixed `SandboxReviewError` 边界归一化，`__cause__` 与 `__context__` 均为空，不泄漏动态对象、时间、nonce、签名或内部异常。

### 17.4 非全局单调 fake clock 正例与既有不变量

新 regression 的合法 live 前置历史同时覆盖两个不同 challenges/attempts：challenge A 在较晚 fake time issue/stage/apply，随后 clock 整体回拨 100 秒，再 issue/stage/apply challenge B。A event 的 `applied_at` 明确大于 B，但原始 live snapshot 仍可逐字 restore，且 event attempt tuple 与 review-applied transition attempt tuple 同序。

因此 R4 不比较不同 events、challenges 或 attempts 的时间戳，不引入全局 wall/fake-clock 单调假设；顺序 authority 仅来自 append-only sequence。R3 的 live/restore shared causal predicate、cross-attempt stage/apply sequence，R2 的 full sealed-attempt binding/cardinality/current authority，R1 与初始 expiry/event/chainless findings，以及 32-thread 精确 `1 applied + 31 replayed_or_conflict` 全部保留。本修复不实现 L5-4 stale 写入、full safety recheck 或任何生产 Runtime/DB/HTTP 集成。

### 17.5 R4 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R4 结果 |
|---|---|
| L5-3/R4 专项 | 最终交付树 `58 passed in 2.73s`；实现后前次 `58 passed in 2.50s` |
| 独立 AST 边界 | `1 passed, 57 deselected in 2.26s` |
| accepted L5-2 回归 | `18 passed in 10.88s` |
| accepted L5-1 回归 | `14 passed in 18.41s` |
| Safety 回归 | 合同命令 `71 passed, 3 deselected in 2.23s`；额外宽跑三个 Safety 文件也为 `89 passed, 3 deselected` |
| L4.5-11 privacy 回归 | `76 passed in 7.10s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.66s` |
| `uv lock --check` | `Resolved 84 packages in 6ms` |
| diff/scope/tracked | 提交前最终审计精确限定为 17.1 节三个 tracked 文件，working/cached diff check 均须无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1714 passed, 362 deselected in 133.33s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1715 passed, 362 deselected in 135.79s` |

全部执行没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway、网络或外部服务。

### 17.6 R4 提交、限制与回退

- 单一 R4 开发提交消息为 `fix: align L5-3 review append order`，exact parent 必须为 `b557a36fbf130073aa63e14ba53e984e6dac5a0b`，提交只含 17.1 节三个原允许文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 parent/message、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- in-memory snapshot integrity 仍是本地 reference-store structural/causal integrity，不是外部持久化、加密签名、数据库 transaction 或生产 durability；真实 Runtime/identity/DB/HTTP 与 L5-4 full recheck 仍未实现、未发布。
- 若 R4 独立验收失败，使用 `git revert <r4-delivery-exact-head>` 保留历史，不 reset 或覆盖前四轮失败交付与 accepted L5-1/L5-2。

---

**R4 已交付，申请验收。**

## 18. 项目经理 R4 独立验收（2026-07-22）

- delivery `1f8503e0c28dd19dcc48b6e63b3e5103ce9adeda` 的 exact parent、三个文件 scope、tracked 与 clean 均正确。
- 独立 CI 通过合同校准：L5-3 `58 passed`；L5-2 `18`；L5-1 `14`；Safety `71/3 deselected`；privacy `76`；L0 `131`；Ruff/mypy/lock/AST/diff 全绿。强制全量 `1 failed, 1714 passed, 362 deselected` 且唯一为既有 `APP_ENV` defaults 冲突；校准全量 `1715 passed, 362 deselected`。
- 独立 Reviewer 确认 R4 指定 event/apply transition 顺序、非全局单调正例与全部历史 findings 通过，但发现 P2=1：反转 A/B 初始 transitions、重编号并重算 refs 后，restore 仍接受与 challenge issue 顺序相反的 live-unreachable history。
- PM 结论：**R4 未接受 / 发布 L5-3-R5**（`ACC-20260722-029`、`DEC-20260722-022`）。R5 以全部显式 sequenced projections 为根因边界；不为无 sequence 的 keyed collections 发明顺序。保留前五次 delivery 和全部证据；L5-4 不得发布。

R5 合同见 [agent-refactor-l5-3-sandbox-rework-5-task.md](agent-refactor-l5-3-sandbox-rework-5-task.md)。

## 19. L5-3-R5 开发交付（2026-07-22）

### 19.1 状态、基线与限定范围

- 状态：**R5 已交付，申请验收**；执行者不声明 accepted、clinical approved 或 production ready。
- R5 clean release / exact parent：`ef8139dee1320860cf8c924ecf2c53de4e860925`；其 parent 为失败 R4 delivery `1f8503e0c28dd19dcc48b6e63b3e5103ce9adeda`。原 delivery、R1～R4、五轮 finding、`ACC-20260722-025/026/027/028/029`、`DEC-20260722-018/019/020/021/022` 与 R1～R5 任务书全部保留，没有 reset、覆盖或删除失败历史。
- R5 只修改原三个文件：`app/agent_runtime/sandbox_review.py`、`tests/test_l5_3_sandbox_reviewer_interrupt_resume.py` 与本文；没有修改 R5 任务书、PM 台账、accepted L5-1/L5-2、配置、依赖、Runtime、Legacy、HTTP/DB/Gateway 或 L5-4。

### 19.2 指定先行回归与真实 RED

在 production 仍为 `1f8503e0c28dd19dcc48b6e63b3e5103ce9adeda` 行为、worktree 唯一修改为指定 regression 时，以第 8 节完整 fake env 与 `UV_OFFLINE=1` 运行专项。exact HEAD 仍为 `ef8139dee1320860cf8c924ecf2c53de4e860925`，production diff 为空；结果为退出码 `1`，`1 failed, 58 passed in 2.82s`，59 项全部收集：

- `test_l5_3_restart_snapshot_rejects_initial_transition_order_opposite_issue_order` 在同一 store 依次 issue 两个 pending challenges A/B，保持 sources/challenges/checkpoints 原样，只反转两条 `decided -> review_pending` transitions、按新位置重编号 sequence 并重算两个 transition refs；R4 restore 实际 `DID NOT RAISE SandboxReviewError`。

RED 前没有修改 production、handoff 或范围外文件，没有 skip、xfail、条件绕过、依赖 stale ref 或弱化原 58 项。实现后的首次专项为 `59 passed in 2.46s`。

### 19.3 Issue/initial-transition append projection P2 closure

- snapshot 继续先要求 sources/checkpoints 的 `issue_sequence` 从零连续，并以 strict zip 验证 sources/challenges/checkpoints 同长度、同位置、同 scope、同 checkpoint/interrupt 与 source/challenge refs；
- 每个 challenge 继续精确一条 `resume_attempt_ref is None`、`decided -> review_pending`、`observed_at == issued_at` 的 initial transition；transition sequence 连续、derived refs、uniqueness 与逐 challenge cardinality 全部先保持；
- 上述逐记录和逐 challenge 校验完成后，restore 精确比较 challenge-order refs 与 transition log 中全部 initial-transition challenge refs 的 tuple；反转 initial transitions 后即使同步重编号 sequence 和重算 refs，仍 fixed chainless reject；
- R4 的 event-order 到 `review_applied` transition-order attempt tuple 等式原样保留，失败继续由既有 create/restore fixed `SandboxReviewError` 边界归一化且无 cause/context。

### 19.4 Explicit sequenced collection audit

对 `SandboxReviewStoreSnapshotV1` 与 store lock 内全部 append/replace 路径的显式审计结论如下：

| Collection | 顺序 authority | 跨 collection 投影 |
|---|---|---|
| `sources/challenges/checkpoints` | source/checkpoint 连续 `issue_sequence`，三者 strict zip/cross-reference 固定同一 issue order | R5 精确投影到 `decided -> review_pending` initial transitions 的 challenge-ref tuple |
| `events` | 连续 `event.sequence`，live apply 在 store lock 内 append | R4 精确投影到 `review_applied` transitions 的 attempt-ref tuple |
| `transitions` | 连续 `transition.sequence`，承担 issue/stage/apply 的 append-only master order | 接收上述 issue/apply 两项投影；逐 challenge 的 stage/apply 状态、连续三 transition 与 cardinality 仍独立验证 |
| `attempts` | 无 `sequence`；按唯一 `resume_attempt_ref` 寻址并可原位更新 state | keyed unordered；不新增排序或位置 authority |
| `current_authorities` | 按唯一 scope key 寻址并原位替换；其 `issue_sequence` 是指向 latest checkpoint 的 authority 值，不是 current collection 自身的 append sequence | keyed unordered；继续使用 scope uniqueness/latest-marker cross-reference，不新增排序 |

因此全部有合同依据的跨 collection 显式序列映射恰为 issue projection 与 apply projection。R5 没有为 attempts/current-authorities 发明顺序，也没有按时间、ref 或对象内容排序任何 collection。

### 19.5 非全局单调 fake clock 与既有不变量

新 regression 的合法前置历史在较晚 fake time issue A，随后 clock 回拨 100 秒再 issue B；`B.issued_at < A.issued_at`，但正常 A/B live snapshot 仍可逐字 restore。issue 顺序只取自 store-lock append sequence，不比较跨 challenge/event/attempt 时间戳，也不要求 wall/fake clock 全局单调。

R4 event projection、R3 live/restore causal predicate 与 cross-attempt stage/apply sequence、R2 full sealed-attempt binding/cardinality/current authority、R1/初始 findings，以及 32-thread 精确 `1 applied + 31 replayed_or_conflict` 全部保留。本修复不实现 L5-4 stale 写入、full safety recheck 或任何生产 Runtime/DB/HTTP 集成。

### 19.6 R5 最终门禁

除 calibrated full 只移除 `APP_ENV` 外，全部命令使用第 8 节完整 fake env 与 `UV_OFFLINE=1`：

| 门禁 | R5 结果 |
|---|---|
| L5-3/R5 专项 | 最终交付树 `59 passed in 2.57s`；实现后前次 `59 passed in 2.46s` |
| 独立 AST 边界 | `1 passed, 58 deselected in 2.51s` |
| accepted L5-2 回归 | `18 passed in 9.56s` |
| accepted L5-1 回归 | `14 passed in 16.76s` |
| Safety 回归 | `71 passed, 3 deselected in 3.20s` |
| L4.5-11 privacy 回归 | `76 passed in 7.05s` |
| Ruff | `All checks passed!` |
| mypy | `Success: no issues found in 1 source file`；只有既有 `pymilvus.*` unused-section note |
| L0 | `131 passed in 2.95s` |
| `uv lock --check` | `Resolved 84 packages in 5ms` |
| diff/scope/tracked | 提交前最终审计精确限定为 19.1 节三个 tracked 文件，working/cached diff check 均须无错误 |
| 强制 `APP_ENV=sandbox-test` 全量 | `1 failed, 1715 passed, 362 deselected in 130.67s`；唯一失败为既有 `test_load_with_defaults` 预期 `local`、实际为强制值 `sandbox-test` |
| 只移除 `APP_ENV` 的校准全量 | `1716 passed, 362 deselected in 131.46s` |

全部执行没有读取/显示 `.env`，没有读取 ignored `data/`/`.codex_tmp`，没有启动或连接应用、HTTP/E2E、容器、DB、Redis、Milvus、模型/embedding Gateway、网络或外部服务。

### 19.7 R5 提交、限制与回退

- 单一 R5 开发提交消息为 `fix: align L5-3 issue append order`，exact parent 必须为 `ef8139dee1320860cf8c924ecf2c53de4e860925`，提交只含 19.1 节三个原允许文件。
- Git SHA 不能在包含本文的同一提交中自引用；delivery exact HEAD 由提交后外部报告的 `git rev-parse HEAD`、上述 parent/message、三文件 scope 与 clean worktree 共同冻结，后续由独立 Reviewer/CI/PM 锚定验收。
- in-memory snapshot integrity 仍是本地 reference-store structural/causal integrity，不是外部持久化、加密签名、数据库 transaction 或生产 durability；真实 Runtime/identity/DB/HTTP 与 L5-4 full recheck 仍未实现、未发布。
- 若 R5 独立验收失败，使用 `git revert <r5-delivery-exact-head>` 保留历史，不 reset 或覆盖前五轮失败交付与 accepted L5-1/L5-2。

---

**R5 已交付，申请验收。**

## 20. 项目经理 R5 最终验收（2026-07-22）

- exact delivery：`95be09a1b766c36eb4da411162b33a2efb4a1346`；exact parent `ef8139dee1320860cf8c924ecf2c53de4e860925`；单一提交只含三个允许且 tracked 的文件，验收前后 worktree/index clean。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0；确认 issue/apply 两类显式 sequence 投影完整，attempts/current-authorities 保持 keyed-unordered，全部历史 findings 与恢复后 32 并发回归通过。
- 独立 CI：L5-3 `59 passed`；L5-2 `18`；L5-1 `14`；Safety `71/3 deselected`；privacy `76`；L0 `131`；Ruff/mypy/lock/AST/diff 全绿。强制全量 `1 failed, 1715 passed, 362 deselected` 且唯一为既有 `APP_ENV` defaults 差异；只移除 `APP_ENV` 的校准全量 `1716 passed, 362 deselected`。
- PM 定向复验：同 exact HEAD 六项 `6 passed in 1.96s`，覆盖两类顺序投影、live/restart 时间一致性、复用 checkpoint current 定位与 32 并发；运行后 HEAD 不变且 clean。
- 结论：**L5-3 与 R5 accepted**（`ACC-20260722-030`、`DEC-20260722-023`）；`R-L5-RESUME-001` 关闭；L5 为 3/4 accepted。
- 限制：只接受个人学习、固定虚构/合成数据、离线单元和 in-memory sandbox。L5-4 尚未发布；Runtime、HTTP、容器、部署、DB、Gateway、Legacy、真实临床/患者/公开生产继续 NO-GO，L6 未开始。

## 21. L5 最终组合第 2 轮（未通过，L5-3 reopened）

- 冻结 exact HEAD：`4ffbaff7374bd6a13b1a9d058e9c920709593119`；工作区前后 clean；`ACC-20260722-039` 与 R6 delivery 均为祖先。
- 最终独立 CI：L5 四层组合 `141 passed`，calibrated full `1766 passed, 362 deselected`；forced full 仅既有 defaults 差异；全部相邻、静态、scope/tracked/exact/clean 门禁通过。
- PM 跨层：`13 passed`，证明正常 live 路径和组合回归保持。
- 最终 Reviewer：P0=0、P1=0、P2=1、P3=0。协调修改一个真实 initial applied review snapshot 的 schema v1→v2并重派生 challenge/attempt/event/transition refs、checkpoint/current 后，`SandboxInMemoryReviewStore` 仍接受；该状态不是当前 live issue 路径可产生。
- 根因：live issue 使用模块固定 `_REVIEW_SCHEMA_VERSION`，但 shared snapshot restore 只校验记录彼此一致，没有校验 challenge 使用唯一受支持版本。
- 结论：最终组合 **未通过**（`ACC-20260722-040`、`DEC-20260722-033`）；`ACC-030` 历史单项结论保留，但 L5-3 与 `R-L5-RESUME-001` 重新打开；shared R7 已发布。
- 边界：R7 只在 L5-3 shared restore 持有 fixed schema authority，并以 L5-3/L5-4 两层回归证明；不增加迁移、多版本兼容、Runtime 或外部能力，L6 未开始。

## 22. L5-3/4-R7 fixed review schema restore authority 交付（2026-07-23）

### 22.1 状态、基线与范围

- 状态：**R7 已交付，申请独立验收**；执行者不声明 accepted、专业批准或 production ready。
- clean management release / exact parent：`54e357f89f5d6f206dd7ae685151cf242e32e0d1`；其中保留 final R2 失败、`ACC-20260722-040`、`DEC-20260722-033` 与 R7 任务书。
- 单一 R7 交付只修改任务书允许的五个 tracked 文件：L5-3 shared production、本专项、L5-4 专项及两份 handoff；`sandbox_recheck.py` production、PM 六台账、任务书、L5-1/L5-2、配置、依赖与锁文件均未修改。

### 22.2 两层完整重派生 RED 与 GREEN

production diff 为空、工作区仅新增两层 regression 时，完整 fake env 与 `UV_OFFLINE=1` 下定向收集 4 项，真实 RED 为 `4 failed in 2.54s`：

1. L5-3 `issued` 与 `applied/modify_fixture` 两个真实 snapshot 把 challenge schema 从固定 v1 协调改为 v2；同时重算 challenge、attempt、event、transition refs 以及 checkpoint/current 绑定后，旧 store 均错误接受；
2. AST 结构检查确认 `_snapshot_is_integral` 的 shared challenge loop 没有 fixed schema guard；
3. L5-4 真实 `modify_applied` initial-only snapshot 作同样 private 全链重派生，再重算 initial revision/current ref 后，旧 coordinator 仍错误接受。

上述 RED 不依赖 stale ref、单侧字段修改、删除历史、skip 或 xfail。最小 production 修复只在 L5-3 shared challenge restore loop、任何 `challenge.state` 分支之前，无条件验证 `challenge.sandbox_schema_version == _REVIEW_SCHEMA_VERSION`；live provisional/final challenge 继续共用同一模块常量。没有修改 L5-4 production，也没有增加 migration、registry、negotiation 或多版本机制。修复后同 4 项为 `4 passed in 2.08s`。

结构/正例回归还证明未修改 v1 的 `issued/expired/applied` 三种 snapshot 均可逐字 round-trip。R6 的 current-child 完整重派生用例同步加强为：L5-3 private store 已在 shared boundary 固定拒绝，L5-4 outer coordinator 也固定拒绝，且输入不变；这只是把历史测试前提校准到 R7 authority，没有弱化 R6 child==parent 守卫或其他既有断言。

### 22.3 最终门禁

| 门禁 | R7 结果 |
|---|---|
| R7 四项定向 | `4 passed in 2.08s` |
| L5-3 / L5-4 完整专项 | `62 passed in 2.35s`；`51 passed in 3.55s` |
| L5-1 / L5-2 | `14 passed in 13.51s`；`18 passed in 6.47s` |
| Safety / privacy | `71 passed, 3 deselected in 1.99s`；`76 passed in 4.58s` |
| Runtime/Legacy/public / public flag | `57 passed, 11 deselected in 0.89s`；`10 passed in 1.94s` |
| AST/结构边界 | `5 passed in 2.26s` |
| Ruff / mypy | `All checks passed!`；两份 production / 0 issues；仅既有 `pymilvus.*` unused-section note |
| L0 / lock | `131 passed in 2.31s`；`Resolved 84 packages in 3ms`，lock 未变化 |
| 强制全量首次 | `2 failed, 1768 passed, 362 deselected in 149.49s`；除既有 defaults 外，出现一次既有 L3 deadline/privacy code 偏差，证据保留 |
| L3 同环境复验 | 参数族连续 5 轮均为 `4 passed, 52 deselected`，每轮覆盖首次偏差参数 |
| 强制全量有效复跑 | `1 failed, 1769 passed, 362 deselected in 149.18s`；唯一为 `test_load_with_defaults` 的 local / sandbox-test 既有差异 |
| 只移除 `APP_ENV` 的校准全量 | `1770 passed, 362 deselected in 147.97s` |

全部 fixture 继续是 inline fixed-fictitious/synthetic 技术数据；没有读取 `.env`、ignored `data/` 或 `.codex_tmp`，没有访问网络或启动应用、HTTP、容器、数据库、Gateway 或外部服务。首次 forced 偶发结果没有被删除或写成通过；有效完整复跑与校准全量均针对最终代码内容。

### 22.4 提交、限制与回退

- 单一开发提交消息为 `fix: anchor L5 review schema restore authority`；exact parent 必须为 `54e357f89f5d6f206dd7ae685151cf242e32e0d1`；提交只含 22.1 节五个文件，提交后 worktree/index 必须 clean。
- Git SHA 无法在包含本文的同一提交中自引用；冻结后由 `git rev-parse HEAD`、上述 parent/message、五文件 scope 与 clean 状态共同报告，并由新的独立 Reviewer/CI/PM 锚定。
- 本交付仍只是固定虚构/合成、offline unit/in-memory reference restore；不提供进程外 durability、Runtime、HTTP、DB、真实 completion/export、专业准入或公开生产能力。L5 当前仍为 2/4，L6 未发布、未开始。
- 若 R7 独立验收失败，对单一 R7 delivery 执行 `git revert <r7-delivery-commit>` 并保留全部历史，不 reset、amend 或覆盖。

---

**R7 已交付，申请独立验收。**

## 23. L5-3/4-R7 独立 shared acceptance（通过）

- 冻结 delivery：`d3ee3ce48fd39c115df30d8aad446edac14770a6`；parent `54e357f89f5d6f206dd7ae685151cf242e32e0d1`；精确 5 个允许且 tracked 的文件，前后 exact/clean 与 diff check 通过。
- 独立 Reviewer：P0=0、P1=0、P2=0、P3=0；shared fixed schema guard 位于 challenge loop 且早于状态分支，live 两构造点共用同一常量；两层完整重派生与合法三状态 round-trip 成立。
- 独立 CI：R7 `4`、L5-3/4 `62/51`、L5-1/2 `14/18`、Safety `71/3 deselected`、privacy `76`、Runtime/Legacy/public `57/11 deselected`、public flag `10`、AST `5`、L0 `131`、Ruff/mypy/lock 全通过。
- 双全量：forced `1 failed, 1769 passed, 362 deselected` 且唯一为既有 defaults 差异、无额外波动；calibrated `1770 passed, 362 deselected`。
- PM 六项 `6 passed`：R7 两层、shared guard、R6 child guard、R5 helper ownership；HEAD 未变且 clean。
- 结论：**L5-3/L5-4 与 R7 accepted**（`ACC-20260723-041`、`DEC-20260723-034`）；再次关闭两个工程风险；L5 恢复 4/4 individually accepted。
- 后续：本节不关闭 L5 整体。必须从包含 shared acceptance 的新 clean exact HEAD 调用全新的 final R3 Reviewer、独立 CI 与 PM；不得复用前两轮结果，L6 未开始。

## 24. L5 最终组合第 3 轮（未通过，L5-3 reopened）

- 冻结 exact HEAD：`78c9b13c7790eef5fb3f01705c2ebfef2a3efa36`；R7 delivery/acceptance 均为祖先；前后 clean。
- 最终独立 CI：四层 `14/18/62/51`、组合 `145`、calibrated full `1770 passed, 362 deselected`；forced 仅既有 defaults 差异；全部相邻/静态/scope/tracked 门禁通过。
- PM：跨层 `13 passed` + R7 定向 `4 passed`，正常 live 与 shared schema 行为保持。
- 最终 Reviewer：P0=0、P1=0、P2=1、P3=0。live proof 的三个 identifier 使用统一长度/格式约束，但 persisted sealed attempt/event 没有复用；协调修改并重派生 refs 后 restore 接受 live DTO 明确拒绝的值。
- 结论：final R3 **未通过**（`ACC-20260723-042`、`DEC-20260723-035`）；历史 acceptance 保留，但 L5-3 与两个工程风险重新打开；shared R8 已发布。
- R8 必须以单一 constrained alias 收敛三字段×三模型全族，不逐字段复制条件；同时证明 L5-4 composition，L6 未开始。
