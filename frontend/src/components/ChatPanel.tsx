/**
 * 悬壶 WebUI —— 问诊对话主区（P8-4 增强）
 *
 * 组合 useSessionDetail + useMessages + useSessionStream + MessageList + MessageInput
 * + 患者信息条 + 步骤条（含 agent 运行状态）+ 阶段结果面板 + 流连接状态
 * + 医师确认操作区 + 处方编辑/否决 Modal + 病历 Panel。
 *
 * 选中会话变化时：detail hook 自动拉取；本组件监听 sessionId 加载消息历史。
 * 提交消息：传当前 detail.state_version；版本冲突由 useMessages 刷新重提。
 * SSE 事件：触发 refreshDetail() / loadMessages() 以 GET 为权威来源。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Spin, Typography } from 'antd'
import {
  CloseOutlined,
  MessageOutlined,
  ProfileOutlined,
} from '@ant-design/icons'
import type { UseSessionDetailResult } from '@/hooks/useSessionDetail'
import type { UseMessagesResult } from '@/hooks/useMessages'
import { useSessionStream } from '@/hooks/useSessionStream'
import type { UseCommandReconciliationResult, CommandReconciliationEntry } from '@/hooks/useCommandReconciliation'
import { COMMAND_STATUS_UNAVAILABLE } from '@/hooks/useCommandReconciliation'
import { isAsyncCommandAccepted } from '@/types/api'
import type { Formula, FormulaOverride, SafetyIssue, RecordResponse, RecordUpdateRequest, BaseFormulaAlternative } from '@/types/api'
import { StepBar } from './StepBar'
import { MessageList } from './MessageList'
import { MessageInput } from './MessageInput'
import { ErrorBanner } from './ErrorBanner'
import { StreamStatus } from './StreamStatus'
import { StageResultsPanel } from './StageResultsPanel'
import { ReviewActionsBar } from './ReviewActionsBar'
import { FormulaEditModal } from './FormulaEditModal'
import { RejectModal } from './RejectModal'
import { RequestMoreInfoModal } from './RequestMoreInfoModal'
import { RecordPanel } from './RecordPanel'
import { LangGraphAdvanceBar } from './LangGraphAdvanceBar'
import { SafetyConfirmationPanel } from './SafetyConfirmationPanel'
import { ThinkingHint } from './ThinkingHint'
import { pendingFormulaFromReadModel } from '@/utils/readModel'
import { langGraphDisposition } from '@/utils/agent'
import { reviewPrescription, getRecord, updateRecord, exportRecord, advanceSession, rollbackMessages } from '@/api/index'
import { downloadFileResponse } from '@/api/download'
import { ApiRequestError, TransportErrorCode } from '@/api/errors'
import { generateIdempotencyKey } from '@/utils/id'

const { Text, Title } = Typography

interface ChatPanelProps {
  sessionId: string | null
  detailHook: UseSessionDetailResult
  messagesHook: UseMessagesResult
  /** R7：异步命令终态对账器（由 App 创建，消息命令已登记；本组件注册终态处理器）。 */
  commandReconciler: UseCommandReconciliationResult
}

export function ChatPanel({ sessionId, detailHook, messagesHook, commandReconciler }: ChatPanelProps) {
  const [pendingReviewFormula, setPendingReviewFormula] = useState<Formula | null>(null)
  const [blockedIssues, setBlockedIssues] = useState<SafetyIssue[] | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null)
  const [rollbackPending, setRollbackPending] = useState(false)
  const [rollbackError, setRollbackError] = useState<string | null>(null)
  const [contextOpen, setContextOpen] = useState(false)
  const [pendingSafetyCount, setPendingSafetyCount] = useState<number | null>(null)
  // P1 多方案：候选方案状态
  const [baseFormulaAlternatives, setBaseFormulaAlternatives] = useState<BaseFormulaAlternative[] | null>(null)
  const [alternativeSubmitting, setAlternativeSubmitting] = useState(false)
  const [selectedAlternativeIndex, setSelectedAlternativeIndex] = useState<number | null>(null)
  const [alternativeError, setAlternativeError] = useState<string | null>(null)

  const { detail, loading, error, selectSession, refreshDetail } = detailHook
  const {
    messages,
    loading: msgLoading,
    error: msgError,
    submitting,
    submitError,
    pendingSubmission,
    lastFailedContent,
    loadMessages,
    submit,
    retryPending,
    clear,
  } = messagesHook

  // 同步外部选中的 sessionId 到 detail hook
  useEffect(() => {
    selectSession(sessionId)
  }, [sessionId, selectSession])

  // 选中会话变化时加载消息历史；离开时清空
  // 同时清空 SSE 衍生的本地状态（pendingReviewFormula/blockedIssues/rollbackTarget）
  // R7：切换会话 / 离开时清空未决异步命令对账，避免上一个会话的命令污染当前 UI。
  //
  // 注意：commandReconciler 的引用在未决命令集合变化时也会更新（useMemo 依赖
  // outstanding），因此不能把它当作「会话变化」的触发源——否则登记新命令（202
  // 登记对账引起 outstanding 变化）会立刻重跑本 effect 并 clear() 抹掉刚登记的
  // 命令，导致按钮闪回可点击、转圈中断。这里用 prevSessionIdRef 守卫：仅当
  // sessionId 真正变化时才清空/重置；effect 因 reconciler 引用变化重跑时直接跳过。
  const prevSessionIdRef = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    if (prevSessionIdRef.current === sessionId) return
    prevSessionIdRef.current = sessionId
    commandReconciler.clear()
    if (!sessionId) {
      clear()
      setPendingReviewFormula(null)
      setBlockedIssues(null)
      setRollbackTarget(null)
      setContextOpen(false)
      setPendingSafetyCount(null)
      return
    }
    // Invalidate in-flight loads/submissions from the previously selected
    // session before establishing the new message history boundary.
    clear()
    setPendingReviewFormula(null)
    setBlockedIssues(null)
    setRollbackTarget(null)
    setContextOpen(false)
    setPendingSafetyCount(null)
    void loadMessages(sessionId)
  }, [sessionId, loadMessages, clear, commandReconciler])

  // SSE 回调：onStageChanged — 阶段变化时刷新会话详情（GET 为权威）
  const handleStageChanged = useCallback(
    (_toStage: string, _stateVersion: number) => {
      void refreshDetail()
    },
    [refreshDetail],
  )

  // SSE 回调：onMessageCreated — 全量刷新消息历史，避免乱序/重复/丢失
  const handleMessageCreated = useCallback(
    (_messageId: string) => {
      if (sessionId) void loadMessages(sessionId)
    },
    [sessionId, loadMessages],
  )

  // SSE 回调：onAgentFinished — 任意 agent 完成都刷新详情与消息，以 GET 为权威。
  // 过去只刷 intake/safety_confirmation：推进（advance）流的多候选终点补发的
  // agent.finished(reasoning) 被忽略，界面只能靠命令对账轮询刷新；SSE 唤醒或
  // 对账预算任一断掉就需要手动刷新。这里对全部 agent 一视同仁（读模型 GET 便宜，
  // 多刷几次无害），保证 reasoning 完成事件真正触发 UI 刷新。
  const handleAgentFinished = useCallback(
    (_agentName: string) => {
      void refreshDetail()
      if (sessionId) void loadMessages(sessionId)
    },
    [loadMessages, refreshDetail, sessionId],
  )

  // SSE 回调：onResync — 流断裂后全量同步；同时对所有未决异步命令做一次对账
  // （重连/断流后不能依赖 SSE 唤醒，需以 GET status 追平终态）。
  const handleResync = useCallback(
    (_reason: string) => {
      void refreshDetail()
      void commandReconciler.reconcileAll()
    },
    [refreshDetail, commandReconciler],
  )

  // SSE 回调：onReviewRequired — 使用 modified_formula 设置待确认处方
  const handleReviewRequired = useCallback(
    (modifiedFormula: Formula, _safetyReview: Record<string, unknown>) => {
      setPendingReviewFormula(modifiedFormula)
      void refreshDetail()
    },
    [refreshDetail],
  )

  // SSE 回调：onSafetyBlocked — 记录阻断 issues 与回退目标
  const handleSafetyBlocked = useCallback(
    (issues: SafetyIssue[], rt?: string | null) => {
      setBlockedIssues(issues)
      setRollbackTarget(rt ?? null)
      void refreshDetail()
    },
    [refreshDetail],
  )

  // SSE 回调：onPollingRefresh — 轮询降级时的权威刷新入口
  const handlePollingRefresh = useCallback(async () => {
    await refreshDetail()
  }, [refreshDetail])

  // session.done — 会话终态，刷新详情
  const handleSessionDone = useCallback(
    (_recordId?: string) => {
      void refreshDetail()
    },
    [refreshDetail],
  )

  // session.blocked — 会话阻断，刷新详情
  const handleSessionBlocked = useCallback(
    (_reason: string) => {
      void refreshDetail()
    },
    [refreshDetail],
  )

  // session.terminated — 会话终止，刷新详情
  const handleSessionTerminated = useCallback(() => {
    void refreshDetail()
  }, [refreshDetail])

  // P1 多方案：从 detail 同步候选方案到本地状态
  useEffect(() => {
    const alts = detail?.base_formula_alternatives
    if (alts && alts.length > 0) {
      setBaseFormulaAlternatives(alts)
      setAlternativeError(null)
    } else {
      setBaseFormulaAlternatives(null)
      setSelectedAlternativeIndex(null)
    }
  }, [detail?.base_formula_alternatives, detail?.state_version])

  // P1 多方案：医师选择方案
  const handleSelectAlternative = useCallback(
    async (index: number) => {
      if (!sessionId || !detail) return
      const idemKey = generateIdempotencyKey()
      setSelectedAlternativeIndex(index)
      setAlternativeSubmitting(true)
      setAlternativeError(null)
      let acceptedCommand = false
      try {
        const result = await advanceSession(
          sessionId,
          { alternative_index: index },
          { idempotencyKey: idemKey, stateVersion: detail.state_version },
        )
        if (isAsyncCommandAccepted(result)) {
          // R7: 202 仅表示已接受，登记对账；不乐观刷新，终态由 reconciler 处理
          // （成功刷新读模型；失败展示有界错误）。期间保持 submitting 禁用按钮。
          acceptedCommand = true
          commandReconciler.registerAccepted(result, sessionId, idemKey)
          return
        }
        // 同步成功：选择后立即刷新，后端会走 resume 路径，完成后推进到 safety/review
        setBaseFormulaAlternatives(null)
        setSelectedAlternativeIndex(null)
        await refreshDetail()
        if (sessionId) await loadMessages(sessionId)
      } catch (err) {
        const apiErr = err instanceof ApiRequestError ? err : null
        setAlternativeError(apiErr?.message ?? '方案选择失败，请重试')
        setSelectedAlternativeIndex(null)
      } finally {
        // 已接受（202）时保留 submitting 直到终态，避免把已接受命令当新命令重发。
        if (!acceptedCommand) setAlternativeSubmitting(false)
      }
    },
    [sessionId, detail, refreshDetail, loadMessages, commandReconciler],
  )

  const handleHoverAlternative = useCallback((_index: number | null) => {
    // 预留 hover 交互（如高亮对应药味对比）
  }, [])

  // 问诊回退：删除目标消息及其之后的所有消息，重建事实状态后刷新权威数据。
  const handleRollback = useCallback(
    async (messageId: string, _content: string) => {
      if (!sessionId) return
      setRollbackPending(true)
      setRollbackError(null)
      try {
        await rollbackMessages(sessionId, messageId, {}, { stateVersion: detail?.state_version })
        // 回退撤销了未决提交：清除 pendingSubmission 与命令对账，否则残留状态
        // 会禁用输入栏（"上一条消息结果尚未确认"），用户无法继续回答。
        commandReconciler.clear()
        clear()
        await refreshDetail()
        await loadMessages(sessionId)
      } catch (err) {
        const apiErr = err instanceof ApiRequestError ? err : null
        if (apiErr?.code === 'INVALID_STATE_VERSION') {
          // 版本已推进：以 GET 为权威刷新后让医师重试。
          await refreshDetail()
          setRollbackError('会话状态已更新，请重试回退。')
        } else {
          setRollbackError(apiErr?.message ?? '回退失败，请重试')
        }
      } finally {
        setRollbackPending(false)
      }
    },
    [sessionId, detail?.state_version, refreshDetail, loadMessages, clear, commandReconciler],
  )

  const canRollback = Boolean(
    detail
    && detail.agent_runtime === 'langgraph'
    && detail.current_stage === 'inquiry'
    && detail.status === 'active'
    && detail.recovery_status === 'normal'
  )

  const streamHook = useSessionStream({
    sessionId,
    stateVersion: detail?.state_version,
    onStageChanged: handleStageChanged,
    onMessageCreated: handleMessageCreated,
    onAgentFinished: handleAgentFinished,
    onResync: handleResync,
    onReviewRequired: handleReviewRequired,
    onSafetyBlocked: handleSafetyBlocked,
    onSessionDone: handleSessionDone,
    onSessionBlocked: handleSessionBlocked,
    onSessionTerminated: handleSessionTerminated,
    onPollingRefresh: handlePollingRefresh,
    // R7：command.* SSE 仅作唤醒信号；权威状态以 GET status + 读模型为准。
    onCommandEvent: commandReconciler.handleCommandEvent,
  })

  // 推导当前运行的 agent 名称（取第一个 status==='running' 的条目）
  const runningAgent = useMemo(() => {
    const entries = Object.entries(streamHook.agentRuns)
    const running = entries.find(([, v]) => v.status === 'running')
    return running ? running[0] : null
  }, [streamHook.agentRuns])

  const replyToQuestionId = useMemo(() => {
    const latest = messages.at(-1)
    if (
      latest?.role === 'agent'
      && latest.agent_name === 'question_composer'
      && typeof latest.structured_delta?.selected_dimension === 'string'
    ) {
      return latest.id
    }
    return undefined
  }, [messages])

  const pendingSafetyHint = detail?.read_model.unresolved.some(
    (item) => (
      item.source === 'safety_confirmation'
      && item.kind === 'unconfirmed_safety_fact'
      && item.key !== 'red_flag'
    ),
  ) ?? false
  const hasPendingSafety = pendingSafetyCount === null
    ? pendingSafetyHint
    : pendingSafetyCount > 0
  const waitingOnlyForSafetyConfirmation = Boolean(
    detail?.agent_runtime === 'langgraph'
    && detail.current_stage === 'inquiry'
    && detail.status === 'active'
    && hasPendingSafety
    && !replyToQuestionId,
  )
  // 问诊采集完整（completeness=ready）时，聊天区不应再无意义地接收新输入；
  // 下一步操作在右侧诊疗摘要的「进入辨证开方」按钮上，自动打开该面板避免流程断掉。
  const intakeReady = Boolean(
    detail?.agent_runtime === 'langgraph'
    && detail.current_stage === 'inquiry'
    && detail.status === 'active'
    && langGraphDisposition(detail) === 'ready',
  )
  const clinicalWorkspaceMode = Boolean(
    detail && (detail.current_stage !== 'inquiry' || intakeReady),
  )

  useEffect(() => {
    if (intakeReady) setContextOpen(true)
  }, [intakeReady])

  const handleSafetyConfirmationChanged = useCallback(async () => {
    await refreshDetail()
    if (sessionId) await loadMessages(sessionId)
  }, [loadMessages, refreshDetail, sessionId])

  // ---------- P8-4 医师确认与病历状态 ----------
  const [reviewSubmitting, setReviewSubmitting] = useState(false)
  const [reviewError, setReviewError] = useState<ApiRequestError | null>(null)
  const [modifyModalOpen, setModifyModalOpen] = useState(false)
  const [rejectModalOpen, setRejectModalOpen] = useState(false)
  const [requestMoreInfoModalOpen, setRequestMoreInfoModalOpen] = useState(false)
  const [modifyReviewError, setModifyReviewError] = useState<ApiRequestError | null>(null)

  // ---------- R7 异步命令终态处理 ----------
  // succeeded：以 GET 权威读模型刷新；failed：展示有界（PHI 安全）错误。
  const handleCommandSucceeded = useCallback(async (entry: CommandReconciliationEntry) => {
    if (entry.operation === 'intake.message') {
      messagesHook.settleMessage(entry.commandId, true)
      if (sessionId) await loadMessages(sessionId)
      await refreshDetail()
    } else if (entry.operation === 'session.advance') {
      setBaseFormulaAlternatives(null)
      setSelectedAlternativeIndex(null)
      setAlternativeSubmitting(false)
      await refreshDetail()
      if (sessionId) await loadMessages(sessionId)
    } else if (entry.operation === 'prescription.review') {
      setModifyModalOpen(false)
      setRejectModalOpen(false)
      setRequestMoreInfoModalOpen(false)
      setReviewSubmitting(false)
      await refreshDetail()
      if (sessionId) await loadMessages(sessionId)
    }
  }, [messagesHook, sessionId, loadMessages, refreshDetail])

  const handleCommandFailed = useCallback((entry: CommandReconciliationEntry) => {
    const bounded = entry.errorCode ?? null
    if (entry.operation === 'intake.message') {
      messagesHook.settleMessage(entry.commandId, false, bounded ?? undefined)
    } else if (entry.operation === 'session.advance') {
      setAlternativeSubmitting(false)
      setSelectedAlternativeIndex(null)
      setAlternativeError(bounded ?? '推进失败，请稍后重试')
    } else if (entry.operation === 'prescription.review') {
      setModifyModalOpen(false)
      setRejectModalOpen(false)
      setRequestMoreInfoModalOpen(false)
      setReviewSubmitting(false)
      setReviewError(new ApiRequestError({
        code: bounded ?? 'COMMAND_FAILED',
        userMessage: bounded ? `复核处理失败（${bounded}）` : '复核处理失败',
        status: 409,
        retryable: false,
      }))
    }
  }, [messagesHook])

  // R7 attention：对账预算耗尽、状态暂不可得。释放 spinner/loading 语义并只暴露
  // 固定的 PHI 安全本地码（COMMAND_STATUS_UNAVAILABLE），绝不伪造命令失败，也不
  // 允许把它当作新逻辑命令重发。消息命令的 pending 保留（不确定态，可重试同一幂等键）。
  const handleCommandAttention = useCallback((entry: CommandReconciliationEntry) => {
    if (entry.operation === 'session.advance') {
      setAlternativeSubmitting(false)
      setAlternativeError(
        '命令状态暂不可得，请稍后重试（' + COMMAND_STATUS_UNAVAILABLE + '）',
      )
    } else if (entry.operation === 'prescription.review') {
      setReviewSubmitting(false)
      setReviewError(new ApiRequestError({
        code: COMMAND_STATUS_UNAVAILABLE,
        userMessage: '命令状态暂不可得，请稍后重试',
        status: 0,
        retryable: true,
      }))
    }
    // intake.message：保留 pendingSubmission（不确定态），由消息输入栏的重试
    // （同一幂等键，非新逻辑命令）或 resync 触发 recheck 恢复。
  }, [])

  useEffect(() => {
    commandReconciler.setHandlers(
      handleCommandSucceeded,
      handleCommandFailed,
      handleCommandAttention,
    )
  }, [commandReconciler, handleCommandSucceeded, handleCommandFailed, handleCommandAttention])

  // 病历
  const [record, setRecord] = useState<RecordResponse | null>(null)
  const [recordGenerationRequested, setRecordGenerationRequested] = useState(false)
  const [recordLoading, setRecordLoading] = useState(false)
  const [recordError, setRecordError] = useState<ApiRequestError | null>(null)
  const [recordEditing, setRecordEditing] = useState(false)
  const [recordSaving, setRecordSaving] = useState(false)
  const [recordSaveError, setRecordSaveError] = useState<ApiRequestError | null>(null)
  const [recordExportError, setRecordExportError] = useState<ApiRequestError | null>(null)

  // 当 detail 变为 done 时拉取病历
  useEffect(() => {
    if (!sessionId || detail?.session_id !== sessionId) {
      setRecord(null)
      setRecordGenerationRequested(false)
      setRecordError(null)
      setRecordEditing(false)
      return
    }
    if (detail?.current_stage === 'done' && detail?.status === 'done') {
      setRecordLoading(true)
      setRecordError(null)
      getRecord(sessionId, 'latest')
        .then((data) => {
          setRecord(data)
          setRecordLoading(false)
        })
        .catch((err: unknown) => {
          if (err instanceof ApiRequestError) setRecordError(err)
          setRecordLoading(false)
        })
    } else {
      setRecord(null)
      setRecordError(null)
      setRecordEditing(false)
      setRecordExportError(null)
      if (detail?.current_stage !== 'record') setRecordGenerationRequested(false)
    }
  }, [sessionId, detail?.session_id, detail?.current_stage, detail?.status])

  // ---------- 医师确认操作 ----------

  const handleConfirm = useCallback(() => {
    if (!sessionId || !detail || detail.session_id !== sessionId) return
    const idemKey = generateIdempotencyKey()
    setReviewSubmitting(true)
    setReviewError(null)
    let acceptedCommand = false
    reviewPrescription(
      sessionId,
      { action: 'confirm' },
      { stateVersion: detail.state_version, idempotencyKey: idemKey },
    )
      .then((result) => {
        if (isAsyncCommandAccepted(result)) {
          // R7: 202 仅表示已接受，登记对账；终态由 reconciler 处理。
          acceptedCommand = true
          commandReconciler.registerAccepted(result, sessionId, idemKey)
          return
        }
        void refreshDetail()
      })
      .catch((err: unknown) => {
        if (err instanceof ApiRequestError) setReviewError(err)
      })
      .finally(() => {
        // 已接受时保留 submitting（禁用按钮）直到终态，避免重复提交同一逻辑命令。
        if (!acceptedCommand) setReviewSubmitting(false)
      })
  }, [sessionId, detail, refreshDetail, commandReconciler])

  const handleModify = useCallback(() => {
    setModifyModalOpen(true)
    setModifyReviewError(null)
  }, [])

  const handleModifySubmit = useCallback(
    (override: FormulaOverride, feedback?: string) => {
      if (!sessionId || !detail || detail.session_id !== sessionId) return
      const idemKey = generateIdempotencyKey()
      setReviewSubmitting(true)
      setModifyReviewError(null)
      let acceptedCommand = false
      reviewPrescription(
        sessionId,
        {
          action: 'modify',
          formula_override: override,
          feedback: feedback || undefined,
        },
        { stateVersion: detail.state_version, idempotencyKey: idemKey },
      )
        .then((result) => {
          if (isAsyncCommandAccepted(result)) {
            // R7: 已接受，登记对账并关闭弹窗；终态刷新由 reconciler 负责。
            acceptedCommand = true
            commandReconciler.registerAccepted(result, sessionId, idemKey)
            setModifyModalOpen(false)
            return
          }
          setModifyModalOpen(false)
          void refreshDetail()
        })
        .catch((err: unknown) => {
          if (err instanceof ApiRequestError) {
            // 二次安全审核失败：在 Modal 内展示 issues，不关闭弹窗
            if (err.code === 'SAFETY_REVIEW_BLOCKED') {
              setModifyReviewError(err)
            } else {
              setReviewError(err)
              setModifyModalOpen(false)
            }
          }
        })
        .finally(() => {
          if (!acceptedCommand) setReviewSubmitting(false)
        })
    },
    [sessionId, detail, refreshDetail, commandReconciler],
  )

  const handleReject = useCallback(() => {
    setRejectModalOpen(true)
  }, [])

  const handleRequestMoreInfo = useCallback(() => {
    setRequestMoreInfoModalOpen(true)
    setReviewError(null)
  }, [])

  const handleRequestMoreInfoSubmit = useCallback((feedback: string) => {
    if (!sessionId || !detail || detail.session_id !== sessionId) return
    const idemKey = generateIdempotencyKey()
    setReviewSubmitting(true)
    setReviewError(null)
    let acceptedCommand = false
    reviewPrescription(
      sessionId,
      { action: 'request_more_info', feedback },
      { stateVersion: detail.state_version, idempotencyKey: idemKey },
    )
      .then((result) => {
        if (isAsyncCommandAccepted(result)) {
          // R7: 已接受，登记对账并关闭弹窗；终态刷新由 reconciler 负责。
          acceptedCommand = true
          commandReconciler.registerAccepted(result, sessionId, idemKey)
          setRequestMoreInfoModalOpen(false)
          return
        }
        setRequestMoreInfoModalOpen(false)
        void refreshDetail()
      })
      .catch((err: unknown) => {
        if (err instanceof ApiRequestError) setReviewError(err)
      })
      .finally(() => {
        if (!acceptedCommand) setReviewSubmitting(false)
      })
  }, [sessionId, detail, refreshDetail, commandReconciler])

  const handleRejectSubmit = useCallback(
    (feedback: string) => {
      if (!sessionId || !detail || detail.session_id !== sessionId) return
      const idemKey = generateIdempotencyKey()
      setReviewSubmitting(true)
      setReviewError(null)
      let acceptedCommand = false
      reviewPrescription(
        sessionId,
        { action: 'reject', feedback: feedback || undefined },
        { stateVersion: detail.state_version, idempotencyKey: idemKey },
      )
        .then((result) => {
          if (isAsyncCommandAccepted(result)) {
            // R7: 已接受，登记对账并关闭弹窗；终态刷新由 reconciler 负责。
            acceptedCommand = true
            commandReconciler.registerAccepted(result, sessionId, idemKey)
            setRejectModalOpen(false)
            return
          }
          setRejectModalOpen(false)
          void refreshDetail()
        })
        .catch((err: unknown) => {
          if (err instanceof ApiRequestError) setReviewError(err)
          setRejectModalOpen(false)
        })
        .finally(() => {
          if (!acceptedCommand) setReviewSubmitting(false)
        })
    },
    [sessionId, detail, refreshDetail, commandReconciler],
  )

  const handleReviewRetry = useCallback(() => {
    setReviewError(null)
    void refreshDetail()
    // R7：attention 命令（状态暂不可得）手动重查，不发起新 POST。
    void commandReconciler.reconcileAll()
  }, [refreshDetail, commandReconciler])

  const handleRecordGenerationStart = useCallback(() => {
    setRecordGenerationRequested(true)
    setRecordLoading(true)
    setRecordError(null)
  }, [])

  const handleRecordGenerationFailed = useCallback(() => {
    setRecordGenerationRequested(false)
    setRecordLoading(false)
  }, [])

  // ---------- 病历操作 ----------

  const handleRecordEdit = useCallback(() => {
    setRecordEditing(true)
    setRecordSaveError(null)
  }, [])

  const handleRecordCancelEdit = useCallback(() => {
    setRecordEditing(false)
    setRecordSaveError(null)
  }, [])

  const handleRecordSave = useCallback(
    (body: RecordUpdateRequest) => {
      if (!sessionId || !detail || detail.session_id !== sessionId) return
      setRecordSaving(true)
      setRecordSaveError(null)
      updateRecord(sessionId, body, { stateVersion: detail.state_version })
        .then(() => {
          setRecordSaving(false)
          setRecordEditing(false)
          // 刷新详情（拿新 state_version） + 重新拉病历
          void refreshDetail().then(() => {
            if (sessionId) {
              getRecord(sessionId, 'latest')
                .then((data) => setRecord(data))
                .catch(() => { /* ignore */ })
            }
          })
        })
        .catch((err: unknown) => {
          setRecordSaving(false)
          if (err instanceof ApiRequestError) setRecordSaveError(err)
        })
    },
    [sessionId, detail, refreshDetail],
  )

  const handleExport = useCallback(
    (format: 'txt' | 'json' | 'md') => {
      if (!sessionId) return
      setRecordExportError(null)
      exportRecord(sessionId, format, 'latest')
        .then((response) => downloadFileResponse(response, '病历', format))
        .catch((err: unknown) => {
          if (err instanceof ApiRequestError) {
            setRecordExportError(err)
          } else {
            setRecordExportError(
              new ApiRequestError({
                code: TransportErrorCode.NETWORK_ERROR,
                userMessage: '导出失败，请重试',
                status: 0,
                retryable: true,
              }),
            )
          }
        })
    },
    [sessionId],
  )

  // 待确认处方：优先 SSE；LangGraph 刷新后只从完整性校验通过的 Read Model 恢复。
  const restoredPendingFormula =
    detail?.agent_runtime === 'langgraph'
      ? pendingFormulaFromReadModel(detail.read_model)
      : null
  const effectivePendingFormula =
    pendingReviewFormula
    ?? restoredPendingFormula
    ?? detail?.modified_formula
    ?? null

  if (!sessionId) {
    return (
      <main className="xh-empty-workspace">
        <div className="xh-empty-illustration" aria-hidden="true">
          <span>问</span>
        </div>
        <Text className="xh-section-kicker">CLINICAL WORKSPACE</Text>
        <Title level={3}>开始一次问诊</Title>
        <Text type="secondary">
          从左侧选择已有会话，或新建问诊。对话、诊疗摘要和复核动作会在各自区域清晰呈现。
        </Text>
        <div className="xh-empty-flow" aria-label="问诊流程">
          <span>问诊采集</span>
          <i />
          <span>辨证分析</span>
          <i />
          <span>安全复核</span>
        </div>
      </main>
    )
  }

  // 当前阶段是否允许提交问诊消息：仅 inquiry 阶段允许
  if (detail && detail.session_id !== sessionId) {
    return (
      <main className="xh-empty-workspace" data-testid="session-detail-boundary-loading">
        <Spin />
        <Text type="secondary">{'\u6b63\u5728\u5207\u6362\u4f1a\u8bdd\u2026'}</Text>
      </main>
    )
  }

  const canSubmit = detail
    ? (
        detail.current_stage === 'inquiry'
        && !waitingOnlyForSafetyConfirmation
        && !intakeReady
        && pendingSubmission == null
      )
    : false

  const handleRefreshDetailForVersion = async (): Promise<number | undefined> => {
    const fresh = await refreshDetail()
    return fresh?.state_version
  }

  const submitContent = (content: string) => {
    if (!sessionId || !detail || detail.session_id !== sessionId) return
    const submission = replyToQuestionId
      ? submit(
          sessionId,
          content,
          detail.state_version,
          handleRefreshDetailForVersion,
          replyToQuestionId,
        )
      : submit(
          sessionId,
          content,
          detail.state_version,
          handleRefreshDetailForVersion,
        )
    void submission.then((ok) => {
      if (ok) {
        // 提交成功后刷新会话详情（stage 可能推进，state_version 更新）
        void refreshDetail()
      }
    })
  }

  const retryLastSubmit = () => {
    if (
      !sessionId
      || !detail
      || detail.session_id !== sessionId
      || !pendingSubmission
    ) return
    // The hook owns the immutable reply/question binding.  The latest message
    // may already be a newer question after a lost network response.
    void retryPending(
      sessionId,
      handleRefreshDetailForVersion,
    ).then((ok) => {
      if (ok) void refreshDetail()
    })
  }

  const hasClinicalSummary = detail != null && (
    detail.sufficiency_report != null
    || detail.syndrome_result != null
    || detail.base_formula != null
    || detail.modified_formula != null
    || effectivePendingFormula != null
    || detail.safety_review != null
    || blockedIssues != null
    || detail.agent_runtime === 'langgraph'
    || detail.current_stage === 'record'
    || detail.current_stage === 'done'
  )

  return (
    <>
      <main className="xh-clinical-workspace">
        <Button
          className="xh-context-trigger"
          icon={<ProfileOutlined />}
          aria-label={clinicalWorkspaceMode ? '打开诊疗工作台' : '打开诊疗摘要'}
          onClick={() => setContextOpen(true)}
        >
          {clinicalWorkspaceMode ? '诊疗工作台' : '诊疗摘要'}
        </Button>

        <div className={`xh-workspace-columns${clinicalWorkspaceMode ? ' is-clinical-workspace' : ''}`}>
          <section className="xh-conversation-pane" aria-label={clinicalWorkspaceMode ? '问诊记录' : '问诊对话'}>
            <div className="xh-pane-heading">
              <div className="xh-pane-heading-main">
                <Title level={5}>
                  <MessageOutlined aria-hidden="true" />
                  {clinicalWorkspaceMode ? '问诊记录' : '问诊对话'}
                </Title>
              </div>
            </div>
            <div className="xh-conversation-flow">
              <StepBar
                currentStage={detail?.current_stage ?? null}
                agentRuns={streamHook.agentRuns}
              />
            </div>
            <StreamStatus
              state={streamHook.connectionState}
              lastError={streamHook.lastError}
              runningAgent={runningAgent}
              onReconnect={streamHook.reconnect}
            />
            {error ? (
              <div className="xh-inline-feedback">
                <ErrorBanner error={error} onRetry={refreshDetail} />
              </div>
            ) : null}
            {alternativeError ? (
              <div className="xh-inline-feedback">
                <ErrorBanner
                  error={new ApiRequestError({
                    code: 'ALTERNATIVE_SELECTION_FAILED',
                    userMessage: alternativeError,
                    status: 500,
                    retryable: false,
                  })}
                  onRetry={() => {
                    setAlternativeError(null)
                    // R7：attention 命令（状态暂不可得）手动重查，不发起新 POST。
                    void commandReconciler.reconcileAll()
                  }}
                />
              </div>
            ) : null}
            {loading && !detail ? (
              <div className="xh-centered-state xh-detail-loading">
                <Spin />
              </div>
            ) : null}
            <div className="xh-message-surface">
              {rollbackError ? (
                <div className="xh-message-rollback-error" role="alert">
                  <Alert type="error" showIcon message={rollbackError} onClose={() => setRollbackError(null)} closable />
                </div>
              ) : null}
              <MessageList
                messages={messages}
                loading={msgLoading}
                error={msgError}
                onRetry={() => sessionId && void loadMessages(sessionId)}
                canRollback={canRollback}
                rollbackPending={rollbackPending}
                onRollback={handleRollback}
              />
              {detail ? (
                <SafetyConfirmationPanel
                  key={detail.session_id}
                  sessionId={detail.session_id}
                  refreshKey={detail.state_version}
                  enabled={
                    detail.agent_runtime === 'langgraph'
                    && detail.current_stage === 'inquiry'
                    && detail.status === 'active'
                  }
                  pendingHint={pendingSafetyHint}
                  blocksFreeInput={waitingOnlyForSafetyConfirmation}
                  onPendingChange={setPendingSafetyCount}
                  onChanged={handleSafetyConfirmationChanged}
                />
              ) : null}
              <ThinkingHint
                active={submitting || runningAgent != null}
                agent={runningAgent}
              />
              <MessageInput
                submitting={submitting}
                error={submitError}
                disabled={detail != null && !canSubmit}
                disabledReason={intakeReady
                  ? '问诊要素已采集完整，请点击右侧「进入辨证开方」进入下一步'
                  : waitingOnlyForSafetyConfirmation
                    ? '请先完成上方安全信息确认，系统随后会继续问诊'
                  : pendingSubmission
                    ? '上一条消息结果尚未确认，请先使用错误提示中的重试'
                    : undefined}
                onRetry={pendingSubmission ? retryLastSubmit : undefined}
                lastContent={pendingSubmission?.content ?? lastFailedContent ?? undefined}
                onSubmit={submitContent}
              />
            </div>
          </section>

          <aside
            className={`xh-context-pane${contextOpen ? ' is-open' : ''}`}
            aria-label={clinicalWorkspaceMode ? '诊疗工作台' : '诊疗摘要'}
          >
            <div className="xh-context-pane-heading">
              <div>
                <Title level={5}>
                  <ProfileOutlined aria-hidden="true" />
                  {clinicalWorkspaceMode ? '诊疗工作台' : '诊疗摘要'}
                </Title>
              </div>
              <Button
                className="xh-context-close"
                type="text"
                icon={<CloseOutlined />}
                aria-label={clinicalWorkspaceMode ? '关闭诊疗工作台' : '关闭诊疗摘要'}
                onClick={() => setContextOpen(false)}
              />
            </div>
            <div className="xh-context-scroll">
              {!hasClinicalSummary ? (
                <div className="xh-summary-empty">
                  <div className="xh-summary-empty-icon" aria-hidden="true">诊</div>
                  <Text strong>等待诊疗结果</Text>
                  <Text type="secondary">
                    完成问诊后，完备性判断、辨证、处方和安全审核会依次显示在这里。
                  </Text>
                </div>
              ) : null}
              <StageResultsPanel
                detail={detail}
                pendingReviewFormula={effectivePendingFormula}
                blockedIssues={blockedIssues}
                rollbackTarget={rollbackTarget}
                baseFormulaAlternatives={baseFormulaAlternatives}
                onSelectAlternative={handleSelectAlternative}
                alternativeSubmitting={alternativeSubmitting}
                selectedAlternativeIndex={selectedAlternativeIndex}
                onHoverAlternative={handleHoverAlternative}
              />
              {detail ? (
                <LangGraphAdvanceBar
                  detail={detail}
                  onRefresh={refreshDetail}
                  onRecordGenerationStart={handleRecordGenerationStart}
                  onRecordGenerationFailed={handleRecordGenerationFailed}
                  onAdvanced={async () => {
                    await refreshDetail()
                    if (sessionId) await loadMessages(sessionId)
                  }}
                  onRecovered={async () => {
                    await refreshDetail()
                    if (sessionId) await loadMessages(sessionId)
                  }}
                  // R7: 已接受（202）的推进命令登记对账，pending 期间禁用按钮。
                  onCommandAccepted={(accepted, idemKey) => {
                    if (sessionId) commandReconciler.registerAccepted(accepted, sessionId, idemKey)
                  }}
                  pending={commandReconciler.isOutstandingFor('session.advance')}
                />
              ) : null}
              {detail ? (
                <ReviewActionsBar
                  detail={detail}
                  pendingReviewFormula={effectivePendingFormula}
                  blockedIssues={blockedIssues}
                  submitting={reviewSubmitting}
                  error={reviewError}
                  onConfirm={handleConfirm}
                  onModify={handleModify}
                  onReject={handleReject}
                  onRequestMoreInfo={handleRequestMoreInfo}
                  onRetry={handleReviewRetry}
                />
              ) : null}
              {detail ? (
                <RecordPanel
                  detail={detail}
                  record={record}
                  generationRequested={recordGenerationRequested}
                  loading={recordLoading}
                  error={recordError}
                  editing={recordEditing}
                  saving={recordSaving}
                  saveError={recordSaveError}
                  exportError={recordExportError}
                  onExport={handleExport}
                  onExportErrorDismiss={() => setRecordExportError(null)}
                  onEdit={handleRecordEdit}
                  onCancelEdit={handleRecordCancelEdit}
                  onSave={handleRecordSave}
                  onRetry={() => {
                    setRecordError(null)
                    if (sessionId) {
                      setRecordLoading(true)
                      getRecord(sessionId, 'latest')
                        .then((data) => { setRecord(data); setRecordLoading(false) })
                        .catch((err: unknown) => {
                          if (err instanceof ApiRequestError) setRecordError(err)
                          setRecordLoading(false)
                        })
                    }
                  }}
                />
              ) : null}
            </div>
          </aside>
          <button
            type="button"
            className={`xh-context-scrim${contextOpen ? ' is-open' : ''}`}
            aria-label={clinicalWorkspaceMode ? '关闭诊疗工作台' : '关闭诊疗摘要'}
            onClick={() => setContextOpen(false)}
          />
        </div>
      </main>

      <FormulaEditModal
        open={modifyModalOpen}
        initialFormula={effectivePendingFormula}
        submitting={reviewSubmitting}
        reviewError={modifyReviewError}
        onCancel={() => { setModifyModalOpen(false); setModifyReviewError(null) }}
        onSubmit={handleModifySubmit}
      />
      <RejectModal
        open={rejectModalOpen}
        submitting={reviewSubmitting}
        onCancel={() => setRejectModalOpen(false)}
        onSubmit={handleRejectSubmit}
      />
      <RequestMoreInfoModal
        open={requestMoreInfoModalOpen}
        submitting={reviewSubmitting}
        onCancel={() => setRequestMoreInfoModalOpen(false)}
        onSubmit={handleRequestMoreInfoSubmit}
      />
    </>
  )
}

export default ChatPanel
