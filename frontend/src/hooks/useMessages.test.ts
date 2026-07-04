import { describe, expect, it, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useMessages } from './useMessages'
import * as api from '@/api/index'
import { ApiRequestError } from '@/api/errors'
import type { CursorData, MessageItem } from '@/types/api'

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
      expect.objectContaining({ stateVersion: 1 }),
    )
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
  })
})
