/**
 * 悬壶 WebUI —— 消息历史 + 提交 hook（P8-2）
 *
 * 职责：
 * - loadMessages(id)：游标分页拉取消息历史（按 created_at 升序展示）。
 * - submit(content)：携带 ctx.stateVersion 提交；版本冲突刷新重提一次；
 *   SESSION_BUSY 等可重试错误由 submitMessageWithRetry 自动重试，耗尽后 setError。
 * - 提交成功后刷新消息历史 + 调用 onSubmitted 回调（供上层刷新 session detail）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError } from '@/api/errors'
import { ErrorCode } from '@/types/api'
import { listMessages, submitMessageWithRetry } from '@/api/index'
import type { MessageItem } from '@/types/api'

export interface UseMessagesResult {
  messages: MessageItem[]
  loading: boolean
  error: ApiRequestError | null
  submitting: boolean
  submitError: ApiRequestError | null
  loadMessages: (sessionId: string) => Promise<void>
  /** 提交问诊消息。stateVersion 为当前会话版本号；onRefreshDetail 用于版本冲突后拉新版本。 */
  submit: (
    sessionId: string,
    content: string,
    stateVersion: number | undefined,
    onRefreshDetail: () => Promise<number | undefined>,
  ) => Promise<boolean>
  clear: () => void
}

export function useMessages(): UseMessagesResult {
  const [messages, setMessages] = useState<MessageItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<ApiRequestError | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const loadMessages = useCallback(async (sessionId: string) => {
    setLoading(true)
    setError(null)
    try {
      const data = await listMessages(sessionId, { limit: 100 })
      if (mounted.current) {
        // 后端默认可能按时间倒序，前端统一升序展示（旧→新）。
        const items = [...data.items].sort((a, b) => a.created_at.localeCompare(b.created_at))
        setMessages(items)
      }
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof ApiRequestError ? err : null)
      }
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  const clear = useCallback(() => {
    setMessages([])
    setError(null)
    setSubmitError(null)
  }, [])

  const doSubmit = useCallback(
    async (
      sessionId: string,
      content: string,
      stateVersion: number | undefined,
    ): Promise<boolean> => {
      await submitMessageWithRetry(
        sessionId,
        { content, role: 'doctor' },
        { stateVersion },
      )
      return true
    },
    [],
  )

  const submit = useCallback(
    async (
      sessionId: string,
      content: string,
      stateVersion: number | undefined,
      onRefreshDetail: () => Promise<number | undefined>,
    ): Promise<boolean> => {
      setSubmitting(true)
      setSubmitError(null)
      try {
        await doSubmit(sessionId, content, stateVersion)
        // 成功后刷新消息历史
        await loadMessages(sessionId)
        return true
      } catch (err) {
        if (!(err instanceof ApiRequestError)) {
          if (mounted.current) setSubmitError(null)
          return false
        }
        // 版本冲突：拉新版本后重提一次
        if (err.code === ErrorCode.INVALID_STATE_VERSION) {
          const newVersion = await onRefreshDetail()
          try {
            await doSubmit(sessionId, content, newVersion)
            await loadMessages(sessionId)
            return true
          } catch (err2) {
            if (mounted.current) {
              setSubmitError(err2 instanceof ApiRequestError ? err2 : null)
            }
            return false
          }
        }
        if (mounted.current) {
          setSubmitError(err)
        }
        return false
      } finally {
        if (mounted.current) setSubmitting(false)
      }
    },
    [doSubmit, loadMessages],
  )

  return {
    messages,
    loading,
    error,
    submitting,
    submitError,
    loadMessages,
    submit,
    clear,
  }
}
