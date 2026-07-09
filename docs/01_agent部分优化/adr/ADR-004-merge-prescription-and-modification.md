# ADR-004：合并 PrescriptionAgent 和 ModificationAgent 为 FormulaDraftAgent

## 状态

已采纳（2026-07-09）

## 背景

当前实现将处方生成拆分为两个阶段、两个 Agent：

1. **PrescriptionAgent**（`P6-1`）：由 `SyndromeResult`（辨证结论）生成基础方（`FormulaResult`），含方名、组成、出处、方义。
2. **ModificationAgent**（`P6-2`）：在基础方基础上做加减化裁，生成 `ModifiedFormulaResult`，含修改列表（`ModificationItem`，动作为 add/remove/replace/adjust）。

`Supervisor._decide_next_stage` 将 PRESCRIPTION → MODIFICATION → SAFETY 设为刚性顺序：

```python
# app/agents/supervisor.py
if current == Stage.PRESCRIPTION:
    return Stage.MODIFICATION, None
if current == Stage.MODIFICATION:
    return Stage.SAFETY, None
```

当前拆分存在以下问题：

1. **串行 LLM 调用浪费**：生成基础方和加减需要两次独立的 LLM 调用（两次 Prompt 构建、两次模型推理），但临床实践中"辨证→开方→加减"是连续的思维过程，分两步没有临床必要。
2. **中间状态管理**：`base_formula` 作为中间产物存储，与 `modified_formula` 同时存在于 State 中（`state_snapshot.base_formula` 和 `state_snapshot.modified_formula`），增加了 State 复杂度。
3. **基础方可能直接被审核**：如果 `ModificationAgent` 未执行（如失败回退），安全审核可能直接审核 `base_formula`（`Supervisor._run_safety_rule_engine` 中的后备逻辑），而基础方未经加减调整可能不适用于当前患者。
4. **审记问题 A-002**：`agent-audit-report.md` 第 1 节指出流程中存在多次重复和冗余判断，处方生成的两段拆分加剧了这一问题。

《多Agent架构设计-Harness版.md》第 4.3 节明确说明"为什么合并 Prescription 和 Modification"，并给出了合并后的 `FormulaDraftAgent` 输出结构。

## 决策

**将 PrescriptionAgent 和 ModificationAgent 合并为一个 FormulaDraftAgent。**

具体决策：

1. **FormulaDraftAgent** 接收 `SyndromeResult`（辨证结论）和 `PatientInfo`（患者信息），一次性输出完整的 `FormulaDraft`：
   ```python
   class FormulaDraft(BaseModel):
       """合并后的处方草案。"""
       base_formula: FormulaResult          # 基础方
       modifications: list[ModificationItem] # 加减列表（可为空）
       final_formula: FormulaResult          # 最终处方（基础方 + 加减合并后的完整处方）
       rationale_combined: str               # 合并方义（含加减理由）
       confidence: float                     # 置信度 [0, 1]
   ```
2. **`final_formula` 是安全审核的直接输入**：不再有"先审核 base_formula 还是 modified_formula"的歧义，安全审核始终审核 `final_formula`。
3. **不再保留独立的 `base_formula` 和 `modified_formula` 中间产物**：Domain State 中只存储 `formula_draft`（合并产物）。`base_formula` 和 `modifications` 作为 `formula_draft` 的子字段保留，供医师复核时审查。
4. **Prescription 和 Modification 阶段合并为一个 `FORMULA` 阶段**：替换当前的 PRESCRIPTION → MODIFICATION → SAFETY 线性流为 FORMULA → SAFETY 两段流。

## 决策依据

1. **减少 LLM 调用**：一次 LLM 调用替代两次，节省约 40% 的处方生成推理成本。
2. **消除基础方后备逻辑**：`Supervisor._run_safety_rule_engine` 中的"modification 缺失时审核 base_formula"的后备路径不再需要，简化安全审核路由。
3. **提高处方质量**：LLM 在一次推理中完整考虑"辨证→选方→加减"的连贯思维，避免分步时第二步可能"忘记"第一步的方义考虑。
4. **简化 State**：Domain State 中处方相关字段从 `base_formula` + `modified_formula` + `safety_rule_result.normalized_formula` 三个减为 `formula_draft` + `safety_rule_result.normalized_formula` 两个。
5. **与 Harness 架构一致**：《多Agent架构设计-Harness版.md》第 2.7 节"最简 Agent 集合"原则和第 4.3 节的明确建议。
6. **不影响医师修改**：医师在 review 阶段仍可通过 `modify` 动作修改 `final_formula`，`FormulaOverride` Schema 不变。

## 明确边界

### FormulaDraftAgent 负责

- 接收 `SyndromeResult` + `PatientInfo` + 相关 Evidence → 输出 `FormulaDraft`
- `base_formula` 必须满足基本处方规范（有方名、有组成、有方义）
- `modifications` 可以为空列表（即不需要加减的情况）
- `final_formula` 是 `base_formula.composition + modifications` 的合并结果，Agent 必须确保剂量单位一致

### FormulaDraftAgent 不负责

- **安全审核**：安全审核始终由确定性 `SafetyRuleEngine` 执行，Agent 不参与安全决策
- **剂量上限判定**：剂量是否超标由 `SafetyRuleEngine` 的 `_check_dose_limits` 判定，Agent 只负责生成处方
- **医师复核**：Agent 不得绕过医师复核直接生效

### 与合并前的差异

| 维度 | 合并前（Legacy） | 合并后（LangGraph） |
|------|-----------------|-------------------|
| Agent 数量 | 2（Prescription + Modification） | 1（FormulaDraft） |
| LLM 调用次数 | 2 | 1 |
| 中间产物 | `base_formula` + `modified_formula` | `formula_draft.base_formula` + `formula_draft.final_formula` |
| 安全审核输入 | 优先 `modified_formula.formula`，回退 `base_formula` | 始终 `formula_draft.final_formula` |
| 阶段序列 | PRESCRIPTION → MODIFICATION → SAFETY | FORMULA → SAFETY |
| 医师可修改 | `modified_formula.formula` | `formula_draft.final_formula` |

### 不改变的内容

- **医师修改处方 Schema**：`ReviewRequest.formula_override`（`FormulaOverride` 含 `composition: list[HerbOverrideItem]`）保持不变
- **安全审核 Schema**：`SafetyRuleResult` 结构不变
- **SSE 事件**：`review.required` 事件的 payload 结构不变（仍包含 `modified_formula`，重命名为 `formula_draft` 由 LangGraph 路径处理，Legacy 路径不变）
- **API 契约**：`POST /review` 的请求/响应保持不变

## 正面影响

- **减少 LLM 调用**：每会话减少 1 次 LLM 调用（约 40% 处方生成成本）
- **消除回退逻辑**：安全审核路由简化，不再有"用 base_formula 替代 modified_formula"的复杂分支
- **State 精简**：处方相关字段从 3 个减为 2 个
- **处方一致性**：一次推理生成基础方和加减，避免分步时方义不一致

## 风险与代价

1. **单次 Prompt 变长**：FormulaDraftAgent 的 Prompt 需要同时指导"选方"和"加减"，可能比两个独立 Prompt 更长。缓解：通过 Prompt 版本管理和优化确保不超出模型上下文窗口。
2. **单次推理失败影响更大**：如果合并后的一次调用失败，整个处方生成失败（而合并前可能只影响基础方或加减之一）。缓解：Agent 层仍保持重试机制（`agent_max_retries`），且 checkpointer 保存失败前的执行状态。
3. **Legacy 兼容性**：LangGraph 路径使用 `formula_draft`，Legacy 路径使用 `base_formula` + `modified_formula`，两者 Schema 不同。缓解：在 LangGraph 路径的边界（API 响应构建）中，将 `formula_draft.final_formula` 映射为 API 响应中前端期望的结构。

## 迁移策略

1. **L0**：本文档定义合并决策和 `FormulaDraft` Schema。
2. **L4**（临床推理与方药子图）：实现 `FormulaDraftAgent`，在 `ReasoningSubgraph` 中替代 Prescription + Modification 两个节点。
3. **Legacy 路径**：`PrescriptionAgent` + `ModificationAgent` + 两个阶段保持不变，继续运行直到 L9 下线。
4. **API 兼容**：L9 前 API 响应中的处方结构通过兼容层映射，确保前端无感知。

## 回滚策略

- Feature Flag 控制：将 LangGraph 路径切回 Legacy，恢复使用两个独立 Agent 和线性阶段流。
- 处方数据不丢失：`formula_draft` 中的 `base_formula` 子字段可独立提取为旧 Schema 的 `base_formula`。

## 验证方式

- L0-1 契约测试验证本文档的不可变约束
- L4 `FormulaDraftAgent` 单元测试：在标准辨证输入下的输出包含 base_formula + modifications + final_formula 三个必需字段
- L4 Golden E2E 测试：标准辨证→处方场景的完整链路
- 回归：现有 `test_review_api.py` 中 modify 路径的测试（`formula_override`）在 LangGraph 路径下等价
