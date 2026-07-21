# L3-4 GapSelector 与 Question Composer 交接

## 修改文件清单

- `app/schemas/question.py`：新增 GapSelection、Question Composer、模型输出和执行 outcome 的严格冻结 DTO 与版本常量。
- `app/agent_runtime/gap_selector.py`：新增纯确定性 GapSelector、Completeness 权威重验、不可变优先级注册表和固定错误码。
- `app/agents/question_composer.py`：新增模板优先 Question Composer、不可变模板注册表、单问验证器和受限 AgentRuntime 模型兜底。
- `app/agents/prompts/question_composer_v1.jinja2`：新增版本化 Question Composer prompt。
- `app/agents/prompts/manifest.yaml`：注册 `question_composer` prompt。
- `app/agent_runtime/__init__.py`、`app/agents/__init__.py`：导出 L3-4 公共契约。
- `tests/test_l3_4_gap_question.py`：新增 23 项 L3-4 专项测试。
- `docs/dev-handoff/agent-refactor-l3-4.md`：本交接文件。

未修改 MainGraph、生产 API、Repository、DB、migration、Outbox、Redis、SSE、SafetyRuleEngine、Legacy InquiryAgent/SufficiencyAgent 或 `AGENT_RUNTIME_VERSION`。项目经理维护的进度表修改为任务开始前已有，本任务保留未动。

## 输入/输出 Schema 与版本

- GapSelector 输入权威：`CompletenessPolicyResult`
  - `schema_version="completeness-result.v1"`
  - `policy_version="completeness-policy.v1"`
  - `gate_name="completeness"`
- GapSelector 输出：`GapSelectionResult`
  - `schema_version="gap-selection-result.v1"`
  - `policy_version="gap-selector-policy.v1"`
  - `disposition=selected|no_selection`
  - `selection_kind=required|conflict|none`
  - `selected_dimension` 最多一个
  - `priority_rule_id`
  - `source_completeness_disposition`
- 模型兜底输入：`QuestionComposerModelInput`
  - 只包含 `selected_dimension`、`selection_kind`、固定 `safety_instruction`
- 模型兜底输出：`QuestionComposerModelOutput`
  - `schema_version="question-composer-model-output.v1"`
  - `question`
- 最终成功结果：`QuestionComposerResult`
  - `schema_version="question-composer-result.v1"`
  - `input_state_version`
  - `selected_dimension`
  - `selection_kind`
  - `question`
  - `source=template|model`
  - `template_version` 或 `prompt_version`

所有新增 DTO 均 `frozen=True`、`extra="forbid"`，集合使用 tuple 或 frozenset。

## Gap 优先级注册表

`GAP_PRIORITY_RULES` 是 `FrozenGapPriorityRegistry`，底层只保存 tuple of frozen `GapPriorityRule`，没有模块级可变 dict backing store。

required 路径优先级：

1. 安全采集：过敏、妊娠、哺乳、当前用药、重大疾病。
2. 主诉：症状、病程。
3. 现病史变化。
4. 主诉相关十问：寒热、二便、饮食、睡眠、呼吸、疼痛、经带、汗出、头身、胸腹、口渴。

conflict 路径为所有当前 L3-3 可能输出的冲突维度登记显式优先级，包括主诉类别、required 维度、optional 维度和适用性辅助维度。未登记的 required/conflict 维度固定拒绝为 `GAP_SELECTION_UNREGISTERED_DIMENSION`，不按字符串排序兜底。

## 路径行为

- `incomplete/FAILED`：只从 `missing_required` 去重后选择一个最高优先级 required 缺口。
- `conflict/FAILED`：只从 `conflicting_dimensions` 去重后选择一个最高优先级 conflict 维度。
- `ready/PASSED`：返回 `no_selection`，不生成问题。
- `stagnated/BLOCKED`：返回 `no_selection`，不生成问题。
- `triage_blocked/BLOCKED`：返回 `no_selection`，不生成问题。
- `missing_optional` 从不参与选择，ready 后不追问 optional。

## 模板注册表及覆盖范围

`QUESTION_TEMPLATES` 是 `FrozenQuestionTemplateRegistry`，底层只保存 tuple of frozen `QuestionTemplate`，没有模块级可变 dict backing store。

模板覆盖：

- 当前所有 required 可选维度。
- 当前所有 conflict 可选维度。
- conflict 模板只要求澄清一个维度，不暴露已有冲突值、value fingerprint 或身份信息。
- required 模板只询问一个缺失维度。

模板命中路径不创建 `AgentRuntime` 调用；专项验证 fake gateway 请求数为 0。

## 模型兜底触发条件

仅当同时满足以下条件才调用模型：

1. GapSelector 已输出 `selected` 且只有一个 `selected_dimension`。
2. `QUESTION_TEMPLATES` 对 `(selected_dimension, selection_kind)` 明确 miss。

模型兜底通过 `AgentRuntime.run()`，不直接调用 `ModelGatewayClient`。`AgentSpec`：

- `name="question_composer"`
- `version="question-composer-agent.v1"`
- `input_schema=QuestionComposerModelInput`
- `output_schema=QuestionComposerModelOutput`
- `tool_permissions={READ_STATE}`，无写 State、阶段迁移、数据库或批准权限
- `max_attempts=1`
- `temperature=0.1`
- `max_tokens=120`
- `timeout_seconds=10`

`RunSpec.prompt_version="question_composer_v1.jinja2"`，`total_attempt_budget=1`。普通测试使用 fake gateway，fallback 测试确认最多 1 次请求。

## 单问验证规则

模板和模型输出共用 `validate_single_question_text()`：

- 非空，长度不超过 160。
- 必须恰好一个 `?` 或 `？`，并以问号结尾。
- 不允许换行、编号列表或多问结构。
- 禁止“另外、此外、还有、同时、顺便、再问一下”。
- 禁止“以及、或者、或、和、and、or”拼接多任务。
- 禁止索取姓名、电话、手机号、身份证、门诊号、病历号、住址、地址。
- 禁止诊断、处方、开方、阶段、路由、安全批准、充分性/ready 等权威结论。
- 禁止 prompt、API key、Bearer、DB URL、raw_model_output。
- 禁止手机号和身份证号格式。

失败 outcome 只返回固定 `QuestionComposerFailureCode`，不回显原问题文本。

## Canonical 重验与防伪

GapSelector 每次公开调用都：

1. 用 `CompletenessPolicyResult.__pydantic_serializer__.to_json()` canonical 序列化。
2. 用 `CompletenessPolicyResult.model_validate_json()` 重建。
3. 递归检查原对象 `__dict__`、`__pydantic_extra__`、dict/list/tuple 子结构，拒绝未声明字段。
4. 递归拒绝 `next_gap`、`selected_gap`、`selected_dimension`、`route`、`stage`、`ready`、`force`、`manual_override`。
5. 验证 schema、policy、gate name、input_state_version。
6. 验证顶层 disposition、covered、missing、conflicting、stagnation、rule_outcomes 与 gate details 一致。
7. 验证 disposition 与 GateDecision 一致。

模型输出也 canonical 重验，并拒绝 `selected_dimension`、`next_gap`、`missing_dimensions`、`ready`、`sufficient`、`route`、`stage`、`force`、`manual_override`、`triage`、`safety_decision`、`questions`、`diagnosis`、`prescription` 等字段。

固定错误码包括：

- `GAP_SELECTION_INPUT_SCHEMA_INVALID`
- `GAP_SELECTION_AUTHORITY_FIELD_FORBIDDEN`
- `GAP_SELECTION_COMPLETENESS_RESULT_MISMATCH`
- `GAP_SELECTION_UNREGISTERED_DIMENSION`
- `QUESTION_INPUT_SCHEMA_INVALID`
- `QUESTION_PROMPT_CONTRACT_MISMATCH`
- `QUESTION_CONTEXT_BUILD_FAILED`
- `QUESTION_SELECTION_REQUIRED`
- `QUESTION_MODEL_OUTPUT_INVALID`
- `QUESTION_SINGLE_QUESTION_INVALID`

## 深度不可变实现

- 新增 DTO 均 frozen。
- 规则、优先级和模板条目均 frozen Pydantic DTO。
- 优先级和模板 registry 只有 tuple backing store，覆写、删除或替换内部 `_rules`/`_templates` 均抛 `TypeError`。
- 结果与模板副本被修改不会影响 GapSelector 权威选择。

## 隐私边界

Question Composer 模型上下文只包含：

- `selected_dimension`
- `selection_kind`
- 固定安全说明

不传患者原始消息、临床原文、完整 Domain State、Triage details、Completeness details、Prompt injection 文本、身份信息、value fingerprint、药物名、过敏原、疾病名、API key、Bearer token 或 DB URL。最终输出不包含临床原文、身份信息、Prompt 或原始模型输出。

## 精确测试命令与结果

```text
uv run pytest tests/test_l3_4_gap_question.py -q -rs
23 passed in 1.48s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py tests/test_l3_4_gap_question.py -q -rs
136 passed in 2.18s

uv run pytest -q -rs
1281 passed, 1 xfailed, 10 warnings in 228.62s (0:03:48)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 103 source files

uv lock --check
Resolved 83 packages in 4ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py、app/agents/__init__.py、app/agents/prompts/manifest.yaml 和既有进度表 LF -> CRLF 工作区转换 warning
```

warnings 为既有 Pydantic 字段名 shadow、asyncpg cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 为既有 Legacy 红旗行为基线。

## 未实现项及 L3-5 接入点

未实现：

- L3-5 IntakeSubgraph。
- Graph node、conditional edge、interrupt、阶段迁移或 ready 写入。
- `/messages`、`/advance` 或生产 API 改造。
- Repository、数据库、migration、Outbox、Redis、SSE。
- L4 内容。

L3-5 接入建议：

1. 先运行 L3-3 `evaluate_completeness_policy()` 获得权威 `CompletenessPolicyResult`。
2. 调用 `select_gap()` 获得 `GapSelectionResult`。
3. 对 `selected` 调用 `compose_question()`；对 `no_selection` 不生成问题。
4. 在 L3-5 的事务边界提交 gate/question 产物；不得让模型改变 Completeness/Triage 或阶段路由。

## AR-B-024 第 1 轮限定返工

### 五项问题根因

1. `compose_question()` 公开入口只接收裸 `GapSelectionResult`，调用方可合法构造 `selected + source_completeness_disposition=ready` 并绕过 Completeness 权威结果。
2. `select_gap(..., priority_registry=...)` 把优先级注册表暴露为公开生产参数，调用方可替换规则并改变首选缺口。
3. `compose_question(..., template_registry=...)` 把模板注册表暴露为公开生产参数，调用方可注入 key 与模板字段不一致的模板。
4. fallback 只校验 prompt version，并会在缺少 RunSpec 时自动构造随机 session/run 上下文，导致 state version、AgentSpec、attempt budget 等未绑定。
5. 单问身份校验只覆盖少量中文词，英文 phone/full name/ID number 及常见分隔变体可绕过。

### Composer 与权威 Completeness→Gap 的绑定方式

公开 `compose_question()` 改为接收 `completeness_result`，内部无条件调用公开 `select_gap(completeness_result)` 重新计算权威 selection。调用方若额外传入 `selection`，只作为校验副本；canonical 重验后必须与内部重算结果完全相等，否则固定返回 `QUESTION_SELECTION_AUTHORITY_MISMATCH`。

因此：

- ready、stagnated、triage_blocked 只能得到 `no_question`。
- incomplete 只能为权威 required gap 生成问题。
- conflict 只能为权威 conflict dimension 生成问题。
- state version 只来自权威 Completeness 结果。
- 伪造 `selected + source ready/stagnated/triage_blocked`、伪造 selected dimension 或伪造 priority rule 均不能生成问题。

### 生产优先级不可替换方案

公开 `select_gap()` 已删除 `priority_registry` 参数，并固定使用私有 `_GAP_PRIORITY_RULES_AUTHORITY`。导出的 `GAP_PRIORITY_RULES` 仍用于审计/测试读取；即使模块属性被重新绑定，公开 `select_gap()` 仍使用私有权威注册表。

未登记维度测试改为调用不可导出的私有 `_select_gap_with_priority_registry()` seam，生产入口不可选择替代注册表。

### 生产模板不可替换及模板一致性校验

公开 `compose_question()` 已删除 `template_registry` 参数，并固定使用私有 `_QUESTION_TEMPLATES_AUTHORITY`。模板 miss/fallback 测试改为调用不可导出的私有 `_compose_question_with_template_registry()` seam。

读取模板后会验证：

- registry key 等于 `(selection.selected_dimension, selection.selection_kind)`。
- `template.dimension == selected_dimension`。
- `template.selection_kind == selection_kind`。
- `template.template_version == question-template-registry.v1`。

任一不一致固定返回 `QUESTION_TEMPLATE_CONTRACT_MISMATCH`，且不调用模型兜底。本应模板命中的维度不能通过公开 composer 强制进入 fallback。

### RunSpec/AgentSpec 前置校验

fallback 不再自动创建 RunSpec；模板 miss 且缺少 RunSpec 时固定返回 `QUESTION_RUNTIME_CONTRACT_MISMATCH`，gateway 请求数为 0。

任何模型请求前校验：

- `run_spec.state_version == selection.input_state_version`
- `run_spec.agent_spec_version == agent_spec.version`
- `run_spec.prompt_version == question_composer_v1.jinja2`
- prompt loader 版本等于 `question_composer_v1.jinja2`
- `agent_spec.name == question_composer`
- `agent_spec.version == question-composer-agent.v1`
- `agent_spec.input_schema is QuestionComposerModelInput`
- `agent_spec.output_schema is QuestionComposerModelOutput`
- `agent_spec.model_policy.max_attempts == 1`
- `run_spec.total_attempt_budget == 1`
- `run_spec.stage == intake_question`
- 权限不含 WRITE_STATE、TRANSITION_STAGE、WRITE_DATABASE、APPROVE_SAFETY、APPROVE_DOCTOR_REVIEW
- deadline 尚未过期

任一不匹配均在模型请求前固定失败，不输出 RunSpec、Prompt 或底层异常文本。

### 中英文身份语义规则

身份请求校验扩展为中文关键词 + 英文 alias token 规则：

- 中文覆盖：姓名、名字、全名、电话、联系电话、手机、手机号、手机号码、身份证、身份证号、证件号、门诊号、挂号号、病历号、住院号、地址、住址、家庭住址、联系方式。
- 英文覆盖：name、full name、phone、phone number、mobile、mobile number、telephone、contact number、ID、ID number、identity number、identity card、national ID、outpatient number、medical record number、hospital number、address、home address、contact details。
- 英文匹配大小写不敏感，支持空格、下划线、连字符和点号分隔变体。

错误 outcome 只返回 `QUESTION_SINGLE_QUESTION_INVALID`，不回显原问题文本。现有中文模板全部通过单问验证。

### 新增对抗测试

本轮 L3-4 专项从 23 项扩展为 45 项，新增重点覆盖：

- 伪造 selected + source ready/stagnated/triage_blocked 不得生成问题。
- 伪造 selected dimension 或 priority_rule_id 固定拒绝。
- 公开 `select_gap()` 不接受替代 priority registry。
- 模块导出 `GAP_PRIORITY_RULES` 被重新绑定后，过敏与主诉同时缺失仍选择过敏。
- 公开 Composer 不接受替代 template registry。
- 模板 key 与 `template.dimension` 或 `template.selection_kind` 不匹配固定拒绝且模型请求数为 0。
- 应命中模板的维度不能被公开调用方强制走 fallback。
- fallback 缺少 RunSpec、RunSpec state version/agent spec version/prompt version/budget/stage/deadline 不匹配，均在 gateway 前失败。
- AgentSpec name/version/input schema/output schema/max_attempts/权限不匹配均在 gateway 前失败。
- 英文 phone/full name/ID/outpatient number/medical record number/home address 全部拒绝。
- 大小写、下划线、连字符、点号、多空格身份别名全部拒绝。
- 中文名字、联系方式、住院号等变体全部拒绝。
- 正常中文模板继续通过单问验证。

### 精确门禁结果

```text
uv run pytest tests/test_l3_4_gap_question.py -q -rs
45 passed in 1.53s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py tests/test_l3_4_gap_question.py -q -rs
158 passed in 2.06s

uv run pytest -q -rs
1303 passed, 1 xfailed, 10 warnings in 223.25s (0:03:43)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 103 source files

uv lock --check
Resolved 83 packages in 5ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py、app/agents/__init__.py、app/agents/prompts/manifest.yaml 和既有进度表 LF -> CRLF 工作区转换 warning
```

warnings 仍为既有 Pydantic 字段名 shadow、asyncpg/SQLAlchemy cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 仍为既有 Legacy 红旗行为基线。

### 未实现范围声明

本返工未实现 L3-5 IntakeSubgraph、Graph node/edge、interrupt、阶段迁移、生产 API、Repository、DB、migration、Outbox、Redis、SSE、SafetyRuleEngine 修改、Legacy InquiryAgent/SufficiencyAgent 修改、`AGENT_RUNTIME_VERSION` 修改或 L4 内容。

### AR-B-024 git status 摘要

```text
 M app/agent_runtime/__init__.py
 M app/agents/__init__.py
 M app/agents/prompts/manifest.yaml
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/gap_selector.py
?? app/agents/prompts/question_composer_v1.jinja2
?? app/agents/question_composer.py
?? app/schemas/question.py
?? tests/test_l3_4_gap_question.py
```

本返工未创建 Git commit。

## AR-B-024 第 2 轮限定返工

### 两项剩余问题根因

1. supplied selection 的 `_canonicalize_selection()` 只用 `GapSelectionResult` 目标 serializer 生成 JSON 并重建，没有递归检查原对象未声明字段。`model_copy(update={"route":"ready","force":true})` 的隐藏字段被 serializer 丢弃后，canonical supplied selection 与权威 selection 相等，从而成功生成问题。
2. fallback AgentSpec 前置校验只覆盖 name/version/schema/max_attempts 和写权限，未固定 temperature、max_tokens、timeout_seconds、verifier_chain、failure_policy 和完整只读权限集合。调用方可传入高温、超长输出、超长 timeout、空 verifier chain 的 AgentSpec 并发起模型请求。

### Supplied Selection Canonical 与未声明字段检测

`_canonicalize_selection()` 现在执行：

1. `GapSelectionResult.model_validate()` 接收输入。
2. `GapSelectionResult.__pydantic_serializer__.to_json()` 生成 canonical JSON。
3. `GapSelectionResult.model_validate_json()` 重建 canonical DTO。
4. 递归检查原对象与 canonical 对象：
   - `__dict__`
   - `__pydantic_extra__`
   - dict/list/tuple 嵌套结构
   - DTO subclass 附加字段
5. 先检查隐藏授权语义，再检查未声明字段；任一命中均在生成问题前失败。

新增固定错误码：

- `QUESTION_SELECTION_INPUT_INVALID`：schema、constructed 非法组合或重建失败。
- `QUESTION_SELECTION_AUTHORITY_FIELD_FORBIDDEN`：supplied selection 内含隐藏授权字段或未声明字段。

公开 `compose_question()` 中 supplied selection 失败只返回上述固定 code，不回显输入内容。

### 隐藏授权字段拒绝规则

supplied selection 顶层或任意嵌套 dict/list/tuple 中出现以下 key 均拒绝：

- `route`
- `stage`
- `ready`
- `sufficient`
- `force`
- `manual_override`
- `next_gap`
- `selected_gap`
- `missing_dimensions`
- `triage`
- `safety_decision`
- `questions`

覆盖 `model_copy()`、subclass extra、`model_construct()` 非法组合，以及隐藏字段嵌入 dict/list/tuple 的对抗路径。所有失败路径 gateway 请求数为 0。

### Question Composer 固定 AgentSpec 契约

新增并复用同一份固定常量：

- `QUESTION_COMPOSER_VERIFIER_CHAIN=("question_schema","single_question","no_authority_fields")`
- `QUESTION_COMPOSER_TOOL_PERMISSIONS=frozenset({READ_STATE})`
- `QUESTION_COMPOSER_FAILURE_POLICY=FailurePolicy()`

`build_question_composer_agent_spec()` 和 `_runtime_contract_failure()` 均引用这些常量，避免构建与校验两套配置漂移。

### ModelPolicy、Verifier、FailurePolicy、权限校验

除 model 名允许由配置或测试注入外，fallback 模型请求前精确校验：

- `model_policy.temperature == 0.1`
- `model_policy.max_tokens == 120`
- `model_policy.timeout_seconds == 10`
- `model_policy.max_attempts == 1`
- `verifier_chain == QUESTION_COMPOSER_VERIFIER_CHAIN`，顺序必须一致
- `failure_policy == QUESTION_COMPOSER_FAILURE_POLICY`
- `tool_permissions == QUESTION_COMPOSER_TOOL_PERMISSIONS`

同时保留第 1 轮校验：

- name/version/schema 固定
- RunSpec state/version/prompt/stage/budget/deadline 固定
- 禁止 WRITE_STATE、TRANSITION_STAGE、WRITE_DATABASE、APPROVE_SAFETY、APPROVE_DOCTOR_REVIEW

任一不匹配返回 `QUESTION_RUNTIME_CONTRACT_MISMATCH`，gateway 请求数为 0。

### 新增对抗测试

本轮 L3-4 专项从 45 项扩展为 55 项，新增重点覆盖：

- `model_copy(update={"route":"ready"})` 固定拒绝。
- `model_copy(update={"force":true})` 固定拒绝。
- `model_copy(update={"next_gap":"ten_questions.sleep"})` 固定拒绝。
- 同时包含多个隐藏授权字段固定拒绝。
- GapSelectionResult subclass 增加 `route` 固定拒绝。
- `model_construct()` 构造非法 disposition/kind/dimension 组合固定拒绝。
- 隐藏字段嵌入 dict/list/tuple 固定拒绝。
- 正常 supplied selection 继续成功。
- supplied selection 省略时 Composer 内部重算并成功。
- AgentSpec `temperature=2.0`、`temperature=0.100001` 固定拒绝。
- `max_tokens=200000`、`max_tokens=121` 固定拒绝。
- `timeout_seconds=86400`、`timeout_seconds=11` 固定拒绝。
- `max_attempts=2` 固定拒绝。
- `verifier_chain=()`、缺一项、顺序变化、增加额外 verifier 均固定拒绝。
- failure_policy 增加 retryable code 固定拒绝。
- tool_permissions 增加 `READ_EVIDENCE` 或为空均固定拒绝。
- 既有 name/version/schema/写权限 mismatch 回归继续通过。
- 正常固定 AgentSpec + fake model 仍最多 1 次请求并成功。

### 原第 1 轮回归保持情况

继续保持：

- Composer 内部重算权威 selection。
- ready/stagnated/triage_blocked 不生成问题。
- 伪造 selected dimension/priority rule 固定拒绝。
- 公开生产入口不能替换优先级注册表。
- 公开生产入口不能替换模板注册表。
- 模板 key/dimension/kind/version 绑定。
- RunSpec state version、agent spec version、prompt、stage、budget、deadline 严格匹配。
- fallback 缺少 RunSpec 时不调用模型。
- 英文 phone/full name/ID/outpatient/medical record/address 拒绝。
- 中文身份信息变体拒绝。
- 模板命中时模型请求数为 0。
- fallback 最多一个 fake-model 请求。
- 模型不能输出 route、ready、next_gap 或 selected_dimension。
- 单问、多问、并列问和权威文本验证继续有效。
- 输出保持深度不可变和隐私安全。

### 第 2 轮精确门禁结果

```text
uv run pytest tests/test_l3_4_gap_question.py -q -rs
55 passed in 1.40s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py tests/test_l3_4_gap_question.py -q -rs
168 passed in 2.22s

uv run pytest -q -rs
1313 passed, 1 xfailed, 10 warnings in 216.24s (0:03:36)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 103 source files

uv lock --check
Resolved 83 packages in 3ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py、app/agents/__init__.py、app/agents/prompts/manifest.yaml 和既有进度表 LF -> CRLF 工作区转换 warning
```

warnings 仍为既有 Pydantic 字段名 shadow、asyncpg/LangGraph cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 仍为既有 Legacy 红旗行为基线。

### 第 2 轮未实现范围声明

本返工未实现 L3-5 IntakeSubgraph、Graph node/edge、interrupt、阶段迁移、生产 API、Repository、DB、migration、Outbox、Redis、SSE、SafetyRuleEngine 修改、Legacy InquiryAgent/SufficiencyAgent 修改、`AGENT_RUNTIME_VERSION` 修改或 L4 内容。

### 第 2 轮 git status 摘要

```text
 M app/agent_runtime/__init__.py
 M app/agents/__init__.py
 M app/agents/prompts/manifest.yaml
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/gap_selector.py
?? app/agents/prompts/question_composer_v1.jinja2
?? app/agents/question_composer.py
?? app/schemas/question.py
?? tests/test_l3_4_gap_question.py
```

本返工未创建 Git commit。

## git status 摘要

`git status --short --untracked-files=all` 摘要：

```text
 M app/agent_runtime/__init__.py
 M app/agents/__init__.py
 M app/agents/prompts/manifest.yaml
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/gap_selector.py
?? app/agents/prompts/question_composer_v1.jinja2
?? app/agents/question_composer.py
?? app/schemas/question.py
?? tests/test_l3_4_gap_question.py
```

`docs/` 被项目 `.gitignore` 忽略，因此本交接文件不出现在普通 status 中，但已落盘。进度表修改是任务开始前已有，已保留未动。

## 提交声明

本任务未创建 Git commit。等待项目经理验收。
