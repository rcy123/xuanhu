/**
 * 悬壶 WebUI —— 会话列表 hook（P8-2）
 *
 * 职责：加载会话列表、创建会话（含幂等键）、刷新、loading/error 状态。
 * 创建会话响应不含 state_version，调用方需在选中后用 getSession() 拉权威状态。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError } from '@/api/errors'
import { createSession, listSessions } from '@/api/index'
import type { SessionCreateRequest, SessionListItem } from '@/types/api'
import { generateIdempotencyKey } from '@/utils/id'

export interface UseSessionsResult {
  sessions: SessionListItem[]
  loading: boolean
  error: ApiRequestError | null
  refresh: () => Promise<void>
  createSession: (body: SessionCreateRequest) => Promise<string>
  creating: boolean
  createError: ApiRequestError | null
}

export function useSessions(): UseSessionsResult {
  const [sessions, setSessions] = useState<SessionListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<ApiRequestError | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listSessions({ sort: 'updated_at:desc', page_size: 50 })
      if (mounted.current) {
        setSessions(data.items)
      }
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof ApiRequestError ? err : null)
      }
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const create = useCallback(async (body: SessionCreateRequest): Promise<string> => {
    setCreating(true)
    setCreateError(null)
    try {
      const data = await createSession(body, {
        idempotencyKey: generateIdempotencyKey(),
      })
      // 创建后刷新列表以包含新会话（不阻塞返回 id）
      void refresh()
      return data.session_id
    } catch (err) {
      if (mounted.current) {
        setCreateError(err instanceof ApiRequestError ? err : null)
      }
      throw err
    } finally {
      if (mounted.current) setCreating(false)
    }
  }, [refresh])

  return {
    sessions,
    loading,
    error,
    refresh,
    createSession: create,
    creating,
    createError,
  }
}
