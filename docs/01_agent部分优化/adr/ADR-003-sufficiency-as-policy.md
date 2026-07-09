# ADR-003：Sufficiency Policy 由确定性规则控制而非模型

## 状态

已采纳（2026-07-09）

## 背景

当前实现中，`POST /api/v1/consult/sessions/{session_id}/messages` 在每轮问诊后触发 `SufficiencyAgent`（`app/agents/sufficiency.py`），这是一个 LLM Agent，由模型判断问诊信息是否充分。同时 `POST /api/v1/consult/sessions/{session_id}/advance` 在预校验中检查 `state_snapshot.sufficiency_report.sufficient`：

```python
# app/api/advance.py L119-128
if stage == "inquiry" and not force:
    snapshot = session.state_snapshot or {}
    suff = snapshot.get("sufficiency_report")
    sufficient = bool(suff.get("sufficient")) if isinstance(suff, dict) else False
    if not sufficient:
        raise InsufficientInquiryError(...)
```

`agent-audit-report.md` 第 1 节第 2 条发现明确指出：**"`/messages` 与 `/advance` 重复执行 Inquiry/Sufficiency，且 `next_question` 在 Sufficiency 判定之前已生成"**（问题 A-002）。模型控制 Sufficiency 决策存在以下风险：

1. **不确定性**：同一临床信息在不同的 LLM 调用中可能得出不同的充分性判断（`sufficient=true` vs `false`），导致阶段推进不可预测。
2. **责任不清**：Sufficiency 实际上是策略决策——"是否需要继续问诊"——而非需要 LLM 推理的医学判断。应属于 Harness 的 Policy Gate（《多Agent架构设计-Harness版.md》第 3.1 节），而非 Model Agent。
3. **冗余调用**：当前 `/messages` 在每个提问轮次都调用 SufficiencyAgent，即使问诊刚刚开始、明显信息不足，造成浪费。
4. **时序错误**：当前 SufficiencyAgent 在 InquiryAgent 生成 `next_question` 之后执行（`MessageService._run_inquiry_agents_locked`），即先问了问题再判断信息是否充分——实际上 `next_question` 已经用于下一轮对话，Sufficiency 判断滞后。

《多Agent架构设计-Harness版.md》第 4.2 节明确将 SufficiencyAgent 列为"不再使用模型代理的组件"，并替换为 `CompletenessPolicy`（确定性规则）。

## 决策

**Sufficiency（问诊完备性）判定不再由 LLM Agent 执行，改为由确定性策略引擎（`CompletenessPolicy`）执行。**

具体决策：

1. **`CompletenessPolicy`** 是一个确定性规则引擎（非 LLM），输入为 Domain State 中已采集的临床字段，输出为 `CompletenessResult(sufficient: bool, covered: list[str], missing: list[str], suggestions: list[str])`。
2. **判定逻辑**基于可审计的阈值规则，而非模型推理：
   - 必填维度（`chief_complaint`、`present_illness`、至少部分 `ten_questions` 维度）缺失 → `sufficient=false`
   - 安全必填（`pregnancy_status` 非 `unknown`、`allergies` 已询问）缺失 → `sufficient=false`
   - 所有必填维度已覆盖 → `sufficient=true`
   - 建议维度（`past_history`、`four_diagnosis` 等）缺失时不阻止推进，但在 `missing` 列表中标注
3. **当前 `SufficiencyAgent`（LLM）在 LangGraph 迁移后作为补充**（可选）：在 `CompletenessPolicy` 返回 `sufficient=true` 后，可选调用 `SemanticCriticAgent`（《多Agent架构设计-Harness版.md》第 4.1.7 节）对已采集信息的质量和一致性进行语义审查，但审查结果不得推翻 `CompletenessPolicy` 的 `sufficient=true` 结论。
4. **阈值定义在配置中**，不是模型 Prompt 中：
   ```python
   # 示例配置（L2 实现）
   class CompletenessConfig:
       required_dimensions: list[str] = [
           "chief_complaint", "present_illness",
           "ten_questions.cold_heat", "ten_questions.stool_urine",
           "ten_questions.diet", "ten_questions.sleep",
           "patient_info.pregnancy_status",
       ]
       suggested_dimensions: list[str] = [
           "past_history", "four_diagnosis.inspection",
           "four_diagnosis.palpation",
       ]
       min_inquiry_rounds: int = 2  # 最少问诊轮次（即使必填维度已覆盖）
   ```

## 决策依据

1. **确定性优先**：问诊完备性是一个策略决策，有明确的临床规则（"主诉、现病史、十问歌核心维度必须覆盖"），不需要 LLM 的模糊判断。确定性规则对医师透明、可审计、可申诉。
2. **与 Harness 架构一致**：《多Agent架构设计-Harness版.md》第 2.2 节"确定性规则优先"原则和第 4.2 节"不再使用模型代理的组件"明确将 Sufficiency 排除在模型代理之外。
3. **消除审记问题**：A-002（Inquiry/Sufficiency 重复）、A-003（next_question 时序错误）的根本原因是 Sufficiency 作为 LLM Agent 嵌在消息提交流中。将 Sufficiency 改为确定性规则后，策略判定独立于 LLM 调用，时序自然正确。
4. **减少 LLM 调用**：每轮问诊节省一次 LLM 调用（SufficiencyAgent），在 5–10 轮问诊的典型会话中可节省 5–10 次模型调用。
5. **与现有 API 契约兼容**：`MessageCreateResponse.sufficiency_report` 的 Schema（`SufficiencyReportData`：`sufficient: bool`、`covered: list[str]`、`missing: list[str]`、`suggestions: list[str]`）不需要变更，`CompletenessPolicy` 输出相同的 Schema。
6. **医师保留最终决定权但不改写 Gate**：Legacy 路径继续兼容 `force=true` 的现有行为；LangGraph 路径只允许从 `READY_FOR_REASONING` 执行 `/advance`，因此 `force=true` 不得把 `sufficient=false` 改为可推进，也不得绕过红旗（red flags）、过敏/妊娠/当前用药采集状态及其他医疗硬前置条件。未来若经单独架构决策引入人工覆盖，必须建模为独立、可审计的 `ManualOverrideRecord`，并由新的确定性 Gate 处理，不得将 `CompletenessPolicy` 改写为通过。

## 明确边界

### CompletenessPolicy 负责

- 基于 Domain State 已采集字段和阈值规则，判定 `sufficient: bool`
- 列出 `covered`（已覆盖维度）和 `missing`（缺失维度）
- 生成 `suggestions`（建议补充采集的维度）

### CompletenessPolicy 不负责

- **不调用 LLM**：所有判定基于纯函数式规则
- **不生成问诊问题**：下一问由模板或 `QuestionComposer` 生成；信息缺口由确定性 `GapSelector` 选择唯一缺口
- **不评估信息质量**：不对已采集信息的准确性/一致性进行语义判断（留给 `SemanticCriticAgent`）
- **不解释或执行人工覆盖**：LangGraph 路径的 `force=true` 不得把 `sufficient=false` 改为可推进，也不得绕过红旗、过敏/妊娠/当前用药采集状态等医疗硬前置条件。未来的医师人工覆盖必须先有独立 ADR，并建模为可审计的 `ManualOverrideRecord`，不得将 `CompletenessPolicy` 改写为通过

### LLM 的残余角色

- **IntakeExtractionAgent**（LLM）：从对话中抽取结构化事实（observations、safety delta、red flag candidates），不生成下一问、不判定完备性
- **QuestionComposerAgent**（可选 LLM）：只能对 `GapSelector` 确定性选择的唯一信息缺口进行措辞；优先使用模板，不得自选缺口、追加第二个问题或改变完备性结果
- **SemanticCriticAgent**（LLM，可选）：在 `CompletenessPolicy` 通过后，审查已采集信息是否有矛盾、模糊或不够具体，输出建议但不阻止推进

## 正面影响

- **可预测性**：每次问诊后的充分性判定完全确定、可复现。
- **可配置性**：阈值规则可根据科室/场景调整，无需修改 Prompt 或重新部署模型。
- **成本**：每会话减少 5–10 次 LLM 调用。
- **可审计性**：充分性判定的规则版本、阈值和执行结果写入 `audit_events`，支持回溯。

## 风险与代价

1. **阈值硬编码风险**：如果阈值过于宽松，可能过早推进导致信息不足；过于严格可能导致永不充分。缓解：通过配置化阈值 + L0-2 Golden E2E 基线测试验证。
2. **覆盖不全**：确定性规则只能覆盖预定义的维度，无法发现新类型的信息缺口。缓解：`SemanticCriticAgent` 可选启用来补充语义层面的质量审查。
3. **与传统中医实践的冲突**：部分中医师可能认为"问诊充分性"需要临床判断而非规则。缓解：`force=true` 和 `SemanticCriticAgent` 提供灵活性，最终决策权始终在医师。

## 迁移策略

1. **L0**：本文档定义 Sufficiency 为 Policy 而非 LLM Agent。
2. **L3**：在 IntakeSubgraph 中实现 `CompletenessPolicy`，替代当前 `SufficiencyAgent`。保留 `SufficiencyAgent` 代码但不再调用。
3. **Legacy 路径**：当前 `SufficiencyAgent`（LLM）继续运行，直到 L9 下线。`CompletenessPolicy` 仅在 LangGraph 路径生效。

## 回滚策略

- Feature Flag 将 `AGENT_RUNTIME_VERSION` 切回 `legacy`，所有新会话通过 Legacy 路径执行，Legacy 路径中的 `SufficiencyAgent`（LLM）继续使用。
- LangGraph 路径中 `CompletenessPolicy` 始终是确定性 Gate，不提供回退到 LLM SufficiencyAgent 的内部开关。模型不得决定充分性判定或阶段路由。
- Legacy `SufficiencyAgent` 只在 Legacy 会话中运行，LangGraph 会话不得调用。

## 验证方式

- L0-1 契约测试验证本文档的不可变约束
- L3 单元测试：`CompletenessPolicy` 在各组输入下的确定性输出（sufficient=true/false、covered/missing 列表）
- L3 Golden E2E 测试：标准问诊场景在 CompletenessPolicy 下的推进路径
- 回归：现有 `test_advance_api.py` 的 `test_advance_from_inquiry_insufficient` 测试在 LangGraph 路径下行为等价
