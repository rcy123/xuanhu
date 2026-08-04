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
import { ApiRequestError, TransportErrorCode } from '@/api/errors'
import { ErrorCode } from '@/types/api'
import { listMessages, submitMessageWithRetry } from '@/api/index'
import type { MessageItem } from '@/types/api'
import { generateIdempotencyKey } from '@/utils/id'

/**
 * One logical doctor reply.  Keep this object intact until the server has
 * acknowledged it: changing either the bound question or the idempotency key
 * turns a retry into a different clinical command.
 */
export interface PendingMessageSubmission {
  readonly content: string
  readonly replyToMessageId?: string
  readonly idempotencyKey: string
  readonly stateVersion: number | undefined
}

function currentStructuredQuestionId(messages: MessageItem[]): string | undefined {
  const latest = messages.at(-1)
  if (
    latest?.role === 'agent'
    && latest.agent_name === 'question_composer'
    && typeof latest.structured_delta?.selected_dimension === 'string'
  ) return latest.id
  return undefined
}

function isUncertainSubmissionFailure(error: ApiRequestError): boolean {
  return (
    error.code === TransportErrorCode.NETWORK_ERROR
    || error.code === TransportErrorCode.TIMEOUT
    || error.code === TransportErrorCode.ABORTED
    || error.code === TransportErrorCode.BAD_RESPONSE
    || (error.retryable && error.code === ErrorCode.AGENT_TRIGGER_FAILED)
    || error.code === 'HTTP_COMMAND_RECOVERY_REQUIRED'
  )
}

function ambiguousSubmissionError(cause: unknown): ApiRequestError {
  return new ApiRequestError({
    code: 'AMBIGUOUS_SUBMISSION_RESULT',
    userMessage: '\u672a\u80fd\u786e\u8ba4\u4e0a\u4e00\u6761\u6d88\u606f\u662f\u5426\u5df2\u6210\u529f\uff0c\u8bf7\u5148\u91cd\u8bd5\u8be5\u6d88\u606f\u3002',
    status: 0,
    retryable: true,
    detail: cause instanceof Error ? cause.message : String(cause),
    cause,
  })
}

export interface UseMessagesResult {
  messages: MessageItem[]
  loading: boolean
  error: ApiRequestError | null
  submitting: boolean
  submitError: ApiRequestError | null
  pendingSubmission: PendingMessageSubmission | null
  lastFailedContent: string | null
  loadMessages: (sessionId: string) => Promise<MessageItem[] | null>
  /** 提交问诊消息。stateVersion 为当前会话版本号；onRefreshDetail 用于版本冲突后拉新版本。 */
  submit: (
    sessionId: string,
    content: string,
    stateVersion: number | undefined,
    onRefreshDetail: () => Promise<number | undefined>,
    replyToMessageId?: string,
  ) => Promise<boolean>
  /** Retry the exact pending command; never rebind it to the latest question. */
  retryPending: (
    sessionId: string,
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
  const [pendingSubmission, setPendingSubmission] = useState<PendingMessageSubmission | null>(null)
  const [lastFailedContent, setLastFailedContent] = useState<string | null>(null)
  const pendingSubmissionRef = useRef<PendingMessageSubmission | null>(null)
  const pendingSessionIdRef = useRef<string | null>(null)
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionGenerationRef = useRef(0)
  const loadGenerationRef = useRef(0)
  const hasMessagesRef = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const loadMessages = useCallback(async (sessionId: string) => {
    if (activeSessionIdRef.current !== sessionId) {
      // Cross-session activation is explicit: ChatPanel calls clear() first.
      // This prevents a queued callback from an old SSE connection from
      // switching the hook back after the user selected another session.
      if (activeSessionIdRef.current !== null) return null
      activeSessionIdRef.current = sessionId
      sessionGenerationRef.current += 1
    }
    const sessionGeneration = sessionGenerationRef.current
    const loadGeneration = ++loadGenerationRef.current
    const isCurrentLoad = () => (
      mounted.current
      && activeSessionIdRef.current === sessionId
      && sessionGenerationRef.current === sessionGeneration
      && loadGenerationRef.current === loadGeneration
    )

    setLoading(!hasMessagesRef.current)
    setError(null)
    try {
      const data = await listMessages(sessionId, { limit: 100 })
      if (isCurrentLoad()) {
        // 后端默认可能按时间倒序，前端统一升序展示（旧→新）。
        const items = [...data.items].sort((a, b) => a.created_at.localeCompare(b.created_at))
        hasMessagesRef.current = items.length > 0
        setMessages(items)
        setLoading(false)
        return items
      }
      return null
    } catch (err) {
      if (isCurrentLoad()) {
        setError(err instanceof ApiRequestError ? err : null)
      }
      return null
    } finally {
      if (isCurrentLoad()) setLoading(false)
    }
  }, [])

  const clear = useCallback(() => {
    activeSessionIdRef.current = null
    sessionGenerationRef.current += 1
    loadGenerationRef.current += 1
    hasMessagesRef.current = false
    setMessages([])
    setLoading(false)
    setError(null)
    setSubmitting(false)
    setSubmitError(null)
    pendingSubmissionRef.current = null
    pendingSessionIdRef.current = null
    setPendingSubmission(null)
    setLastFailedContent(null)
  }, [])

  const doSubmit = useCallback(
    async (
      sessionId: string,
      submission: PendingMessageSubmission,
    ): Promise<boolean> => {
      await submitMessageWithRetry(
        sessionId,
        {
          content: submission.content,
          role: 'doctor',
          ...(submission.replyToMessageId
            ? { reply_to_message_id: submission.replyToMessageId }
            : {}),
        },
        {
          stateVersion: submission.stateVersion,
          idempotencyKey: submission.idempotencyKey,
        },
      )
      return true
    },
    [],
  )

  const executeSubmission = useCallback(
    async (
      sessionId: string,
      submission: PendingMessageSubmission,
      onRefreshDetail: () => Promise<number | undefined>,
      sessionGeneration: number,
    ): Promise<boolean> => {
      const isCurrentSession = () => (
        mounted.current
        && activeSessionIdRef.current === sessionId
        && sessionGenerationRef.current === sessionGeneration
      )
      if (!isCurrentSession()) return false

      setSubmitting(true)
      setSubmitError(null)
      try {
        await doSubmit(sessionId, submission)
        if (!isCurrentSession()) return false
        // 成功后刷新消息历史
        await loadMessages(sessionId)
        if (!isCurrentSession()) return false
        if (
          pendingSubmissionRef.current === submission
          && pendingSessionIdRef.current === sessionId
        ) {
          pendingSubmissionRef.current = null
          pendingSessionIdRef.current = null
          setPendingSubmission(null)
          setLastFailedContent(null)
        }
        return true
      } catch (err) {
        if (!isCurrentSession()) return false
        if (!(err instanceof ApiRequestError)) {
          setLastFailedContent(submission.content)
          setSubmitError(ambiguousSubmissionError(err))
          return false
        }
        // A stale state version is a completed failure, not an uncertain
        // transport outcome. Rebase only while the bound question is current.
        if (err.code === ErrorCode.INVALID_STATE_VERSION) {
          const newVersion = await onRefreshDetail()
          if (!isCurrentSession()) return false
          if (newVersion === undefined) {
            if (
              pendingSubmissionRef.current === submission
              && pendingSessionIdRef.current === sessionId
            ) {
              pendingSubmissionRef.current = null
              pendingSessionIdRef.current = null
              setPendingSubmission(null)
            }
            setLastFailedContent(submission.content)
            setSubmitError(new ApiRequestError({
              code: 'REBASE_STATE_VERSION_MISSING',
              userMessage: '\u672a\u80fd\u83b7\u53d6\u6700\u65b0\u4f1a\u8bdd\u7248\u672c\uff0c\u672a\u81ea\u52a8\u91cd\u8bd5\uff1b\u8bf7\u5237\u65b0\u4f1a\u8bdd\u540e\u91cd\u65b0\u4f5c\u7b54\u3002',
              status: 409,
              retryable: false,
            }))
            return false
          }
          const latestMessages = await loadMessages(sessionId)
          if (!isCurrentSession()) return false

          const latestQuestionId = latestMessages
            ? currentStructuredQuestionId(latestMessages)
            : undefined
          if (!latestMessages || latestQuestionId !== submission.replyToMessageId) {
            if (
              pendingSubmissionRef.current === submission
              && pendingSessionIdRef.current === sessionId
            ) {
              pendingSubmissionRef.current = null
              pendingSessionIdRef.current = null
              setPendingSubmission(null)
              setLastFailedContent(submission.content)
            }
            setSubmitError(new ApiRequestError({
              code: latestMessages ? 'REPLY_CONTEXT_CHANGED' : 'REPLY_CONTEXT_UNAVAILABLE',
              userMessage: latestMessages
                ? '当前问诊问题已更新，未自动重试；请根据最新问题重新作答。'
                : '无法确认当前问诊问题，未自动重试；请刷新会话后重新作答。',
              status: 409,
              retryable: false,
              detail: `original_question=${submission.replyToMessageId ?? 'none'}; latest_question=${latestQuestionId ?? 'none'}`,
            }))
            return false
          }

          const rebasedSubmission: PendingMessageSubmission = Object.freeze({
            content: submission.content,
            ...(submission.replyToMessageId
              ? { replyToMessageId: submission.replyToMessageId }
              : {}),
            stateVersion: newVersion,
            idempotencyKey: generateIdempotencyKey(),
          })
          if (
            pendingSubmissionRef.current !== submission
            || pendingSessionIdRef.current !== sessionId
          ) return false
          pendingSubmissionRef.current = rebasedSubmission
          setPendingSubmission(rebasedSubmission)
          try {
            await doSubmit(sessionId, rebasedSubmission)
            if (!isCurrentSession()) return false
            await loadMessages(sessionId)
            if (!isCurrentSession()) return false
            if (
              pendingSubmissionRef.current === rebasedSubmission
              && pendingSessionIdRef.current === sessionId
            ) {
              pendingSubmissionRef.current = null
              pendingSessionIdRef.current = null
              setPendingSubmission(null)
              setLastFailedContent(null)
            }
            return true
          } catch (err2) {
            if (isCurrentSession()) {
              setLastFailedContent(rebasedSubmission.content)
              if (
                err2 instanceof ApiRequestError
                && !isUncertainSubmissionFailure(err2)
                && pendingSubmissionRef.current === rebasedSubmission
                && pendingSessionIdRef.current === sessionId
              ) {
                pendingSubmissionRef.current = null
                pendingSessionIdRef.current = null
                setPendingSubmission(null)
              }
              setSubmitError(
                err2 instanceof ApiRequestError ? err2 : ambiguousSubmissionError(err2),
              )
            }
            return false
          }
        }
        setLastFailedContent(submission.content)
        if (
          !isUncertainSubmissionFailure(err)
          && pendingSubmissionRef.current === submission
          && pendingSessionIdRef.current === sessionId
        ) {
          pendingSubmissionRef.current = null
          pendingSessionIdRef.current = null
          setPendingSubmission(null)
        }
        setSubmitError(err)
        return false
      } finally {
        if (isCurrentSession()) setSubmitting(false)
      }
    },
    [doSubmit, loadMessages],
  )

  const submit = useCallback(
    async (
      sessionId: string,
      content: string,
      stateVersion: number | undefined,
      onRefreshDetail: () => Promise<number | undefined>,
      replyToMessageId?: string,
    ): Promise<boolean> => {
      if (activeSessionIdRef.current === null) {
        activeSessionIdRef.current = sessionId
        sessionGenerationRef.current += 1
      }
      if (activeSessionIdRef.current !== sessionId) return false

      // Do not silently replace an uncertain command with a new one.  The UI
      // must resolve it through retry (or an explicit session clear) first.
      if (pendingSubmissionRef.current) return false
      setLastFailedContent(null)

      const submission: PendingMessageSubmission = Object.freeze({
        content,
        ...(replyToMessageId ? { replyToMessageId } : {}),
        stateVersion,
        idempotencyKey: generateIdempotencyKey(),
      })
      pendingSubmissionRef.current = submission
      pendingSessionIdRef.current = sessionId
      if (mounted.current) setPendingSubmission(submission)
      return executeSubmission(
        sessionId,
        submission,
        onRefreshDetail,
        sessionGenerationRef.current,
      )
    },
    [executeSubmission],
  )

  const retryPending = useCallback(
    async (
      sessionId: string,
      onRefreshDetail: () => Promise<number | undefined>,
    ): Promise<boolean> => {
      const submission = pendingSubmissionRef.current
      if (
        !submission
        || pendingSessionIdRef.current !== sessionId
        || activeSessionIdRef.current !== sessionId
      ) return false
      return executeSubmission(
        sessionId,
        submission,
        onRefreshDetail,
        sessionGenerationRef.current,
      )
    },
    [executeSubmission],
  )

  return {
    messages,
    loading,
    error,
    submitting,
    submitError,
    pendingSubmission,
    lastFailedContent,
    loadMessages,
    submit,
    retryPending,
    clear,
  }
}
