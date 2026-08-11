/**
 * 悬壶 WebUI —— R7 异步命令终态对账 hook 单元测试
 *
 * 验收覆盖：
 * - registerAccepted：登记 202 命令、去重、立即对账（追上已终态命令）。
 * - 终态对账：succeeded 触发 onSucceeded；failed 触发 onFailed（带 PHI 安全 error_code）。
 * - 幂等保留：到达终态前 idempotencyKey 一直保留在条目中。
 * - SSE command.* 事件仅作唤醒（handleCommandEvent），权威以 GET status 为准。
 * - 有界轮询：SSE 丢失时靠轮询兜底；全部终态后停止定时器。
 * - 取消安全：clear() 清空未决条目并停止定时器。
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, cleanup } from '@testing-library/react'
import { useCommandReconciliation } from './useCommandReconciliation'
import type {
  AsyncCommandAccepted,
  AsyncCommandStatus,
  CommandEventPayload,
} from '@/types/api'

function makeAccepted(overrides: Partial<AsyncCommandAccepted> = {}): AsyncCommandAccepted {
  return {
    command_id: 'cmd-1',
    operation: 'session.advance',
    status: 'queued',
    replayed: false,
    attempt_count: 0,
    links: { self: '/self', session: '/s', stream: '/stream' },
    ...overrides,
  }
}

function makeStatus(
  status: AsyncCommandStatus['status'],
  overrides: Partial<AsyncCommandStatus> = {},
): AsyncCommandStatus {
  return {
    command_id: 'cmd-1',
    operation: 'session.advance',
    status,
    attempt_count: 1,
    timestamps: { created_at: '2026-08-01T00:00:00Z' },
    links: { self: '/self', session: '/s', stream: '/stream' },
    ...overrides,
  }
}

describe('useCommandReconciliation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('登记 202 命令后立即做一次对账以追上终态（succeeded）', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('succeeded'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 4000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-1')
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(fetchStatus).toHaveBeenCalledWith('s1', 'cmd-1')
    // 幂等保留到终态：onSucceeded 收到含 idempotencyKey 的终态条目。
    expect(onSucceeded).toHaveBeenCalledTimes(1)
    expect(onSucceeded.mock.calls[0][0]).toMatchObject({
      commandId: 'cmd-1',
      operation: 'session.advance',
      idempotencyKey: 'idem-1',
      state: 'succeeded',
    })
    // 终态后不再 outstanding。
    expect(result.current.hasOutstanding).toBe(false)
    expect(result.current.getEntry('cmd-1')).toBeUndefined()
  })

  it('failed 终态触发 onFailed 并携带有界 PHI 安全 error_code', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(
      makeStatus('failed', { error: { code: 'AGENT_TRIGGER_FAILED' } }),
    )
    const onFailed = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onFailed, pollIntervalMs: 4000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted({ operation: 'intake.message' }), 's1', 'idem-2')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(onFailed).toHaveBeenCalledTimes(1)
    expect(onFailed.mock.calls[0][0]).toMatchObject({
      state: 'failed',
      errorCode: 'AGENT_TRIGGER_FAILED',
    })
    // 绝不把私有 payload 传入失败条目。
    expect(onFailed.mock.calls[0][0].errorCode).not.toBeUndefined()
    expect(result.current.hasOutstanding).toBe(false)
  })

  it('同一 commandId 去重：重复登记不会重复对账/执行', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('running'))
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, pollIntervalMs: 4000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-1')
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-1')
    })

    expect(result.current.hasOutstanding).toBe(true)
    // 两条登记只保留一条 outstanding。
    expect(result.current.outstanding).toHaveLength(1)
  })

  it('未终态时保留条目并允许 SSE 事件唤醒对账', async () => {
    const fetchStatus = vi
      .fn()
      .mockResolvedValueOnce(makeStatus('running'))
      .mockResolvedValueOnce(makeStatus('succeeded'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 4000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-3')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // 第一次对账得到 running，仍 outstanding。
    expect(result.current.hasOutstanding).toBe(true)

    // SSE 事件：仅唤醒，不做本地乐观处理。
    const event: CommandEventPayload = {
      command_id: 'cmd-1',
      operation: 'session.advance',
      status: 'succeeded',
      attempt: 1,
    }
    act(() => {
      result.current.handleCommandEvent(event)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // 权威以 GET status 为准，SSE 本身不直接清空条目。
    expect(onSucceeded).toHaveBeenCalledTimes(1)
    expect(result.current.hasOutstanding).toBe(false)
  })

  it('SSE 丢失时有界轮询兜底，全部终态后停止定时器', async () => {
    const fetchStatus = vi
      .fn()
      .mockResolvedValueOnce(makeStatus('running'))
      .mockResolvedValueOnce(makeStatus('succeeded'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-4')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.hasOutstanding).toBe(true)

    // 不触发任何 SSE 事件，仅靠轮询推进。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(fetchStatus).toHaveBeenCalledTimes(2)
    expect(onSucceeded).toHaveBeenCalledTimes(1)
    expect(result.current.hasOutstanding).toBe(false)

    // 定时器已停止：再多等也不会有额外对账。
    const callsBeforeIdle = fetchStatus.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(fetchStatus.mock.calls.length).toBe(callsBeforeIdle)
  })

  it('状态接口失败时保留条目，等待下一次轮询，不永久 loading', async () => {
    const fetchStatus = vi
      .fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(makeStatus('succeeded'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-5')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // 第一次失败：不崩溃、保留条目、不触发终态。
    expect(result.current.hasOutstanding).toBe(true)
    expect(onSucceeded).not.toHaveBeenCalled()

    // 下一次轮询成功追上终态。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(onSucceeded).toHaveBeenCalledTimes(1)
    expect(result.current.hasOutstanding).toBe(false)
  })

  it('clear() 清空未决条目并停止定时器（会话切换/卸载）', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('running'))
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-6')
    })
    expect(result.current.hasOutstanding).toBe(true)

    act(() => {
      result.current.clear()
    })
    expect(result.current.hasOutstanding).toBe(false)
    expect(result.current.outstanding).toHaveLength(0)

    const callsBefore = fetchStatus.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(fetchStatus.mock.calls.length).toBe(callsBefore)
  })

  it('setHandlers 可在登记后再注册终态回调（注册顺序无关）', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('succeeded'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, pollIntervalMs: 4000 }),
    )

    // 先登记命令，后注册回调（模拟 App 先登记、ChatPanel 后 setHandlers）。
    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-7')
    })
    act(() => {
      result.current.setHandlers(onSucceeded)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(onSucceeded).toHaveBeenCalledTimes(1)
  })

  it('isOutstandingFor 按操作判断未决命令', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('running'))
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted({ operation: 'session.advance' }), 's1', 'k')
    })

    expect(result.current.isOutstandingFor('session.advance')).toBe(true)
    expect(result.current.isOutstandingFor('intake.message')).toBe(false)
  })

  it('返回的 result 对象引用稳定：无状态变化时标识不变', () => {
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus: vi.fn(), pollIntervalMs: 1000 }),
    )
    const first = result.current
    // 触发一次无内容变化的渲染（本测试仅验证无未决命令时 clear 不产生 state 变更）。
    act(() => {
      result.current.clear()
    })
    expect(result.current).toBe(first)
    act(() => {
      result.current.clear()
    })
    expect(result.current).toBe(first)
  })

  it('clear 幂等：无未决命令时反复调用不触发 state 变更（避免渲染循环）', () => {
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus: vi.fn(), pollIntervalMs: 1000 }),
    )
    const identity = result.current
    for (let i = 0; i < 20; i++) {
      act(() => {
        result.current.clear()
      })
    }
    expect(result.current).toBe(identity)
    expect(result.current.outstanding).toHaveLength(0)
  })

  it('自动对账预算耗尽：停止轮询、保留命令为 attention、暴露固定 PHI 安全码', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('running'))
    const onAttention = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({
        fetchStatus,
        onAttention,
        pollIntervalMs: 1000,
        maxPollAttempts: 3,
        pollDeadlineMs: 0,
      }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-attn')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.hasOutstanding).toBe(true)

    // 推进 3 个轮询周期（每次 viaPoll=true 计一次预算）→ 预算耗尽 → attention。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(onAttention).toHaveBeenCalledTimes(1)
    expect(result.current.hasAttention).toBe(true)
    expect(result.current.hasOutstanding).toBe(false)
    expect(result.current.attention).toHaveLength(1)
    // 幂等键与 commandId 保留（不确定/待处理，绝不被清除或伪造失败）。
    const attn = result.current.attention[0]
    expect(attn.idempotencyKey).toBe('idem-attn')
    expect(attn.commandId).toBe('cmd-1')
    // 不触发 failed/succeeded（不伪造终态）。
    expect(result.current.getEntry('cmd-1')?.state).toBe('attention')

    // 无永久 interval：再多等待也不会有额外自动对账。
    const callsBeforeIdle = fetchStatus.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(fetchStatus.mock.calls.length).toBe(callsBeforeIdle)
  })

  it('手动 retryStatus 可从 attention 恢复：重置预算、重新对账同一命令', async () => {
    const fetchStatus = vi
      .fn()
      .mockResolvedValue(makeStatus('running'))
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({
        fetchStatus,
        onSucceeded,
        pollIntervalMs: 1000,
        maxPollAttempts: 2,
        pollDeadlineMs: 0,
      }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-recover')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // 耗尽预算 → attention
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(result.current.hasAttention).toBe(true)

    // 后端恢复为 succeeded，手动 retryStatus 重新对账（不发起新 POST，仅 GET）。
    fetchStatus.mockResolvedValue(makeStatus('succeeded'))
    await act(async () => {
      await result.current.retryStatus('cmd-1')
    })

    expect(onSucceeded).toHaveBeenCalledTimes(1)
    expect(onSucceeded.mock.calls[0][0]).toMatchObject({
      commandId: 'cmd-1',
      idempotencyKey: 'idem-recover',
      state: 'succeeded',
    })
    expect(result.current.hasAttention).toBe(false)
    expect(result.current.getEntry('cmd-1')).toBeUndefined()
  })

  it('clear 后迟到的 status 结果失效：不结算到新登记的同命令', async () => {
    let resolveStatus!: (s: AsyncCommandStatus) => void
    const fetchStatus = vi.fn().mockImplementation(
      () => new Promise<AsyncCommandStatus>((resolve) => { resolveStatus = resolve }),
    )
    const onSucceeded = vi.fn()
    const { result } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-old')
    })
    // 立即对账发出 GET，但结果未返回。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // 会话切换：clear 使旧条目失效。
    act(() => {
      result.current.clear()
    })
    expect(result.current.getEntry('cmd-1')).toBeUndefined()

    // 新会话以同一 commandId 重新登记。
    act(() => {
      result.current.registerAccepted(makeAccepted(), 's2', 'idem-new')
    })
    // 旧请求迟到返回 succeeded → 必须被 generation 失效，不得结算到 s2 的新条目。
    await act(async () => {
      resolveStatus(makeStatus('succeeded'))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(onSucceeded).not.toHaveBeenCalled()
    expect(result.current.getEntry('cmd-1')).toMatchObject({ sessionId: 's2', idempotencyKey: 'idem-new' })
  })

  it('卸载后迟到结果失效：不再结算，也不重建定时器', async () => {
    let resolveStatus!: (s: AsyncCommandStatus) => void
    const fetchStatus = vi.fn().mockImplementation(
      () => new Promise<AsyncCommandStatus>((resolve) => { resolveStatus = resolve }),
    )
    const onSucceeded = vi.fn()
    const { result, unmount } = renderHook(() =>
      useCommandReconciliation({ fetchStatus, onSucceeded, pollIntervalMs: 1000 }),
    )

    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-unmount')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    unmount()
    await act(async () => {
      resolveStatus(makeStatus('succeeded'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(onSucceeded).not.toHaveBeenCalled()
  })

  it('幂等保留贯穿 attention：预算耗尽后 idempotencyKey 仍保留在条目中', async () => {
    const fetchStatus = vi.fn().mockResolvedValue(makeStatus('running'))
    const { result } = renderHook(() =>
      useCommandReconciliation({
        fetchStatus,
        pollIntervalMs: 1000,
        maxPollAttempts: 1,
        pollDeadlineMs: 0,
      }),
    )
    act(() => {
      result.current.registerAccepted(makeAccepted(), 's1', 'idem-keep')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(result.current.hasAttention).toBe(true)
    expect(result.current.getEntry('cmd-1')).toMatchObject({
      idempotencyKey: 'idem-keep',
      state: 'attention',
    })
  })
})
