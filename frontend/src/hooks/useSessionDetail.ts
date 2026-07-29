/**
 * 悬壶 WebUI —— 当前会话详情 hook（P8-2）
 *
 * 职责：选中会话后用 getSession() 拉取权威状态（含 state_version），
 * 供写操作回传 X-State-Version。提供 refreshDetail 供提交后刷新。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError } from '@/api/errors'
import { getSession } from '@/api/index'
import type { SessionDetail } from '@/types/api'

export interface UseSessionDetailResult {
  sessionId: string | null
  detail: SessionDetail | null
  loading: boolean
  error: ApiRequestError | null
  /** 切换/选中会话。传 null 清空。 */
  selectSession: (id: string | null) => void
  /** 刷新当前会话详情（重新拉取 state_version）。 */
  refreshDetail: () => Promise<SessionDetail | null>
}

export function useSessionDetail(): UseSessionDetailResult {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const mounted = useRef(true)
  const requestedSessionIdRef = useRef<string | null>(null)
  const requestGenerationRef = useRef(0)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      requestGenerationRef.current += 1
    }
  }, [])

  const refreshDetail = useCallback(async (): Promise<SessionDetail | null> => {
    const targetSessionId = requestedSessionIdRef.current
    if (!targetSessionId) return null
    const generation = ++requestGenerationRef.current
    const isCurrentRequest = () => (
      mounted.current
      && requestedSessionIdRef.current === targetSessionId
      && requestGenerationRef.current === generation
    )
    setLoading(true)
    setError(null)
    try {
      const data = await getSession(targetSessionId)
      if (isCurrentRequest() && data.session_id === targetSessionId) {
        setDetail(data)
        return data
      }
      return null
    } catch (err) {
      if (isCurrentRequest()) {
        setError(err instanceof ApiRequestError ? err : null)
        setDetail(null)
      }
      return null
    } finally {
      if (isCurrentRequest()) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!sessionId) {
      setDetail(null)
      setError(null)
      setLoading(false)
      return
    }
    void refreshDetail()
  }, [sessionId, refreshDetail])

  const selectSession = useCallback((id: string | null) => {
    if (requestedSessionIdRef.current === id) return
    requestedSessionIdRef.current = id
    requestGenerationRef.current += 1
    setDetail(null)
    setError(null)
    setLoading(id !== null)
    setSessionId(id)
  }, [])

  return {
    sessionId,
    detail,
    loading,
    error,
    selectSession,
    refreshDetail,
  }
}
