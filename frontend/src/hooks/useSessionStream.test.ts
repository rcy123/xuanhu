import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSessionStream } from './useSessionStream'
import * as sse from '@/api/sse'
import type { SseConnection, SseHandlers } from '@/api/sse'
import type { SessionEvent } from '@/types/api'

function makeConn(overrides: Partial<SseConnection> = {}): SseConnection {
  return {
    closed: false,
    lastEventId: null,
    close: vi.fn(),
    ...overrides,
  }
}

type HandlerCapture = {
  onEvent: (event: SessionEvent) => void
  onOpen: () => void
  onError: (err: Event) => void
  onResync: (event: SessionEvent) => void
}

function setupConnectSpy() {
  let captured: HandlerCapture | null = null
  const conn = makeConn()
  const spy = vi.spyOn(sse, 'connectSessionStream').mockImplementation(
    (_sessionId: string, handlers: SseHandlers): SseConnection => {
      captured = {
        onEvent: handlers.onEvent,
        onOpen: handlers.onOpen ?? (() => {}),
        onError: handlers.onError ?? (() => {}),
        onResync: handlers.onResync ?? (() => {}),
      }
      return conn
    },
  )
  const getCaptured = (): HandlerCapture => {
    if (!captured) throw new Error('connectSessionStream not called yet')
    return captured
  }
  return { spy, conn, getCaptured }
}

function makeEvent(type: string, payload: Record<string, unknown> = {}): SessionEvent {
  return {
    event_id: 'evt-1',
    event_type: type as SessionEvent['event_type'],
    payload,
  }
}

describe('useSessionStream', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('sessionId=null 时 connectionState 为 idle，不建立连接', () => {
    const spy = vi.spyOn(sse, 'connectSessionStream')
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: null, stateVersion: undefined }),
    )
    expect(result.current.connectionState).toBe('idle')
    expect(spy).not.toHaveBeenCalled()
  })

  it('sessionId 非空时建立连接，connectionState 为 connected', async () => {
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    // onOpen 触发
    await act(async () => {
      getCaptured().onOpen()
    })
    expect(result.current.connectionState).toBe('connected')
  })

  it('stage.changed 触发 onStageChanged', async () => {
    const onStageChanged = vi.fn()
    const { getCaptured } = setupConnectSpy()
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onStageChanged,
      }),
    )
    await act(async () => {
      getCaptured().onEvent(makeEvent('stage.changed', { to_stage: 'sufficiency', state_version: 3 }))
    })
    expect(onStageChanged).toHaveBeenCalledWith('sufficiency', 3)
  })

  it('message.created 触发 onMessageCreated', async () => {
    const onMessageCreated = vi.fn()
    const { getCaptured } = setupConnectSpy()
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onMessageCreated,
      }),
    )
    await act(async () => {
      getCaptured().onEvent(makeEvent('message.created', { message_id: 'msg-1' }))
    })
    expect(onMessageCreated).toHaveBeenCalledWith('msg-1')
  })

  it('review.required 触发 onReviewRequired 且使用 modified_formula', async () => {
    const onReviewRequired = vi.fn()
    const { getCaptured } = setupConnectSpy()
    const modifiedFormula = { name: '加减方', composition: [{ herb: '甘草', dose: 6, unit: 'g' }] }
    const safetyReview = { passed: true, issues: [] }
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onReviewRequired,
      }),
    )
    await act(async () => {
      getCaptured().onEvent(
        makeEvent('review.required', {
          modified_formula: modifiedFormula,
          safety_review: safetyReview,
          base_formula: { name: '基础方', composition: [] }, // 不该被使用
        }),
      )
    })
    expect(onReviewRequired).toHaveBeenCalledWith(modifiedFormula, safetyReview)
    // 断言 base_formula 没有被传给回调
    const callArgs = onReviewRequired.mock.calls[0]
    expect(callArgs[0]).toBe(modifiedFormula)
    expect(callArgs[0]).not.toHaveProperty('name', '基础方')
  })

  it('safety.blocked 触发 onSafetyBlocked', async () => {
    const onSafetyBlocked = vi.fn()
    const { getCaptured } = setupConnectSpy()
    const issues = [{ severity: 'blocker', message: '十八反', rollback_target: 'prescription' }]
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onSafetyBlocked,
      }),
    )
    await act(async () => {
      getCaptured().onEvent(
        makeEvent('safety.blocked', {
          issues,
          rollback_target: 'prescription',
        }),
      )
    })
    expect(onSafetyBlocked).toHaveBeenCalledWith(issues, 'prescription')
  })

  it('resync 触发 onResync', async () => {
    const onResync = vi.fn()
    const { getCaptured } = setupConnectSpy()
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onResync,
      }),
    )
    await act(async () => {
      getCaptured().onResync(makeEvent('resync', { reason: 'stream trimmed' }))
    })
    expect(onResync).toHaveBeenCalledWith('stream trimmed')
  })

  it('agent.started 设置 agentRuns 为 running', async () => {
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    await act(async () => {
      getCaptured().onEvent(
        makeEvent('agent.started', { agent_name: 'syndrome', agent_run_id: 'run-1' }),
      )
    })
    expect(result.current.agentRuns['syndrome']).toEqual({
      status: 'running',
      agentRunId: 'run-1',
    })
  })

  it('agent.finished 设置 agentRuns 为 done', async () => {
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    await act(async () => {
      getCaptured().onEvent(
        makeEvent('agent.finished', { agent_name: 'syndrome', agent_run_id: 'run-1' }),
      )
    })
    expect(result.current.agentRuns['syndrome']).toEqual({
      status: 'done',
      agentRunId: 'run-1',
    })
  })

  it('agent.failed 设置 agentRuns 为 failed', async () => {
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    await act(async () => {
      getCaptured().onEvent(
        makeEvent('agent.failed', { agent_name: 'safety', error_code: 'SAFETY_ENGINE_FAILED' }),
      )
    })
    expect(result.current.agentRuns['safety']).toEqual({
      status: 'failed',
      error: 'SAFETY_ENGINE_FAILED',
    })
  })

  it('连续 3 次 onError 后进入 polling', async () => {
    const onPollingRefresh = vi.fn().mockResolvedValue(undefined)
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onPollingRefresh,
      }),
    )
    // 3 次 error
    await act(async () => {
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
    })
    expect(result.current.connectionState).toBe('polling')

    // 推进 3500ms
    await act(async () => {
      vi.advanceTimersByTime(3500)
    })
    expect(onPollingRefresh).toHaveBeenCalled()
  })

  it('onOpen 后从 polling 回到 connected', async () => {
    const { getCaptured } = setupConnectSpy()
    const { result } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    // 先进入 polling
    await act(async () => {
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
    })
    expect(result.current.connectionState).toBe('polling')

    // manual reconnect → 新连接 → onOpen
    await act(async () => {
      result.current.reconnect()
    })
    await act(async () => {
      getCaptured().onOpen()
    })
    expect(result.current.connectionState).toBe('connected')
  })

  it('session 变化时关闭旧连接', async () => {
    const { conn, getCaptured } = setupConnectSpy()
    const { rerender } = renderHook(
      ({ id }: { id: string | null }) =>
        useSessionStream({ sessionId: id, stateVersion: 1 }),
      { initialProps: { id: 's1' } },
    )
    await act(async () => {
      getCaptured().onOpen()
    })
    rerender({ id: 's2' })
    expect(conn.close).toHaveBeenCalled()
  })

  it('卸载时清理连接和定时器', async () => {
    const { conn, getCaptured } = setupConnectSpy()
    const { unmount } = renderHook(() =>
      useSessionStream({ sessionId: 's1', stateVersion: 1 }),
    )
    await act(async () => {
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
      getCaptured().onError(new Event('error'))
    })
    unmount()
    expect(conn.close).toHaveBeenCalled()
  })

  it('EventSource 不支持时直接 polling', async () => {
    const originalES = globalThis.EventSource
    // @ts-expect-error 测试用：移除 EventSource 模拟不支持
    delete globalThis.EventSource
    const onPollingRefresh = vi.fn().mockResolvedValue(undefined)
    renderHook(() =>
      useSessionStream({
        sessionId: 's1',
        stateVersion: 1,
        onPollingRefresh,
      }),
    )
    // 推进 3500ms → polling 触发
    await act(async () => {
      vi.advanceTimersByTime(3500)
    })
    expect(onPollingRefresh).toHaveBeenCalled()
    globalThis.EventSource = originalES
  })
})
