# L5-3 Sandbox Reviewer interrupt/resume（Offline State Machine）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付；尚未实施 |
| 发布人 | Codex（工程项目经理） |
| 已接受前置 | L5-2 delivery `1957ad311b3997499e4f9a0e3f2dd95aa652fa9e`；验收提交 `7662915df0468e14924c2b3a979df445575a2bbd`；验收 `ACC-20260722-023` |
| 依据 | L5 准入包 §7.1、§7.2、§7.5、§7.7、§8、§8.1；`DEC-20260722-017` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l5-3-sandbox.md` |

## 目标

新增一个纯离线、strict、immutable、single-use、restartable 的 `SandboxReviewCoordinator` 与线程安全 in-memory domain store，证明 accepted L5-1/L5-2 synthetic artifact 在 `review_pending` interrupt 后只能凭完整绑定的 challenge 精确恢复一次。

本任务实现的是隔离领域状态机和 transport contract，不接入真实 LangGraph `Command`、MainGraph、HTTP、数据库或身份系统。等价 `SandboxResumeCommandV1` 必须只有 `resume_attempt_ref`；可信 action、reviewer test identity、signature verdict 和全部绑定由 coordinator 从 store 重读。任务不实现 formula 修改、L5-4 全量重检、真实完成/导出或任何不可逆业务效果。

## 必须实现

### 1. strict immutable authority、测试身份与 challenge

- strict/frozen review source/snapshot 必须绑定 accepted `SandboxSafetySubjectV1`、`SandboxSafetyResultV1` 与可选 `SandboxExplanationResultV1` 的 canonical bytes/digest；source 与 result 的 `decision_subject_digest`、explanation 的 `source_result_digest` 必须相互一致；
- challenge authority 至少绑定 `sandbox_schema_version`、`adapter_version`、`graph_version`、namespace、`test_session_id`、thread/checkpoint/interrupt ID、test state/formula revision、input/result/rule/dataset/review-render digest、allowed actions、issued/expiry time 和 256-bit nonce 的 SHA-256；
- allowed actions 精确为 `confirm`、`reject`、`modify_fixture`；本任务中 `modify_fixture` 只追加 test review event 并保持 completion blocked，不创建新 revision 或调用规则；
- 身份字段只能是 `sandbox_test_reviewer_id`、`sandbox_test_role="sandbox_reviewer_test_role"`、`sandbox_test_organization_label="local_synthetic_sandbox"`、`sandbox_test_qualification_label="not_a_medical_credential"`、`sandbox_test_signature_scheme`、`sandbox_test_key_id`、`sandbox_test_signed_payload_digest`、`sandbox_test_signature`；禁止 doctor/physician/clinician/license/credential-approved 语义；
- challenge payload 只含引用、版本、digest、allowed actions 和 synthetic technical summary；不含 formula/profile/issue/explanation 原文、Prompt、凭据或真实/个人属性；
- plaintext nonce 必须由 injected 32-byte fake nonce factory 产生，只在首次 issue delivery 返回一次；store/checkpoint/event/error/`repr` 均只能保存 nonce digest，不得保存或显示 plaintext nonce。

所有传入 source/challenge/attempt/store snapshot 在信任边界处必须 strict 重解析；store 以 canonical bytes 或等价深重建保存和返回，禁止跨 caller/coordinator/store 共享 nested DTO 引用。

### 2. attempt staging 与 ref-only resume transport

- injected fake clock 只返回整数 epoch seconds；challenge 默认有效期精确 900 秒，不得读取 wall clock、sleep 或等待真实时间；
- injected fake signer/verifier Protocol 只处理 canonical signed payload digest、test key ID/scheme/signature；不得读取环境变量、密钥文件、外部 KMS 或网络；
- 完整 `SandboxResumeSubmissionV1` 在 transport boundary 校验 namespace/session/challenge/action/nonce/signature 与 payload size 后，转换为不含 plaintext nonce 的 sealed attempt；失败只返回 fixed rejection，不能创建 attempt ref；
- `SandboxResumeCommandV1` strict/frozen 且字段集合精确等于 `{resume_attempt_ref}`，未知字段、类型强制转换或携带 action/reviewer/digest/nonce/signature 一律 schema reject；
- coordinator 收到 command 后只按 ref 从 Domain Store 重读 sealed attempt、challenge、checkpoint、accepted source 和全部精确绑定；command/caller 自报内容不是 authority；
- resume submission canonical bytes 最大 65,536 bytes；65,537 bytes 必须在 signer/store/CAS 零调用前 fixed reject，不截断、不部分接受；
- exception、坏 schema、missing ref 或 store/signer 故障不得暴露 payload/signature/nonce/cause/context，且不得推进 checkpoint。

### 3. 原子状态机、CAS 与 append-only 事件

状态机精确为：

```text
decided
  → create_single_use_challenge
  → review_pending
  → stage_verified_resume_attempt
  → atomic issued → claimed → applied
       ├─ mismatch / missing / expired / replay / lost race → resume_rejected
       └─ append one bound test_review event → review_applied
```

- store 必须是本模块内明确标记 sandbox-only 的线程安全 in-memory reference store；CAS、checkpoint 状态变化、challenge tombstone 和 test review event append 在同一 lock/transaction boundary 内原子完成；
- 32 个线程对同一 `resume_attempt_ref` 并发时，精确一个返回 `applied`，其余返回同一 fixed `replayed_or_conflict`；只追加一个 review event、一个 applied transition，不产生重复副作用；
- stale graph/state/formula/input/result/rule/dataset/review-render digest、错 action、错 nonce/signature、跨 session/namespace、过期、重放、missing checkpoint/challenge 全部 fixed reject，checkpoint 不从 `review_pending` 前进；不得构造“等价” challenge；
- 一旦 fake clock 观察到过期，challenge 原子写入 expired tombstone；即使 fake clock 回拨也不能复活 nonce；
- issue 同一 interrupt 必须幂等：首次返回 plaintext nonce，重试或 fake process restart 只能恢复同一 `review_pending` challenge ref/checkpoint，不重发 nonce、不新增 challenge/event；
- 新建 coordinator 复用同一 in-memory store 模拟进程重启；必须从精确 checkpoint/challenge 恢复并接受原首次 delivery 的有效 attempt，不能依赖进程本地 cache；
- event log 为 immutable append-only tuple/records；旧事件不得删除、覆盖、重排或原地改写。review event 必须绑定全部 authority digest/version、test identity、安全的 action 与 attempt/challenge ref，但不得包含 nonce/signature/fixture 原文。

### 4. completion/export 不可达与范围隔离

- coordinator 只能提供无副作用的 eligibility probe；只有 current exact authority 上成功 applied 的 `confirm` test review 可报告 `eligible`；无 review、stale review、`reject`、`modify_fixture`、pending/expired/replayed challenge 全部 fixed `blocked`；
- 本模块不得定义或调用 `complete`、`record`、`export`、处方/医疗建议、真实签署或外部副作用实现；eligibility 不执行完成或导出；
- interrupt 前后只存在 versioned synthetic source/safety/explanation snapshot、challenge/checkpoint 和 append-only event；没有网络、文件、数据库、消息队列、outbox sender、Runtime 或 Legacy 调用；
- production import 只允许 Python 标准库、Pydantic、accepted `sandbox_safety` 与 `sandbox_explanation`；不得导入 Settings、环境加载、LangGraph、FastAPI、SQLAlchemy、Redis、Milvus、Gateway、LLM、Legacy review/record/export。

## 必须先红

在生产模块不存在的 clean release HEAD，先新增测试并运行 collection RED；必须因缺少 `app.agent_runtime.sandbox_review` 失败，不得 skip、xfail、动态替身或先写生产代码。

至少保留以下测试名：

- `test_l5_3_valid_confirm_and_reject_apply_exactly_once_with_all_bindings`
- `test_l5_3_resume_command_contains_only_resume_attempt_ref`
- `test_l5_3_test_identity_fields_are_exact_and_non_credentialing`
- `test_l5_3_plaintext_nonce_is_returned_once_and_never_persisted_or_rendered`
- `test_l5_3_nonce_factory_requires_exactly_256_bits_before_any_store_write`
- `test_l5_3_stale_versions_and_every_bound_digest_are_fixed_rejected`
- `test_l5_3_replay_cross_session_namespace_expiry_nonce_and_signature_are_rejected`
- `test_l5_3_expired_nonce_does_not_revive_when_fake_clock_moves_back`
- `test_l5_3_thirty_two_concurrent_resumes_have_exactly_one_success`
- `test_l5_3_fake_restart_recovers_exact_checkpoint_without_reissuing_challenge`
- `test_l5_3_missing_checkpoint_or_challenge_is_rejected_without_reconstruction`
- `test_l5_3_no_current_confirm_review_blocks_completion_and_export_is_absent`
- `test_l5_3_modify_fixture_only_appends_review_and_remains_blocked`
- `test_l5_3_resume_payload_64kib_plus_one_is_rejected_before_signer_and_store`
- `test_l5_3_caller_and_store_nested_changes_cannot_change_authority`
- `test_l5_3_events_are_immutable_append_only_and_secret_free`
- `test_l5_3_errors_are_fixed_chainless_and_payload_free`
- `test_l5_3_no_settings_env_network_runtime_db_gateway_legacy_or_export_imports`

参数化可以覆盖多个 binding/失败分支，但不得用单一弱断言代替每个字段的精确拒绝与 checkpoint/event 不变量。

## 允许修改范围

- 新增 `app/agent_runtime/sandbox_review.py`
- 新增 `tests/test_l5_3_sandbox_reviewer_interrupt_resume.py`
- 新增/更新 `docs/dev-handoff/agent-refactor-l5-3-sandbox.md`

除此之外全部禁止。若无法在三个文件内完成，停止并由项目经理裁定；执行者不得修改 L5-1/L5-2、PM 台账、任务书、配置、依赖、公共开关、Runtime、Legacy 或顺手实现 L5-4。

## 受控环境

沿用完整不可用 loopback fake 覆盖：

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

不得读取/显示本地 `.env` 值；不得读取 ignored `data/`/`.codex_tmp`；不得启动应用、HTTP/E2E、容器、数据库、Redis、Milvus、模型/embedding Gateway、网络或真实 wall-clock wait。

## 验收命令

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

强制 `APP_ENV=sandbox-test` 的既有 defaults 测试冲突必须原样记录；另运行只移除 `APP_ENV`、保持全部 fake endpoints 与 `UV_OFFLINE=1` 的校准全量。不得修改 `tests/test_config.py` 或公共配置制造通过。

## 停止条件

- 需要修改允许列表外文件、accepted L5-1/L5-2 authority、依赖、配置、migration 或 feature flag；
- 需要接入真实 LangGraph/MainGraph、身份认证、签署、数据库、HTTP、文件持久化、消息系统或任何外部服务；
- 需要让 command/caller payload 成为 action、binding、reviewer 或 signature authority，或把 plaintext nonce 写入 store/checkpoint/event/error/`repr`；
- 不能证明 32 并发恰好一次、fake restart、expired tombstone、append-only、ref-only command、64 KiB 与 complete/export blocked；
- 需要读取 `.env`、ignored/真实数据、外部日志或有效凭据；
- 出现真实/可关联个人数据、不可逆真实业务效果、无法归属 diff 或范围外 P0/P1。

普通专项、回归、静态、全量或资源失败属于合同内返工，不构成暂停理由。

## 交付要求

handoff 必须记录 release/delivery exact HEAD、实际 diff、真实 RED、GREEN、authority/challenge/attempt/event schema、nonce 一次性与 secret-free 证据、全部 binding 负向矩阵、32 并发、fake restart、completion/export blocked、64 KiB、全量校准、scope/tracked/clean 和未决限制。创建单一开发交付提交；只能写“已交付，申请验收”，不得自称 accepted、clinical approved 或 production ready。
