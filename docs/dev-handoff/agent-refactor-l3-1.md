# L3-1 IntakeExtractionAgent 与抽取验证交接

## 交付范围与修改文件

- `app/schemas/intake.py`：版本化、冻结、`extra="forbid"` 的 Intake 输入/候选输出 DTO。
- `app/agent_runtime/intake_verifier.py`：独立、纯函数、无副作用的 Intake 专用验证边界；未修改 L2 `DEFAULT_VERIFIER_CHAIN`。
- `app/agents/intake_extraction.py`：基于现有 `AgentRuntime` 和 `ContextBuilder` 的单次执行入口及固定失败结果。
- `app/agents/prompts/intake_extraction_v1.jinja2`：L3-1 固定权限和 Prompt Injection 边界。
- `app/agents/prompts/manifest.yaml`：注册 `intake_extraction_v1.jinja2`。
- `app/agent_runtime/__init__.py`：公开 Intake 验证合约。
- `app/agents/__init__.py`：公开 Intake 执行入口。
- `tests/test_l3_1_intake_extraction.py`：47 项 fake-only 正常、拒绝、失败、来源、权限和隐私测试。
- `docs/dev-handoff/agent-refactor-l3-1.md`：本交接文档。

项目经理维护的 `docs/01_agent部分优化/Agent优化任务进度表.md` 在任务开始前已有工作区改动，本任务只读该文件，未覆盖、清理或追加任何内容。既有 `.claude/settings.local.json` 也未修改。

## Intake 输入与输出 Schema

Schema 版本为 `intake-extraction.v1`，通过 DTO `ClassVar` 和 JSON Schema 的 `x-schema-version` 固定，不向模型输出增加业务字段。所有 Intake DTO 均冻结、可序列化、禁止额外字段，并对字符串、集合数量、值域、置信度及关系做边界限制。

`IntakeExtractionInput` 只接收：

- 1～8 条本轮 `patient` 消息，每条含 UUID `message_id` 和最长 4000 字符内容；assistant 消息和重复 ID 被拒绝。
- 最多 128 条最小历史 active fact，只含 observation ID、fact key、value/normalized value，用于去重和识别显式更正/撤回。

`IntakeExtractionOutput` 的字段精确且仅有：

- `decision`：`extracted | needs_clarification | abstained`；
- `observations`；
- `patient_safety_delta`；
- `red_flag_candidates`；
- `ambiguities`。

不存在 `next_question`、`route`、`stage`、`ready`、充分性、GateResult、安全通过或医师批准字段。`ObservationDelta` 只表达 add/correct/retract 候选；correct/retract 必须引用输入中已有的 active observation，不生成权威 Observation 主键、状态版本或时间戳。

## Safety 三态

allergy、pregnancy、lactation、medications、major conditions、contraindications 均保留：

- `unknown`：无值且无 source；
- `explicitly_none`：无值但必须有本轮 patient source；
- `collected`：必须有合法值和本轮 patient source。

妊娠复用 `PregnancyValue`，哺乳复用 `LactationValue`。列表值必须非空、去重且有长度/数量限制。DTO 构造与 Intake verifier 均检查三态和值一致性，不把未询问解释成明确无。模型不能输出 safety passed/failed、处方可用性或临床批准。

## AgentSpec、Prompt 与权限

- Agent name：`intake_extraction`。
- AgentSpec version：`intake-extraction-agent.v1`。
- Prompt version：`intake_extraction_v1.jinja2`，已注册 manifest。
- output schema：`IntakeExtractionOutput`。
- tool permissions：仅 `READ_STATE`；无写 State、写数据库、阶段迁移、安全批准或医师批准权限。
- `ModelPolicy.max_attempts=1`，FailurePolicy 不允许重试。
- 调用方 `RunSpec.total_attempt_budget` 必须精确为 1，否则模型调用前固定拒绝。
- 当前项目声明的 Intake 目标阶段只允许现有 `inquiry`；其他 stage 在模型调用前拒绝。

执行入口 `execute_intake_extraction()` 只执行：严格输入校验、构造分层 Context、调用现有 `AgentRuntime`、运行 Intake 专用验证、返回已验证候选或固定失败码。它不导入或调用 Reducer、Repository、Outbox、Graph/checkpoint、API 或 Legacy Supervisor。

### 第 1 轮限定返工：输入实例不可绕过重验

公开入口不再以 `isinstance(input_payload, IntakeExtractionInput)` 作为已验证证明。每次调用都无条件执行 canonical 两阶段重验：先按目标 DTO 接收输入，再显式使用 `IntakeExtractionInput` 自身的 Pydantic serializer 生成规范 JSON，最后通过 `IntakeExtractionInput.model_validate_json()` 重建对象。该过程会重新运行所有嵌套 DTO validator，并且不采用子类可覆盖的 `model_dump()` 动态分派。

因此以下输入均在 Context/Prompt 构造和 `AgentRuntime.run()` 之前返回固定 `INTAKE_INPUT_SCHEMA_INVALID`，fake gateway 实际请求数为 0：

- `model_construct()` 构造的 assistant-only 当前消息；
- `model_construct()` 构造的重复 current message ID；
- `IntakeExtractionInput` 子类通过 `model_construct()` 绕过最小条数 validator 的空消息集合。

正常、完整验证过的 Intake DTO 仍走相同 canonical 重验并成功执行。返工未修改 L2 Runtime 通用行为。

### 第 2 轮限定返工：输出 canonical 边界与身份信息拒绝

AR-B-017/018 的修复限定在 L3 Intake 输出边界。`AgentRuntime` 返回的对象无论是否已经是 `IntakeExtractionOutput`，都必须先经过 `canonicalize_intake_output()`：

1. 使用 `IntakeExtractionOutput` 基类 serializer 生成 canonical JSON，关闭可能包含原始值的 serializer warning；
2. 通过基类 `model_validate_json()` 严格重建 DTO；
3. 将原始对象的 `__dict__`、`__pydantic_extra__`、子类/嵌套 BaseModel 或 dict 字段与 canonical Schema 递归比对，任何未声明或被 serializer 隐藏的字段固定拒绝；
4. 创建只替换 `output` 的新 `RunArtifact`，后续 verifier 只接收该 canonical artifact；成功结果只返回 canonical 基类 DTO，不返回原始模型对象。

由此，`model_construct()` 的字符串 decision 会先规范为 Enum，再由 decision/content 一致性规则拒绝；`model_copy(update={"route": ...})` 注入但被 Pydantic 常规 dump 隐藏的顶层字段会以 `INTAKE_AUTHORITY_FIELD_FORBIDDEN` 拒绝。

身份信息规则同时收紧为确定性递归检查：

- fact key 使用非字母数字分隔符 token 化并识别 `full_name`、`mobile_number`、`phone_number`、`national_id` 等别名；
- 任意嵌套 JSON key 中的 `patient_name`、`phone`、`id_card` 等身份 key 均拒绝；
- 手机号和身份证号允许空格、连字符等常见分隔格式后仍能识别并拒绝。

新增回归覆盖 `patient.full_name = "Alice"`、`{"patient_name": "Alice"}`、`contact.mobile_number = "138-0013-8000"`。AR-B-016 的 assistant-only、重复 message ID、DTO 子类三项输入绕过测试继续通过且 gateway 请求数均为 0。L2 Runtime 未修改。

### 第 3 轮限定返工：命名空间复合身份键拒绝

`_is_identity_key()` 对规范化 key 使用连续身份别名后缀匹配：key 必须等于身份别名，或以 `_<identity_alias>` 结尾。该规则保留原有 name/phone/mobile/telephone token 检查，同时让 `id_card`、`identity_card`、`national_id`、`outpatient_no`、`medical_record_no` 在 `patient.`、`contact.` 等任意命名空间下仍确定性命中。

Observation fact key 和任意嵌套 JSON key 均复用同一 `_is_identity_key()`，没有单独的弱化分支。专项新增并通过：`patient.id_card`、`patient.identity_card`、`patient.national_id`、`patient.outpatient_no`、`patient.medical_record_no` 和嵌套 `{"patient.id_card": "MASKED-ID"}`；全部返回 `INTAKE_IDENTITY_FACT_FORBIDDEN`、`output=None`。AR-B-016、AR-B-017 和原 AR-B-018 回归全部继续通过。

### 第 4 轮限定返工：统一复合身份别名规范化

身份别名只维护一份 canonical 语义集合，不再逐项维护下划线和无下划线字符串。初始化时统一移除 canonical alias 中的下划线；检查时先把输入 key 的非字母数字分隔符规范为 token，再计算所有连续 token 后缀的紧凑形式并与 canonical identity forms 比较。因此 `id_card/idcard`、`identity_card/identitycard`、`national_id/nationalid`、`outpatient_no/outpatientno`、`medical_record_no/medicalrecordno` 自动等价，且 `patient.`、`contact.` 等任意命名空间不影响识别。

Observation fact key 与嵌套 JSON key 仍统一调用 `_is_identity_key()`。新增 `patient.idcard`、`patient.identitycard`、`patient.nationalid`、`patient.outpatientno`、`patient.medicalrecordno` 和嵌套 `{"contact.medicalrecordno": "MASKED-ID"}` 回归；全部返回 `INTAKE_IDENTITY_FACT_FORBIDDEN`、`output=None`。AR-B-016、AR-B-017、AR-B-019 和原 AR-B-018 测试全部继续通过。L2 Runtime 未修改。

## Context 白名单与隐私处理

入口复用 L2 `ContextBuilder`，固定层次为 `system -> developer -> context -> user`，token 上限 6000、超限拒绝。context 顶层白名单只有 `historical_active_facts`；本轮消息只在 user 数据层，并带调用方给出的 message ID。手机号/身份证号样式不会被接受为候选事实；身份类 fact key（姓名、电话、证件号、门诊号、病历号）由 verifier 固定拒绝。

普通 Runtime recorder 仍只接收 L2 的最小 run metadata；本入口的错误 DTO 不保存完整 Prompt、模型原始输出、患者文本、API key 或底层异常。执行结果和验证报告仅包含固定 Enum、无自由文本 check、及绑定确切输出的 SHA-256 摘要。

## Intake 专用验证链与固定失败码

Intake 链按固定顺序验证：

1. exact output type 与 Schema 重验；
2. AgentSpec、run ID、trace ID、agent/prompt version、attempt=1 一致性；
3. stage allowlist；
4. observation/safety/red flag/ambiguity 的 source message 必须属于本轮 patient 消息；
5. Safety 三态和值一致性；
6. decision 与内容一致性；
7. 重复 observation、历史事实重复抽取、更正/撤回目标和 JSON 安全性；
8. 越权字段和身份事实。

主要固定 Intake failure code：`INTAKE_SCHEMA_INVALID`、`INTAKE_OUTPUT_TYPE_INVALID`、`INTAKE_AGENT_SPEC_INVALID`、`INTAKE_RUN_PROVENANCE_MISMATCH`、`INTAKE_STAGE_NOT_ALLOWED`、`INTAKE_SOURCE_NOT_ALLOWED`、`INTAKE_SAFETY_SEMANTICS_INVALID`、`INTAKE_DECISION_CONTENT_MISMATCH`、`INTAKE_DUPLICATE_OBSERVATION`、`INTAKE_HISTORICAL_FACT_REEXTRACTED`、`INTAKE_CORRECTION_TARGET_INVALID`、`INTAKE_VALUE_NOT_JSON`、`INTAKE_AUTHORITY_FIELD_FORBIDDEN`、`INTAKE_IDENTITY_FACT_FORBIDDEN`。Runtime timeout/unavailable/structured-output 等失败继续使用 L2 固定 `RuntimeErrorCode`；Prompt/Context 边界使用 `INTAKE_INPUT_SCHEMA_INVALID`、`INTAKE_PROMPT_CONTRACT_MISMATCH`、`INTAKE_CONTEXT_BUILD_FAILED`。

`DEFAULT_VERIFIER_CHAIN` 保持原实现和 DomainDelta-only 语义，未修改、跳过或削弱。Intake report 不是 canonical passed report，也没有任何 Reducer/Repository 提交授权能力。

## Provenance、去重与 Prompt Injection

- 每个候选 source 必须在 `IntakeExtractionInput.current_messages` 中且角色为 patient；伪造 UUID 和 assistant 来源均拒绝。
- 历史 active fact 原样重新抽取固定拒绝；同一输出中的重复 observation 操作固定拒绝。
- correction/retraction 只能指向输入的历史 active observation，并要求 fact key 一致；最终合法性仍留给后续 Domain Verifier/Reducer。
- Prompt 明确把患者指令、quoted instruction 和 injection 视为不可信数据。system/developer 权限约束与 user 数据分层；Schema、AgentSpec、source、stage 和单请求限制由确定性代码再次执行，不能被患者文本改变。

## 单次模型调用证据

专项 fake gateway 逐次统计 `actual_request_count`，验证成功、malformed、parse failure、gateway unavailable、timeout 和 Prompt Injection 场景均至多一次请求。AgentSpec `max_attempts=1`、空 retry policy、RunSpec budget 必须等于 1，且 L2 Runtime 对支持该参数的 Gateway 传 `max_requests=1`，因此 transport retry 和 structured fallback 不能增加请求。无效 stage/prompt/budget 在请求前拒绝，计数为 0。

## 精确测试结果

执行环境：Windows，Python 3.12.12，pytest 8.4.2，普通测试全部使用 fake gateway，未调用真实模型。

```text
uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
47 passed in 1.62s

$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest tests/test_l2_1_domain_schemas_and_migration.py tests/test_l2_2_agent_runtime.py tests/test_l2_3_context_builder.py tests/test_l2_4_verifier_reducer.py tests/test_l2_5_repository_outbox.py -q -rs
68 passed, 9 warnings in 7.55s

uv run pytest tests/test_inquiry_agent.py tests/test_base_agent.py tests/test_gateway.py -q -rs --basetemp=<workspace-isolated-dir>
78 passed, 1 warning in 3.79s

$env:DB_URL='postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu'
uv run pytest -q -rs
1170 passed, 1 xfailed, 11 warnings in 213.34s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 96 source files

uv lock --check
Resolved 83 packages in 1ms

git diff --check
exit 0（仅既有 LF -> CRLF 提示）
```

第 1 轮限定返工完成后指定门禁的精确结果：

```text
uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
28 passed in 1.15s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 96 source files

uv lock --check
Resolved 83 packages in 5ms

git diff --check
exit 0（仅既有 LF -> CRLF 提示）
```

第 2 轮限定返工完成后指定门禁的精确结果：

```text
uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
35 passed in 1.06s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 96 source files

uv lock --check
Resolved 83 packages in 3ms

git diff --check
exit 0（仅既有 LF -> CRLF 提示）
```

第 3 轮限定返工完成后指定门禁的精确结果：

```text
uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
41 passed in 1.09s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 96 source files

uv lock --check
Resolved 83 packages in 3ms

git diff --check
exit 0（仅既有 LF -> CRLF 提示）
```

第 4 轮限定返工完成后指定门禁的精确结果：

```text
uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
47 passed in 1.62s

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 96 source files

uv lock --check
Resolved 83 packages in 5ms

git diff --check
exit 0（仅既有 LF -> CRLF 提示）
```

当前受限环境不能访问默认用户 uv cache、pytest Temp 和仓库 `.pytest_cache`。有效复跑通过 `UV_CACHE_DIR`、`TEMP`、`TMP` 或 `--basetemp` 指向工作区隔离目录；首次 Legacy 尝试因此在 fixture setup 得到 `56 passed, 22 errors`，没有测试断言失败，隔离复跑的完整 78 项全部通过。测试生成目录已在交付前清理。

warning 均为既有 Alembic `path_separator` 弃用提示、Pydantic 字段名提示、一次既有 asyncpg cancellation runtime warning，以及受限环境 pytest cache 写权限提示。Golden xfail 是既有 Legacy 红旗行为基线。

## 未完成项与后续接入点

- 本任务有意不提供 `IntakeExtractionOutput -> DomainDelta` 转换。L3-5 应在 Intake report 通过后，以纯函数注入 session/run/state version、UUID 和时间戳，再交给未削弱的 canonical Domain verifier/reducer；每次转换/提交边界仍必须重验，不能把 Intake report 当提交授权。
- L3-2 可只消费已验证的 `red_flag_candidates`，由确定性 `TriagePolicy` 产生权威 GateResult；本任务的 candidate 不做自动急诊、阻断或阶段迁移。
- L3-3/L3-4/L3-5 负责 Completeness、Gap/Question、Subgraph/API 和持久化编排；本任务不判断 ready/incomplete，不生成下一问，不修改 MainGraph 或生产 API。
- 未新增 migration，未修改数据库结构，未接入 RAG。

明确声明：本任务未调用真实模型；未实现 L3-2～L3-5、TriagePolicy、CompletenessPolicy、Question Composer、IntakeSubgraph、生产 API、Repository/数据库/Outbox、DomainState 修改、Graph checkpoint、Legacy 切流或任何禁止范围；未切换 `AGENT_RUNTIME_VERSION` 默认值。

## 最终工作区与提交声明

清理本任务测试临时目录后，`git status --short --untracked-files=all` 为：

```text
 M app/agent_runtime/__init__.py
 M app/agents/__init__.py
 M app/agents/prompts/manifest.yaml
 M docs/01_agent部分优化/Agent优化任务进度表.md
?? .claude/settings.local.json
?? app/agent_runtime/intake_verifier.py
?? app/agents/intake_extraction.py
?? app/agents/prompts/intake_extraction_v1.jinja2
?? app/schemas/intake.py
?? tests/test_l3_1_intake_extraction.py
```

其中进度表改动是任务开始前已有且本任务未触碰；`.claude/settings.local.json` 是既有无关文件。`docs/` 被项目 `.gitignore` 忽略，因此本交接文件不出现在普通 status 中，但已落盘。

本任务未创建 Git commit，也未自行把任务标记为验收通过。
