import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/index'
import type { SessionDetail } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'
import { useSessionDetail } from './useSessionDetail'

function detail(sessionId: string): SessionDetail {
  return {
    session_id: sessionId,
    status: 'active',
    current_stage: 'inquiry',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 1,
    agent_runtime: 'langgraph',
    read_model: emptySessionReadModel('langgraph', 1),
    patient_info: {},
    chief_complaint: sessionId,
    created_at: '2026-07-29T00:00:00Z',
    updated_at: '2026-07-29T00:00:00Z',
  }
}

describe('useSessionDetail session boundary', () => {
  afterEach(() => vi.restoreAllMocks())

  it('clears immediately and ignores a slow response from the previous session', async () => {
    let resolveS1!: (value: SessionDetail) => void
    let resolveS2!: (value: SessionDetail) => void
    const s1 = new Promise<SessionDetail>((resolve) => { resolveS1 = resolve })
    const s2 = new Promise<SessionDetail>((resolve) => { resolveS2 = resolve })
    vi.spyOn(api, 'getSession').mockImplementation((id) => {
      if (id === 'seed') return Promise.resolve(detail('seed'))
      return id === 's1' ? s1 : s2
    })
    const { result } = renderHook(() => useSessionDetail())

    act(() => result.current.selectSession('seed'))
    await waitFor(() => expect(result.current.detail?.session_id).toBe('seed'))

    act(() => result.current.selectSession('s1'))
    expect(result.current.detail).toBeNull()
    act(() => result.current.selectSession('s2'))
    expect(result.current.detail).toBeNull()
    expect(result.current.loading).toBe(true)

    await act(async () => {
      resolveS2(detail('s2'))
      await s2
    })
    await waitFor(() => expect(result.current.detail?.session_id).toBe('s2'))

    await act(async () => {
      resolveS1(detail('s1'))
      await s1
    })
    expect(result.current.detail?.session_id).toBe('s2')
  })
})
