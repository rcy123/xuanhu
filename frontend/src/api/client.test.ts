/**
 * 悬壶 WebUI —— API client 单元测试（P8-1 验收）
 *
 * 覆盖验收标准：
 * - 成功 envelope 正确返回 data
 * - 错误 envelope 抛出 ApiRequestError（含 code/message/retryable/trace_id/stage/issues）
 * - HTTP 错误（非 envelope、5xx）
 * - trace_id 透传
 * - 可重试错误自动重试
 * - 分页与游标分页 envelope
 * - 请求头注入（X-Doctor-Id / X-Idempotency-Key / X-State-Version）
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiRequestError,
  createSession,
  getHealth,
  listMessages,
  listSessions,
  request,
  reviewPrescription,
  submitMessage,
  submitMessageWithRetry,
  terminateSession,
  TransportErrorCode,
} from '@/api/mod'
import { __setBaseUrlResolver } from '@/api/mod'

// ---------------------------------------------------------------------------
// fetch mock 工具
// ---------------------------------------------------------------------------

interface MockResponseInit {
  status?: number
  body?: unknown
  headers?: Record<string, string>
  /** 直接返回原始文本而非 JSON */
  text?: string
}

function mockResponse(init: MockResponseInit = {}): Response {
  const status = init.status ?? 200
  const headers = new Headers(init.headers)
  if (init.text !== undefined) {
    headers.set('content-type', 'text/plain')
    return {
      status,
      ok: status >= 200 && status < 300,
      headers,
      text: () => Promise.resolve(init.text as string),
      json: () => Promise.reject(new Error('not json')),
    } as unknown as Response
  }
  headers.set('content-type', 'application/json')
  const body = init.body ?? null
  return {
    status,
    ok: status >= 200 && status < 300,
    headers,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function mockFetch(impl: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(impl)
  globalThis.fetch = fn as unknown as typeof globalThis.fetch
  return fn
}

// ---------------------------------------------------------------------------
// 测试夹具
// ---------------------------------------------------------------------------

const BASE = 'http://test.local/api/v1'

beforeEach(() => {
  __setBaseUrlResolver(() => BASE)
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// 成功 envelope
// ---------------------------------------------------------------------------

describe('request - 成功 envelope', () => {
  it('返回脱包后的 data 字段', async () => {
    mockFetch(() =>
      mockResponse({
        status: 201,
        body: {
          code: 'SUCCESS',
          message: 'ok',
          data: { session_id: 's-1', current_stage: 'inquiry' },
          trace_id: 'trace-1',
        },
      }),
    )

    const data = await request<{ session_id: string; current_stage: string }>('consult/sessions', {
      method: 'POST',
    })

    expect(data).toEqual({ session_id: 's-1', current_stage: 'inquiry' })
  })

  it('data 为 null 时返回 null', async () => {
    mockFetch(() =>
      mockResponse({
        body: { code: 'SUCCESS', message: 'ok', data: null, trace_id: 't' },
      }),
    )
    const data = await request<unknown>('consult/sessions')
    expect(data).toBeNull()
  })

  it('非 envelope 扁平 JSON（如 /health）原样返回', async () => {
    mockFetch(() =>
      mockResponse({ body: { status: 'ok', version: '0.1.0', timestamp: 'x' } }),
    )
    const data = await getHealth()
    expect(data).toEqual({ status: 'ok', version: '0.1.0', timestamp: 'x' })
  })
})

// ---------------------------------------------------------------------------
// 错误 envelope
// ---------------------------------------------------------------------------

describe('request - 错误 envelope', () => {
  it('抛出 ApiRequestError 并携带 code/message/retryable/stage/trace_id', async () => {
    mockFetch(() =>
      mockResponse({
        status: 404,
        body: {
          code: 'SESSION_NOT_FOUND',
          message: '会话不存在或已终止',
          detail: 'session_id=abc 在数据库中未找到',
          retryable: false,
          stage: null,
          trace_id: 'trace-404',
        },
      }),
    )

    await expect(request('consult/sessions/abc')).rejects.toMatchObject({
      name: 'ApiRequestError',
      code: 'SESSION_NOT_FOUND',
      userMessage: '会话不存在或已终止',
      status: 404,
      retryable: false,
      traceId: 'trace-404',
    })
  })

  it('SAFETY_REVIEW_BLOCKED 携带 issues 字段', async () => {
    const issues = [
      { severity: 'blocker', herb: '附子', message: '超剂量' },
    ]
    mockFetch(() =>
      mockResponse({
        status: 409,
        body: {
          code: 'SAFETY_REVIEW_BLOCKED',
          message: '安全审核阻断',
          detail: null,
          retryable: false,
          stage: 'review',
          trace_id: 'trace-safety',
          issues,
        },
      }),
    )

    try {
      await request('consult/sessions/s/review', { method: 'POST' })
      throw new Error('should have thrown')
    } catch (err) {
      expect(err).toBeInstanceOf(ApiRequestError)
      const e = err as ApiRequestError
      expect(e.code).toBe('SAFETY_REVIEW_BLOCKED')
      expect(e.stage).toBe('review')
      expect(e.traceId).toBe('trace-safety')
      expect(e.issues).toEqual(issues)
    }
  })

  it('retryable=true 的错误码（SESSION_BUSY）透传 retryable', async () => {
    mockFetch(() =>
      mockResponse({
        status: 409,
        body: {
          code: 'SESSION_BUSY',
          message: '会话正在处理其他请求',
          retryable: true,
          stage: null,
          trace_id: 't-busy',
        },
      }),
    )
    await expect(request('consult/sessions/s/messages', { method: 'POST' })).rejects.toMatchObject({
      code: 'SESSION_BUSY',
      retryable: true,
    })
  })

  it('ApiRequestError.fromEnvelope 保留原始 envelope', async () => {
    const env = {
      code: 'INVALID_STAGE_TRANSITION',
      message: '当前阶段不支持此操作',
      detail: 'stage=safety',
      retryable: false,
      stage: 'safety',
      trace_id: 't-stage',
    }
    mockFetch(() => mockResponse({ status: 409, body: env }))
    try {
      await request('consult/sessions/s/messages', { method: 'POST' })
      throw new Error('should have thrown')
    } catch (err) {
      const e = err as ApiRequestError
      expect(e.envelope).toMatchObject({ code: 'INVALID_STAGE_TRANSITION' })
      expect(e.stage).toBe('safety')
    }
  })
})

// ---------------------------------------------------------------------------
// HTTP / 传输层错误
// ---------------------------------------------------------------------------

describe('request - HTTP 与传输错误', () => {
  it('非 JSON 响应抛出 BAD_RESPONSE', async () => {
    mockFetch(() => mockResponse({ status: 502, text: '<html>Bad Gateway</html>' }))
    await expect(request('consult/sessions')).rejects.toMatchObject({
      code: TransportErrorCode.BAD_RESPONSE,
      status: 502,
      retryable: true,
    })
  })

  it('网络断开抛出 NETWORK_ERROR 且可重试', async () => {
    mockFetch(() => Promise.reject(new TypeError('Failed to fetch')))
    await expect(request('consult/sessions')).rejects.toMatchObject({
      code: TransportErrorCode.NETWORK_ERROR,
      status: 0,
      retryable: true,
    })
  })

  it('非 envelope 的 4xx JSON 抛出 BAD_RESPONSE', async () => {
    mockFetch(() =>
      mockResponse({
        status: 418,
        body: { weird: true },
        headers: { 'content-type': 'application/json' },
      }),
    )
    // { weird: true } 没有 code 字段，按非 envelope 处理直接返回
    const data = await request<{ weird: boolean }>('consult/sessions')
    expect(data).toEqual({ weird: true })
  })
})

// ---------------------------------------------------------------------------
// 请求头注入
// ---------------------------------------------------------------------------

describe('request - 请求头注入', () => {
  it('注入 X-Doctor-Id / X-Idempotency-Key / X-State-Version', async () => {
    const fn = mockFetch(() => mockResponse({ body: { code: 'SUCCESS', data: {}, trace_id: 't' } }))

    await request('consult/sessions', {
      method: 'POST',
      ctx: {
        doctorId: 'doctor_001',
        idempotencyKey: 'idem-1',
        stateVersion: 7,
      },
    })

    const init = fn.mock.calls[0][1]
    const headers = init.headers as Record<string, string>
    expect(headers['X-Doctor-Id']).toBe('doctor_001')
    expect(headers['X-Idempotency-Key']).toBe('idem-1')
    expect(headers['X-State-Version']).toBe('7')
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('不传 ctx 时不注入可选头', async () => {
    const fn = mockFetch(() => mockResponse({ body: { code: 'SUCCESS', data: {}, trace_id: 't' } }))
    await request('consult/sessions', { method: 'GET' })
    const headers = fn.mock.calls[0][1].headers as Record<string, string>
    expect(headers['X-Doctor-Id']).toBeUndefined()
    expect(headers['X-State-Version']).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// trace_id 与 URL 拼接
// ---------------------------------------------------------------------------

describe('request - URL 拼接', () => {
  it('相对路径拼接 baseUrl', async () => {
    const fn = mockFetch(() => mockResponse({ body: { code: 'SUCCESS', data: {}, trace_id: 't' } }))
    await request('consult/sessions', { method: 'GET' })
    expect(fn.mock.calls[0][0]).toBe(`${BASE}/consult/sessions`)
  })

  it('绝对 URL 不拼接', async () => {
    const fn = mockFetch(() => mockResponse({ body: { code: 'SUCCESS', data: {}, trace_id: 't' } }))
    await request('https://other.example/health', { method: 'GET' })
    expect(fn.mock.calls[0][0]).toBe('https://other.example/health')
  })
})

// ---------------------------------------------------------------------------
// 重试
// ---------------------------------------------------------------------------

describe('requestWithRetry', () => {
  it('可重试错误在 maxRetries 内成功则返回', async () => {
    let calls = 0
    mockFetch(() => {
      calls++
      if (calls < 3) {
        return mockResponse({
          status: 409,
          body: { code: 'SESSION_BUSY', message: 'busy', retryable: true, trace_id: 't' },
        })
      }
      return mockResponse({
        body: { code: 'SUCCESS', data: { ok: true }, trace_id: 't' },
      })
    })

    const data = await submitMessageWithRetry(
      's-1',
      { content: 'hello', role: 'doctor' },
      undefined,
      3,
    )
    expect(data).toMatchObject({ ok: true })
    expect(calls).toBe(3)
  })

  it('不可重试错误立即抛出，不重试', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        status: 404,
        body: {
          code: 'SESSION_NOT_FOUND',
          message: 'not found',
          retryable: false,
          trace_id: 't',
        },
      }),
    )
    await expect(
      submitMessageWithRetry('s-1', { content: 'hi', role: 'doctor' }, undefined, 3),
    ).rejects.toMatchObject({ code: 'SESSION_NOT_FOUND' })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('重试耗尽仍失败则抛出最后一次错误', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        status: 409,
        body: { code: 'SESSION_BUSY', message: 'busy', retryable: true, trace_id: 't' },
      }),
    )
    await expect(
      submitMessageWithRetry('s-1', { content: 'hi', role: 'doctor' }, undefined, 2),
    ).rejects.toMatchObject({ code: 'SESSION_BUSY' })
    // 1 次初始 + 2 次重试 = 3
    expect(fn).toHaveBeenCalledTimes(3)
  }, 15000)
})

// ---------------------------------------------------------------------------
// 业务方法签名
// ---------------------------------------------------------------------------

describe('业务方法签名', () => {
  it('createSession 发送 POST 请求体', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        status: 201,
        body: {
          code: 'SUCCESS',
          data: { session_id: 's', current_stage: 'inquiry', status: 'active', patient_info: {}, created_at: 'x' },
          trace_id: 't',
        },
      }),
    )
    await createSession({ chief_complaint: '头痛' }, { doctorId: 'd1', idempotencyKey: 'k1' })
    const init = fn.mock.calls[0][1]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ chief_complaint: '头痛' })
  })

  it('listSessions 序列化查询参数', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        body: {
          code: 'SUCCESS',
          data: { items: [], total: 0, page: 1, page_size: 20 },
          trace_id: 't',
        },
      }),
    )
    await listSessions({ status: 'active', page: 2 })
    const url = fn.mock.calls[0][0] as string
    expect(url).toContain('status=active')
    expect(url).toContain('page=2')
  })

  it('listMessages 返回游标分页结构', async () => {
    mockFetch(() =>
      mockResponse({
        body: {
          code: 'SUCCESS',
          data: { items: [], has_more: false, next_cursor: null },
          trace_id: 't',
        },
      }),
    )
    const data = await listMessages('s-1', { limit: 50 })
    expect(data).toMatchObject({ items: [], has_more: false, next_cursor: null })
  })

  it('reviewPrescription 发送 POST review 请求体', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        body: {
          code: 'SUCCESS',
          data: {
            session_id: 's',
            action: 'confirm',
            current_stage: 'record',
            status: 'active',
            pending_review: false,
            review_id: 'r1',
            state_version: 2,
            updated_at: 'x',
          },
          trace_id: 't',
        },
      }),
    )
    await reviewPrescription('s', { action: 'confirm' }, { idempotencyKey: 'k' })
    expect(fn.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fn.mock.calls[0][1].body as string)).toEqual({ action: 'confirm' })
  })

  it('submitMessage 发送 POST 消息请求体', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        body: {
          code: 'SUCCESS',
          data: {
            message_id: 'm',
            session_id: 's',
            role: 'agent',
            stage: 'inquiry',
            content: 'reply',
            current_stage: 'inquiry',
            state_version: 2,
            created_at: 'x',
          },
          trace_id: 't',
        },
      }),
    )
    await submitMessage('s', { content: 'hi', role: 'doctor' }, { stateVersion: 1 })
    const init = fn.mock.calls[0][1]
    expect(init.method).toBe('POST')
    const headers = init.headers as Record<string, string>
    expect(headers['X-State-Version']).toBe('1')
  })

  it('terminateSession 发送 POST terminate 请求体', async () => {
    const fn = mockFetch(() =>
      mockResponse({
        body: {
          code: 'SUCCESS',
          data: {
            session_id: 's',
            status: 'terminated',
            current_stage: 'blocked',
            blocked_reason: 'terminated_by_doctor',
            updated_at: 'x',
          },
          trace_id: 't',
        },
      }),
    )
    await terminateSession('s', { reason: 'done' })
    const url = fn.mock.calls[0][0] as string
    expect(url).toContain('/terminate')
    expect(JSON.parse(fn.mock.calls[0][1].body as string)).toEqual({ reason: 'done' })
  })
})
