/**
 * 悬壶 WebUI —— HTTP 客户端封装
 *
 * 基于 fetch 的轻量客户端，不引入第三方依赖（P8-1 阶段也无需 Axios）。
 *
 * 核心职责：
 * 1. 统一 base URL（走 Vite proxy 或 VITE_API_BASE_URL）。
 * 2. 统一处理 envelope（成功/错误/分页），抛出 ApiRequestError。
 * 3. 统一注入 trace_id 和请求头。
 * 4. 为可重试错误提供 requestWithRetry 便捷方法。
 *
 * 注意：SSE 不走这个客户端；SSE 使用 EventSource 封装，见 `src/api/sse.ts`。
 */

import { ApiRequestError, TransportErrorCode } from './errors'
import type { ApiError, ApiSuccess } from '@/types/api'

// ---------------------------------------------------------------------------
// 构造
// ---------------------------------------------------------------------------

let _resolveBaseUrl: () => string = () => import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** 覆盖 base URL 解析函数（测试用）。 */
export function __setBaseUrlResolver(fn: () => string): void {
  _resolveBaseUrl = fn
}

/** 获取当前 base URL。 */
export function getBaseUrl(): string {
  return _resolveBaseUrl()
}

// ---------------------------------------------------------------------------
// 请求上下文
// ---------------------------------------------------------------------------

/** 每次请求的上下文。 */
export interface RequestContext {
  /** 医师标识（可选，MVP 阶段） */
  doctorId?: string
  /** 幂等键（创建会话、review 等写操作建议提供） */
  idempotencyKey?: string
  /** 客户端 state_version（写操作携带） */
  stateVersion?: number
  /** 额外请求头 */
  extraHeaders?: Record<string, string>
}

// ---------------------------------------------------------------------------
// 核心 fetch 封装
// ---------------------------------------------------------------------------

/**
 * 发起 API 请求并返回脱包后的 data。
 *
 * 若后端返回 success envelope 则直接返回 `data` 字段；
 * 若后端返回 error envelope 则抛出 `ApiRequestError`；
 * 若网络断开/超时/非 JSON 响应则抛出 `ApiRequestError`（code 为传输层占位码）。
 *
 * 输入 `path` 若以 `/` 开头则拼接 baseUrl；否则直接作为相对路径。
 */
export async function request<T>(
  path: string,
  options: RequestInit & {
    ctx?: RequestContext
    /** 若为 true 则返回原始 Response 不解析，用于 SSE 等非 JSON 接口。 */
    raw?: boolean
  } = {},
): Promise<T> {
  const { ctx, raw, ...fetchOptions } = options
  const url = buildUrl(path)

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> | undefined),
  }

  if (ctx?.doctorId) {
    headers['X-Doctor-Id'] = ctx.doctorId
  }
  if (ctx?.idempotencyKey) {
    headers['X-Idempotency-Key'] = ctx.idempotencyKey
  }
  if (ctx?.stateVersion !== undefined) {
    headers['X-State-Version'] = String(ctx.stateVersion)
  }
  if (ctx?.extraHeaders) {
    Object.assign(headers, ctx.extraHeaders)
  }

  let response: Response
  try {
    response = await fetch(url, { ...fetchOptions, headers })
  } catch (err: unknown) {
    throw new ApiRequestError({
      code: TransportErrorCode.NETWORK_ERROR,
      userMessage: '网络连接失败，请检查网络',
      status: 0,
      detail: err instanceof Error ? err.message : String(err),
      retryable: true,
      cause: err,
    })
  }

  if (raw) {
    return response as unknown as T
  }

  const contentType = response.headers.get('content-type') ?? ''
  const isJson = contentType.includes('application/json')

  if (!isJson) {
    const text = await response.text().catch(() => '')
    throw new ApiRequestError({
      code: TransportErrorCode.BAD_RESPONSE,
      userMessage: `服务器返回了非 JSON 响应（${response.status}）`,
      status: response.status,
      detail: text.slice(0, 512),
      retryable: response.status >= 500,
    })
  }

  // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
  const body: unknown = await response.json().catch(() => null)

  // 可能是 envelope 也可能不是（health 等扁平响应）
  if (body && typeof body === 'object' && 'code' in body) {
    const env = body as ApiSuccess<T> | ApiError

    if (env.code === 'SUCCESS') {
      return (env as ApiSuccess<T>).data as T
    }

    // 错误 envelope
    throw ApiRequestError.fromEnvelope(response.status, env as ApiError)
  }

  // 非 envelope 响应（如 /health 的扁平 JSON）——直接返回。
  return body as unknown as T
}

/**
 * 带自动重试的请求。
 *
 * 仅在 `ApiRequestError.retryable === true` 时重试。
 * 最多重试 `maxRetries` 次，每次间隔 `delayMs` 毫秒。
 */
export async function requestWithRetry<T>(
  path: string,
  options: RequestInit & {
    ctx?: RequestContext
    maxRetries?: number
    delayMs?: number
  } = {},
): Promise<T> {
  const { maxRetries = 3, delayMs = 1500, ...fetchOptions } = options

  let lastError: ApiRequestError | undefined
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await request<T>(path, fetchOptions)
    } catch (err: unknown) {
      if (err instanceof ApiRequestError && err.retryable && attempt < maxRetries) {
        lastError = err
        await sleep(delayMs)
        continue
      }
      throw err
    }
  }
  throw lastError
}

// ---------------------------------------------------------------------------
// 响应头 trace_id 提取
// ---------------------------------------------------------------------------

/**
 * 从 Response 头中提取 X-Trace-Id（用于 SSE 等无 envelope 的接口）。
 */
export function getTraceIdFromResponse(response: Response): string | undefined {
  return response.headers.get('x-trace-id') ?? undefined
}

// ---------------------------------------------------------------------------
// 内部工具
// ---------------------------------------------------------------------------

function buildUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return path
  }
  const base = getBaseUrl()
  if (!base.endsWith('/') && !path.startsWith('/')) {
    return `${base}/${path}`
  }
  return `${base}${path}`
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}