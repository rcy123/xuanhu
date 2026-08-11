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
  /**
   * R7 异步偏好：为 true 时注入标准 `Prefer: respond-async`（RFC 7240）。
   * 三处 R7 写封装（submitMessage / advanceSession / reviewPrescription）缺省
   * 即为 true；显式传 false 仅**省略该兼容偏好头**，并不强制后端同步——R7
   * 后端就绪时即便没有该头也会返回 HTTP 202。真正的同步回退是部署方设置
   * `XUANHU_ASYNC_COMMAND_ENABLED=false`。前端一律用 `isAsyncCommandAccepted`
   * 判别实际返回（202 或同步结果）。
   */
  respondAsync?: boolean
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
    /**
     * 仅与 `raw: true` 组合使用。当为 true 时，对非 2xx 响应识别后端 JSON
     * 错误 envelope 并抛出 `ApiRequestError`；非 JSON 错误响应同样抛出
     * `ApiRequestError`（BAD_RESPONSE）。2xx 成功响应仍原样返回 Response，
     * 不按 envelope 解析，避免影响文件下载路径。
     *
     * 专用于 raw 文件接口（如病历导出）的错误处理，不改变全局 raw 语义，
     * 不影响 SSE 或未来其他 raw 接口。
     */
    rawErrorEnvelope?: boolean
  } = {},
): Promise<T> {
  const { ctx, raw, rawErrorEnvelope, ...fetchOptions } = options
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
  if (ctx?.respondAsync) {
    headers['Prefer'] = 'respond-async'
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
    // raw 文件接口（如病历导出）：非 2xx 时识别后端 JSON 错误 envelope
    // 并抛出 ApiRequestError，避免把错误响应当作文件下载。
    if (rawErrorEnvelope && !response.ok) {
      await throwRawResponseError(response)
    }
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
    /** Retryable business errors that must still terminate this command. */
    retryExcludedCodes?: readonly string[]
  } = {},
): Promise<T> {
  const {
    maxRetries = 3,
    delayMs = 1500,
    retryExcludedCodes = [],
    ...fetchOptions
  } = options

  let lastError: ApiRequestError | undefined
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await request<T>(path, fetchOptions)
    } catch (err: unknown) {
      if (
        err instanceof ApiRequestError
        && err.retryable
        && !retryExcludedCodes.includes(err.code)
        && attempt < maxRetries
      ) {
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

/**
 * 对 raw 文件接口的非 2xx 响应做错误识别：
 * - 若响应体为 JSON 且包含 `code` 字段（后端错误 envelope），复用
 *   `ApiRequestError.fromEnvelope` 抛出，保留 code/message/retryable/trace_id 等。
 * - 否则抛出 BAD_RESPONSE 占位码，message 含状态码，retryable 仅 5xx 为 true。
 *
 * 读取响应体后会消耗 response（不可再被下游当作文件下载使用）。
 */
async function throwRawResponseError(response: Response): Promise<never> {
  const contentType = response.headers.get('content-type') ?? ''
  const isJson = contentType.includes('application/json')
  const text = await response.text().catch(() => '')

  if (isJson && text) {
    try {
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      const body: unknown = JSON.parse(text)
      if (body && typeof body === 'object' && 'code' in body) {
        throw ApiRequestError.fromEnvelope(response.status, body as ApiError)
      }
    } catch (err) {
      // fromEnvelope 抛出的 ApiRequestError 直接向上传播
      if (err instanceof ApiRequestError) throw err
      // JSON 解析失败 → fall through 到 BAD_RESPONSE
    }
  }

  throw new ApiRequestError({
    code: TransportErrorCode.BAD_RESPONSE,
    userMessage: `病历导出失败（HTTP ${response.status}）`,
    status: response.status,
    detail: text.slice(0, 512),
    retryable: response.status >= 500,
  })
}
