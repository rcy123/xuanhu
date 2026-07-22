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
