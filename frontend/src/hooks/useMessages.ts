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
import { ErrorCode, isAsyncCommandAccepted } from '@/types/api'
import { listMessages, submitMessageWithRetry } from '@/api/index'
import type { AsyncCommandAccepted, MessageItem } from '@/types/api'
import { generateIdempotencyKey } from '@/utils/id'

/**
 * R7 选项：收到已接受（HTTP 202）消息命令时回调。上层据此登记对账并保留
 * 幂等键——已接受的命令绝不能被当作新逻辑命令重发。
 */
export interface UseMessagesOptions {
  onCommandAccepted?: (accepted: AsyncCommandAccepted, idempotencyKey: string) => void
}

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

/**
 * 内部提交执行结果判别（executeSubmission 使用）：
 * - 'accepted'：HTTP 202，命令已登记对账（onCommandAccepted 已触发）。不是完成，
 *   内部保留 pending 与幂等键，绝不做乐观刷新/关闭 UI 为完成态；终态由
 *   settleMessage 处理。
 * - 'completed'：同步成功完成。
 * - 'failed'：同步失败（submitError 已设置；上层可展示错误）。
 *
 * 注意：公共 submit / retryPending 对外保持历史 boolean 契约——'accepted' 与
 * 'completed' 都映射为 true（202 表示 HTTP 提交已被接受），'failed'/中止为 false。
 * 上层据 pendingSubmission 是否保留来区分「已接受待终态」与「已同步完成」。
 */
export type SubmissionOutcome = 'accepted' | 'completed' | 'failed'

/** 把内部执行结果映射回历史 boolean 契约（true=提交被接受或同步完成）。 */
function outcomeToBoolean(outcome: SubmissionOutcome | false): boolean {
  return outcome === 'accepted' || outcome === 'completed'
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
  /**
   * 提交问诊消息。stateVersion 为当前会话版本号；onRefreshDetail 用于版本冲突后拉新版本。
   * 返回历史 boolean 契约：true 表示 HTTP 提交已被接受（同步完成或已返回 202 登记对账），
   * false 表示同步失败/中止。202 场景下 pendingSubmission 仍保留且已通过 onCommandAccepted
   * 登记对账——上层不得据此视为业务完成，终态由 settleMessage/对账回调推进。
   */
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
  /**
   * R7：对账到终态后由上层调用，清除该已接受消息命令的 pending 状态。
   * ok=true 视为成功（权威读模型已由上层刷新）；ok=false 展示有界错误。
   */
  settleMessage: (commandId: string, ok: boolean, errorCode?: string) => void
  clear: () => void
}

export function useMessages(options?: UseMessagesOptions): UseMessagesResult {
  const [messages, setMessages] = useState<MessageItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<ApiRequestError | null>(null)
  const [pendingSubmission, setPendingSubmission] = useState<PendingMessageSubmission | null>(null)
  const [lastFailedContent, setLastFailedContent] = useState<string | null>(null)
  const pendingSubmissionRef = useRef<PendingMessageSubmission | null>(null)
  const pendingSessionIdRef = useRef<string | null>(null)
  /** R7：记录当前 pending 是否对应一个已接受（202）命令，终态对账时据此清空。 */
  const pendingAcceptedCommandIdRef = useRef<string | null>(null)
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionGenerationRef = useRef(0)
  const loadGenerationRef = useRef(0)
  const hasMessagesRef = useRef(false)
  const mounted = useRef(true)
  const optionsRef = useRef<UseMessagesOptions | undefined>(options)
  optionsRef.current = options

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
    pendingAcceptedCommandIdRef.current = null
    setPendingSubmission(null)
    setLastFailedContent(null)
  }, [])

  const doSubmit = useCallback(
    async (
      sessionId: string,
      submission: PendingMessageSubmission,
    ): Promise<AsyncCommandAccepted | boolean> => {
      const result = await submitMessageWithRetry(
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
      // R7: 202 envelope 表示「已接受为持久命令」，不是已完成业务结果。
      if (isAsyncCommandAccepted(result)) return result
      // 同步成功：返回业务结果（此处仅关心非 accepted，等价 true）。
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
    ): Promise<SubmissionOutcome | false> => {
      const isCurrentSession = () => (
        mounted.current
        && activeSessionIdRef.current === sessionId
        && sessionGenerationRef.current === sessionGeneration
      )
      if (!isCurrentSession()) return false

      setSubmitting(true)
      setSubmitError(null)
      try {
        const result = await doSubmit(sessionId, submission)
        if (!isCurrentSession()) return false
        if (isAsyncCommandAccepted(result)) {
          // R7: 202 仅表示已接受，非完成。保留 pendingSubmission 阻止把已接受
          // 的命令当新逻辑命令重发；登记对账，终态由 settleMessage 清空并由
          // 上层以权威读模型刷新。绝不在此时做乐观刷新。
          pendingAcceptedCommandIdRef.current = result.command_id
          optionsRef.current?.onCommandAccepted?.(result, submission.idempotencyKey)
          return 'accepted'
        }
        // 同步成功：刷新消息历史
        await loadMessages(sessionId)
        if (!isCurrentSession()) return false
        if (
          pendingSubmissionRef.current === submission
          && pendingSessionIdRef.current === sessionId
        ) {
          pendingSubmissionRef.current = null
          pendingSessionIdRef.current = null
          pendingAcceptedCommandIdRef.current = null
          setPendingSubmission(null)
          setLastFailedContent(null)
        }
        return 'completed'
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
            const rebasedResult = await doSubmit(sessionId, rebasedSubmission)
            if (!isCurrentSession()) return false
            if (isAsyncCommandAccepted(rebasedResult)) {
              // R7: rebased 提交同样返回 202 → 与首次 202 完全一致地登记对账，
              // 保留 rebased 的幂等键与 pending，不做乐观刷新/清除；终态由
              // settleMessage（上层对账回调）处理。绝不能把 202 当同步成功。
              pendingAcceptedCommandIdRef.current = rebasedResult.command_id
              optionsRef.current?.onCommandAccepted?.(rebasedResult, rebasedSubmission.idempotencyKey)
              return 'accepted'
            }
            // 同步成功：刷新消息历史
            await loadMessages(sessionId)
            if (!isCurrentSession()) return false
            if (
              pendingSubmissionRef.current === rebasedSubmission
              && pendingSessionIdRef.current === sessionId
            ) {
              pendingSubmissionRef.current = null
              pendingSessionIdRef.current = null
              pendingAcceptedCommandIdRef.current = null
              setPendingSubmission(null)
              setLastFailedContent(null)
            }
            return 'completed'
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
      ).then(outcomeToBoolean)
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
      ).then(outcomeToBoolean)
    },
    [executeSubmission],
  )

  // R7: 已接受消息命令对账到终态后，由上层调用以清除 pending 并展示有界结果。
  const settleMessage = useCallback((commandId: string, ok: boolean, errorCode?: string) => {
    if (pendingAcceptedCommandIdRef.current !== commandId) return
    if (!ok) {
      setLastFailedContent(pendingSubmissionRef.current?.content ?? null)
      setSubmitError(new ApiRequestError({
        code: errorCode ?? 'COMMAND_FAILED',
        userMessage: errorCode ? `命令处理失败（${errorCode}）` : '命令处理失败',
        status: 409,
        retryable: false,
      }))
    }
    pendingSubmissionRef.current = null
    pendingSessionIdRef.current = null
    pendingAcceptedCommandIdRef.current = null
    setPendingSubmission(null)
  }, [])

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
    settleMessage,
    clear,
  }
}
