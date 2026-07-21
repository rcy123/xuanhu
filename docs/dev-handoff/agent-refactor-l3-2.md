# L3-2 TriagePolicy 与人工转介交接

## 修改文件与边界

- `app/schemas/triage.py`：新增严格、冻结、`extra="forbid"` 的 Triage 输入、处置、规则结果和策略结果 DTO。
- `app/agent_runtime/triage_policy.py`：新增纯函数 `evaluate_triage_policy()`、后续图接入点 `triage_gate_result()` 和显式兼容适配 `to_gate_result_schema()`，只消费 L3-1 已验证的 `RedFlagCandidate`。
- `app/agent_runtime/__init__.py`：导出 TriagePolicy 公共契约。
- `tests/test_l3_2_triage_policy.py`：新增 23 项本地纯数据测试。
- `docs/dev-handoff/agent-refactor-l3-2.md`：本交接文件。

未修改生产 API、Legacy Supervisor、runtime feature flag、Graph、Repository、DB、Outbox 或 `SafetyRuleEngine`。本任务没有创建 Git commit。

## Schema 与版本

- 输入：`TriagePolicyInput`
  - `schema_version="triage-input.v1"`
  - `input_state_version >= 1`
  - `red_flag_candidates: tuple[RedFlagCandidate, ...]`
- 输出：`TriagePolicyResult`
  - `schema_version="triage-result.v1"`
  - `policy_version="triage-red-flag.v1"`
  - `disposition`
  - `gate_result: TriageGateResult`
  - `rule_outcomes`
- 权威 Gate：
  - `TriageGateResult` 与 `TriageGateDetails` 均为 frozen、`extra="forbid"`，details 内部只使用 tuple 和 frozen 子 DTO。
  - `gate_name="triage"`
  - `policy_version="triage-red-flag.v1"`
  - `input_state_version` 与输入一致
- 通用兼容：
  - `to_gate_result_schema()` 显式生成短生命周期 `GateResultSchema` 副本；该可变 DTO 不作为 Triage 权威结果长期暴露。

## RedFlagCategory 映射

版本化规则表：`TRIAGE_RED_FLAG_RULES`，对外暴露为自定义 `FrozenTriageRuleRegistry`，内部只保存 tuple of frozen `TriageRule`，不保留普通 dict backing store。替换、删除、新增单个 category 规则，或原地降级单条规则都会失败。

| RedFlagCategory | rule_id | disposition |
|---|---|---|
| `severe_pain` | `red_flag.severe_pain.emergency_referral.v1` | `emergency_referral` |
| `breathing_difficulty` | `red_flag.breathing_difficulty.emergency_referral.v1` | `emergency_referral` |
| `altered_consciousness` | `red_flag.altered_consciousness.emergency_referral.v1` | `emergency_referral` |
| `severe_bleeding` | `red_flag.severe_bleeding.emergency_referral.v1` | `emergency_referral` |
| `neurologic_deficit` | `red_flag.neurologic_deficit.emergency_referral.v1` | `emergency_referral` |
| `high_fever` | `red_flag.high_fever.emergency_referral.v1` | `emergency_referral` |
| `other` | `red_flag.other.manual_review.v1` | `manual_review` |

导入时会检查规则表覆盖所有 `RedFlagCategory`。`OTHER` 保守进入人工复核。

## GateDecision 与 disposition

| 条件 | disposition | GateDecision |
|---|---|---|
| 无红旗候选 | `continue` | `PASSED` |
| 任意 `emergency_referral` 规则命中 | `emergency_referral` | `BLOCKED` |
| 仅 `manual_review` 规则命中 | `manual_review` | `BLOCKED` |

无候选是唯一 `continue/PASSED` 路径。任意候选存在都不会 `PASSED`。

## Canonical 重验与绕过防护

`canonicalize_triage_input()` 对每次调用无条件执行 canonical 重验：

1. 通过 `TriagePolicyInput.model_validate()` 接收输入；
2. 用目标 DTO 自身 serializer 生成 canonical JSON；
3. 用 `TriagePolicyInput.model_validate_json()` 重建；
4. 递归比较原对象与 canonical 对象，拒绝 subclass、`model_copy(update=...)`、`model_construct()` 隐藏字段。

错误版本、非法 `input_state_version`、非法嵌套候选固定拒绝为 `TRIAGE_INPUT_SCHEMA_INVALID`。隐藏授权字段固定拒绝为 `TRIAGE_INPUT_AUTHORITY_FIELD_FORBIDDEN`。错误消息只包含固定 code。

## 幂等与重复处理

策略按 `category + source_message_id` 去重，忽略模型提供的 `severity`、`confidence` 和 `evidence`。规则输出按 category 字符串和 source UUID 排序。相同语义候选的乱序或重复不会改变 `disposition`、`GateDecision` 或 details。

高危类别即使 `severity=low`、`confidence=0` 仍进入 `emergency_referral`。

## 隐私与模型边界

TriagePolicy 不调用 LLM、`ModelGateway`、`AgentRuntime`、Graph、DB、Repository 或外部服务。普通测试全部使用本地构造 DTO。

权威 `TriageGateDetails` 仅包含：

- `disposition`
- 去重后候选计数
- category 计数
- rule_id 列表
- rule outcome
- source message UUID 引用

不保存或输出 evidence 原文、患者身份、Prompt、原始模型输出、severity 或 confidence。

## 第 1 轮限定返工：深度不可变

返工点：

- `TRIAGE_RED_FLAG_RULES` 从普通 dict 改为结构不可变 mapping。
- `TriagePolicyResult.gate_result` 从通用可变 `GateResultSchema` 改为 frozen `TriageGateResult`。
- `details` 从嵌套 dict/list 改为 frozen `TriageGateDetails`，其中 `category_counts`、`rules`、`rule_ids`、`source_message_ids` 均为 tuple/frozen DTO。
- `triage_gate_result()` 返回不可篡改的 `TriageGateResult`。
- `to_gate_result_schema()` 是唯一显式通用 GateResultSchema 适配边界，返回的是副本，不影响权威结果。

新增回归覆盖：

- 将 `BLOCKED` 赋值为 `PASSED` 失败。
- 将 `details.disposition` 改为 `continue` 失败。
- 修改 `category_counts`、`rules`、`rule_ids`、`source refs` 失败。
- 替换呼吸困难规则为 `manual_review` 失败。
- 删除或新增规则失败。
- 所有修改尝试后重新 evaluate 仍为 `emergency_referral/BLOCKED`。

## AR-B-021 / AR-B-022 限定返工

本轮只修复规则注册表可变 backing store 问题，并保留 AR-B-021 已覆盖的深度不可变契约。

实现变更：

- 删除模块级 `_TRIAGE_RED_FLAG_RULES` 普通 dict。
- 删除 `MappingProxyType` 包装方案。
- 新增 `FrozenTriageRuleRegistry`，实现 `Mapping[RedFlagCategory, TriageRule]`，底层仅为 tuple，registry 自身拒绝属性替换和删除。
- `TriageRule` 继续保持 frozen，单条规则的 `disposition` 不可原地修改。

新增 AR-B-021/022 回归：

- 扫描 `app.agent_runtime.triage_policy` 模块属性，查找包含 `TriageRule` 的可变 dict backing store；必须不存在。
- 尝试通过公开 registry 替换、删除、新增 category 规则；必须失败。
- 尝试替换 registry 内部 `_rules` 属性；必须失败。
- 尝试修改单个 `TriageRule.disposition`；必须失败。
- 所有尝试后再次 evaluate，`BREATHING_DIFFICULTY` 仍为 `emergency_referral/BLOCKED`。

## 精确测试结果

```text
uv run pytest tests/test_l3_2_triage_policy.py -q -rs
23 passed in 0.68s

uv run pytest tests/test_l3_1_intake_extraction.py -q -rs
47 passed in 1.14s

uv run pytest -q -rs
1211 passed, 1 xfailed, 10 warnings in 212.49s (0:03:32)

uv run ruff check .
All checks passed!

uv run mypy app
Success: no issues found in 98 source files

AR-B-021/022 限定返工实际执行：

uv run pytest tests/test_l3_2_triage_policy.py -q -rs
23 passed in 0.68s

uv run ruff check app/agent_runtime/triage_policy.py app/schemas/triage.py tests/test_l3_2_triage_policy.py
All checks passed!

uv run mypy app/agent_runtime/triage_policy.py app/schemas/triage.py
Success: no issues found in 2 source files
```

warnings 均为既有 Pydantic 字段名提示、asyncpg cancellation runtime warning 和 Alembic `path_separator` deprecation。Golden xfail 为既有 Legacy 红旗基线。

## 未实现项与后续接入点

- 未实现 Graph node、conditional edge、interrupt、阶段迁移或 `/messages` 编排。
- 未实现 Repository、State 写入、GateResult ORM、DB migration 或 Outbox。
- 未实现 CompletenessPolicy、GapSelector、Question Composer、IntakeSubgraph 或 L3-3 至 L3-5。
- 未修改 `SafetyRuleEngine`；处方安全权威仍由原 Safety Gate 负责。

后续 L3-5 可调用 `triage_gate_result(TriagePolicyInput)` 取得 `GateResultSchema`，但必须在完整 L3-1 验证上下文和 Domain reducer/repository 边界内接入；不得把本任务的 passed report 或 L3-1 report 当作可伪造授权令牌。
