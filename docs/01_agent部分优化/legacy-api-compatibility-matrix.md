# Legacy API / SSE 兼容矩阵

## 文档说明

本文档基于 **2026-07-09 当前代码实际行为**（Legacy 路径）逐端点记录 HTTP 方法、路径、请求体、成功响应、错误码、`state_version` 行为和迁移要求。目标架构列描述 LangGraph 迁移后（L9 前）的兼容承诺；"当前实现"与"目标架构"明确区分，不将目标设计冒充为已实现行为。

## 通用约定

- **Base URL**：`/api/v1/consult`
- **认证**：MVP 可选 `X-Doctor-Id` Header
- **状态版本**：`X-State-Version` Header（整数），非整数时返回 422 `VALIDATION_ERROR`
- **会话锁**：所有写操作通过 `SessionLock` 获取 Redis 会话锁，冲突时返回 409 `SESSION_BUSY`，响应含 `retryable: true`
- **响应 Envelope**：成功时 `{"code": "SUCCESS", "message": "ok", "data": {...}, "trace_id": "..."}`；错误时 `{"code": "...", "message": "...", "detail": "...", "retryable": true/false, "stage": null, "trace_id": "..."}`
- **幂等性**：除 GET 请求外，所有写操作不保证幂等（每次调用产生新的 state_version）。Legacy 路径不支持幂等键（idempotency key）。
- **并发控制**：基于 Redis 会话锁（`SessionLock`），TTL 90 秒，不等待。同一 session 的并发写请求串行化。

---

## 端点矩阵

### 1. POST /api/v1/consult/sessions/{session_id}/messages

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/messages.py` + `app/services/message.py` |
| **请求体** | `{"content": "string" (1–5000), "role": "doctor" \| "patient_proxy"}` |
| **成功状态码** | 200 |
| **成功响应 data** | `{"message_id": "...", "session_id": "...", "role": "...", "stage": "inquiry", "content": "...", "current_stage": "inquiry", "state_version": N, "created_at": "...", "agent_message": {"message_id": "...", "role": "agent", "agent_name": "inquiry", "stage": "inquiry", "content": "...", "agent_run_id": "...", "created_at": "..."} \| null, "sufficiency_report": {"sufficient": bool, "covered": [...], "missing": [...], "suggestions": [...]} \| null}` |
| **关键字段** | `state_version`（递增 2：医生消息 +1，Agent 回复 +1）、`agent_message`（InquiryAgent 的 next_question）、`sufficiency_report`（SufficiencyAgent LLM 输出） |
| **错误码** | 400 `SESSION_TERMINATED`（会话已终止）、404 `SESSION_NOT_FOUND`（会话不存在）、409 `INVALID_STAGE_TRANSITION`（非 inquiry 阶段）、409 `INVALID_STATE_VERSION`（版本冲突）、409 `SESSION_BUSY`（锁冲突）、422 `VALIDATION_ERROR`（content 为空或 role 非法）、503 `AGENT_TRIGGER_FAILED`（Agent 调用失败，医生消息已落库）、503 `MODEL_GATEWAY_UNAVAILABLE`（模型网关不可用） |
| **state_version** | 每轮 +2。`X-State-Version` 若携带则必须等于当前版本，否则 409。 |
| **LangGraph 迁移后兼容要求** | 请求体和成功响应 data 的 Schema 保持不变。`agent_message` 和 `sufficiency_report` 保持可选。`sufficiency_report.sufficient` 在 LangGraph 路径中由 `CompletenessPolicy`（确定性规则）生成，而非 LLM，但输出 Schema 一致。`agent_message.content` 在 LangGraph 路径中由模板或 `QuestionComposer` 基于 `GapSelector` 确定性选择的唯一信息缺口生成，不再由 InquiryAgent（LLM）生成。 |
| **已知差异（Inquiry/Sufficiency）** | Legacy：每轮 POST /messages 后调用 InquiryAgent + SufficiencyAgent（两个 LLM 调用）。LangGraph：每轮 POST /messages 后触发 IntakeSubgraph。IntakeExtractionAgent 只抽取结构化事实（observations、safety delta、red flag candidates），不生成下一问、不判定完备性。信息缺口由确定性 `GapSelector` 选择，下一问由模板或 `QuestionComposer` 生成。`CompletenessPolicy` 始终是确定性 Gate，模型不得决定充分性或阶段路由。`sufficiency_report` 由确定性规则而非 LLM 生成。`state_version` 递增节奏可能不同。 |
| **L9 前是否允许变化** | 允许 API 内部实现变化（Agent 调用链、state_version 递增节奏），禁止请求/响应 Schema 变化，禁止错误码语义变化。 |
| **回滚预期行为** | Feature Flag 切回 Legacy：请求/响应 Schema 不变，`sufficiency_report` 恢复由 `SufficiencyAgent`（LLM）生成，`state_version` 递增节奏恢复当前行为。 |

### 2. GET /api/v1/consult/sessions/{session_id}/messages

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/messages.py` + `app/services/message.py` |
| **请求参数** | Query: `before`（游标 message_id）、`limit`（1–100，默认 50）、`stage`（可选阶段过滤） |
| **成功状态码** | 200 |
| **成功响应 data** | `{"items": [{"id": "...", "session_id": "...", "role": "...", "agent_name": "..." \| null, "stage": "...", "content": "...", "structured_delta": {...} \| null, "agent_run_id": "..." \| null, "created_at": "..."}], "has_more": bool, "next_cursor": "..." \| null}` |
| **关键字段** | `has_more`（是否还有更多）、`next_cursor`（下一页游标，无更多时为 null）、`structured_delta`（Agent 消息的结构化输出） |
| **错误码** | 404 `SESSION_NOT_FOUND` |
| **state_version** | 不适用（只读操作） |
| **LangGraph 迁移后兼容要求** | 响应 Schema 完全不变。分页游标机制不变。`structured_delta` 字段在 LangGraph 路径中继续包含 Agent 结构化输出（`InquiryAgentOutput`、`FormulaDraft` 等）。 |
| **L9 前是否允许变化** | 不允许 Schema 变化。允许 `items` 中新增 `agent_name` 值（如 `formula_draft` 替代 `prescription`+`modification`）。 |
| **回滚预期行为** | 完全兼容，只读操作无行为差异。 |

### 3. POST /api/v1/consult/sessions/{session_id}/advance

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/advance.py` + `app/agents/supervisor.py` |
| **请求体** | `{"target_stage": "..." \| null, "force": bool}` |
| **成功状态码** | 200 |
| **成功响应 data** | `{"session_id": "...", "current_stage": "...", "from_stage": "...", "state_version": N, "blocked_reason": null \| "...", "agent_name": "..." \| null, "trace_id": "..."}` |
| **关键字段** | `current_stage`（推进后阶段）、`from_stage`（推进前阶段）、`blocked_reason`（非空时表示进入 blocked） |
| **错误码** | 400 `INSUFFICIENT_INQUIRY`（sufficient=false 且未 force）、404 `SESSION_NOT_FOUND`、409 `PENDING_DOCTOR_REVIEW`（review 阶段挂起）、409 `INVALID_STAGE_TRANSITION`（done/blocked 终态或 review 阶段）、409 `INVALID_STATE_VERSION`（版本冲突）、409 `SESSION_BUSY`（锁冲突）、503 `MODEL_GATEWAY_UNAVAILABLE` |
| **state_version** | 推进成功时递增。通过 `X-State-Version` Header 校验客户端版本。 |
| **LangGraph 迁移后兼容要求** | 请求/响应 Schema 不变。内部路由从 `Supervisor._route_and_run` → `LangGraph StateGraph` 节点 + 条件边。请求中的 `force` 字段为兼容而保留，但 LangGraph 路径只允许从 `READY_FOR_REASONING` 执行 `/advance`；`force=true` 不得把 `sufficient=false` 改为可推进，也不得绕过红旗（red flags）、过敏/妊娠/当前用药采集状态及其他医疗硬前置条件。未来若引入医师人工推进语义，必须先通过独立 ADR，并建模为独立、可审计的 `ManualOverrideRecord`，不得将 `CompletenessPolicy` 改写为通过。`blocked_reason` 继续使用相同值集。 |
| **已知差异（阶段序列）** | Legacy 阶段序列：INQUIRY → SUFFICIENCY → SYNDROME → PRESCRIPTION → MODIFICATION → SAFETY → REVIEW → RECORD → DONE。LangGraph 阶段序列：INQUIRY → SYNDROME → FORMULA → SAFETY → REVIEW → RECORD → DONE（SUFFICIENCY 合并为 CompletenessPolicy 确定性 Gate 在 Intake 子图中，PRESCRIPTION + MODIFICATION 合并为 FORMULA，SYNDROME 保留为独立阶段边界）。API 响应中的 `current_stage` 值在 LangGraph 路径中可能返回 `formula`（而非 `prescription`/`modification`），`syndrome` 继续保留。 |
| **L9 前是否允许变化** | 允许 `current_stage` 值集变化（新增 `formula`、移除 `sufficiency`/`prescription`/`modification` 作为独立阶段）。禁止错误码语义和请求 Schema 变化。 |
| **回滚预期行为** | Feature Flag 切回 Legacy：恢复 9 阶段序列，`current_stage` 恢复为旧值集。 |

### 4. POST /api/v1/consult/sessions/{session_id}/review

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/review.py` + `app/services/review.py` |
| **请求体** | `{"action": "confirm" \| "modify" \| "reject", "formula_override": {"name": "..." \| null, "composition": [{"herb": "...", "dose": N \| null, "unit": "g", "note": "..." \| null}]}, "feedback": "..." \| null}` |
| **成功状态码** | 200 |
| **成功响应 data** | `{"session_id": "...", "action": "...", "current_stage": "...", "status": "...", "pending_review": false, "review_id": "...", "state_version": N, "original_formula": {...} \| null, "formula_override": {...} \| null, "feedback": "..." \| null, "safety_recheck": {"passed": true, "issues": [...]} \| null, "medical_record": null, "updated_at": "..."}` |
| **关键字段** | `review_id`（doctor_reviews 记录 ID）、`safety_recheck`（modify 时的二次安全审核结果）、`medical_record`（P7-1 中始终为 null） |
| **错误码** | 400 `SESSION_TERMINATED`、400 `FORMULA_OVERRIDE_REQUIRED`（modify 缺少 formula_override）、404 `SESSION_NOT_FOUND`、409 `INVALID_RESULT_ACTION`（无效 action）、409 `INVALID_STAGE_TRANSITION`（非 review 阶段或无 pending_review）、409 `INVALID_STATE_VERSION`、409 `SAFETY_REVIEW_BLOCKED`（modify 二次安全审核阻断，含 issues 列表）、409 `SESSION_BUSY`、409 `SAFETY_ACCEPT_RISK_UNSUPPORTED`（MVP 不可接受风险）、422 `VALIDATION_ERROR` |
| **state_version** | 每次确认成功后递增。`X-State-Version` 校验客户端版本。 |
| **LangGraph 迁移后兼容要求** | 请求/响应 Schema 不变。内部从 `ReviewService` 直接操作 DB 改为 `Command(resume=...)` 驱动 LangGraph 图恢复。`safety_recheck` 由 `SafetyRuleEngine.check(formula_source="doctor_override")` 执行，行为等价。`medical_record` 在 P7-2 后可能非 null，与本任务无关。 |
| **已知差异** | Legacy：确认后 `ReviewService` 直接更新 `session.current_stage` 和 `state_snapshot`。LangGraph：`Command(resume=...)` 触发图从 `interrupt()` 恢复，条件边路由到 record 节点。modify 的二次安全审核在图中执行（复用 `SafetyRuleEngine` 节点），而非 `ReviewService._do_modify` 内联。 |
| **L9 前是否允许变化** | 不允许请求/响应 Schema 变化。允许 `safety_recheck` 增加更多字段（如 `rule_version`、`execution_order`），但现有字段语义不变。 |
| **回滚预期行为** | Feature Flag 切回 Legacy：恢复 `ReviewService` 直接操作 DB，`review.required` 事件语义不变。 |

### 5. GET /api/v1/consult/sessions/{session_id}/record

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/record.py` + `app/services/record_service.py` |
| **请求参数** | Query: `version`（正整数或 `"latest"`，默认 latest） |
| **成功状态码** | 200 |
| **成功响应 data** | `{"id": "...", "session_id": "...", "version": N, "record_text": "...", "record_json": {...}, "disclaimer": "...", "edited_by_doctor": bool, "doctor_review_id": "..." \| null, "diff_from_previous": {...} \| null, "created_at": "...", "updated_at": "..."}` |
| **关键字段** | `version`（版本号，初版为 1）、`record_text`（可读病历文本）、`record_json`（结构化病历 JSON）、`disclaimer`（免责声明）、`edited_by_doctor` |
| **错误码** | 404 `SESSION_NOT_FOUND`、404 `RECORD_NOT_FOUND`（病历不存在）、422 `VALIDATION_ERROR`（无效 version 参数） |
| **state_version** | 不适用（只读操作） |
| **LangGraph 迁移后兼容要求** | 响应 Schema 完全不变。病历存储仍使用 `medical_records` 表，不受 LangGraph 影响。 |
| **L9 前是否允许变化** | 不允许 Schema 变化。允许 `record_json` 内部结构随业务 Schema 演进，但顶层字段不变。 |
| **回滚预期行为** | 完全兼容，只读操作无行为差异。 |

### 6. PUT /api/v1/consult/sessions/{session_id}/record

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/record.py` + `app/services/record_service.py` |
| **请求体** | `{"record_text": "..." \| null, "record_json": {...} \| null}`（至少提供一个） |
| **成功状态码** | 200 |
| **成功响应 data** | `{"id": "...", "session_id": "...", "version": N (≥2), "diff_from_previous": {...} \| null, "edited_by_doctor": true, "doctor_review_id": "..." \| null, "updated_at": "..."}` |
| **关键字段** | `version`（新版本号，每次编辑 +1）、`diff_from_previous`（变更摘要，含 changed_fields 列表） |
| **错误码** | 404 `SESSION_NOT_FOUND`、404 `RECORD_NOT_FOUND`（无已有病历作为基准）、409 `INVALID_STAGE_TRANSITION`（非 record/done 阶段）、409 `INVALID_STATE_VERSION`、409 `SESSION_BUSY`、422 `VALIDATION_ERROR`（空请求体） |
| **state_version** | 编辑成功后 session.state_version 递增。`X-State-Version` 校验。 |
| **LangGraph 迁移后兼容要求** | 请求/响应 Schema 不变。编辑仍在 `record` 和 `done` 阶段允许。病历版本化存储机制不变（`medical_records` 表新增行）。 |
| **L9 前是否允许变化** | 不允许 Schema 变化。 |
| **回滚预期行为** | 完全兼容。 |

### 7. GET /api/v1/consult/sessions/{session_id}/record/export

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/record.py` + `app/services/record_service.py` |
| **请求参数** | Query: `format`（必填，txt/json/md）、`version`（可选，默认 latest） |
| **成功状态码** | 200 |
| **成功响应** | 文件下载（Content-Disposition 含 RFC 5987 UTF-8 文件名编码）。Content-Type: `text/plain; charset=utf-8` / `application/json; charset=utf-8` / `text/markdown; charset=utf-8`。响应头含 `X-Trace-Id`。 |
| **关键字段** | 不使用标准 envelope，直接返回文件内容。`record.exported` 审计事件写入。 |
| **错误码** | 400 `EXPORT_FORMAT_UNSUPPORTED`（不支持格式）、404 `SESSION_NOT_FOUND`、404 `RECORD_NOT_FOUND` |
| **state_version** | 不适用（只读操作） |
| **LangGraph 迁移后兼容要求** | 响应格式和 Content-Type 完全不变。`build_export_content` 和 `build_markdown` 逻辑不变。 |
| **L9 前是否允许变化** | 不允许。 |
| **回滚预期行为** | 完全兼容。 |

### 8. POST /api/v1/consult/sessions/{session_id}/recover

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/recovery.py` + `app/services/recovery.py` |
| **请求体** | `{"action": "resume_from_pg_snapshot" \| "retry_current_stage" \| "rollback_to_stage" \| "terminate", "target_stage": "..." \| null, "reason": "..." \| null}` |
| **成功状态码** | 200 |
| **成功响应 data** | `{"session_id": "...", "current_stage": "...", "status": "...", "recovery_status": "normal", "action": "...", "updated_at": "..."}` |
| **关键字段** | `action`（执行的恢复动作）、`recovery_status`（恢复后为 `normal`）、`current_stage`（恢复后阶段） |
| **错误码** | 400 `RECOVERY_NOT_NEEDED`（会话状态正常无需恢复）、404 `SESSION_NOT_FOUND`、409 `STATE_RECOVERY_REQUIRED`（无 snapshot 且无 checkpoint 无法自动恢复）、409 `SESSION_BUSY`、422 `VALIDATION_ERROR`（无效 action 或缺少 target_stage） |
| **state_version** | 恢复成功后递增。不通过 `X-State-Version` Header 校验（recovery 是人工操作，无需版本校验）。 |
| **LangGraph 迁移后兼容要求** | 请求/响应 Schema 不变。**关键差异**：LangGraph 会话的恢复使用 `thread_id` + graph-version namespace + `Command(resume=...)`，而非 `RecoveryService`。两类会话（Legacy、LangGraph）恢复路径严格隔离，不得互相恢复。`POST /recover` API 在处理 LangGraph 会话时内部调用 LangGraph 恢复机制，但请求/响应 Schema 对调用方透明。 |
| **已知差异（恢复机制）** | Legacy：`RecoveryService` 直接操作 `consult_sessions` 表和 Redis checkpoint。LangGraph：LangGraph checkpointer 自动管理 checkpoint，`resume_from_pg_snapshot` 从 PG Domain State 重建，`retry_current_stage` 通过 `Command(resume=...)` 重新执行当前节点，`rollback_to_stage` 通过 `graph.update_state(config, state)` 修改 checkpoint 后 `Command(resume=...)` 恢复。两类恢复路径不得交叉。 |
| **L9 前是否允许变化** | 允许 API 内部实现变化（Legacy vs LangGraph 恢复路径），禁止请求/响应 Schema 变化，禁止错误码语义变化。 |
| **回滚预期行为** | Feature Flag 切回 Legacy：恢复 `RecoveryService` 直接操作 DB。已通过 LangGraph 恢复的会话不受影响（其 Domain State 在 PG 中一致）。 |

### 9. GET /api/v1/consult/sessions/{session_id}/stream

| 属性 | 值 |
|------|-----|
| **当前实现** | `app/api/stream.py` + `app/services/events.py` |
| **请求参数** | Query: `last_event_id`（可选）、Header: `Last-Event-ID`（可选） |
| **成功状态码** | 200 |
| **成功响应** | `text/event-stream` SSE 流。标准 SSE 格式：`event: <event_type>\nid: <event_id>\ndata: <json_payload>\n\n`。 |
| **关键 Header** | `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no` |
| **错误码** | 404 `SESSION_NOT_FOUND`（会话不存在；terminated 会话允许连接 stream） |
| **state_version** | 不适用（事件流，非请求-响应模式） |
| **LangGraph 迁移后兼容要求** | SSE 格式完全不变。所有 13 种事件类型继续支持（见 SSE 事件表）。LangGraph 路径中，`agent.started`、`agent.finished`、`agent.failed`、`stage.changed` 等事件由 `astream()` 的图事件映射生成，事件类型不变。`heartbeat` 和 `resync` 机制不变。 |
| **已知差异** | LangGraph 路径中 `agent.started`/`agent.finished` 可能包含额外的 `node_name` 字段（图节点标识），但保持向后兼容。`stage.changed` 事件的 `from_stage`/`to_stage` 值可能包含 `formula`（而非 `prescription`/`modification`）。 |
| **L9 前是否允许变化** | 允许 payload 中增加字段（向后兼容），允许 `current_stage` 值集变化。禁止事件类型名称变化，禁止移除现有事件类型，禁止 SSE 格式变化。 |
| **回滚预期行为** | Feature Flag 切回 Legacy：事件类型不变，`from_stage`/`to_stage` 恢复旧值集。 |

---

## SSE 事件覆盖

所有事件均通过 Redis Stream 存储（`xuanhu:events:{session_id}`），`EventService.append_session_event` 写入，`EventService.iter_sse` 读取并格式化为 SSE。

| # | 事件类型 | 当前触发点 | Payload 关键字段 | LangGraph 后兼容 | 备注 |
|---|---------|-----------|-----------------|-----------------|------|
| 1 | `stage.changed` | `Supervisor._advance_locked`（非 review、非 done、非 blocked 的阶段推进） | `from_stage`, `to_stage`, `state_version`, `trace_id` | 兼容。`from_stage`/`to_stage` 值可能含 `formula` | Legacy：每次 advance 成功写入一条。LangGraph：每个图节点完成后写入。 |
| 2 | `message.created` | `MessageService._append_message_created_event`（医生消息 + Agent 消息） | `message_id`, `role`, `agent_name`, `stage`, `content`, `structured_delta`, `agent_run_id`, `created_at` | 完全兼容 | 医生消息和 Agent 消息各写一条。 |
| 3 | `agent.started` | 当前未发射（保留事件类型） | — | 兼容。LangGraph 路径中通过 `astream()` 事件映射发射 | L1 实现 LangGraph 骨架后启用。 |
| 4 | `agent.finished` | 当前未发射（保留事件类型） | — | 兼容 | 同上。 |
| 5 | `agent.failed` | `MessageService._record_agent_failed`（Agent 失败时 best-effort 审计） | `agent_name`, `error_code`, `retryable`, `trace_id` | 兼容 | Redis Stream 写入失败不阻断错误返回。 |
| 6 | `review.required` | `Supervisor._advance_locked`（SAFETY 通过后进入 REVIEW） | `from_stage`, `to_stage`, `state_version`, `trace_id`, `modified_formula`, `safety_review` | 兼容。`modified_formula` 在 LangGraph 路径中替换为 `formula_draft` 结构（向后兼容字段保留） | 必须包含 `modified_formula`，禁止包含 `base_formula`（P3-3 契约）。 |
| 7 | `safety.blocked` | `Supervisor._advance_locked`（SAFETY 未通过且非 review/blocked 路径） | `from_stage`, `to_stage`, `state_version`, `trace_id`, `safety_review`, `rollback_counts` | 兼容 | 仅 rollback 路径（回退到 PRESCRIPTION/MODIFICATION）时发射。 |
| 8 | `session.blocked` | `Supervisor._enter_blocked` | `from_stage`, `blocked_reason`, `state_version`, `trace_id` | 兼容 | blocked 是终态之一，需通过 `/recover` 恢复。 |
| 9 | `session.done` | `Supervisor._advance_locked`（RECORD → DONE） | `from_stage`, `to_stage`, `state_version`, `trace_id`, `record_id` | 兼容 | 仅在成功生成 medical_records 后发射。 |
| 10 | `session.terminated` | `RecoveryService._do_terminate` | `action: "terminate"`, `reason`, `previous_status`, `previous_stage`, `terminated_at`, `terminated_by` | 兼容 | |
| 11 | `doctor.reviewed` | `ReviewService._do_confirm` / `_do_modify` / `_do_reject` | `action`, `review_id`, `to_stage`, `state_version` | 兼容 | |
| 12 | `heartbeat` | `EventService.iter_sse`（空闲时本地生成） | `timestamp` | 完全兼容 | 本地生成的事件，不写入 Redis Stream。间隔由 `sse_heartbeat_interval_seconds` 控制（默认 30s）。 |
| 13 | `resync` | `EventService.iter_sse`（`last_event_id` 不可用时本地生成） | `session_id`, `reason`, `timestamp` | 完全兼容 | 要求前端全量同步。 |

### SSE 安全约束（当前实现）

- 禁止字段进入 Redis Stream payload：`api_key`、`apiKey`、`authorization`、`password`、`secret`、`token`、`prompt`、`raw_prompt`、`raw_response`、`full_model_response`（`EventService._FORBIDDEN_PAYLOAD_KEYS`）
- `review.required` 必须包含 `modified_formula`，禁止包含 `base_formula`
- Redis Stream 最大长度 1000（`EVENT_STREAM_MAXLEN`），近似裁剪（`approximate=True`）

---

## 错误码汇总

| 错误码 | HTTP 状态码 | retryable | 涉及端点 | 说明 |
|--------|-----------|-----------|---------|------|
| `SESSION_NOT_FOUND` | 404 | false | 全部 | 会话不存在或 session_id 格式非法 |
| `SESSION_TERMINATED` | 400 | false | messages, review | 会话已终止，不可操作 |
| `SESSION_BUSY` | 409 | true | messages, advance, review, record, recover | 会话锁冲突（并发请求） |
| `INVALID_STAGE_TRANSITION` | 409 | false | messages, advance, review, record | 当前阶段不允许此操作 |
| `INVALID_STATE_VERSION` | 409 | true | messages, advance, review, record | 客户端 state_version 与服务端不一致 |
| `VALIDATION_ERROR` | 422 | false | messages, review, record, recover | 请求参数校验失败 |
| `INSUFFICIENT_INQUIRY` | 400 | false | advance | 问诊信息不足，不可推进（除非 force=true） |
| `PENDING_DOCTOR_REVIEW` | 409 | false | advance | review 阶段等待医师确认 |
| `INVALID_RESULT_ACTION` | 409 | false | review | 无效的 review action |
| `FORMULA_OVERRIDE_REQUIRED` | 400 | false | review | modify 时缺少 formula_override |
| `SAFETY_REVIEW_BLOCKED` | 409 | false | review | modify 后二次安全审核阻断 |
| `SAFETY_ACCEPT_RISK_UNSUPPORTED` | 409 | false | review | MVP 不支持接受风险继续 |
| `AGENT_TRIGGER_FAILED` | 503 | 取决于 Agent 错误 | messages | Agent 调用失败（医生消息已保存） |
| `MODEL_GATEWAY_UNAVAILABLE` | 503 | 取决于网关 | messages, advance | 模型网关不可用 |
| `RECOVERY_NOT_NEEDED` | 400 | false | recover | 会话状态正常，无需恢复 |
| `STATE_RECOVERY_REQUIRED` | 409 | false | recover | 无法自动恢复，需人工处理 |
| `RECORD_NOT_FOUND` | 404 | false | record | 病历不存在 |
| `EXPORT_FORMAT_UNSUPPORTED` | 400 | false | record/export | 不支持的导出格式 |

---

## Feature Flag 行为（L0-3 负责实现，本任务只定义边界）

| Feature Flag 值 | 会话创建 | POST /messages | POST /advance | POST /review | POST /recover | GET /stream |
|-----------------|---------|---------------|---------------|-------------|--------------|-------------|
| `legacy`（默认） | Legacy 路径 | Legacy Agent + Supervisor | Legacy Supervisor | Legacy ReviewService | Legacy RecoveryService | Legacy EventService |
| `langgraph` | LangGraph 路径 | LangGraph IntakeSubgraph | LangGraph StateGraph | LangGraph Command(resume=...) | LangGraph checkpointer 恢复 | LangGraph astream() → SSE 映射 |
| 切换条件 | 会话创建时确定，此后不可隐式切换。Feature Flag 只决定新会话的运行时身份，既有会话在生命周期内不得混合使用两种执行路径 | — | — | — | — | — |
| 交叉恢复 | — | — | — | — | Legacy 与 LangGraph 会话不得互相恢复 | — |
| 迁移期间 | Legacy 实现禁止删除 | — | — | — | — | — |

---

## 不在此矩阵中但相关的约束

1. **数据库 Schema 不变**：所有 API 依赖的 PG 表（`consult_sessions`、`consult_messages`、`doctor_reviews`、`medical_records`、`safety_rule_runs`、`audit_events`）在 L0–L9 期间不可有破坏性变更。
2. **Redis 键名约定不变**：`xuanhu:events:{session_id}`（事件流）、`xuanhu:checkpoint:{session_id}`（checkpoint）、会话锁键格式保持不变。
3. **`SafetyRuleEngine` 不变**：安全规则引擎的输入/输出 Schema 和行为在所有阶段不变。
4. **审计事件 Schema 不变**：`audit_events` 的 `event_type` 值集可扩展但不可移除现有类型。
5. **会话锁不变**：`SessionLock` 的 TTL（90s）和等待策略（不等待）不变。
