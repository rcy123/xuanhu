# ADR-005：Doctor Review 作为 LangGraph interrupt 硬门控

## 状态

已采纳（2026-07-09）

## 背景

当前实现中，Doctor Review（医师复核）通过以下机制实现：

1. **阶段挂起**：`Supervisor._advance_locked` 在 SAFETY → REVIEW 时将 `session.pending_review = True`、`session.status = "pending_review"`，并发射 `review.required` SSE 事件。
2. **阶段保护**：`Supervisor._ensure_stage_can_advance` 阻止从 REVIEW 阶段自动推进（`raise InvalidStageTransitionError`），同时 `/advance` API 的 `_precheck_stage` 在 `review` 阶段抛出 `PendingDoctorReviewError`。
3. **医师确认**：`POST /api/v1/consult/sessions/{session_id}/review` 由 `ReviewService` 处理，支持 three 条路径：
   - `confirm`：确认处方 → 推进到 `record` 阶段
   - `modify`：修改处方 → 二次安全审核（`SafetyRuleEngine.check()` with `formula_source="doctor_override"`）→ 通过后推进到 `record`
   - `reject`：否决处方 → 回退到 `prescription` 阶段
4. **病历生成前置校验**：`Supervisor._validate_doctor_review_for_record`（P7-2-fix B-014）在 RECORD → DONE 前强制校验 `doctor_review` 存在且 action 为 `confirm` 或 `modify`。

`agent-audit-report.md` 第 1 节第 6 条发现明确指出：**"Record 在 doctor review 验证前运行"**（问题 A-006），并已在 P7-2-fix B-014 中修复——但修复是在 Supervisor 中手动插入校验逻辑，而非使用框架中断。

当前机制的缺点：
- **无标准化 interrupt 语义**：暂停和恢复通过 `pending_review` 标志 + 手动校验 + 阶段锁实现，不是框架原生的 Human-in-the-loop 抽象。
- **恢复后漏洞**：Legacy 路径中，`modify` 动作在 `ReviewService._do_modify` 中完成二次安全审核并直接推进到 `record`，期间不存在标准的暂停/恢复 checkpoint。
- **并发不安全**：依赖 SessionLock 保证同一时刻只有一个线程操作会话，但如果锁被绕过，`pending_review` 标志可能被并发修改。

## 决策

**Doctor Review 使用 LangGraph 的 `interrupt()` / `Command(resume=...)` 实现，作为不可绕过的 hard gate。**

具体决策：

1. **`interrupt()`** 在安全审核通过后调用，将图执行暂停在 `review` 节点：
   ```python
   # 伪代码（L5 实现）
   def review_node(state: GraphState) -> GraphState:
       # SAFETY 节点已验证通过
       # 暂停执行，等待医师确认
       interrupt({
           "reason": "doctor_review_required",
           "modified_formula_ref": state.formula_draft_ref,
           "safety_rule_result_ref": state.safety_rule_result_ref,
       })
       # 此后的代码在 Command(resume=...) 触发后才执行
       return state
   ```
2. **`Command(resume=...)`** 在 `POST /review` API 的处理中调用，携带医师动作：
   ```python
   # 伪代码（L5 实现）
   # POST /review handler 内部
   command = Command(resume={
       "action": request.action,          # "confirm" | "modify" | "reject"
       "formula_override": ...,           # modify 时的修改后处方
       "feedback": request.feedback,
       "doctor_id": doctor_id,
       "review_id": review_id,
   })
   graph.astream(command, config={"configurable": {"thread_id": session_id}})
   ```
3. **硬门控保证**：
   - 没有 `Command(resume=...)` 时，图永远停在 `review` 节点，不会进入 `record` 节点。
   - `interrupt()` 后的所有节点（record → done）在中断解除前不可达。
   - 不存在"绕过 interrupt"的代码路径。绕过 interrupt 的唯一方式是修改图结构（移除 interrupt 调用），而图结构变更必须走正式的代码审查和部署流程。
4. **modify 后二次安全审核**：`Command(resume={"action": "modify", ...})` 触发图恢复后，条件边路由到安全审核节点（复用 `SafetyRuleEngine`）→ 通过则继续 → 不通过则再次 `interrupt()` 告知医师。

## 决策依据

1. **框架原生 Hard Gate**：LangGraph 的 `interrupt()` 是框架级的暂停机制，恢复必须通过 `Command(resume=...)` 携带指定载荷，不存在代码绕过路径——比手动检查 `pending_review` 标志更安全。
2. **标准 Human-in-the-loop 模式**：`interrupt()` / `Command(resume=...)` 是 LangGraph 和更广泛的 AI Agent 生态中 Human-in-the-loop 的标准模式，文档和社区支持完善。
3. **checkpoint 自动保存**：`interrupt()` 前的状态由 checkpointer 自动保存，恢复时从精确的暂停点继续，不需要手动管理"恢复后从哪个阶段开始"。
4. **可审计性**：`interrupt()` 和 `Command(resume=...)` 形成清晰的"暂停→恢复"事件对，可映射为 `audit_events`（`review.required` → `doctor.reviewed`），审计链完整。
5. **与现有 `/review` API 契约兼容**：`POST /review` 的请求体和响应结构保持不变，`Command(resume=...)` 的载荷将 API 请求参数转发给图。

## 明确边界

### Doctor Review Hard Gate 规则

1. **不可绕过性**：有效复核前（`action ∈ {confirm, modify}` + `review_id` 可追溯到 `doctor_reviews` 记录 + `action="modify"` 时二次安全审核通过），不得生成最终病历（`medical_records`），不得将 `status` 置为 `done`，不得发射 `session.done` 事件。
2. **modify 后二次安全审核**：医师修改处方后，新的 `final_formula` 必须重新执行完整的 `SafetyRuleEngine.check()`（`formula_source="doctor_override"`）。二次审核不通过时，图再次 `interrupt()`，告知医师安全问题，不得继续到 record。
3. **reject 后回退**：医师否决处方后，图回退到 `formula` 节点（重新生成处方草案），而非直接进入下一个阶段。回退计数计入 `rollback_counts`。
4. **审核记录不可变**：每次 Doctor Review 写入一条 `doctor_reviews` 记录（不可变），关联 `safety_rule_run_id`、`reviewed_by`、`action` 和 `formula_override`（如有）。

### 不负责/不允许

- **模型不得代表医师做 Decision**：LLM 不得生成 `confirm`/`modify`/`reject` 动作。只有来自 `POST /review` API（真人医师操作）的 `Command(resume=...)` 才能解除 interrupt。
- **模型不得控制阶段迁移**：从 review 到 record 的迁移由条件边决定（基于 `Command(resume=...)` 中的 action），模型不得通过自然语言回复"跳过"医师复核。
- **模型不得修改医师的 formula_override**：医师在 `modify` 中提交的处方修改是最终版本，Agent 不得覆盖或"优化"。

### 与 Legacy 路径的对比

| 维度 | Legacy（当前） | LangGraph（目标） |
|------|--------------|-----------------|
| 暂停机制 | `pending_review=true` + `status=pending_review` + 阶段锁 | `interrupt()` |
| 恢复机制 | `ReviewService.review()` 直接更新 session | `Command(resume=...)` |
| 硬门控 | `_ensure_stage_can_advance` + `_precheck_stage` + B-014 手动校验 | 框架级 `interrupt()` — 不可绕过 |
| 病历前置校验 | B-014 `_validate_doctor_review_for_record` | 图结构保证 review → record 不可跳过 |
| modify 二次审核 | `ReviewService._do_modify` 同步执行 | 图节点复用 `SafetyRuleEngine` + 条件边 |

## 正面影响

- **安全级别提升**：`interrupt()` 是框架级保证，不可通过代码修改绕过。
- **审计完整性**：中断/恢复对清晰映射到审计事件，不存在"中间状态"导致的审计缺口。
- **标准化**：使用 LangGraph 社区标准的 Human-in-the-loop 模式，降低维护和 onboarding 成本。
- **可测试性**：`interrupt()` 和 `Command(resume=...)` 可在单元测试中模拟，不依赖实际 HTTP 请求。

## 风险与代价

1. **interrupt 恢复时序**：在 `interrupt()` 到 `Command(resume=...)` 之间，Domain State 可能被其他操作修改（如医师在另一个客户端修改了处方）。缓解：`state_version` 校验在 `Command(resume=...)` 之前执行，版本冲突时拒绝恢复。
2. **Long-running interrupt**：如果医师长时间不响应，checkpoint 中保存的状态可能过期（如患者信息在另一个会话中更新）。缓解：Domain State 加载在恢复后执行，使用最新 PG 数据。
3. **checkpointer 依赖**：`interrupt()` 的暂停状态存储在 checkpointer 中，checkpointer 故障会导致无法恢复。缓解：checkpointer 使用 PostgreSQL（与业务库同实例），可用性等同于业务库。

## 迁移策略

1. **L0**：本文档定义 Doctor Review 为 Hard Gate / interrupt。
2. **L5**：在 Safety 与医师 HITL 子图中实现 `interrupt()` 和 `Command(resume=...)`，替代当前 `pending_review` + `ReviewService` 路径。
3. **API 兼容**：`POST /review` 的请求/响应 Schema 不变，内部从直接操作 DB 改为通过 `Command(resume=...)` 驱动图执行。
4. **Legacy 路径**：当前 `ReviewService` + `Supervisor._validate_doctor_review_for_record` 保持不变，直到 L9 下线。

## 回滚策略

- Feature Flag 将 LangGraph 路径切回 Legacy，恢复使用 `pending_review` 标志和 `ReviewService`。
- 所有已写入 `doctor_reviews` 的记录在两种路径下格式一致，回滚后数据无差异。

## 验证方式

- L0-1 契约测试验证本文档的不可变约束（hard gate、不可绕过、二次审核、模型禁止）
- L5 单元测试：`interrupt()` 后图暂停，`Command(resume={"action": "confirm"})` 后图继续到 record
- L5 单元测试：`Command(resume={"action": "modify"})` 后二次安全审核 → 通过/不通过 → 继续/interrupt
- L5 单元测试：`Command(resume={"action": "reject"})` 后回退到 formula 节点
- L5 单元测试：验证无 `Command(resume=...)` 时图永远停在 review 节点
- 回归：现有 `test_review_api.py` 所有测试在 LangGraph 路径下等价通过
