import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useMessages } from './useMessages'
import * as api from '@/api/index'
import { ApiRequestError } from '@/api/errors'
import type { CursorData, MessageCreateData, MessageItem } from '@/types/api'

function makeMsg(id: string, role: MessageItem['role']): MessageItem {
  return {
    id,
    session_id: 's',
    role,
    stage: 'inquiry',
    content: `c-${id}`,
    created_at: `2026-07-03T10:3${id}:00+08:00`,
  }
}

describe('useMessages', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('loadMessages 加载并按时间升序', async () => {
    // 后端返回倒序，前端应反转为升序
    const data: CursorData<MessageItem> = {
      items: [makeMsg('9', 'agent'), makeMsg('1', 'doctor')],
      has_more: false,
      next_cursor: null,
    }
    vi.spyOn(api, 'listMessages').mockResolvedValue(data)
    const { result } = renderHook(() => useMessages())
    await act(async () => {
      await result.current.loadMessages('s')
    })
    expect(result.current.messages.map((m) => m.id)).toEqual(['1', '9'])
  })

  it('submit 携带 stateVersion 并刷新消息', async () => {
    const submitSpy = vi
      .spyOn(api, 'submitMessageWithRetry')
      .mockResolvedValue({
        message_id: 'm',
        session_id: 's',
        role: 'agent',
        stage: 'inquiry',
        content: 'reply',
        current_stage: 'inquiry',
        state_version: 2,
        created_at: '',
      })
    vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [makeMsg('1', 'doctor'), makeMsg('2', 'agent')],
      has_more: false,
      next_cursor: null,
    })
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 1 as number)
    let ok = false
    await act(async () => {
      ok = await result.current.submit('s', '头痛', 1, refresh)
    })
    expect(ok).toBe(true)
    expect(submitSpy).toHaveBeenCalledWith(
      's',
      { content: '头痛', role: 'doctor' },
      expect.objectContaining({
        stateVersion: 1,
        idempotencyKey: expect.any(String),
      }),
    )
    expect(result.current.pendingSubmission).toBeNull()
  })

  it('INVALID_STATE_VERSION 触发刷新并重提一次', async () => {
    const versionErr = new ApiRequestError({
      code: 'INVALID_STATE_VERSION',
      userMessage: '版本落后',
      status: 409,
      retryable: true,
    })
    const submitSpy = vi
      .spyOn(api, 'submitMessageWithRetry')
      .mockRejectedValueOnce(versionErr)
      .mockResolvedValueOnce({
        message_id: 'm',
        session_id: 's',
        role: 'agent',
        stage: 'inquiry',
        content: 'reply',
        current_stage: 'inquiry',
        state_version: 2,
        created_at: '',
      })
    vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [],
      has_more: false,
      next_cursor: null,
    })
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 2 as number)
    let ok = false
    await act(async () => {
      ok = await result.current.submit('s', '头痛', 1, refresh)
    })
    expect(ok).toBe(true)
    expect(refresh).toHaveBeenCalled()
    // 第一次用旧版本 1，重试用刷新后的 2
    expect(submitSpy.mock.calls[0][2]?.stateVersion).toBe(1)
    // 重试调用应使用刷新后的版本号 2（最后一次调用）
    const lastCall = submitSpy.mock.calls[submitSpy.mock.calls.length - 1]
    expect(lastCall[2]?.stateVersion).toBe(2)
    expect(lastCall[2]?.idempotencyKey).not.toBe(submitSpy.mock.calls[0][2]?.idempotencyKey)
    expect(lastCall[1]).toEqual(submitSpy.mock.calls[0][1])
  })

  it('does not rebase a stale command after the structured question has changed', async () => {
    const versionErr = new ApiRequestError({
      code: 'INVALID_STATE_VERSION',
      userMessage: 'stale version',
      status: 409,
      retryable: true,
    })
    const submitSpy = vi.spyOn(api, 'submitMessageWithRetry').mockRejectedValue(versionErr)
    vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [{
        ...makeMsg('question-new', 'agent'),
        agent_name: 'question_composer',
        structured_delta: { selected_dimension: 'safety.allergy_status' },
      }],
      has_more: false,
      next_cursor: null,
    })
    const { result } = renderHook(() => useMessages())

    let ok = true
    await act(async () => {
      ok = await result.current.submit(
        's',
        'old answer',
        1,
        async () => 2,
        'question-old',
      )
    })

    expect(ok).toBe(false)
    expect(submitSpy).toHaveBeenCalledTimes(1)
    expect(result.current.pendingSubmission).toBeNull()
    expect(result.current.submitError).toMatchObject({
      code: 'REPLY_CONTEXT_CHANGED',
      retryable: false,
    })
  })

  it('does not send a rebased command when refreshed state_version is missing', async () => {
    const versionErr = new ApiRequestError({
      code: 'INVALID_STATE_VERSION',
      userMessage: 'stale version',
      status: 409,
      retryable: true,
    })
    const submitSpy = vi.spyOn(api, 'submitMessageWithRetry').mockRejectedValue(versionErr)
    const listSpy = vi.spyOn(api, 'listMessages')
    const { result } = renderHook(() => useMessages())

    let ok = true
    await act(async () => {
      ok = await result.current.submit('s', 'answer', 1, async () => undefined)
    })

    expect(ok).toBe(false)
    expect(submitSpy).toHaveBeenCalledTimes(1)
    expect(listSpy).not.toHaveBeenCalled()
    expect(result.current.submitError?.code).toBe('REBASE_STATE_VERSION_MISSING')
    expect(result.current.pendingSubmission).toBeNull()
  })

  it('does not let an old session history response overwrite the newly selected session', async () => {
    let resolveOld!: (data: CursorData<MessageItem>) => void
    const oldResponse = new Promise<CursorData<MessageItem>>((resolve) => {
      resolveOld = resolve
    })
    vi.spyOn(api, 'listMessages').mockImplementation(async (sessionId) => {
      if (sessionId === 'old-session') return oldResponse
      return {
        items: [makeMsg('8', 'agent')],
        has_more: false,
        next_cursor: null,
      }
    })
    const { result } = renderHook(() => useMessages())

    let oldLoad!: Promise<MessageItem[] | null>
    await act(async () => {
      oldLoad = result.current.loadMessages('old-session')
      await Promise.resolve()
    })
    await act(async () => {
      result.current.clear()
      await result.current.loadMessages('new-session')
    })
    expect(result.current.messages.map((item) => item.id)).toEqual(['8'])

    await act(async () => {
      resolveOld({
        items: [makeMsg('1', 'doctor')],
        has_more: false,
        next_cursor: null,
      })
      await oldLoad
    })

    expect(result.current.messages.map((item) => item.id)).toEqual(['8'])
    expect(result.current.loading).toBe(false)
  })

  it('does not let an old submission completion clear the new session pending command', async () => {
    let resolveOld!: (data: MessageCreateData) => void
    const oldResponse = new Promise<MessageCreateData>((resolve) => {
      resolveOld = resolve
    })
    const newResponseLost = new ApiRequestError({
      code: 'NETWORK_ERROR',
      userMessage: 'new response lost',
      status: 0,
      retryable: true,
    })
    vi.spyOn(api, 'submitMessageWithRetry')
      .mockImplementationOnce(() => oldResponse)
      .mockRejectedValueOnce(newResponseLost)
    const listSpy = vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [],
      has_more: false,
      next_cursor: null,
    })
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 1 as number)

    let oldSubmit!: Promise<boolean>
    await act(async () => {
      oldSubmit = result.current.submit('old-session', 'old answer', 1, refresh, 'old-question')
      await Promise.resolve()
    })
    await act(async () => {
      result.current.clear()
      await result.current.loadMessages('new-session')
    })
    await act(async () => {
      await result.current.submit('new-session', 'new answer', 1, refresh, 'new-question')
    })
    const newPending = result.current.pendingSubmission
    expect(newPending).toMatchObject({
      content: 'new answer',
      replyToMessageId: 'new-question',
    })

    await act(async () => {
      resolveOld({
        message_id: 'old-message',
        session_id: 'old-session',
        role: 'agent',
        stage: 'inquiry',
        content: 'old next question',
        current_stage: 'inquiry',
        state_version: 2,
        created_at: '',
      })
      await oldSubmit
    })

    expect(result.current.pendingSubmission).toBe(newPending)
    expect(result.current.submitError).toBe(newResponseLost)
    expect(listSpy).toHaveBeenCalledTimes(1)
    expect(listSpy).toHaveBeenCalledWith('new-session', { limit: 100 })
  })

  it('keeps the exact saved-message command for explicit AGENT_TRIGGER_FAILED retry', async () => {
    const agentFailed = new ApiRequestError({
      code: 'AGENT_TRIGGER_FAILED',
      userMessage: 'message saved but agent failed',
      status: 503,
      retryable: true,
    })
    const submitSpy = vi.spyOn(api, 'submitMessageWithRetry')
      .mockRejectedValueOnce(agentFailed)
      .mockResolvedValueOnce({
        message_id: 'message-1',
        session_id: 's',
        role: 'agent',
        stage: 'inquiry',
        content: 'resumed',
        current_stage: 'inquiry',
        state_version: 5,
        created_at: '',
      })
    vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [],
      has_more: false,
      next_cursor: null,
    })
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 99 as number)

    let firstOk = true
    await act(async () => {
      firstOk = await result.current.submit('s', 'answer', 4, refresh, 'question-1')
    })
    expect(firstOk).toBe(false)
    const savedCommand = result.current.pendingSubmission
    expect(savedCommand).toMatchObject({
      content: 'answer',
      replyToMessageId: 'question-1',
      stateVersion: 4,
      idempotencyKey: expect.any(String),
    })

    let retryOk = false
    await act(async () => {
      retryOk = await result.current.retryPending('s', refresh)
    })
    expect(retryOk).toBe(true)
    expect(refresh).not.toHaveBeenCalled()
    expect(submitSpy).toHaveBeenCalledTimes(2)
    expect(submitSpy.mock.calls[1][1]).toEqual(submitSpy.mock.calls[0][1])
    expect(submitSpy.mock.calls[1][2]).toEqual(submitSpy.mock.calls[0][2])
    expect(result.current.pendingSubmission).toBeNull()
  })

  it('network response loss keeps the original question binding and idempotency key for manual retry', async () => {
    const lostResponse = new ApiRequestError({
      code: 'NETWORK_ERROR',
      userMessage: 'response lost',
      status: 0,
      retryable: true,
    })
    const submitSpy = vi
      .spyOn(api, 'submitMessageWithRetry')
      .mockRejectedValueOnce(lostResponse)
      .mockResolvedValueOnce({
        message_id: 'patient-message',
        session_id: 's',
        role: 'agent',
        stage: 'inquiry',
        content: 'next question',
        current_stage: 'inquiry',
        state_version: 3,
        created_at: '',
      })
    vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [{
        ...makeMsg('3', 'agent'),
        agent_name: 'question_composer',
        structured_delta: { selected_dimension: 'safety.medication_status' },
      }],
      has_more: false,
      next_cursor: null,
    })

    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 3 as number)

    let firstOk = true
    await act(async () => {
      firstOk = await result.current.submit('s', 'no', 1, refresh, 'question-old')
    })
    expect(firstOk).toBe(false)
    expect(result.current.pendingSubmission).toMatchObject({
      content: 'no',
      replyToMessageId: 'question-old',
      stateVersion: 1,
      idempotencyKey: expect.any(String),
    })
    expect(Object.isFrozen(result.current.pendingSubmission)).toBe(true)
    const uncertainPending = result.current.pendingSubmission

    let replacementOk = true
    await act(async () => {
      replacementOk = await result.current.submit('s', 'different answer', 3, refresh, 'question-new')
    })
    expect(replacementOk).toBe(false)
    expect(submitSpy).toHaveBeenCalledTimes(1)
    expect(result.current.pendingSubmission).toBe(uncertainPending)

    // SSE/history refresh has already exposed a newer question before the
    // doctor presses retry.
    await act(async () => {
      await result.current.loadMessages('s')
    })
    expect(result.current.messages.at(-1)?.id).toBe('3')

    let retryOk = false
    await act(async () => {
      retryOk = await result.current.retryPending('s', refresh)
    })
    expect(retryOk).toBe(true)

    const [firstBody, retryBody] = submitSpy.mock.calls.map((call) => call[1])
    expect(firstBody.reply_to_message_id).toBe('question-old')
    expect(retryBody.reply_to_message_id).toBe('question-old')
    expect(retryBody.content).toBe('no')
    expect(submitSpy.mock.calls[0][2]?.stateVersion).toBe(1)
    expect(submitSpy.mock.calls[1][2]?.stateVersion).toBe(1)
    expect(submitSpy.mock.calls[1][2]?.idempotencyKey).toBe(
      submitSpy.mock.calls[0][2]?.idempotencyKey,
    )
    expect(result.current.pendingSubmission).toBeNull()
  })

  it('不可重试错误设置 submitError', async () => {
    const err = new ApiRequestError({
      code: 'INVALID_STAGE_TRANSITION',
      userMessage: '当前阶段不可提交',
      status: 409,
      retryable: false,
    })
    vi.spyOn(api, 'submitMessageWithRetry').mockRejectedValue(err)
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => undefined)
    let ok = true
    await act(async () => {
      ok = await result.current.submit('s', '头痛', 1, refresh)
    })
    expect(ok).toBe(false)
    await waitFor(() => expect(result.current.submitError?.code).toBe('INVALID_STAGE_TRANSITION'))
    expect(result.current.pendingSubmission).toBeNull()
    expect(result.current.lastFailedContent).toBe('\u5934\u75db')
  })
})

function makeAccepted(overrides: Partial<import('@/types/api').AsyncCommandAccepted> = {}) {
  return {
    command_id: 'cmd-1',
    operation: 'intake.message' as const,
    status: 'queued' as const,
    replayed: false,
    attempt_count: 0,
    links: { self: '/self', session: '/s', stream: '/stream' },
    ...overrides,
  }
}

describe('useMessages R7 \u5df2\u63a5\u53d7\u547d\u4ee4', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('202 accepted\uff1a\u767b\u8bb0\u5bf9\u8d26\u3001\u4e0d\u505a\u4e50\u89c2\u5237\u65b0\u3001\u4fdd\u7559 pending', async () => {
    const accepted = makeAccepted()
    const submitSpy = vi.spyOn(api, 'submitMessageWithRetry').mockResolvedValue(accepted)
    const listSpy = vi.spyOn(api, 'listMessages')
    const onCommandAccepted = vi.fn()
    const { result } = renderHook(() => useMessages({ onCommandAccepted }))
    const refresh = vi.fn(async () => 1 as number)

    let ok = false
    await act(async () => {
      ok = await result.current.submit('s', '\u5934\u75db', 1, refresh)
    })

    expect(ok).toBe(true)
    // \u7edd\u4e0d\u628a 202 \u5f53\u4f5c\u5df2\u5b8c\u6210\u4e1a\u52a1\uff1a\u4e0d\u505a\u4e50\u89c2\u5237\u65b0\u3001\u4e0d\u6e05\u7a7a pending\u3001\u4e0d\u62c9\u5386\u53f2\u3002
    expect(listSpy).not.toHaveBeenCalled()
    expect(refresh).not.toHaveBeenCalled()
    expect(result.current.pendingSubmission).toMatchObject({
      content: '\u5934\u75db',
      idempotencyKey: expect.any(String),
    })
    // \u767b\u8bb0\u5bf9\u8d26\uff0c\u5e76\u628a\u5e42\u7b49\u952e\u4e00\u5e76\u4ea4\u7ed9\u4e0a\u5c42\uff08\u5230\u8fbe\u7ec8\u6001\u524d\u4fdd\u7559\uff09\u3002
    expect(onCommandAccepted).toHaveBeenCalledTimes(1)
    const [acceptedArg, idemKey] = onCommandAccepted.mock.calls[0]
    expect(acceptedArg).toMatchObject({ command_id: 'cmd-1', operation: 'intake.message' })
    expect(idemKey).toBeTruthy()
    expect(submitSpy).toHaveBeenCalledTimes(1)
  })

  it('accepted \u5f85\u51b3\u671f\u95f4\u518d\u6b21 submit \u4e0d\u628a\u5df2\u63a5\u53d7\u547d\u4ee4\u5f53\u4f5c\u65b0\u903b\u8f91\u547d\u4ee4\u91cd\u53d1', async () => {
    const accepted = makeAccepted()
    const submitSpy = vi.spyOn(api, 'submitMessageWithRetry').mockResolvedValue(accepted)
    const onCommandAccepted = vi.fn()
    const { result } = renderHook(() => useMessages({ onCommandAccepted }))
    const refresh = vi.fn(async () => 1 as number)

    await act(async () => {
      await result.current.submit('s', 'first', 1, refresh)
    })
    expect(result.current.pendingSubmission).not.toBeNull()

    let secondOk = true
    await act(async () => {
      secondOk = await result.current.submit('s', 'second', 1, refresh)
    })
    // pending \u672a\u51b3 \u2192 \u62d2\u7edd\u518d\u6b21\u63d0\u4ea4\uff0c\u4e0d\u53d1\u51fa\u65b0\u547d\u4ee4\u3002
    expect(secondOk).toBe(false)
    expect(submitSpy).toHaveBeenCalledTimes(1)
  })

  it('settleMessage(commandId, true) \u6210\u529f\u7ec8\u6001\u540e\u6e05\u9664 pending\uff0c\u4e0d\u8bbe\u9519\u8bef', async () => {
    vi.spyOn(api, 'submitMessageWithRetry').mockResolvedValue(makeAccepted())
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 1 as number)

    await act(async () => {
      await result.current.submit('s', '\u5934\u75db', 1, refresh)
    })
    expect(result.current.pendingSubmission).not.toBeNull()

    act(() => {
      result.current.settleMessage('cmd-1', true)
    })
    expect(result.current.pendingSubmission).toBeNull()
    expect(result.current.submitError).toBeNull()
    // \u6210\u529f\u7ec8\u6001\u540e\u4e0a\u5c42\u4ee5\u6743\u5a01\u8bfb\u6a21\u578b\u5237\u65b0\uff0c\u6b64\u5904\u4e0d\u7559\u9519\u8bef\u5185\u5bb9\u3002
    expect(result.current.lastFailedContent).toBeNull()
  })

  it('settleMessage(commandId, false, errorCode) \u5931\u8d25\u7ec8\u6001\u8bbe\u7f6e\u6709\u754c\u9519\u8bef\u5e76\u6e05\u9664 pending', async () => {
    vi.spyOn(api, 'submitMessageWithRetry').mockResolvedValue(makeAccepted())
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 1 as number)

    await act(async () => {
      await result.current.submit('s', '\u5934\u75db', 1, refresh)
    })

    act(() => {
      result.current.settleMessage('cmd-1', false, 'AGENT_TRIGGER_FAILED')
    })
    expect(result.current.pendingSubmission).toBeNull()
    expect(result.current.submitError).toMatchObject({
      code: 'AGENT_TRIGGER_FAILED',
      retryable: false,
    })
    expect(result.current.lastFailedContent).toBe('\u5934\u75db')
  })

  it('settleMessage \u7528\u4e0d\u5339\u914d\u7684 commandId \u65f6\u88ab\u5ffd\u7565', async () => {
    vi.spyOn(api, 'submitMessageWithRetry').mockResolvedValue(makeAccepted())
    const { result } = renderHook(() => useMessages())
    const refresh = vi.fn(async () => 1 as number)

    await act(async () => {
      await result.current.submit('s', '\u5934\u75db', 1, refresh)
    })

    act(() => {
      result.current.settleMessage('other-cmd', true)
    })
    // \u4e0d\u5339\u914d\u7684\u7ec8\u6001\u56de\u8c03\u4e0d\u5e72\u6270\u5f53\u524d pending\u3002
    expect(result.current.pendingSubmission).not.toBeNull()
  })

  it('rebase \u540e\u7b2c\u4e8c\u6b21\u63d0\u4ea4\u8fd4\u56de 202\uff1a\u4e0e\u9996\u6b21 202 \u4e00\u81f4\uff0c\u767b\u8bb0\u5bf9\u8d26\u5e76\u4fdd\u7559 rebased pending', async () => {
    const versionErr = new ApiRequestError({
      code: 'INVALID_STATE_VERSION',
      userMessage: 'stale version',
      status: 409,
      retryable: true,
    })
    const accepted = makeAccepted({ command_id: 'cmd-rebased' })
    const submitSpy = vi
      .spyOn(api, 'submitMessageWithRetry')
      .mockRejectedValueOnce(versionErr)
      .mockResolvedValueOnce(accepted)
    const listSpy = vi.spyOn(api, 'listMessages').mockResolvedValue({
      items: [],
      has_more: false,
      next_cursor: null,
    })
    const onCommandAccepted = vi.fn()
    const { result } = renderHook(() => useMessages({ onCommandAccepted }))
    const refresh = vi.fn(async () => 2 as number)

    let ok = false
    await act(async () => {
      ok = await result.current.submit('s', '\u5934\u75db', 1, refresh)
    })

    expect(ok).toBe(true)
    // \u7edd\u4e0d\u628a 202 \u5f53\u540c\u6b65\u6210\u529f\uff1a\u4e0d\u6e05\u9664 pending\u3001\u4e0d\u4e50\u89c2\u5237\u65b0\u3001
    // \u4e0d\u62c9\u5386\u53f2\uff08\u4ec5 rebase \u65f6\u7684\u7248\u672c\u68c0\u67e5\u90a3\u6b21\uff09\u3001\u4e0d\u8c03\u7528 refresh\u3002
    expect(listSpy).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledTimes(1)
    // \u767b\u8bb0\u5bf9\u8d26\uff0c\u5e26 rebased \u5e42\u7b49\u952e\uff08\u4e0e\u9996\u6b21\u4e0d\u540c\uff09\u3002
    expect(onCommandAccepted).toHaveBeenCalledTimes(1)
    const [acceptedArg, idemKey] = onCommandAccepted.mock.calls[0]
    expect(acceptedArg).toMatchObject({ command_id: 'cmd-rebased', operation: 'intake.message' })
    const firstKey = submitSpy.mock.calls[0][2]?.idempotencyKey
    const secondKey = submitSpy.mock.calls[1][2]?.idempotencyKey
    expect(secondKey).toBeTruthy()
    expect(secondKey).not.toBe(firstKey)
    expect(idemKey).toBe(secondKey)
    // \u672a\u51b3 pending \u4fdd\u7559\uff1a\u5185\u5bb9\u4e0e rebased \u5e42\u7b49\u952e\u3001\u65b0 stateVersion \u5747\u4fdd\u6301\u3002
    expect(result.current.pendingSubmission).toMatchObject({
      content: '\u5934\u75db',
      stateVersion: 2,
      idempotencyKey: secondKey,
    })
    // \u7ec8\u6001\u7ed3\u7b97\uff1a\u4ee5 rebased command id \u6e05\u9664 pending\uff08\u4e0d\u4ee5\u65e7\u7684\u5e42\u7b49\u952e\u91cd\u53d1\uff09\u3002
    act(() => {
      result.current.settleMessage('cmd-rebased', true)
    })
    expect(result.current.pendingSubmission).toBeNull()
    expect(result.current.submitError).toBeNull()
  })
})
