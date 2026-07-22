# L5-1 确定性 SafetyRuleEngine adapter（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付；尚未实施 |
| 发布人 | Codex（工程项目经理） |
| 生产代码基线 | `ad8cbf038cb3e0a18c9ce40f88d5ee235c04a4d7`；执行者另记录包含本任务书的 clean release HEAD |
| 依据 | `L5-PREP-0` 验收；`L5个人学习工程沙盒准入包-2026-07-22.md` §5、§7.1～§7.3、§8 |
| 外部门禁 | G1～G6、EXT-001/EXT-002 未专业通过；当前任务仅 `sandbox_scope_satisfied` |
| 交付文件 | `docs/dev-handoff/agent-refactor-l5-1-sandbox.md` |

## 目标

在不触碰现有 Legacy 医疗流程、数据库、网络、公共 API 或真实数据的前提下，建立 L5-1 的第一段可验证边界：一个 side-effect-free、immutable、fail-closed 的 `SandboxSafetyRuleAdapter`，将 exact synthetic subject + frozen rule bundle + injected deterministic evaluator 转换为可逐字节复现的 `SandboxSafetyResultV1`。

本任务验收只表示“离线 deterministic adapter 核心成立”。它不关闭完整 L5-1 的 Domain 持久化/图集成，不授权 L5-2～L5-4，也不构成临床或运行时准入。

## 必须实现

1. 在新模块中定义 frozen/strict DTO：
   - `SandboxSafetySubjectV1`；
   - `SandboxRuleBundleV1`；
   - 稳定 `SandboxSafetyIssueV1`；
   - frozen `SandboxSafetyResultV1`；
   - 固定失败码与 `SandboxSafetyAdapterError`。
2. subject 精确绑定：test session、Domain state version、formula/profile artifact ID + revision + content digest、graph/adapter/rule bundle version + digest、synthetic dataset version + digest。
3. 明确区分：
   - `decision_subject_digest`：不含时间、trace、run、nonce；
   - `run_envelope_digest`：绑定 command/run/trace，但不得影响裁决。
4. canonical JSON 固定 UTF-8、字段顺序、数字/空值/数组排序；相同 subject + bundle 必须产生逐字节相同结果和 digest。
5. evaluator 通过最小 Protocol 注入；adapter 不读取当前 DB，不调用现有 `SafetyRuleEngine.check()` / `persist_result()`，不接受调用方构造的“已通过”结果。
6. 缺字段、未知/额外字段、版本不一致、digest 不一致、evaluator 异常、非确定输出、limit 超限全部固定 fail closed；错误不含输入、公式、异常 cause/context 或本地配置值。
7. 固定上限：formula item `<=64`、issue `<=256`、subject/result canonical bytes 各 `<=256 KiB`；limit + 1 必须在 evaluator/下游调用前拒绝。
8. 仅使用 inline fixed-fictitious technical fixture；fixture 明确 `fixed_fictitious_manual`、`sandbox_only`、`not_clinically_adjudicated`，不得读取 `data/` 或 `.env`。

## 非目标

- 不实现真实规则包快照、PostgreSQL artifact/gate/outbox 持久化或 graph node；
- 不修改/调用 Legacy `SafetyRuleEngine`、ReviewService、record/export、API、前端或数据库模型；
- 不实现 SafetyExplanation、interrupt/resume、Sandbox Reviewer、challenge、签名或修改后重检；
- 不启动 FastAPI/lifespan、Docker Compose、PostgreSQL、Redis、Milvus、模型/embedding 网关或 importer；
- 不读取 ignored `data/`、`.env`、`.codex_tmp`；
- 不添加 feature flag、依赖、迁移、配置或部署文件；
- 不生成处方、医疗建议或专业/临床结论。

## 允许修改范围

- 新增 `app/agent_runtime/sandbox_safety.py`
- 新增 `tests/test_l5_1_sandbox_safety_adapter.py`
- 新增/更新 `docs/dev-handoff/agent-refactor-l5-1-sandbox.md`

除此之外全部禁止修改。若现有模块无法在该范围内实现，停止并由项目经理发布后续持久化/集成切片，不得扩 scope。

## 必须先红

在生产模块不存在的 release HEAD，新增测试必须因缺少 `app.agent_runtime.sandbox_safety` 而 collection RED；不得通过 skip、xfail、动态替身或先写生产代码伪造 RED。

RED 至少覆盖以下验收名：

- `test_l5_1_same_subject_and_bundle_produce_same_result_digest`
- `test_l5_1_run_envelope_does_not_change_decision_digest`
- `test_l5_1_rule_bundle_digest_change_invalidates_subject`
- `test_l5_1_missing_extra_parse_error_and_version_mismatch_fail_closed`
- `test_l5_1_stale_formula_or_profile_digest_rejected_before_evaluation`
- `test_l5_1_result_and_nested_issues_are_immutable`
- `test_l5_1_evaluator_exception_is_chainless_and_contains_no_payload`
- `test_l5_1_limit_plus_one_rejected_before_evaluator_call`
- `test_l5_1_no_settings_env_data_gateway_review_record_export_or_network_import`
- `test_l5_1_thousand_runs_are_reproducible_and_resource_bounded`

## 验收标准

### 功能与安全

- 相同 canonical subject/bundle 在不同 `PYTHONHASHSEED`、run/trace ID 和重复执行中具有相同 decision/issues/result digest；
- issue ID、severity、decision 和执行顺序只能来自 injected deterministic evaluator，并经 adapter strict 验证/稳定排序；
- nested DTO 真正不可变；不能通过 list/dict alias 或 `object.__setattr__` 之外的正常公共接口改写；
- 所有 invalid/error path 固定拒绝，evaluator/model/network/review/record/export 调用为 0；
- 日志、异常和测试输出不含 formula payload、`.env` 值、credential、Prompt、nonce 或 signature；
- 1,000 次最大合法 fixture：p95 `<50 ms`、p99 `<100 ms`、RSS 增长 `<64 MiB`，记录 Python/CPU/测量方法；不得放宽阈值关闭 finding。

### 受控环境

执行验收前在当前 PowerShell 进程显式覆盖所有可能的外部连接配置为不可用 loopback fake 值；不得输出或读取本地 `.env` 值：

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
```

### 验收命令

```powershell
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run ruff check app/agent_runtime/sandbox_safety.py tests/test_l5_1_sandbox_safety_adapter.py
uv run mypy app/agent_runtime/sandbox_safety.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv run pytest -q -rs
uv lock --check
git diff --check
```

全量命令必须保持 `not integration` 默认 marker；任何命令试图连接外部终端、加载 importer/RAG 或启动应用即停止。

## 停止条件

- 需要修改允许列表以外的任何文件；
- 需要读取 `.env`、ignored `data/`、数据库、Docker volume、模型日志或外部系统；
- 需要启动 API、Compose、PostgreSQL、Redis、Milvus、RAG、模型/embedding 网关或网络；
- 需要复用 Legacy review、record/export、可选 doctor header 或“最新 safety run”语义；
- 需要把 subject/result 变成可变 DTO，或把 rule bundle 简化为只读当前 DB + 版本字符串；
- 无法保持 fail closed、determinism、resource bounds 或零外部副作用；
- 出现真实/可关联个人数据、有效凭据外泄、无法归属 diff 或 P0/P1/P2。

## 交付记录

handoff 必须记录 release/delivery exact HEAD、实际 diff、RED/GREEN、命令和环境覆盖、资源数据、零调用证据、未决限制和 clean worktree。执行者只能写“已交付，申请验收”，不得自称 accepted、`sandbox_scope_satisfied` 或 clinical approved。
