/**
 * 悬壶 WebUI —— SSE 会话事件流 hook（P8-3）
 *
 * 职责：
 * - 建立/关闭 SSE 连接（connectSessionStream）。
 * - 追踪连接健康（idle → connecting → connected → polling → disconnected）。
 * - 连续 3 次 onError 后降级为轮询（3500ms）。
 * - 把 SSE 事件转译为语义回调（onStageChanged / onMessageCreated / onResync / …）。
 * - 追踪 per-agent 运行状态（agent.started / finished / failed）。
 *
 * 注意：本 hook 不直接写入 SessionDetail 或 MessageItem；事件通过回调通知上层，
 * 上层调用 refreshDetail() / loadMessages() 以 GET 为权威来源。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import * as sse from '@/api/sse'
import type { SseConnection } from '@/api/sse'
import type { Formula, SafetyIssue, SessionEvent } from '@/types/api'

export type StreamConnectionState = 'idle' | 'connecting' | 'connected' | 'polling' | 'disconnected'

export interface AgentRunState {
  status: 'running' | 'done' | 'failed'
  agentRunId?: string
  error?: string
}

export interface UseSessionStreamOptions {
  sessionId: string | null
  /** 当前已知 state_version（供 reconnect 参考，当前 MVP 不直接使用）。 */
  stateVersion: number | undefined
  onStageChanged?: (toStage: string, stateVersion: number) => void
  onMessageCreated?: (messageId: string) => void
  onResync?: (reason: string) => void
  onReviewRequired?: (modifiedFormula: Formula, safetyReview: Record<string, unknown>) => void
  onSafetyBlocked?: (issues: SafetyIssue[], rollbackTarget?: string | null) => void
  onSessionDone?: (recordId?: string) => void
  onSessionBlocked?: (reason: string) => void
  onSessionTerminated?: () => void
  /** SSE 不可用或持续失败时由上层提供权威刷新入口（= refreshDetail）。 */
  onPollingRefresh?: () => Promise<void>
}

export interface UseSessionStreamResult {
  connectionState: StreamConnectionState
  agentRuns: Record<string, AgentRunState>
  lastError: string | null
  reconnect: () => void
}

const POLL_INTERVAL_MS = 3500
const MAX_CONSECUTIVE_ERRORS = 3

export function useSessionStream(options: UseSessionStreamOptions): UseSessionStreamResult {
  const { sessionId, stateVersion: _sv } = options
  // stateVersion 当前 MVP 不直接使用，但保留在 options 接口中供 reconnect 参考。
  void _sv

  const [connectionState, setConnectionState] = useState<StreamConnectionState>('idle')
  const [agentRuns, setAgentRuns] = useState<Record<string, AgentRunState>>({})
  const [lastError, setLastError] = useState<string | null>(null)

  const connRef = useRef<SseConnection | null>(null)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const consecutiveErrorsRef = useRef(0)
  const mountedRef = useRef(true)
  const optionsRef = useRef(options)
  optionsRef.current = options

  // 关闭连接 + 定时器
  const teardown = useCallback(() => {
    if (connRef.current) {
      connRef.current.close()
      connRef.current = null
    }
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  // 启动轮询
  const startPolling = useCallback(() => {
    if (pollTimerRef.current !== null) return
    setConnectionState('polling')
    pollTimerRef.current = setInterval(() => {
      optionsRef.current.onPollingRefresh?.()
    }, POLL_INTERVAL_MS)
  }, [])

  // 事件分发
  const handleEvent = useCallback((event: SessionEvent): void => {
    const opts = optionsRef.current
    const { event_type, payload } = event

    switch (event_type) {
      case 'stage.changed': {
        const toStage = payload?.to_stage as string | undefined
        const sv = payload?.state_version as number | undefined
        if (toStage) opts.onStageChanged?.(toStage, sv ?? 0)
        break
      }
      case 'message.created': {
        const messageId = payload?.message_id as string | undefined
        if (messageId) opts.onMessageCreated?.(messageId)
        break
      }
      case 'review.required': {
        const modifiedFormula = payload?.modified_formula as Formula | undefined
        const safetyReview = (payload?.safety_review ?? {}) as Record<string, unknown>
        if (modifiedFormula) opts.onReviewRequired?.(modifiedFormula, safetyReview)
        break
      }
      case 'safety.blocked': {
        const issues = (payload?.issues ?? []) as SafetyIssue[]
        const rollbackTarget = payload?.rollback_target as string | null | undefined
        opts.onSafetyBlocked?.(issues, rollbackTarget ?? null)
        break
      }
      case 'agent.started': {
        const agentName = payload?.agent_name as string | undefined
        const agentRunId = payload?.agent_run_id as string | undefined
        if (agentName) {
          setAgentRuns((prev) => ({
            ...prev,
            [agentName]: { status: 'running', agentRunId },
          }))
        }
        break
      }
      case 'agent.finished': {
        const agentName = payload?.agent_name as string | undefined
        const agentRunId = payload?.agent_run_id as string | undefined
        if (agentName) {
          setAgentRuns((prev) => ({
            ...prev,
            [agentName]: { status: 'done', agentRunId },
          }))
        }
        break
      }
      case 'agent.failed': {
        const agentName = payload?.agent_name as string | undefined
        const errorCode = payload?.error_code as string | undefined
        if (agentName) {
          setAgentRuns((prev) => ({
            ...prev,
            [agentName]: { status: 'failed', error: errorCode },
          }))
        }
        break
      }
      case 'session.done': {
        const recordId = payload?.record_id as string | undefined
        opts.onSessionDone?.(recordId)
        // 终态：关闭连接
        teardown()
        setConnectionState('disconnected')
        break
      }
      case 'session.blocked': {
        const reason = payload?.blocked_reason as string | undefined
        opts.onSessionBlocked?.(reason ?? '')
        teardown()
        setConnectionState('disconnected')
        break
      }
      case 'session.terminated': {
        opts.onSessionTerminated?.()
        teardown()
        setConnectionState('disconnected')
        break
      }
      case 'heartbeat':
      case 'doctor.reviewed':
        // 不处理
        break
      default:
        break
    }
  }, [teardown])

  // 建立 SSE 连接
  const connectInner = useCallback(() => {
    const id = optionsRef.current.sessionId
    if (!id) return

    setConnectionState('connecting')
    setLastError(null)
    consecutiveErrorsRef.current = 0

    const conn = sse.connectSessionStream(id, {
      onEvent: (event: SessionEvent) => {
        if (!mountedRef.current) return
        handleEvent(event)
      },
      onOpen: () => {
        if (!mountedRef.current) return
        consecutiveErrorsRef.current = 0
        if (pollTimerRef.current !== null) {
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
        }
        setConnectionState('connected')
        setLastError(null)
      },
      onError: (_err: Event) => {
        if (!mountedRef.current) return
        consecutiveErrorsRef.current++
        if (consecutiveErrorsRef.current >= MAX_CONSECUTIVE_ERRORS) {
          connRef.current?.close()
          connRef.current = null
          startPolling()
        } else {
          setConnectionState('connecting')
          setLastError('连接中断，正在重连…')
        }
      },
      onResync: (event: SessionEvent) => {
        if (!mountedRef.current) return
        const reason = typeof event.payload?.reason === 'string' ? event.payload.reason : 'unknown'
        optionsRef.current.onResync?.(reason)
      },
    })

    connRef.current = conn
  }, [handleEvent, startPolling])

  // 手动重连
  const reconnect = useCallback(() => {
    teardown()
    consecutiveErrorsRef.current = 0
    connectInner()
  }, [teardown, connectInner])

  // sessionId 变化时建立/切换连接
  useEffect(() => {
    mountedRef.current = true
    if (!sessionId) {
      teardown()
      setConnectionState('idle')
      setAgentRuns({})
      setLastError(null)
      return
    }

    // 检查 EventSource 是否可用
    if (typeof EventSource === 'undefined') {
      // 直接降级为轮询
      startPolling()
      return () => {
        mountedRef.current = false
        teardown()
      }
    }

    connectInner()

    return () => {
      mountedRef.current = false
      teardown()
    }
  }, [sessionId, teardown, connectInner, startPolling])

  return {
    connectionState,
    agentRuns,
    lastError,
    reconnect,
  }
}
