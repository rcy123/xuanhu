# L5-2 SafetyExplanationAgent 限权解释（Sandbox）

## 发布信息

| 项目 | 内容 |
|---|---|
| 状态 | 已发布 / 待交付；尚未实施 |
| 发布人 | Codex（工程项目经理） |
| 已接受前置 | L5-1 delivery `461487e03d6529dfacbc7f3f1ff1fe919e8633d5`；验收 `ACC-20260722-020` |
| 依据 | L5 准入包 §7.4、§7.7、§8、§8.1；`DEC-20260722-015` |
| 执行起点 | 包含本任务书的 clean exact management release HEAD，由项目经理提交后报告 |
| 交付文件 | `docs/dev-handoff/agent-refactor-l5-2-sandbox.md` |

## 目标

新增一个纯离线、optional、strict、immutable、size-bounded 的 `SandboxSafetyExplanationAgent`。它只消费 accepted L5-1 `SandboxSafetyResultV1` 的不可变裁决和 digest，加上 frozen allowlisted rule explanation；通过 injected fake explanation port 生成候选，再由本地 verifier 证明 exact issue/rule reference、text allowlist match 和 non-interference。

解释层永远不是 decision authority。无解释、解释失败或恶意候选不影响 L5-1 result；本任务不实现真实模型、网络、Runtime、review、record/export 或 L5-3。

## 必须实现

### 1. strict immutable DTO 与输入最小化

- strict/frozen `SandboxExplanationIssueRefV1`：只含 `issue_id`、`rule_id`、`severity`；
- strict/frozen allowlist entry/bundle：唯一排序 `rule_id` + fixed explanation text + canonical digest；
- strict/frozen port input：只含 L5-1 `result_digest`、decision、最多 64 个 issue refs 与实际用到的 allowlist entries；
- strict/frozen candidate statement：`issue_id`、`rule_id`、`text`；candidate 不允许任何额外字段；
- strict/frozen final result：只含 source result digest、`attached|explanation_unavailable`、immutable statements、固定 disclaimer、explanation digest；不得包含 decision、severity、issues 集合、formula、处置或 review action。

adapter 入口必须 strict 重解析 L5-1 result 并验证其既有 result digest；不得接收调用方自报的简化“passed”对象。port 输入不得包含 subject、formula/profile、manifest 原文、artifact payload、姓名、联系方式、真实身份、Prompt、credential、nonce/signature 或任意自由上下文。

### 2. exact-reference verifier 与 non-interference

- 每条候选必须一对一引用 source result 中已存在的 exact `issue_id + rule_id`；同一 issue 只能出现一次；
- candidate text 必须逐字等于该 rule 的 allowlisted explanation；不得接受 paraphrase、额外建议、处置、猜测或无依据内容；
- statements 数量不得超过 source issue 数，可为有依据的子集；source 无 issue 时不调用 port，直接 unavailable；
- candidate/output 不得携带或写回 decision、severity、issues、result digest、formula 或 review action；任何 extra/干预字段使整个 explanation unavailable；
- 调用前后 `canonical_result_bytes(source_result)`、decision、severity、issues、decision subject digest 与 result digest 必须逐字节不变；final explanation digest 不进入或覆盖 L5-1 digest。

### 3. fixed unavailable 与边界

以下情况只返回固定 `explanation_unavailable`（statements 为空、固定 disclaimer、无异常链/原值），不抛 port 原异常且不改变 source result：

- source issues `>64` 或 allowlist 缺失/extra/重复/digest 不一致；
- port timeout、异常、返回 `None`、坏 JSON、坏 schema、未知字段；
- candidate 试图添加/修改 decision、severity、issues、处置、formula 或 review action；
- issue/rule 引用错误、重复 issue、文本不等于 allowlist、声明数大于 issue 数；
- candidate/final canonical bytes `>8 KiB`（8 KiB + 1 必须 unavailable，不截断继续）。

error/unavailable、`repr`、日志和测试输出不得包含 fixture 原文、candidate 自由文本、Prompt、凭据或异常 cause/context。模块不得记录日志。

### 4. injected fake port 与资源

- 最小 Protocol 只提供同步 `generate(input)`；测试使用 in-memory fake，timeout 以 `TimeoutError` 故障注入，不等待真实时间；
- port 不是 authority，所有输出必须经 verifier；port 不获得 L5-1 source object 或任何外部 client；
- 最大合法 64 issues、接近但不超过 8 KiB 输出执行 1,000 次，记录 Python/CPU/方法；p95 `<50 ms`、p99 `<100 ms`、RSS 增长 `<64 MiB`；不得放宽门槛关闭 finding。

## 必须先红

在生产模块不存在的 clean release HEAD，先新增测试并运行 collection RED；必须因缺少 `app.agent_runtime.sandbox_explanation` 失败，不得 skip、xfail、动态替身或先写生产代码。

至少保留以下测试名：

- `test_l5_2_valid_exact_references_attach_without_changing_l5_1_result`
- `test_l5_2_attempted_decision_severity_or_issue_mutation_is_unavailable`
- `test_l5_2_unsupported_text_wrong_reference_and_duplicate_are_unavailable`
- `test_l5_2_timeout_exception_bad_schema_are_fixed_chainless_unavailable`
- `test_l5_2_issue_limit_plus_one_rejected_before_port_call`
- `test_l5_2_output_8kib_plus_one_is_unavailable_without_truncation`
- `test_l5_2_result_and_nested_statements_are_immutable`
- `test_l5_2_source_result_and_digest_are_byte_identical_across_all_paths`
- `test_l5_2_port_input_is_minimal_and_contains_no_fixture_or_secret_fields`
- `test_l5_2_no_settings_env_data_model_network_gateway_legacy_review_record_export_import`
- `test_l5_2_thousand_maximum_explanations_are_resource_bounded`

## 允许修改范围

- 新增 `app/agent_runtime/sandbox_explanation.py`
- 新增 `tests/test_l5_2_sandbox_safety_explanation.py`
- 新增/更新 `docs/dev-handoff/agent-refactor-l5-2-sandbox.md`

除此之外全部禁止。若无法在三个文件内完成，停止并由项目经理裁定；执行者不得修改 L5-1 模块/测试、PM 台账、任务书、配置、依赖或公共开关。

## 受控环境

沿用 L5-1 的完整不可用 loopback fake 覆盖：

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

不得读取/显示本地 `.env` 值；不得读取 ignored `data/`/`.codex_tmp`；不得启动应用、HTTP/E2E、容器、数据库、Redis、Milvus、模型/embedding Gateway 或网络。

## 验收命令

```powershell
uv run pytest tests/test_l5_2_sandbox_safety_explanation.py -q -rs
uv run pytest tests/test_l5_1_sandbox_safety_adapter.py -q -rs
uv run pytest tests/test_safety_rule_engine.py -q -rs
uv run pytest tests/test_l4_5_11_1_intake_privacy_projection.py tests/test_l4_5_11_2_runtime_privacy_guard.py -q -rs
uv run ruff check app/agent_runtime/sandbox_explanation.py tests/test_l5_2_sandbox_safety_explanation.py
uv run mypy app/agent_runtime/sandbox_explanation.py
uv run pytest tests/test_l0_1_contract.py -q -rs
uv run pytest -q -rs
uv lock --check
git diff --check
```

强制 `APP_ENV=sandbox-test` 的既有 defaults 测试冲突必须原样记录；另运行只移除 `APP_ENV`、保持全部 fake endpoints 的校准全量。不得修改 `tests/test_config.py` 或公共配置制造通过。

## 停止条件

- 需要修改允许列表外文件、L5-1 authority、依赖、配置、migration 或 feature flag；
- 需要把 generator/LLM 变成 decision authority，或允许自由文本在没有 exact allowlist match 时通过；
- 需要读取 `.env`、ignored/真实数据、数据库、外部日志或外部系统；
- 需要启动/连接模型、网络、API、Compose、DB、Gateway、Runtime、Legacy、review/record/export；
- 不能保持 L5-1 result/digest byte-identical、fixed unavailable、64 issues/8 KiB/资源门槛或错误无 payload；
- 出现真实/可关联个人数据、有效凭据、无法归属 diff 或范围外 P0/P1。

普通专项、回归、静态或资源失败属于合同内返工，不构成暂停理由。

## 交付要求

handoff 必须记录 release/delivery exact HEAD、实际 diff、真实 RED、GREEN、port input schema、non-interference/zero-call/固定 unavailable、64 issues/8 KiB/资源、全量校准、scope/tracked/clean 和未决限制。创建单一开发交付提交；只能写“已交付，申请验收”，不得自称 accepted、clinical approved 或 production ready。
