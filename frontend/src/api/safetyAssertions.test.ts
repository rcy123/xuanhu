import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __setBaseUrlResolver,
  confirmSafetyAssertion,
  listSafetyAssertions,
  rejectSafetyAssertion,
} from '@/api/mod'

const BASE = 'http://test.local/api/v1'

function response(data: unknown): Response {
  return {
    status: 200,
    ok: true,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve({
      code: 'SUCCESS',
      message: 'ok',
      data,
      trace_id: 'trace-safety-ui',
    }),
    text: () => Promise.resolve(''),
  } as unknown as Response
}

describe('safety assertion API', () => {
  beforeEach(() => {
    __setBaseUrlResolver(() => BASE)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists only proposed assertions by default', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [] }))
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch

    await listSafetyAssertions('session / 1')

    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/consult/sessions/session%20%2F%201/safety-assertions?status=proposed`,
    )
    expect(fetchMock.mock.calls[0][1].method).toBe('GET')
  })

  it('confirms with the caller-supplied doctor ID and an idempotency key', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ status: 'confirmed' }))
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch

    await confirmSafetyAssertion(
      'session-1',
      'assertion-1',
      {},
      { doctorId: 'doctor-entered', idempotencyKey: 'decision-1' },
    )

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      `${BASE}/consult/sessions/session-1/safety-assertions/assertion-1/confirm`,
    )
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({})
    expect(init.headers).toMatchObject({
      'X-Doctor-Id': 'doctor-entered',
      'X-Idempotency-Key': 'decision-1',
    })
  })

  it('sends the rejection reason without inventing a doctor identity', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ status: 'rejected' }))
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch

    await rejectSafetyAssertion(
      'session-1',
      'assertion-1',
      { reason_code: 'EXTRACTION_REJECTED' },
      { doctorId: 'doctor-entered' },
    )

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(JSON.parse(init.body as string)).toEqual({ reason_code: 'EXTRACTION_REJECTED' })
    expect((init.headers as Record<string, string>)['X-Doctor-Id']).toBe('doctor-entered')
    expect((init.headers as Record<string, string>)['X-Idempotency-Key']).toBeTruthy()
  })
})
