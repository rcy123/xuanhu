# L3-3 CompletenessPolicy 与停滞策略交接

## 修改文件清单

- `app/schemas/completeness.py`：新增 L3-3 严格、冻结、`extra="forbid"` 的 Completeness 输入、Domain State 投影、停滞、规则结果和权威 Gate DTO。
- `app/agent_runtime/completeness_policy.py`：新增纯函数 `evaluate_completeness_policy()`、`completeness_gate_result()`、显式兼容适配 `completeness_to_gate_result_schema()`、tuple-backed 冻结规则表和停滞阈值配置。
- `app/agent_runtime/__init__.py`：只导出 L3-3 公共契约。
- `tests/test_l3_3_completeness_policy.py`：新增 27 项本地纯数据测试。
- `docs/dev-handoff/agent-refactor-l3-3.md`：本交接文件。

未修改 Legacy SufficiencyAgent、MainGraph、生产 API、Repository、DB、Outbox、SafetyRuleEngine、`AGENT_RUNTIME_VERSION` 或项目经理维护的进度表内容。

## 输入/输出 Schema 与版本

- 输入：`CompletenessPolicyInput`
  - `schema_version="completeness-input.v1"`
  - `input_state_version >= 1`
  - `domain_snapshot: CompletenessDomainSnapshot`
  - `triage_gate: TriageGateResult`
  - `progress: CompletenessProgress`
- 输出：`CompletenessPolicyResult`
  - `schema_version="completeness-result.v1"`
  - `policy_version="completeness-policy.v1"`
  - `disposition`
  - `covered_dimensions`
  - `missing_required`
  - `missing_optional`
  - `conflicting_dimensions`
  - `stagnation`
  - `gate_result: CompletenessGateResult`
- 权威 Gate：
  - `gate_name="completeness"`
  - `policy_version="completeness-policy.v1"`
  - `input_state_version` 与输入一致
  - `decision` 按 disposition 固定映射

所有 L3-3 DTO 均 frozen、禁止额外字段；集合使用 tuple。公开入口每次都用目标 DTO serializer 生成 canonical JSON，再 `model_validate_json()` 重建，并递归检查原对象隐藏字段。

## InquiryDimension 与规则表

`InquiryDimension` 覆盖：

- 主诉症状、基本病程、现病史变化/伴随症状。
- 十问核心维度：寒热、汗出、头身、二便、饮食、胸腹、口渴、睡眠、经带、疼痛、呼吸。
- 安全采集状态：过敏、当前用药、重大疾病、妊娠、哺乳。
- 可选维度：既往史、四诊。
- 适用性辅助维度：性别、年龄、绝经、医师妊娠/哺乳适用性标记。

`COMPLETENESS_DIMENSION_RULES` 是 `FrozenCompletenessRuleRegistry`，底层只保存 tuple of frozen `CompletenessDimensionRule`，不使用 `MappingProxyType` 包装可变 dict。测试覆盖替换、删除、新增规则和模块内可变 backing store 扫描。

## 必需/可选维度

默认必需：

- `chief_complaint.symptom`
- `chief_complaint.course`
- `present_illness.change`
- `safety.allergy_status`
- `safety.medication_status`
- `safety.major_condition_status`

动态必需：

- 主诉类别决定的十问维度。
- 妊娠/哺乳适用性为 `applicable` 或 `unknown` 时，对应安全采集状态必需。

可选：

- `past_history`
- `four_diagnosis`

可选缺失会进入 `missing_optional`，但不会单独阻止 `ready/PASSED`。

## 主诉动态门槛

`COMPLETENESS_COMPLAINT_TEN_QUESTION_RULES` 是 tuple of frozen `ComplaintTenQuestionRule`：

- `respiratory`：寒热、呼吸、睡眠。
- `digestive`：二便、饮食、胸腹。
- `pain`：寒热、疼痛、睡眠。
- `gynecologic`：经带、寒热、二便。
- `urinary`：二便、寒热。
- `general`：寒热、二便、饮食、睡眠。

主诉类别来自当前有效结构化事实的 `chief_complaint.category.normalized_code`。未知类别回退到 `general`。策略不调用模型决定十问门槛。

## 妊娠/哺乳适用性算法

适用性优先级：

1. 医师显式适用性 fact：`patient.pregnancy_applicable` / `patient.lactation_applicable`，`true/applicable` 为适用，`false/not_applicable` 为不适用。
2. 性别、年龄、绝经状态：
   - male、`other_non_applicable`：不适用。
   - female 且绝经：不适用。
   - female 且年龄 `<12` 或 `>=60`：不适用。
   - female 且年龄 `12..59` 且未绝经：适用。
   - 任何必要事实缺失或冲突：unknown。

`unknown` 不会默认为不适用；对应 pregnancy/lactation 采集状态仍列为必需。

## 安全三态处理

过敏、当前用药、重大疾病必须是 `COLLECTED` 或 `EXPLICITLY_NONE` 才算覆盖。`UNKNOWN` 必定缺失。

妊娠/哺乳只有在适用性不是 `not_applicable` 时才要求采集状态覆盖；适用时 `UNKNOWN` 缺失，`COLLECTED` 或 `EXPLICITLY_NONE` 覆盖。投影 DTO 只保留采集状态和计数，不保留药物名、过敏原或疾病名。

## Triage 前置约束

Completeness 每次重验 L3-2 `TriageGateResult`：

- `gate_name` 必须是 `triage`。
- `policy_version` 必须是 `triage-red-flag.v1`。
- `input_state_version` 必须等于 Completeness 输入版本。
- 只有 `decision=PASSED` 且 `details.disposition=continue` 才允许 ready。

Triage blocked 时输出 `triage_blocked/BLOCKED`，不改写 Triage 处置，不执行转诊、interrupt、阶段迁移或持久化副作用。

## 冲突检测

策略只读取当前有效事实：

- 排除被 corrected/retracted/superseded 的旧 observation。
- 对同一 `InquiryDimension` 下多个当前有效值的规范 code/指纹做集合计数。
- 计数 `>=2` 输出 `CompletenessConflict(dimension, rule_id, current_value_count)`。

冲突结果不保存临床原文。`conflict` 固定映射为 `FAILED`，不生成澄清问题。

## 停滞阈值和人工接管信号

版本化配置 `COMPLETENESS_POLICY_CONFIG`：

- `no_new_facts_round_threshold=2`
- `max_followup_rounds=6`

达到任一阈值时：

- `stagnated=True`
- `manual_handoff_required=True`
- `disposition=stagnated`
- `GateDecision=BLOCKED`
- `reason_codes` 为固定枚举：`no_new_facts_threshold`、`max_followup_rounds`

`CompletenessProgress` 是显式、严格、canonical 重验的进度 DTO，但不是持久化授权。L3-5 才负责从持久化状态构造该 DTO，并在事务边界提交结果。

## GateDecision 映射

- `ready` → `PASSED`
- `incomplete` → `FAILED`
- `conflict` → `FAILED`
- `stagnated` → `BLOCKED`
- `triage_blocked` → `BLOCKED`

优先级为：`triage_blocked > stagnated > conflict > incomplete > ready`。

## Canonical 重验与绕过防护

`canonicalize_completeness_input()`：

1. 先用 `CompletenessPolicyInput.model_validate()` 接收输入。
2. 使用 `CompletenessPolicyInput.__pydantic_serializer__.to_json()` 生成 canonical JSON。
3. 用 `CompletenessPolicyInput.model_validate_json()` 重建。
4. 递归检查原始对象的 `__dict__`、`__pydantic_extra__`、dict/list/tuple 子结构，拒绝未声明字段。
5. 再校验 Triage gate 元数据和 state version。

固定拒绝码：

- `COMPLETENESS_INPUT_SCHEMA_INVALID`
- `COMPLETENESS_INPUT_AUTHORITY_FIELD_FORBIDDEN`
- `COMPLETENESS_TRIAGE_GATE_MISMATCH`

错误文本只含固定 code，不携带临床原文、身份信息或底层异常。

## 深度不可变实现

权威结果、details、rule outcomes、conflicts、stagnation 和 applicability 都是 frozen DTO，内部集合为 tuple。适配通用 `GateResultSchema` 必须显式调用 `completeness_to_gate_result_schema()`，该副本可变但不影响权威结果。

测试覆盖：

- 修改 `decision`、`disposition`、`missing_required`、`rule_ids`、停滞原因失败。
- 修改规则表、替换 registry 内部 `_rules`、修改单条规则失败。
- 修改兼容适配副本不影响权威结果。
- 篡改后重新 evaluate 结果保持原判定。

## 隐私边界

权威输出只保留：

- dimension/rule 标识。
- 覆盖、缺失、冲突计数。
- 停滞计数和固定原因码。
- policy version、input_state_version。
- 非敏感状态枚举。

不保存患者姓名、电话、证件号、门诊号、症状原文、药名、过敏原、疾病名、Prompt、原始模型输出、API key、Bearer token、DB URL 或底层异常文本。`CompletenessObservationFact` 只接收 fact key、规范 code 和 value fingerprint，不接收临床原文。

## 精确测试命令和结果

已执行：

```text
uv run pytest tests/test_l3_3_completeness_policy.py -q -rs
27 passed in 0.80s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py -q -rs
97 passed in 1.43s

uv run ruff check app/agent_runtime/completeness_policy.py app/schemas/completeness.py tests/test_l3_3_completeness_policy.py app/agent_runtime/__init__.py
All checks passed!

uv run mypy app/agent_runtime/completeness_policy.py app/schemas/completeness.py
Success: no issues found in 2 source files
```

最终完整门禁：

```text
uv run pytest tests/test_l3_3_completeness_policy.py -q -rs
27 passed in 0.83s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py -q -rs
97 passed in 1.43s

uv run pytest -q -rs
1242 passed, 1 xfailed, 10 warnings in 213.72s (0:03:33)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 100 source files

uv lock --check
Resolved 83 packages in 3ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py 和既有进度表的 LF -> CRLF 工作区转换 warning
```

warnings 均为既有 Pydantic 字段名提示、asyncpg cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 为既有 Legacy 红旗行为基线。

## 未实现项与 L3-4/L3-5 接入点

未实现：

- GapSelector 或唯一 `next_gap`。
- Question Composer 或 `next_question`。
- Graph node、conditional edge、interrupt、阶段迁移或 `READY_FOR_REASONING` 写入。
- `/messages`、`/advance` 或生产 API 改造。
- Repository、数据库、migration、Outbox、Redis、SSE。
- L3-4、L3-5 或 L4 内容。

L3-5 接入建议：

- 从权威 Domain State 构造 `CompletenessDomainSnapshot`，只投影当前有效结构化事实。
- 从持久化问诊回合构造 `CompletenessProgress`。
- 调用 `evaluate_completeness_policy()` 或 `completeness_gate_result()`。
- 在事务边界提交 gate 结果；不得把本 DTO 当持久化授权或人工覆盖授权。

## AR-B-023 第 1 轮限定返工

### 四项问题根因

1. `chief_complaint.category` 与 `chief_complaint.symptom` 被同时登记为 `CHIEF_COMPLAINT_SYMPTOM` 的覆盖来源，导致只有主诉类别、没有真实症状时也可能 `ready/PASSED`。
2. 冲突检测按 `InquiryDimension` 聚合后比较所有值，误把互补子字段视为同一值槽位，例如 category + symptom、stool + urine、inspection + palpation。
3. 人口学适用性推导中，female + 合法年龄但缺少 menopause 状态时直接返回 `applicable`，与“必要事实缺失或冲突为 unknown”的交接规则不一致。
4. Completeness 对传入的 Triage gate 只检查 `decision=PASSED` 和 `disposition=continue`，没有校验 continue/PASSED 路径必须是无候选、无规则、无来源引用的内部一致结果。

### 精确修改内容

- `app/agent_runtime/completeness_policy.py`
  - 从 `CHIEF_COMPLAINT_SYMPTOM` 规则的 `fact_keys` 中移除 `chief_complaint.category`。
  - `chief_complaint.category` 只由 `_complaint_category()` 从当前有效事实读取，用于动态十问规则选择，不再计入任何维度覆盖。
  - 新增 frozen `CompletenessConflictRule` 与 `COMPLETENESS_CONFLICT_RULES`，用于显式别名组冲突判断。
  - 新增固定规则 ID `completeness.conflict.same_canonical_fact_key.v1`，用于同一 canonical fact key 多个当前有效值的冲突判断。
  - `_conflicts_by_dimension()` 改为只比较同一 fact key 或显式别名组，不再把互补子字段按维度整体比较。
  - `_explicit_or_demographic_applicability()` 要求 female + 12..59 年龄段必须有明确 `menopause=false` 才返回 `applicable`；缺失、非法或冲突均返回 `unknown`。
  - `canonicalize_completeness_input()` 增加 `_triage_gate_is_internally_consistent()`，对不一致 Triage gate 固定拒绝为 `COMPLETENESS_TRIAGE_GATE_MISMATCH`。
- `tests/test_l3_3_completeness_policy.py`
  - `complete_general_facts()` 增加真实 `chief_complaint.symptom=headache`，不再用 category 代替症状。
  - 新增 AR-B-023 指定回归用例。

### category 与 symptom 职责分离

- `chief_complaint.symptom` 是主诉症状维度的覆盖来源。
- `chief_complaint.category` 只选择动态十问门槛。
- 只有 category、没有 symptom 时，`CHIEF_COMPLAINT_SYMPTOM` 必须在 `missing_required`，结果为 `incomplete/FAILED`。
- category 与 symptom 同时存在时允许共存，不产生 conflict。

### 新冲突判定规则

冲突只在以下两类条件下成立：

- 同一 canonical fact key 存在多个当前有效值。
- 显式版本化别名组中存在多个不同当前值，例如 `patient.sex` 与 `patient.gender`、`chief_complaint.course` 与 `chief_complaint.duration`。

互补子字段允许共存，不互判冲突，包括：

- `chief_complaint.category` + `chief_complaint.symptom`
- `ten_questions.stool` + `ten_questions.urine`
- `four_diagnosis.inspection` + `four_diagnosis.palpation`
- `present_illness.change` + `present_illness.associated_symptom`

### menopause unknown 适用性逻辑

无医师显式 applicability flag 时：

- female + 年龄 12..59 + `menopause=false`：`applicable`
- female + 年龄 12..59 + menopause 缺失/非法/冲突：`unknown`
- female + `menopause=true`：`not_applicable`
- male 或 `other_non_applicable`：`not_applicable`
- 年龄缺失：`unknown`
- 年龄 `<12` 或 `>=60`：`not_applicable`

`unknown` 不自动免除 pregnancy/lactation 采集要求。

### Triage continue/PASSED 内部一致性条件

当 `decision=PASSED` 或 `details.disposition=continue` 时，必须同时满足：

- `decision=PASSED`
- `details.disposition=continue`
- `candidate_count == 0`
- `category_counts == ()`
- `rule_ids == ()`
- `rules == ()`
- `source_message_ids == ()`

任一候选、规则或来源引用非空时，输入固定拒绝为 `COMPLETENESS_TRIAGE_GATE_MISMATCH`。对 blocked Triage gate，也校验 candidate/rule/category/source 计数内部一致性。

### 新增回归测试

新增并通过的重点测试：

- category 存在但无 symptom 时 incomplete/FAILED。
- category + symptom 同时存在时覆盖症状维度且不 conflict。
- stool + urine 互补子字段同时存在时不 conflict。
- 同一 fact key 多个当前值继续 conflict/FAILED。
- female + 合法年龄 + 缺少 menopause 时 applicability 为 unknown。
- female + 合法年龄 + `menopause=false` 时 applicability 为 applicable。
- female + `menopause=true` 时 applicability 为 not_applicable。
- Triage continue/PASSED 但 candidate_count 非零时固定拒绝。
- Triage continue/PASSED 但 category_counts/rule_ids/rules/source refs 任一非空时固定拒绝。
- 正常 `evaluate_triage_policy()` 生成的无候选 continue/PASSED gate 仍可进入 Completeness 判定。
- 深度不可变和乱序/重复幂等回归继续通过。

### AR-B-023 精确测试命令和结果

```text
uv run pytest tests/test_l3_3_completeness_policy.py -q -rs
38 passed in 0.88s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py -q -rs
108 passed in 1.30s

uv run pytest -q -rs
1253 passed, 1 xfailed, 10 warnings in 216.68s (0:03:36)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 100 source files

uv lock --check
Resolved 83 packages in 4ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py 和既有进度表的 LF -> CRLF 工作区转换 warning

git status --short --untracked-files=all
 M app/agent_runtime/__init__.py
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/completeness_policy.py
?? app/schemas/completeness.py
?? tests/test_l3_3_completeness_policy.py
```

warning 仍为既有 Pydantic 字段名提示、asyncpg cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 仍为既有 Legacy 红旗行为基线。

### 未实现范围声明

本返工未实现 GapSelector、`next_gap`、Question Composer、`next_question`、L3-5 IntakeSubgraph、Graph node/edge、interrupt、阶段迁移、生产 API、Repository、DB、migration、Outbox、Redis、SSE、SafetyRuleEngine 修改、Legacy SufficiencyAgent 修改或 `AGENT_RUNTIME_VERSION` 修改。

本返工未创建 Git commit。

## AR-B-023 第 2 轮限定返工

### 剩余问题根因

第 1 轮将 `chief_complaint.category` 从 `CHIEF_COMPLAINT_SYMPTOM.fact_keys` 中移除后，它不再进入 `_facts_by_dimension()`。当冲突检测只消费维度覆盖分组时，`chief_complaint.category` 这类辅助策略事实会逃逸同 canonical fact key 冲突检测。

结果是两个当前有效的 `chief_complaint.category`（例如 `general` 与 `pain`）不会产生 conflict，`_complaint_category()` 可能静默选择排序后的一个类别并继续动态十问判断。

### 冲突检测为何不能只消费维度覆盖分组

CompletenessPolicy 的权威判断不只依赖覆盖维度，还依赖辅助策略事实：

- `chief_complaint.category` 不覆盖症状维度，但决定动态十问门槛。
- 医师适用性 flag 不覆盖临床主诉，但影响妊娠/哺乳必需性。

因此，同 canonical fact key 冲突必须直接遍历 `_current_facts()` 的完整当前有效事实全集；维度映射只能作为审计归属和显式别名组判断的一部分。

### 辅助事实的冲突建模方式

本轮采用独立辅助维度：

- 新增 `InquiryDimension.CHIEF_COMPLAINT_CATEGORY = "chief_complaint.category"`。
- 该维度不是 required/optional coverage，不参与症状覆盖。
- 新增 `COMPLETENESS_AUXILIARY_FACT_DIMENSIONS`，将 `chief_complaint.category` 映射到辅助维度。
- `_conflicts_by_dimension()` 改为消费完整 `current_facts`：
  - 同一 canonical fact key 多个不同当前值直接产生 conflict。
  - 显式 `COMPLETENESS_CONFLICT_RULES` 继续处理版本化别名组。
  - 互补子字段仍不按 InquiryDimension 整体比较。

`chief_complaint.category` 冲突的审计输出只包含：

- `dimension=chief_complaint.category`
- `rule_id=completeness.conflict.chief_complaint.category.v1`
- `current_value_count`

不保存 category 原值或 value fingerprint。

### category 冲突时的动态规则行为

`_complaint_category()` 现在只在存在唯一规范类别时返回该类别。多个不同 category 当前值时返回固定 fallback，不静默选择任何输入值；同时冲突检测会让最终 disposition 固定为 `conflict/FAILED`，因此 fallback 不会掩盖冲突或产生 ready。

category 缺失或只有一个未知类别时，仍按既有契约使用 `general` fallback。

### 新增测试和精确结果

新增并通过：

- 两个不同当前 `chief_complaint.category` 必须 `conflict/FAILED`。
- 两个 category 交换输入顺序后完整结果一致。
- 两个相同规范 category 重复事实不产生 conflict。
- category 冲突即使其他 symptom/course/safety/ten-question 信息完整，也不能 ready。
- category 冲突输出不包含 `general`、`pain` 或 value fingerprint，只包含维度、rule ID 和计数。

第 1 轮回归继续通过：

- category 单独存在但无 symptom 仍 `incomplete/FAILED`。
- category + symptom 合法组合不冲突。
- stool + urine 等互补字段不冲突。
- 同一普通 canonical fact key 多个不同当前值仍 `conflict/FAILED`。
- menopause unknown/applicable/not_applicable 逻辑保持。
- 不一致 Triage continue/PASSED 固定拒绝，正常 Triage gate 可进入 Completeness。

### AR-B-023 第 2 轮精确门禁

```text
uv run pytest tests/test_l3_3_completeness_policy.py -q -rs
43 passed in 0.93s

uv run pytest tests/test_l3_1_intake_extraction.py tests/test_l3_2_triage_policy.py tests/test_l3_3_completeness_policy.py -q -rs
113 passed in 1.66s

uv run pytest -q -rs
1258 passed, 1 xfailed, 10 warnings in 236.83s (0:03:56)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 100 source files

uv lock --check
Resolved 83 packages in 3ms

git diff --check
exit 0；仅提示 app/agent_runtime/__init__.py 和既有进度表的 LF -> CRLF 工作区转换 warning

git status --short --untracked-files=all
 M app/agent_runtime/__init__.py
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/completeness_policy.py
?? app/schemas/completeness.py
?? tests/test_l3_3_completeness_policy.py
```

warnings 仍为既有 Pydantic 字段名提示、asyncpg cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 仍为既有 Legacy 红旗行为基线。

### 未实现范围声明

本返工未实现 GapSelector、`next_gap`、Question Composer、`next_question`、L3-5 IntakeSubgraph、Graph node/edge、interrupt、阶段迁移、生产 API、Repository、DB、migration、Outbox、Redis、SSE、SafetyRuleEngine 修改、Legacy SufficiencyAgent 修改或 `AGENT_RUNTIME_VERSION` 修改。

本返工未创建 Git commit。

## git status 摘要

`git status --short --untracked-files=all` 原样摘要：

```text
 M app/agent_runtime/__init__.py
 M "docs/01_agent\351\203\250\345\210\206\344\274\230\345\214\226/Agent\344\274\230\345\214\226\344\273\273\345\212\241\350\277\233\345\272\246\350\241\250.md"
?? app/agent_runtime/completeness_policy.py
?? app/schemas/completeness.py
?? tests/test_l3_3_completeness_policy.py
```

其中进度表修改是任务开始前已有，已保留未动。`docs/` 被项目 `.gitignore` 忽略，因此本交接文件不出现在普通 status 中，但已落盘。

## 提交声明

本任务未创建 Git commit。等待项目经理验收。
