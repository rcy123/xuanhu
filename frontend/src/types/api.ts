/**
 * 悬壶 WebUI —— API 类型定义（P8-1 骨架）
 *
 * 与接口设计文档 §1、§4 及后端 `app/schemas/*` 对齐。
 * 仅做类型骨架：覆盖 envelope、错误码、会话、消息、SSE、recover、review。
 *
 * P8-1 不实现完整业务页面，但类型先落地，供 P8-2/P8-3 直接复用。
 * 字段命名与后端 JSON 完全一致（snake_case），不做驼峰转换，降低映射成本。
 */

// ---------------------------------------------------------------------------
// §1.4 通用响应 envelope
// ---------------------------------------------------------------------------

/** 成功响应 envelope。 */
export interface ApiSuccess<T> {
  /** 业务状态码，成功恒为 "SUCCESS"。 */
  code: 'SUCCESS'
  /** 面向用户的简短描述，成功默认 "ok"。 */
  message: string
  /** 业务数据，可为 null（无 data 的成功响应）。 */
  data: T | null
  /** 请求链路 ID（UUID v4），用于日志排查。 */
  trace_id: string
}

/** 错误响应 envelope。 */
export interface ApiError {
  /** 业务错误码，全大写蛇形，详见 §6 错误码表。 */
  code: string
  /** 面向用户的简短中文描述。 */
  message: string
  /** 面向开发者的调试信息（不含敏感数据）。 */
  detail?: string | null
  /** 客户端是否可以原样重试同一请求。 */
  retryable: boolean
  /** 当前会话阶段，错误与阶段相关时提供。 */
  stage?: string | null
  /** 请求链路 ID。 */
  trace_id: string
  /** 仅 SAFETY_REVIEW_BLOCKED 携带：安全问题列表。 */
  issues?: unknown[]
}

/** 列表分页响应的 data 容器（会话列表等）。 */
export interface PageData<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** 游标分页响应的 data 容器（消息历史）。 */
export interface CursorData<T> {
  items: T[]
  has_more: boolean
  next_cursor: string | null
}

// ---------------------------------------------------------------------------
// §6 错误码（与后端 app/core/exceptions.py 对齐）
// ---------------------------------------------------------------------------

/** 后端已知业务错误码字面量集合（用于前端分支判断）。 */
export const ErrorCode = {
  SUCCESS: 'SUCCESS',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  SESSION_NOT_FOUND: 'SESSION_NOT_FOUND',
  SESSION_BUSY: 'SESSION_BUSY',
  INVALID_STAGE_TRANSITION: 'INVALID_STAGE_TRANSITION',
  INVALID_STATE_VERSION: 'INVALID_STATE_VERSION',
  INSUFFICIENT_INQUIRY: 'INSUFFICIENT_INQUIRY',
  PENDING_DOCTOR_REVIEW: 'PENDING_DOCTOR_REVIEW',
  INVALID_REVIEW_ACTION: 'INVALID_REVIEW_ACTION',
  FORMULA_OVERRIDE_REQUIRED: 'FORMULA_OVERRIDE_REQUIRED',
  SAFETY_REVIEW_BLOCKED: 'SAFETY_REVIEW_BLOCKED',
  SAFETY_ACCEPT_RISK_UNSUPPORTED: 'SAFETY_ACCEPT_RISK_UNSUPPORTED',
  SAFETY_ENGINE_FAILED: 'SAFETY_ENGINE_FAILED',
  UNKNOWN_DOSE_UNIT: 'UNKNOWN_DOSE_UNIT',
  RECORD_NOT_FOUND: 'RECORD_NOT_FOUND',
  RECOVERY_NOT_NEEDED: 'RECOVERY_NOT_NEEDED',
  STATE_RECOVERY_REQUIRED: 'STATE_RECOVERY_REQUIRED',
  SESSION_TERMINATED: 'SESSION_TERMINATED',
  ROLLBACK_LIMIT_EXCEEDED: 'ROLLBACK_LIMIT_EXCEEDED',
  MODEL_GATEWAY_UNAVAILABLE: 'MODEL_GATEWAY_UNAVAILABLE',
  EMBEDDING_UNAVAILABLE: 'EMBEDDING_UNAVAILABLE',
  RAG_UNAVAILABLE: 'RAG_UNAVAILABLE',
  DATABASE_UNAVAILABLE: 'DATABASE_UNAVAILABLE',
  REDIS_UNAVAILABLE: 'REDIS_UNAVAILABLE',
  EXPORT_FORMAT_UNSUPPORTED: 'EXPORT_FORMAT_UNSUPPORTED',
  INTERNAL_ERROR: 'INTERNAL_ERROR',
  AGENT_SCHEMA_INVALID: 'AGENT_SCHEMA_INVALID',
  AGENT_TRIGGER_FAILED: 'AGENT_TRIGGER_FAILED',
  RUNTIME_ROLLOUT_NOT_READY: 'RUNTIME_ROLLOUT_NOT_READY',
  LEGACY_RUNTIME_CREATION_DISABLED: 'LEGACY_RUNTIME_CREATION_DISABLED',
} as const

export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode]

/** 可重试的错误码（retryable=true）。前端收到这些可等待后原样重试。 */
export const RETRYABLE_ERROR_CODES: ReadonlySet<string> = new Set([
  ErrorCode.SESSION_BUSY,
  ErrorCode.INVALID_STATE_VERSION,
  ErrorCode.SAFETY_ENGINE_FAILED,
  ErrorCode.MODEL_GATEWAY_UNAVAILABLE,
  ErrorCode.EMBEDDING_UNAVAILABLE,
  ErrorCode.RAG_UNAVAILABLE,
  ErrorCode.DATABASE_UNAVAILABLE,
  ErrorCode.REDIS_UNAVAILABLE,
  ErrorCode.INTERNAL_ERROR,
  ErrorCode.AGENT_SCHEMA_INVALID,
  ErrorCode.AGENT_TRIGGER_FAILED,
])

// ---------------------------------------------------------------------------
// §8 枚举（与后端 app/schemas/types.py 对齐）
// ---------------------------------------------------------------------------

/** 问诊主流程阶段。 */
export type Stage =
  | 'inquiry'
  | 'sufficiency'
  | 'syndrome'
  | 'formula'
  | 'prescription'
  | 'modification'
  | 'safety'
  | 'review'
  | 'record'
  | 'done'
  | 'blocked'

/** 会话状态。 */
export type SessionStatus =
  | 'active'
  | 'pending_review'
  | 'done'
  | 'blocked'
  | 'terminated'

/** 恢复状态。 */
export type RecoveryStatus = 'normal' | 'recovering' | 'manual_required'

/** Persisted execution runtime; existing sessions never switch implicitly. */
export type AgentRuntime = 'legacy' | 'langgraph'

/** 患者性别。 */
export type Gender = 'male' | 'female' | 'unknown'

/** 妊娠/哺乳状态。possible 按妊娠同等严格处理。 */
export type PregnancyStatus =
  | 'unknown'
  | 'no'
  | 'pregnant'
  | 'possible'
  | 'lactating'

/** 消息来源角色。 */
export type MessageRole = 'doctor' | 'patient_proxy' | 'agent'

/** Agent 名称。 */
export type AgentName =
  | 'supervisor'
  | 'intake'
  | 'inquiry'
  | 'question_composer'
  | 'sufficiency'
  | 'reasoning'
  | 'reasoning_subgraph'
  | 'syndrome'
  | 'syndrome_draft'
  | 'formula_draft'
  | 'domain_commit'
  | 'prescription'
  | 'modification'
  | 'safety'
  | 'record'

/** 恢复动作。 */
export type RecoveryAction =
  | 'resume_from_pg_snapshot'
  | 'retry_current_stage'
  | 'rollback_to_stage'
  | 'terminate'

/** 医师确认动作。 */
export type ReviewAction = 'confirm' | 'modify' | 'reject' | 'request_more_info'

/** 安全问题严重度。 */
export type Severity = 'info' | 'warning' | 'high' | 'blocker'

/** High-risk safety facts remain non-authoritative until a doctor decides them. */
export type SafetyAssertionStatus =
  | 'proposed'
  | 'confirmed'
  | 'rejected'
  | 'superseded'
  | 'retracted'

export type SafetyFactField =
  | 'allergy'
  | 'pregnancy'
  | 'lactation'
  | 'medications'
  | 'major_conditions'
  | 'contraindications'
  | 'red_flag'

export interface SafetyEvidenceRef {
  source_message_id: string
  start_char: number
  end_char: number
  quote_digest: string
  reply_to_question_message_id?: string | null
  reply_dimension?: string | null
}

export interface SafetyFactAssertion {
  schema_version: 'safety-fact-assertion.v1'
  assertion_id: string
  session_id: string
  field_name: SafetyFactField
  value: Record<string, unknown>
  value_digest: string
  status: SafetyAssertionStatus
  source_kind: string
  source_message_id: string
  extraction_run_id?: string | null
  template_version: string
  evidence_spans: SafetyEvidenceRef[]
  evidence_digest: string
  proposed_at: string
  confirmed_at?: string | null
  rejected_at?: string | null
  retracted_at?: string | null
  superseded_at?: string | null
  supersedes_assertion_id?: string | null
}

export interface SafetyFactAssertionList {
  items: SafetyFactAssertion[]
}

export interface SafetyAssertionDecisionRequest {
  reason_code?: string | null
}

/** 安全问题类型。 */
export type SafetyIssueType =
  | 'eighteen_incompatibilities'
  | 'nineteen_fears'
  | 'pregnancy'
  | 'dose_limit'
  | 'unit_conversion'
  | 'allergy'
  | 'combination'
  | 'caution'

/** SSE 事件类型（与后端 SupportedEventType 对齐）。 */
export type EventType =
  | 'stage.changed'
  | 'message.created'
  | 'agent.started'
  | 'agent.finished'
  | 'agent.failed'
  | 'review.required'
  | 'safety.blocked'
  | 'session.blocked'
  | 'session.done'
  | 'session.terminated'
  | 'doctor.reviewed'
  | 'heartbeat'
  | 'resync'

// ---------------------------------------------------------------------------
// §4.1 会话管理
// ---------------------------------------------------------------------------

/** 患者基础信息。 */
export interface PatientInfo {
  name?: string | null
  patient_ref?: string | null
  gender?: Gender
  age?: number | null
  visit_time?: string | null
  allergies?: string[]
  pregnancy_status?: PregnancyStatus
  menstruation_summary?: string | null
  special_conditions?: string[]
  current_medications?: string[]
  major_conditions?: string[]
  lactation_status?: 'lactating' | 'not_lactating' | null
}

/** 创建会话请求体。 */
export interface SessionCreateRequest {
  patient_info?: PatientInfo
  chief_complaint?: string | null
  agent_runtime?: AgentRuntime
}

/** 创建会话响应 data。 */
export interface SessionCreateData {
  session_id: string
  current_stage: Stage
  status: SessionStatus
  agent_runtime: AgentRuntime
  patient_info: PatientInfo
  created_at: string
}

export interface SessionGateReadModel {
  gate_id: string
  graph_run_id?: string | null
  gate_name: string
  policy_version: string
  input_state_version: number
  decision: 'passed' | 'failed' | 'blocked'
  details?: Record<string, unknown> | null
}

export interface SessionArtifactReadModel {
  artifact_id: string
  artifact_type: 'syndrome_draft' | 'formula_draft'
  revision: number
  input_state_version: number
  status: 'current'
  produced_by_run_id: string
  payload_schema_version: string
  content_digest: string
  decision: 'completed' | 'needs_more_info' | 'abstained'
  evidence_mode: string
  review_required: boolean
  unresolved: string[]
  verification_gate: SessionGateReadModel
  output: Record<string, unknown>
}

export interface SessionReadModel {
  schema_version: 'session-read-model.v1'
  agent_runtime: AgentRuntime
  graph: {
    graph_run_id?: string | null
    graph_version?: string | null
    revision: number
    input_state_version?: number | null
    status?: 'running' | 'completed' | 'failed' | 'cancelled' | null
  }
  gates: SessionGateReadModel[]
  artifacts: SessionArtifactReadModel[]
  evidence_mode?: string | null
  review_required: boolean
  unresolved: Array<{
    source:
      | 'triage'
      | 'completeness'
      | 'syndrome_draft'
      | 'formula_draft'
      | 'read_model'
      | 'safety_confirmation'
    kind:
      | 'red_flag'
      | 'missing_required'
      | 'conflict'
      | 'missing_input'
      | 'artifact_unavailable'
      | 'unconfirmed_safety_fact'
    key: string
  }>
}

/** 终止会话请求体。 */
export interface SessionTerminateRequest {
  reason?: string | null
}

/** 终止会话响应 data。 */
export interface SessionTerminateData {
  session_id: string
  status: SessionStatus
  current_stage: Stage
  blocked_reason: string
  updated_at: string
}

/** 会话列表项。 */
export interface SessionListItem {
  session_id: string
  patient_info: PatientInfo
  chief_complaint?: string | null
  current_stage: Stage
  status: SessionStatus
  agent_runtime: AgentRuntime
  pending_review: boolean
  created_by?: string | null
  created_at: string
  updated_at: string
}

/** 会话列表查询参数。 */
export interface SessionListParams {
  status?: SessionStatus
  patient_ref?: string
  page?: number
  page_size?: number
  /** 默认 "created_at:desc"，可选 "updated_at:desc"。 */
  sort?: 'created_at:desc' | 'updated_at:desc'
}

/**
 * 会话完整状态（最权威状态同步接口）。
 * 字段对齐后端 SessionDetailResponse；P3 阶段未实现的字段为 null。
 */
export interface SessionDetail {
  session_id: string
  status: SessionStatus
  current_stage: Stage
  pending_review: boolean
  todo?: Record<string, unknown> | null
  recovery_status: RecoveryStatus
  blocked_reason?: string | null
  rollback_counts: Record<string, number>
  state_version: number
  agent_runtime: AgentRuntime
  read_model: SessionReadModel
  patient_info: PatientInfo
  chief_complaint?: string | null
  present_illness?: string | null
  past_history?: string | null
  ten_questions?: Record<string, unknown> | null
  sufficiency_report?: Record<string, unknown> | null
  syndrome_result?: Record<string, unknown> | null
  base_formula?: Formula | null
  modified_formula?: Formula | null
  modifications?: Modification[] | null
  safety_review?: SafetyReview | null
  medical_record?: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------------------
// §4.2 消息
// ---------------------------------------------------------------------------

/** 提交问诊消息请求体。 */
export interface MessageCreateRequest {
  /** 1-5000 字符。 */
  content: string
  /** doctor=医师代录，patient_proxy=预留。 */
  role: 'doctor' | 'patient_proxy'
  /** 当前回答所对应的结构化 Agent 问题。 */
  reply_to_message_id?: string | null
}

/** 提交消息响应 data。P8-6: 新增 agent_message / sufficiency_report。 */
export interface MessageCreateData {
  message_id: string
  session_id: string
  role: string
  stage: Stage
  content: string
  current_stage: Stage
  state_version: number
  created_at: string
  /** P8-6: Agent 回复消息（inquiry 阶段提交后由后端 Agent 生成）。 */
  agent_message?: AgentMessageItem | null
  /** P8-6: 完备性报告。 */
  sufficiency_report?: SufficiencyReport | null
}

/** Agent 回复消息（嵌入 MessageCreateData）。 */
export interface AgentMessageItem {
  message_id: string
  role: string
  agent_name?: string | null
  stage: Stage
  content: string
  agent_run_id?: string | null
  created_at?: string | null
}

/** 完备性报告。 */
export interface SufficiencyReport {
  sufficient: boolean
  covered: string[]
  missing: string[]
  /** 新版接口返回结构化待补充说明；旧快照中可能缺失。 */
  missing_items?: SufficiencyMissingItem[]
  suggestions: string[]
}

/** 完备性报告中的单项待补充说明。 */
export interface SufficiencyMissingItem {
  key: string
  label: string
  reason: string
  suggested_question: string
}

/** 消息历史列表项。 */
export interface MessageItem {
  id: string
  session_id: string
  role: MessageRole
  agent_name?: string | null
  stage: Stage
  content: string
  structured_delta?: Record<string, unknown> | null
  agent_run_id?: string | null
  created_at: string
}

/** 消息历史查询参数（游标分页）。 */
export interface MessageListParams {
  before?: string
  limit?: number
  stage?: Stage
}

// ---------------------------------------------------------------------------
// §4.3 阶段推进与恢复
// ---------------------------------------------------------------------------

/** 阶段推进请求体。 */
export interface AdvanceRequest {
  target_stage?: string | null
  force?: boolean
}

/** 阶段推进响应 data。 */
export interface AdvanceData {
  session_id: string
  current_stage: string
  from_stage: string
  state_version: number
  blocked_reason?: string | null
  agent_name?: string | null
  trace_id?: string | null
}

/** 恢复中断会话请求体。 */
export interface RecoveryRequest {
  action: RecoveryAction
  /** action=rollback_to_stage 时必填。 */
  target_stage?: Stage | null
  reason?: string | null
}

/** 恢复响应 data。 */
export interface RecoveryData {
  session_id: string
  current_stage: Stage
  status: SessionStatus
  recovery_status: RecoveryStatus
  action: RecoveryAction
  updated_at: string
}

// ---------------------------------------------------------------------------
// §4.4 医师确认
// ---------------------------------------------------------------------------

/** 处方药味项。 */
export interface HerbItem {
  herb: string
  dose?: number | null
  unit?: string
  note?: string | null
}

/** 处方。 */
export interface Formula {
  name?: string | null
  composition: HerbItem[]
  source?: string | null
  rationale?: string | null
}

/** 医师修改后的完整处方。composition 必填且至少一味药。 */
export interface FormulaOverride {
  name?: string | null
  composition: HerbItem[]
  source?: string | null
  rationale?: string | null
}

/** 医师确认请求体。 */
export interface ReviewRequest {
  action: ReviewAction
  /** action=modify 时必填。 */
  formula_override?: FormulaOverride | null
  /** reject/request_more_info 时必须填写，用于保留回退原因。 */
  feedback?: string | null
}

/** 医师确认响应 data。 */
export interface ReviewData {
  session_id: string
  action: ReviewAction
  current_stage: Stage
  status: SessionStatus
  pending_review: boolean
  review_id: string
  state_version: number
  original_formula?: Formula | null
  formula_override?: Formula | null
  feedback?: string | null
  safety_recheck?: SafetyReview | null
  medical_record?: Record<string, unknown> | null
  updated_at: string
}

// ---------------------------------------------------------------------------
// 安全审核
// ---------------------------------------------------------------------------

/** 安全问题。 */
export interface SafetyIssue {
  type?: SafetyIssueType
  severity: Severity
  herb?: string | null
  message: string
  detail?: string | null
  suggestion?: string | null
  rollback_target?: 'prescription' | 'modification' | 'none' | null
}

/** 安全审核结果。 */
export interface SafetyReview {
  passed: boolean
  issues: SafetyIssue[]
  rollback_target?: 'prescription' | 'modification' | 'none' | null
}

/** 方药加减项。 */
export interface Modification {
  action: 'add' | 'remove' | 'replace' | 'adjust'
  herb: string
  dose?: number | null
  unit?: string
  reason?: string | null
}

// ---------------------------------------------------------------------------
// §5 SSE 事件
// ---------------------------------------------------------------------------

/** 解析后的 SSE 事件。 */
export interface SessionEvent<T = Record<string, unknown>> {
  event_id: string
  event_type: EventType
  payload: T & {
    /**
     * L9 v2 producer contract. Optional on the client during rolling deploys so
     * an already-open tab can still drain events emitted by an older instance.
     */
    schema_version?: 'session-event.v2'
    session_id?: string
    timestamp?: string
  }
}

/** stage.changed 事件 payload。 */
export interface StageChangedPayload {
  session_id: string
  from_stage: Stage
  to_stage: Stage
  state_version: number
  timestamp: string
}

/** message.created 事件 payload。 */
export interface MessageCreatedPayload {
  session_id: string
  message_id: string
  role: MessageRole
  agent_name?: string | null
  stage: Stage
  content: string
  structured_delta?: Record<string, unknown> | null
  agent_run_id?: string | null
  created_at: string
}

/** review.required 事件 payload。注意使用 modified_formula。 */
export interface ReviewRequiredPayload {
  session_id: string
  modified_formula: Formula
  safety_review: SafetyReview
  timestamp: string
}

/** safety.blocked 事件 payload。 */
export interface SafetyBlockedPayload {
  session_id: string
  issues: SafetyIssue[]
  rollback_target?: 'prescription' | 'modification' | 'none' | null
  timestamp: string
}

/** session.done 事件 payload。 */
export interface SessionDonePayload {
  session_id: string
  record_id?: string
  timestamp: string
}

/** resync 事件 payload（客户端应触发 GET /sessions/{id} 全量同步）。 */
export interface ResyncPayload {
  session_id: string
  reason: string
  timestamp: string
}

// ---------------------------------------------------------------------------
// §4.5 病历
// ---------------------------------------------------------------------------

/** 病历 GET 响应 data。字段对齐后端 RecordResponse，snake_case。 */
export interface RecordResponse {
  id: string
  session_id: string
  version: number
  record_text: string
  record_json: Record<string, unknown>
  disclaimer?: string | null
  edited_by_doctor: boolean
  doctor_review_id?: string | null
  diff_from_previous?: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/** 病历编辑请求体。record_text 与 record_json 至少提供一个。 */
export interface RecordUpdateRequest {
  record_text?: string
  record_json?: Record<string, unknown>
}

/** 病历编辑响应 data。 */
export interface RecordUpdateResponse {
  id: string
  session_id: string
  version: number
  diff_from_previous?: Record<string, unknown> | null
  edited_by_doctor: boolean
  updated_at: string
}
