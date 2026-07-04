/**
 * 悬壶 WebUI —— API 错误类型
 *
 * 与接口设计文档 §1.4、§6 对齐。客户端统一抛出 ApiError，业务层基于
 * `error.code`（而非 HTTP 状态码）做分支判断。
 */

import type { ApiError as ApiErrorEnvelope } from '@/types/api'

/**
 * API 业务/传输错误。
 *
 * - `code` 来自后端 envelope 的 `code` 字段；若后端未返回 envelope（如 5xx 网关错误、
 *   超时、网络断开），则使用 INTERNAL_ERROR / NETWORK_ERROR / TIMEOUT 等占位码。
 * - `retryable` 直接取自后端 envelope；网络/超时类默认可重试。
 * - `traceId` 用于在 UI 错误提示中展示，方便用户上报。
 */
export class ApiRequestError extends Error {
  /** 业务错误码（全大写蛇形）或传输层占位码。 */
  readonly code: string
  /** 面向用户的简短中文描述。 */
  readonly userMessage: string
  /** 面向开发者的调试信息。 */
  readonly detail?: string | null
  /** 客户端是否可原样重试。 */
  readonly retryable: boolean
  /** 当前会话阶段（错误与阶段相关时）。 */
  readonly stage?: string | null
  /** 请求链路 ID。 */
  readonly traceId?: string
  /** HTTP 状态码（用于诊断；业务分支不应依赖它）。 */
  readonly status: number
  /** 仅 SAFETY_REVIEW_BLOCKED 携带：安全问题列表。 */
  readonly issues?: unknown[]
  /** 原始 envelope（保留以备扩展字段）。 */
  readonly envelope?: ApiErrorEnvelope

  constructor(params: {
    code: string
    userMessage: string
    status: number
    detail?: string | null
    retryable?: boolean
    stage?: string | null
    traceId?: string
    issues?: unknown[]
    envelope?: ApiErrorEnvelope
    cause?: unknown
  }) {
    super(params.userMessage)
    this.name = 'ApiRequestError'
    this.code = params.code
    this.userMessage = params.userMessage
    this.status = params.status
    this.detail = params.detail ?? null
    this.retryable = params.retryable ?? false
    this.stage = params.stage ?? null
    this.traceId = params.traceId
    this.issues = params.issues
    this.envelope = params.envelope
    if (params.cause !== undefined) {
      ;(this as { cause?: unknown }).cause = params.cause
    }
  }

  /** 从后端错误 envelope 构造。 */
  static fromEnvelope(status: number, env: ApiErrorEnvelope): ApiRequestError {
    return new ApiRequestError({
      code: env.code,
      userMessage: env.message,
      status,
      detail: env.detail ?? null,
      retryable: env.retryable,
      stage: env.stage ?? null,
      traceId: env.trace_id,
      issues: env.issues,
      envelope: env,
    })
  }
}

/** 传输层占位错误码（后端未返回标准 envelope 时使用）。 */
export const TransportErrorCode = {
  NETWORK_ERROR: 'NETWORK_ERROR',
  TIMEOUT: 'TIMEOUT',
  ABORTED: 'ABORTED',
  BAD_RESPONSE: 'BAD_RESPONSE',
} as const

/**
 * 根据错误码判断是否应重试。
 * 优先使用后端 retryable 字段；网络/超时类直接判为可重试。
 */
export function shouldRetry(err: ApiRequestError): boolean {
  if (err.retryable) return true
  return (
    err.code === TransportErrorCode.NETWORK_ERROR ||
    err.code === TransportErrorCode.TIMEOUT
  )
}
