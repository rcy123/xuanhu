/**
 * 悬壶 WebUI —— R7 异步命令终态对账 hook（terminal reconciliation state machine）
 *
 * 职责：把一次 HTTP 202「已接受命令」推进到终态（succeeded / failed），期间
 * 绝不把 202 当作已完成的临床工作。权威状态始终来自 GET /commands/{id}；
 * SSE 的 command.* 事件仅作唤醒信号。收到终态后由上层以 GET 权威读模型为准。
 *
 * 关键不变量：
 * - 幂等保留：每个已接受命令的 idempotencyKey 在到达终态（或明确进入
 *   attention 待处理）前一直保留在条目中，上层据此「不把已接受的命令当作新
 *   逻辑命令重发」。到达终态后由上层清除；attention 态同样保留，绝不伪造失败。
 * - 有界对账：同一命令的自动轮询有明确上限（maxPollAttempts 次数与/或
 *   pollDeadlineMs 时限，均可测试注入）。预算耗尽后停止自动轮询并释放
 *   spinner/loading 语义，同时把命令保留为 attention（不确定/待人工处理）
 *   条目——不把该命令当作失败，也不允许以新逻辑命令重发。可用
 *   retryStatus / reconcileAll / SSE 唤醒对同一命令重新对账（不发起新 POST）。
 * - PHI 安全：失败只暴露状态接口返回的有界 error_code（后端白名单），绝不回传
 *   私有 payload / 异常文本；attention 只暴露固定的本地码 COMMAND_STATUS_UNAVAILABLE。
 * - 取消安全：会话切换 / 卸载时清空未决条目并停止定时器（generation + mounted），
 *   迟到结果与定时器一并失效。
 *
 * 引用稳定性：返回的 result 对象与全部回调都是引用稳定的（useMemo / useCallback），
 * 且 syncOutstanding 在内容未变化时不做 setState，避免上层依赖 reconciler 标识的
 * 副作用（如 ChatPanel 的 useEffect 依赖）引发无限渲染/副作用循环。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getCommandStatus } from '@/api/index'
import type {
  AsyncCommandAccepted,
  AsyncCommandStatus,
  CommandEventPayload,
  CommandOperation,
} from '@/types/api'

/**
 * 固定的 PHI 安全本地码：对账预算耗尽、状态不可得时暴露给 UI，绝不携带后端
 * 异常文本或私有 payload。UI 不得据此伪造「临床命令失败」。
 */
export const COMMAND_STATUS_UNAVAILABLE = 'COMMAND_STATUS_UNAVAILABLE'

export type CommandReconcileState =
  | 'queued'
  | 'running'
  | 'attention'
  | 'succeeded'
  | 'failed'

export interface CommandReconciliationEntry {
  commandId: string
  sessionId: string
  operation: CommandOperation
  /** 保留到终态/attention；上层绝不能把它当作新逻辑命令重发。 */
  idempotencyKey: string
  state: CommandReconcileState
  /** 仅 failed 时携带，来自状态接口的有界 PHI 安全错误码。 */
  errorCode?: string | null
  /** 内部：已进行的自动轮询次数（有界对账预算）。 */
  attemptCount?: number
  /** 内部：登记时间（毫秒时间戳），用于时限预算。 */
  createdAt?: number
}

export interface UseCommandReconciliationOptions {
  /** succeeded 终态回调：上层在此刷新权威读模型（GET 为准）。 */
  onSucceeded?: (entry: CommandReconciliationEntry) => Promise<void> | void
  /** failed 终态回调：上层在此展示有界错误。 */
  onFailed?: (entry: CommandReconciliationEntry) => void
  /** attention（预算耗尽、状态暂不可得）回调：上层释放 spinner 并展示固定码。 */
  onAttention?: (entry: CommandReconciliationEntry) => void
  /** 有界轮询间隔；SSE 丢失时兜底。 */
  pollIntervalMs?: number
  /** 自动对账最大次数预算（默认 75 次）。测试可注入小值。 */
  maxPollAttempts?: number
  /** 自动对账最大时限（毫秒）；0 = 不启用时限（仅按次数）。默认 300s。 */
  pollDeadlineMs?: number
  /** 测试注入：覆盖状态获取。缺省用真实 getCommandStatus。 */
  fetchStatus?: (sessionId: string, commandId: string) => Promise<AsyncCommandStatus>
}

export interface UseCommandReconciliationResult {
  /** 未决/待处理命令（queued / running / attention）。 */
  outstanding: CommandReconciliationEntry[]
  /** 是否仍有正在自动对账（queued/running）的命令，驱动 spinner 与轮询。 */
  hasOutstanding: boolean
  /** 处于 attention（预算耗尽、待人工处理）的命令。 */
  attention: CommandReconciliationEntry[]
  hasAttention: boolean
  /** 该操作是否有正在自动对账（queued/running）的命令（供 UI 禁用对应按钮）。 */
  isOutstandingFor: (operation: CommandOperation) => boolean
  /** 注册一个新接受的 202 命令。重复 commandId 自动去重。 */
  registerAccepted: (
    accepted: AsyncCommandAccepted,
    sessionId: string,
    idempotencyKey: string,
  ) => void
  /** SSE command.* 唤醒：仅作信号，权威以 GET status 为准；重置该命令预算。 */
  handleCommandEvent: (payload: CommandEventPayload) => void
  /** resync / reconnect 时对全部未决命令做一次对账（重置预算）。 */
  reconcileAll: () => Promise<void>
  /**
   * 手动对账单条命令（不发起新 POST，仅 GET status）。会重置其自动对账预算，
   * 使 attention 条目回到 queued 并恢复有界轮询；无未决时是 no-op。
   */
  retryStatus: (commandId: string) => Promise<void>
  getEntry: (commandId: string) => CommandReconciliationEntry | undefined
  /**
   * 终态回调可在创建后再注册（解决「reconciler 与拥有读模型刷新能力的组件
   * 分属不同层级」的注册顺序问题）。调用后覆盖先前回调。
   */
  setHandlers: (
    onSucceeded?: UseCommandReconciliationOptions['onSucceeded'],
    onFailed?: UseCommandReconciliationOptions['onFailed'],
    onAttention?: UseCommandReconciliationOptions['onAttention'],
  ) => void
  /** 会话切换 / 卸载时清空未决条目与定时器。幂等：无未决时不触发 state 变更。 */
  clear: () => void
}

const DEFAULT_POLL_INTERVAL_MS = 4000
// 对账预算必须显著大于后端最坏命令时长：推进（advance）一次可能串行跑
// 辨证+基础方+重试退避，实测可达 85–120s+（见 REAL-SESSION 9510d47a 复盘）。
// 120s 预算与命令时长同量级，预算耗尽会停掉自动轮询导致界面停在旧状态，
// 必须手动刷新才能看到结果。这里放宽到 300s（75 次 × 4s）后重试预算才真正
// 覆盖最坏情况。
const DEFAULT_MAX_POLL_ATTEMPTS = 75
const DEFAULT_POLL_DEADLINE_MS = 300_000

function entryKey(entry: CommandReconciliationEntry): string {
  return `${entry.commandId}:${entry.state}:${entry.errorCode ?? ''}`
}

export function useCommandReconciliation(
  options: UseCommandReconciliationOptions = {},
): UseCommandReconciliationResult {
  const {
    onSucceeded,
    onFailed,
    onAttention,
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
    maxPollAttempts = DEFAULT_MAX_POLL_ATTEMPTS,
    pollDeadlineMs = DEFAULT_POLL_DEADLINE_MS,
    fetchStatus,
  } = options

  const entriesRef = useRef<Map<string, CommandReconciliationEntry>>(new Map())
  const [outstanding, setOutstanding] = useState<CommandReconciliationEntry[]>([])
  const mountedRef = useRef(true)
  const generationRef = useRef(0)
  const reconcilingRef = useRef<Set<string>>(new Set())
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  /** 上一次已落盘的 outstanding 内容指纹，避免 no-op setState 引发渲染循环。 */
  const outstandingKeyRef = useRef<string>('')
  const optionsRef = useRef({ onSucceeded, onFailed, onAttention, fetchStatus, pollIntervalMs })
  optionsRef.current = { onSucceeded, onFailed, onAttention, fetchStatus, pollIntervalMs }
  const budgetRef = useRef({ maxPollAttempts, pollDeadlineMs })
  budgetRef.current = { maxPollAttempts, pollDeadlineMs }

  const syncOutstanding = useCallback(() => {
    if (!mountedRef.current) return
    const entries = [...entriesRef.current.values()]
    const key = entries.map(entryKey).join('|')
    // 幂等：内容未变化（含清空后仍为空）时不触发 state 变更。
    if (key === outstandingKeyRef.current) return
    outstandingKeyRef.current = key
    setOutstanding(entries)
  }, [])

  const statusFetcher = useCallback(
    (sessionId: string, commandId: string): Promise<AsyncCommandStatus> => {
      if (fetchStatus) return fetchStatus(sessionId, commandId)
      return getCommandStatus(sessionId, commandId)
    },
    [fetchStatus],
  )

  /** 对单个命令做一次终态对账（GET status 为权威）。 */
  const reconcileCommand = useCallback(
    async (commandId: string, viaPoll = false): Promise<void> => {
      const entry = entriesRef.current.get(commandId)
      if (!entry || !mountedRef.current) return
      // attention 条目不参与自动轮询，仅由显式唤醒（SSE/manual/resync）重查。
      if (viaPoll && entry.state === 'attention') return
      if (reconcilingRef.current.has(commandId)) return
      const generation = generationRef.current
      reconcilingRef.current.add(commandId)
      try {
        const status = await statusFetcher(entry.sessionId, commandId)
        if (!mountedRef.current || generationRef.current !== generation) return
        const current = entriesRef.current.get(commandId)
        if (!current) return
        if (status.status === 'succeeded' || status.status === 'failed') {
          const settled: CommandReconciliationEntry = {
            ...current,
            state: status.status,
            errorCode: status.status === 'failed' ? (status.error?.code ?? null) : undefined,
          }
          entriesRef.current.delete(commandId)
          syncOutstanding()
          if (status.status === 'succeeded') {
            await optionsRef.current.onSucceeded?.(settled)
          } else {
            optionsRef.current.onFailed?.(settled)
          }
          return
        }
        // 仍未终态：更新可见状态并保留条目，交由有界轮询继续。
        const attempts = viaPoll ? (current.attemptCount ?? 0) + 1 : 0
        const { maxPollAttempts: maxAttempts, pollDeadlineMs: deadline } = budgetRef.current
        const deadlineHit = deadline > 0
          && Date.now() - (current.createdAt ?? Date.now()) >= deadline
        if (viaPoll && (attempts >= maxAttempts || deadlineHit)) {
          // 预算耗尽：停止自动轮询并释放 spinner，保留命令为 attention。
          const attentionEntry: CommandReconciliationEntry = {
            ...current,
            state: 'attention',
            attemptCount: attempts,
          }
          entriesRef.current.set(commandId, attentionEntry)
          syncOutstanding()
          optionsRef.current.onAttention?.(attentionEntry)
          return
        }
        const nextState = status.status === 'running' ? 'running' : 'queued'
        if (current.state !== nextState || current.attemptCount !== attempts) {
          entriesRef.current.set(commandId, { ...current, state: nextState, attemptCount: attempts })
          syncOutstanding()
        }
      } catch {
        // 网络/状态接口暂不可用：自动轮询时计入预算；显式唤醒仅保留条目。
        if (!mountedRef.current || generationRef.current !== generation) return
        const current = entriesRef.current.get(commandId)
        if (!current) return
        if (viaPoll) {
          const attempts = (current.attemptCount ?? 0) + 1
          const { maxPollAttempts: maxAttempts, pollDeadlineMs: deadline } = budgetRef.current
          const deadlineHit = deadline > 0
            && Date.now() - (current.createdAt ?? Date.now()) >= deadline
          if (attempts >= maxAttempts || deadlineHit) {
            const attentionEntry: CommandReconciliationEntry = {
              ...current,
              state: 'attention',
              attemptCount: attempts,
            }
            entriesRef.current.set(commandId, attentionEntry)
            syncOutstanding()
            optionsRef.current.onAttention?.(attentionEntry)
            return
          }
          if (current.attemptCount !== attempts) {
            entriesRef.current.set(commandId, { ...current, attemptCount: attempts })
            syncOutstanding()
          }
        }
        // 显式唤醒失败或预算内：保留条目，等待下一次轮询/唤醒重试。
      } finally {
        reconcilingRef.current.delete(commandId)
      }
    },
    [statusFetcher, syncOutstanding],
  )

  const reconcileAll = useCallback(async (): Promise<void> => {
    // 显式唤醒：全部条目（含 attention）重新对账，并重置自动对账预算。
    const ids = [...entriesRef.current.keys()]
    await Promise.all(ids.map((id) => reconcileCommand(id, false)))
  }, [reconcileCommand])

  /** 内部：定时器驱动的自动轮询。计入预算（viaPoll=true），不触碰 attention 条目。 */
  const pollAll = useCallback(async (): Promise<void> => {
    const ids = [...entriesRef.current.keys()]
    await Promise.all(ids.map((id) => reconcileCommand(id, true)))
  }, [reconcileCommand])

  const registerAccepted = useCallback(
    (accepted: AsyncCommandAccepted, sessionId: string, idempotencyKey: string) => {
      if (!mountedRef.current) return
      if (entriesRef.current.has(accepted.command_id)) {
        // 同一 commandId 已登记：去重，绝不重复执行已接受的命令。
        return
      }
      entriesRef.current.set(accepted.command_id, {
        commandId: accepted.command_id,
        sessionId,
        operation: accepted.operation,
        idempotencyKey,
        state: 'queued',
        attemptCount: 0,
        createdAt: Date.now(),
      })
      syncOutstanding()
      // 立即做一次对账：命令可能已在登记前完成（快速成功/失败），需追上终态。
      void reconcileCommand(accepted.command_id, false)
    },
    [reconcileCommand, syncOutstanding],
  )

  const handleCommandEvent = useCallback(
    (payload: CommandEventPayload) => {
      const entry = entriesRef.current.get(payload.command_id)
      if (!entry) return
      // SSE 仅作唤醒；权威状态以 GET status 为准，并重置该命令预算。
      void reconcileCommand(payload.command_id, false)
    },
    [reconcileCommand],
  )

  const retryStatus = useCallback(
    (commandId: string): Promise<void> => {
      const entry = entriesRef.current.get(commandId)
      if (!entry || !mountedRef.current) return Promise.resolve()
      // 手动对账：不发起新 POST，仅重新 GET status；重置预算并恢复有界轮询。
      const next = entry.state === 'attention'
        ? { ...entry, state: 'queued' as const, attemptCount: 0, createdAt: Date.now() }
        : { ...entry, attemptCount: 0 }
      entriesRef.current.set(commandId, next)
      syncOutstanding()
      return reconcileCommand(commandId, false)
    },
    [reconcileCommand, syncOutstanding],
  )

  const clear = useCallback(() => {
    generationRef.current += 1
    entriesRef.current.clear()
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    syncOutstanding()
  }, [syncOutstanding])

  // 卸载清理
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      generationRef.current += 1
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [])

  const hasOutstanding = useMemo(
    () => outstanding.some((e) => e.state === 'queued' || e.state === 'running'),
    [outstanding],
  )
  const attention = useMemo(
    () => outstanding.filter((e) => e.state === 'attention'),
    [outstanding],
  )
  const hasAttention = attention.length > 0

  // 有界轮询：存在 queued/running 命令时启动，全部终态或转 attention 后停止。
  useEffect(() => {
    if (!hasOutstanding) {
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
      return
    }
    if (pollTimerRef.current !== null) return
    pollTimerRef.current = setInterval(() => {
      void pollAll()
    }, pollIntervalMs)
    return () => {
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [hasOutstanding, pollIntervalMs, pollAll])

  const isOutstandingFor = useCallback(
    (operation: CommandOperation): boolean => {
      for (const entry of entriesRef.current.values()) {
        // 仅 queued/running 视为「进行中需禁用按钮」；attention 可手动重查。
        if (entry.operation === operation && entry.state !== 'attention') return true
      }
      return false
    },
    [],
  )

  const getEntry = useCallback((commandId: string) => entriesRef.current.get(commandId), [])

  const setHandlers = useCallback(
    (
      onSucceeded?: UseCommandReconciliationOptions['onSucceeded'],
      onFailed?: UseCommandReconciliationOptions['onFailed'],
      onAttention?: UseCommandReconciliationOptions['onAttention'],
    ) => {
      optionsRef.current = {
        ...optionsRef.current,
        onSucceeded,
        onFailed,
        onAttention,
      }
    },
    [],
  )

  // 引用稳定的 result 对象：仅当 outstanding（及其派生量）变化时才更换标识，
  // 避免依赖 commandReconciler 标识的 useEffect 每次渲染都重跑。
  return useMemo(
    () => ({
      outstanding,
      hasOutstanding,
      attention,
      hasAttention,
      isOutstandingFor,
      registerAccepted,
      handleCommandEvent,
      reconcileAll,
      retryStatus,
      getEntry,
      setHandlers,
      clear,
    }),
    [
      outstanding,
      hasOutstanding,
      attention,
      hasAttention,
      isOutstandingFor,
      registerAccepted,
      handleCommandEvent,
      reconcileAll,
      retryStatus,
      getEntry,
      setHandlers,
      clear,
    ],
  )
}
