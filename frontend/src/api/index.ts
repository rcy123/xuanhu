/**
 * 悬壶 WebUI —— 会话/消息/恢复/review 接口方法（P8-1 骨架）
 *
 * 与接口设计文档 §4 对齐。仅暴露方法签名与参数类型，不实现 UI 交互。
 * P8-2/P8-3 直接调用这些方法。
 */

import { request, requestWithRetry } from './client'
import type { RequestContext } from './client'
import { generateIdempotencyKey } from '@/utils/id'
import type {
  AdvanceMutationResult,
  AdvanceRequest,
  AsyncCommandStatus,
  CursorData,
  MessageCreateRequest,
  MessageItem,
  MessageListParams,
  MessageSubmitResult,
  PageData,
  RecordResponse,
  RecordUpdateRequest,
  RecordUpdateResponse,
  RecoveryData,
  RecoveryRequest,
  ReviewMutationResult,
  ReviewRequest,
  SafetyAssertionDecisionRequest,
  SafetyAssertionStatus,
  SafetyFactAssertion,
  SafetyFactAssertionList,
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
  const writeContext = withIdempotencyKey(ctx)
  return request<SessionCreateData>('consult/sessions', {
    method: 'POST',
    body: JSON.stringify(body),
    ctx: writeContext,
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
  const writeContext = withIdempotencyKey(ctx)
  return request<SessionTerminateData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/terminate`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
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
 *
 * R7：缺省即请求异步（`Prefer: respond-async`）。后端就绪时返回 HTTP 202
 * 命令 envelope（`AsyncCommandAccepted`，用 `isAsyncCommandAccepted` 收窄），
 * 未就绪时回退同步返回 `MessageCreateData`。调用方不得把 202 当作已完成业务。
 * `respondAsync=false` 仅省略偏好头，不强制同步（见 RequestContext.respondAsync）。
 */
export function submitMessage(
  sessionId: string,
  body: MessageCreateRequest,
  ctx?: RequestContext,
): Promise<MessageSubmitResult> {
  const writeContext = withR7AsyncDefault(withIdempotencyKey(ctx))
  return request<MessageSubmitResult>(
    `consult/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
    },
  )
}

/**
 * 提交消息（带自动重试，处理 SESSION_BUSY 等不确定失败）。
 * INVALID_STATE_VERSION 是确定失败，由 useMessages 校验回复绑定后新建命令。
 * 默认重试 3 次，间隔 1.5s。
 *
 * R7：默认请求异步；202 是成功的接受（非错误），不会触发重试。
 */
export function submitMessageWithRetry(
  sessionId: string,
  body: MessageCreateRequest,
  ctx?: RequestContext,
  maxRetries = 3,
): Promise<MessageSubmitResult> {
  const writeContext = withR7AsyncDefault(withIdempotencyKey(ctx))
  return requestWithRetry<MessageSubmitResult>(
    `consult/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
      maxRetries,
      // A stale state version is a deterministic command failure.  Replaying
      // the same idempotency key cannot fix it because state_version is part of
      // the backend request digest; useMessages performs the safe rebase.
      // AGENT_TRIGGER_FAILED already has a saved doctor message/failed claim;
      // only an explicit exact-command retry may ask the backend to resume it.
      retryExcludedCodes: ['INVALID_STATE_VERSION', 'AGENT_TRIGGER_FAILED'],
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
// 问诊安全事实确认
// ---------------------------------------------------------------------------

/** 查询安全事实候选；默认只返回尚待医生确认的项目。 */
export function listSafetyAssertions(
  sessionId: string,
  status: SafetyAssertionStatus | undefined = 'proposed',
  ctx?: RequestContext,
): Promise<SafetyFactAssertionList> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return request<SafetyFactAssertionList>(
    `consult/sessions/${encodeURIComponent(sessionId)}/safety-assertions${query}`,
    { method: 'GET', ctx },
  )
}

function decideSafetyAssertion(
  sessionId: string,
  assertionId: string,
  action: 'confirm' | 'reject',
  body: SafetyAssertionDecisionRequest,
  ctx?: RequestContext,
): Promise<SafetyFactAssertion> {
  const writeContext = withIdempotencyKey(ctx)
  return request<SafetyFactAssertion>(
    `consult/sessions/${encodeURIComponent(sessionId)}/safety-assertions/${encodeURIComponent(assertionId)}/${action}`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
    },
  )
}

/** 医生确认候选事实，并由后端投影到权威安全档案。 */
export function confirmSafetyAssertion(
  sessionId: string,
  assertionId: string,
  body: SafetyAssertionDecisionRequest = {},
  ctx?: RequestContext,
): Promise<SafetyFactAssertion> {
  return decideSafetyAssertion(sessionId, assertionId, 'confirm', body, ctx)
}

/** 医生驳回候选事实；该候选不会进入权威安全档案。 */
export function rejectSafetyAssertion(
  sessionId: string,
  assertionId: string,
  body: SafetyAssertionDecisionRequest = {},
  ctx?: RequestContext,
): Promise<SafetyFactAssertion> {
  return decideSafetyAssertion(sessionId, assertionId, 'reject', body, ctx)
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
  const writeContext = withIdempotencyKey(ctx)
  return request<RecoveryData>(
    `consult/sessions/${encodeURIComponent(sessionId)}/recover`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
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
 *
 * R7：默认请求异步；后端就绪时返回 202 命令 envelope，未就绪时回退同步
 * `ReviewData`。202 仅表示已接受，须经命令状态 + 权威读模型确认结果。
 */
export function reviewPrescription(
  sessionId: string,
  body: ReviewRequest,
  ctx?: RequestContext,
): Promise<ReviewMutationResult> {
  const writeContext = withR7AsyncDefault(withIdempotencyKey(ctx))
  return request<ReviewMutationResult>(
    `consult/sessions/${encodeURIComponent(sessionId)}/review`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
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
  const writeContext = withIdempotencyKey(ctx)
  return request<RecordUpdateResponse>(
    `consult/sessions/${encodeURIComponent(sessionId)}/record`,
    {
      method: 'PUT',
      body: JSON.stringify(body),
      ctx: writeContext,
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
 *
 * R7：默认请求异步；后端就绪时返回 202 命令 envelope，未就绪时回退同步
 * `AdvanceData`。202 仅表示已接受，须经命令状态 + 权威读模型确认结果。
 */
export function advanceSession(
  sessionId: string,
  body: AdvanceRequest = {},
  ctx?: RequestContext,
): Promise<AdvanceMutationResult> {
  const writeContext = withR7AsyncDefault(withIdempotencyKey(ctx))
  return request<AdvanceMutationResult>(
    `consult/sessions/${encodeURIComponent(sessionId)}/advance`,
    {
      method: 'POST',
      body: JSON.stringify(body),
      ctx: writeContext,
    },
  )
}

/**
 * GET /consult/sessions/{id}/commands/{commandId} —— 查询 R6-B 异步命令公共状态。
 * 返回的字段全部有界且 PHI 安全（仅状态/attempt/结果 HTTP 码/固定错误码/链接）。
 * SSE 的 command.* 事件是唤醒信号；此处为权威状态源。
 */
export function getCommandStatus(
  sessionId: string,
  commandId: string,
  ctx?: RequestContext,
): Promise<AsyncCommandStatus> {
  return request<AsyncCommandStatus>(
    `consult/sessions/${encodeURIComponent(sessionId)}/commands/${encodeURIComponent(commandId)}`,
    {
      method: 'GET',
      ctx,
    },
  )
}

function withIdempotencyKey(ctx?: RequestContext): RequestContext {
  return {
    ...ctx,
    idempotencyKey: ctx?.idempotencyKey ?? generateIdempotencyKey(),
  }
}

/**
 * R7 默认异步：三处写操作缺省请求 `Prefer: respond-async`（RFC 7240 标准头）。
 * 显式传 `ctx.respondAsync = false` 仅省略该兼容偏好头，**不强制同步**——
 * R7 后端就绪时即使不带该头也默认返回 HTTP 202；真正的同步回退是部署方
 * 设置 `XUANHU_ASYNC_COMMAND_ENABLED=false`。前端以 `isAsyncCommandAccepted`
 * 判别实际返回结果，不依赖该头是否发送。
 */
function withR7AsyncDefault(ctx: RequestContext): RequestContext {
  return {
    ...ctx,
    respondAsync: ctx.respondAsync ?? true,
  }
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
