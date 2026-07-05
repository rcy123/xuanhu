/**
 * 悬壶 WebUI —— 会话/消息/恢复/review 接口方法（P8-1 骨架）
 *
 * 与接口设计文档 §4 对齐。仅暴露方法签名与参数类型，不实现 UI 交互。
 * P8-2/P8-3 直接调用这些方法。
 */

import { request, requestWithRetry } from './client'
import type { RequestContext } from './client'
import type {
  AdvanceData,
  AdvanceRequest,
  CursorData,
  MessageCreateData,
  MessageCreateRequest,
  MessageItem,
  MessageListParams,
  PageData,
  RecordResponse,
  RecordUpdateRequest,
  RecordUpdateResponse,
  RecoveryData,
  RecoveryRequest,
  ReviewData,
  ReviewRequest,
  SessionCreateData,
  SessionCreateRequest,
  SessionDetail,
  SessionListItem,
  SessionListParams,
  SessionTerminateData,
} from '@/types/api'

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------

/** POST /consult/sessions —— 创建问诊会话（支持幂等键）。 */
export function createSession(
  body: SessionCreateRequest,
  ctx?: RequestContext,
): Promise<SessionCreateData> {
  return request<SessionCreateData>('consult/sessions', {
    method: 'POST',
    body: JSON.stringify(body),
    ctx,
  })
}

/** GET /consult/sessions —— 查询会话列表（分页）。 */
export function listSessions(
  params: SessionListParams = {},
  ctx?: RequestContext,
): Promise<PageData<SessionListItem>> {
  return request<PageData<SessionListItem>>(`consult/sessions?${toQuery(params as Record<string, unknown>)}`, {
    method: 'GET',
    ctx,
  })
}

/** GET /consult/sessions/{id} —— 获取会话完整状态（最权威状态同步接口）。 */
export function getSession(sessionId: string, ctx?: RequestContext): Promise<SessionDetail> {
  return request<SessionDetail>(`consult/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'GET',
    ctx,
  })
}

/** POST /consult/sessions/{id}/terminate —— 终止会话。 */
export function terminateSession(
  sessionId: string,
  body: { reason?: string | null } = {},
  ctx?: RequestContext,
): Promise<SessionTerminateData> {
  return request<SessionTerminateData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/terminate`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 消息
// ---------------------------------------------------------------------------

/**
 * POST /consult/sessions/{id}/messages —— 提交问诊消息。
 *
 * 写操作：建议携带 ctx.stateVersion；后端可能返回 409 SESSION_BUSY /
 * INVALID_STATE_VERSION，由调用方决定重试。本封装提供 retry 版本。
 */
export function submitMessage(
  sessionId: string,
  body: MessageCreateRequest,
  ctx?: RequestContext,
): Promise<MessageCreateData> {
  return request<MessageCreateData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

/**
 * 提交消息（带自动重试，处理 409 SESSION_BUSY / INVALID_STATE_VERSION）。
 * 默认重试 3 次，间隔 1.5s。
 */
export function submitMessageWithRetry(
  sessionId: string,
  body: MessageCreateRequest,
  ctx?: RequestContext,
  maxRetries = 3,
): Promise<MessageCreateData> {
  return requestWithRetry<MessageCreateData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
      maxRetries,
    },
  )
}

/** GET /consult/sessions/{id}/messages —— 消息历史（游标分页）。 */
export function listMessages(
  sessionId: string,
  params: MessageListParams = {},
  ctx?: RequestContext,
): Promise<CursorData<MessageItem>> {
  return request<CursorData<MessageItem>>(
    `consult/sessions/${encodeURIComponent(sessionId)}/messages?${toQuery(params as Record<string, unknown>)}`,
    {
      method: 'GET',
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 恢复
// ---------------------------------------------------------------------------

/** POST /consult/sessions/{id}/recover —— 恢复中断的会话。 */
export function recoverSession(
  sessionId: string,
  body: RecoveryRequest,
  ctx?: RequestContext,
): Promise<RecoveryData> {
  return request<RecoveryData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/recover`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 医师确认
// ---------------------------------------------------------------------------

/**
 * POST /consult/sessions/{id}/review —— 医师确认/修改/否决处方（支持幂等键）。
 *
 * 注意（UI §3.3 / P7-1）：confirm/modify 成功后当前只进入
 * current_stage=record/status=active，不会在该响应里返回病历。
 * 病历完成态需通过 session.done SSE 或 GET /sessions/{id} 确认。
 */
export function reviewPrescription(
  sessionId: string,
  body: ReviewRequest,
  ctx?: RequestContext,
): Promise<ReviewData> {
  return request<ReviewData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/review`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 健康检查（非 envelope 扁平响应）
// ---------------------------------------------------------------------------

/** GET /health —— 基础健康检查（扁平 JSON）。 */
export function getHealth(
  ctx?: RequestContext,
): Promise<{ status: string; version: string; timestamp: string }> {
  return request('/health', { method: 'GET', ctx })
}

// ---------------------------------------------------------------------------
// 病历
// ---------------------------------------------------------------------------

/** GET /consult/sessions/{id}/record?version=latest|N —— 获取病历（envelope）。 */
export function getRecord(
  sessionId: string,
  version: number | 'latest' = 'latest',
  ctx?: RequestContext,
): Promise<RecordResponse> {
  return request<RecordResponse>(
    `consult/sessions/${encodeURIComponent(sessionId)}/record?version=${encodeURIComponent(String(version))}`,
    {
      method: 'GET',
      ctx,
    },
  )
}

/**
 * PUT /consult/sessions/{id}/record —— 医师编辑病历。
 *
 * body 至少包含 record_text 或 record_json 之一。
 * ctx 应携带 X-State-Version。
 */
export function updateRecord(
  sessionId: string,
  body: RecordUpdateRequest,
  ctx?: RequestContext,
): Promise<RecordUpdateResponse> {
  return request<RecordUpdateResponse>(
    `consult/sessions/${encodeURIComponent(sessionId)}/record`,
    {
      method: 'PUT',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

/**
 * GET /consult/sessions/{id}/record/export?format=... —— 导出病历。
 *
 * 返回 raw Response（非 envelope 文件响应），调用方需自行处理下载。
 * 非 2xx 响应会识别后端 JSON 错误 envelope 并抛出 ApiRequestError。
 */
export function exportRecord(
  sessionId: string,
  format: 'txt' | 'json' | 'md',
  version?: number | 'latest',
  ctx?: RequestContext,
): Promise<Response> {
  const versionParam = version !== undefined
    ? `&version=${encodeURIComponent(String(version))}`
    : ''
  return request<Response>(
    `consult/sessions/${encodeURIComponent(sessionId)}/record/export?format=${encodeURIComponent(format)}${versionParam}`,
    {
      method: 'GET',
      raw: true,
      rawErrorEnvelope: true,
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 阶段推进（P8-6 §4.3.1）
// ---------------------------------------------------------------------------

/**
 * POST /consult/sessions/{id}/advance —— 阶段推进。
 *
 * 问诊完备性充分后调用，依次执行辨证→开方→加减→安全审核。
 * 安全审核通过后挂起等待医师确认（不进病历生成）。
 * review 阶段不可调用，需先提交医师确认。
 */
export function advanceSession(
  sessionId: string,
  body: AdvanceRequest = {},
  ctx?: RequestContext,
): Promise<AdvanceData> {
  return request<AdvanceData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/advance`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx,
    },
  )
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

/** 将简单查询参数对象序列化为 query string。跳过 undefined/null。 */
export function toQuery(params: Record<string, unknown>): string {
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    sp.set(key, String(value))
  }
  return sp.toString()
}
