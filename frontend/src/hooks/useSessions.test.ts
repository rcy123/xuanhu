import { describe, expect, it, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useSessions } from './useSessions'
import * as api from '@/api/index'
import { ApiRequestError } from '@/api/errors'
import type { PageData, SessionListItem } from '@/types/api'

vi.mock('@/utils/id', () => ({ generateIdempotencyKey: () => 'idem-test' }))

function makeItem(id: string): SessionListItem {
  return {
    session_id: id,
    patient_info: {},
    current_stage: 'inquiry',
    status: 'active',
    agent_runtime: 'legacy',
    pending_review: false,
    created_at: '',
    updated_at: '',
  }
}

describe('useSessions', () => {
  it('初始化加载会话列表', async () => {
    const data: PageData<SessionListItem> = {
      items: [makeItem('s1'), makeItem('s2')],
      total: 2,
      page: 1,
      page_size: 50,
    }
    const spy = vi.spyOn(api, 'listSessions').mockResolvedValue(data)
    const { result } = renderHook(() => useSessions())
    await waitFor(() => expect(result.current.sessions.length).toBe(2))
    expect(spy).toHaveBeenCalled()
  })

  it('createSession 带幂等键并返回新 session_id', async () => {
    vi.spyOn(api, 'listSessions').mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 })
    const createSpy = vi
      .spyOn(api, 'createSession')
      .mockResolvedValue({
        session_id: 's-new',
        current_stage: 'inquiry',
        status: 'active',
        agent_runtime: 'langgraph',
        patient_info: {},
        created_at: '',
      })
    const { result } = renderHook(() => useSessions())
    await waitFor(() => expect(result.current.loading).toBe(false))
    let id: string | undefined
    await act(async () => {
      id = await result.current.createSession({ chief_complaint: '头痛' })
    })
    expect(id).toBe('s-new')
    expect(createSpy).toHaveBeenCalledWith(
      { chief_complaint: '头痛' },
      expect.objectContaining({ idempotencyKey: 'idem-test' }),
    )
  })

  it('加载失败设置 error', async () => {
    const err = new ApiRequestError({
      code: 'INTERNAL_ERROR',
      userMessage: '服务器错误',
      status: 500,
      retryable: true,
    })
    vi.spyOn(api, 'listSessions').mockRejectedValue(err)
    const { result } = renderHook(() => useSessions())
    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error?.code).toBe('INTERNAL_ERROR')
  })
})
