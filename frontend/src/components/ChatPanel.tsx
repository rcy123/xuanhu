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

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Spin, Tag, Typography } from 'antd'
import {
  CloseOutlined,
  FileSearchOutlined,
  MessageOutlined,
} from '@ant-design/icons'
import type { UseSessionDetailResult } from '@/hooks/useSessionDetail'
import type { UseMessagesResult } from '@/hooks/useMessages'
import { useSessionStream } from '@/hooks/useSessionStream'
import type { SessionDetail, Formula, FormulaOverride, SafetyIssue, RecordResponse, RecordUpdateRequest } from '@/types/api'
import { StepBar } from './StepBar'
import { MessageList } from './MessageList'
import { MessageInput } from './MessageInput'
import { ErrorBanner } from './ErrorBanner'
import { StreamStatus } from './StreamStatus'
import { StageResultsPanel } from './StageResultsPanel'
import { ReviewActionsBar } from './ReviewActionsBar'
import { FormulaEditModal } from './FormulaEditModal'
import { RejectModal } from './RejectModal'
import { RecordPanel } from './RecordPanel'
import { LangGraphAdvanceBar } from './LangGraphAdvanceBar'
import { SafetyConfirmationPanel } from './SafetyConfirmationPanel'
import { pendingFormulaFromReadModel } from '@/utils/readModel'
import { stageLabel } from '@/utils/stage'
import { langGraphDisposition } from '@/utils/agent'
import { reviewPrescription, getRecord, updateRecord, exportRecord } from '@/api/index'
import { downloadFileResponse } from '@/api/download'
import { ApiRequestError, TransportErrorCode } from '@/api/errors'

const { Text, Title } = Typography

interface ChatPanelProps {
  sessionId: string | null
  detailHook: UseSessionDetailResult
  messagesHook: UseMessagesResult
}

function PatientBar({ detail }: { detail: SessionDetail }) {
  const p = detail.patient_info
  const parts: string[] = []
  if (p.gender && p.gender !== 'unknown') {
    parts.push(p.gender === 'male' ? '男' : '女')
  }
  if (p.age != null) parts.push(`${p.age}岁`)
  return (
    <div className="xh-patient-bar">
      <div className="xh-patient-avatar" aria-hidden="true">
        {p.name?.trim().slice(0, 1) || '患'}
      </div>
      <div className="xh-patient-copy">
        <div className="xh-patient-name-row">
          <Text strong>{p.name || '未命名患者'}</Text>
          {parts.length > 0 ? <Text type="secondary">{parts.join(' · ')}</Text> : null}
          <Tag className="xh-stage-tag" color="processing">
            {stageLabel(detail.current_stage)}
          </Tag>
        </div>
        <Text type="secondary" ellipsis={{ tooltip: detail.chief_complaint }}>
          主诉：{detail.chief_complaint || '尚未填写'}
        </Text>
      </div>
    </div>
  )
}

export function ChatPanel({ sessionId, detailHook, messagesHook }: ChatPanelProps) {
  const [pendingReviewFormula, setPendingReviewFormula] = useState<Formula | null>(null)
  const [blockedIssues, setBlockedIssues] = useState<SafetyIssue[] | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null)
  const [contextOpen, setContextOpen] = useState(false)
  const [pendingSafetyCount, setPendingSafetyCount] = useState<number | null>(null)

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
  useEffect(() => {
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
  }, [sessionId, loadMessages, clear])

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

  const handleAgentFinished = useCallback(
    (agentName: string) => {
      if (agentName !== 'intake' && agentName !== 'safety_confirmation') return
      void refreshDetail()
      if (sessionId) void loadMessages(sessionId)
    },
    [loadMessages, refreshDetail, sessionId],
  )

  // SSE 回调：onResync — 流断裂后全量同步
  const handleResync = useCallback(
    (_reason: string) => {
      void refreshDetail()
    },
    [refreshDetail],
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
  const [modifyReviewError, setModifyReviewError] = useState<ApiRequestError | null>(null)

  // 病历
  const [record, setRecord] = useState<RecordResponse | null>(null)
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
    }
  }, [sessionId, detail?.session_id, detail?.current_stage, detail?.status])

  // ---------- 医师确认操作 ----------

  const handleConfirm = useCallback(() => {
    if (!sessionId || !detail || detail.session_id !== sessionId) return
    setReviewSubmitting(true)
    setReviewError(null)
    reviewPrescription(
      sessionId,
      { action: 'confirm' },
      { stateVersion: detail.state_version },
    )
      .then(() => {
        setReviewSubmitting(false)
        void refreshDetail()
      })
      .catch((err: unknown) => {
        setReviewSubmitting(false)
        if (err instanceof ApiRequestError) setReviewError(err)
      })
  }, [sessionId, detail, refreshDetail])

  const handleModify = useCallback(() => {
    setModifyModalOpen(true)
    setModifyReviewError(null)
  }, [])

  const handleModifySubmit = useCallback(
    (override: FormulaOverride, feedback?: string) => {
      if (!sessionId || !detail || detail.session_id !== sessionId) return
      setReviewSubmitting(true)
      setModifyReviewError(null)
      reviewPrescription(
        sessionId,
        {
          action: 'modify',
          formula_override: override,
          feedback: feedback || undefined,
        },
        { stateVersion: detail.state_version },
      )
        .then(() => {
          setReviewSubmitting(false)
          setModifyModalOpen(false)
          void refreshDetail()
        })
        .catch((err: unknown) => {
          setReviewSubmitting(false)
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
    },
    [sessionId, detail, refreshDetail],
  )

  const handleReject = useCallback(() => {
    setRejectModalOpen(true)
  }, [])

  const handleRequestMoreInfo = useCallback(() => {
    if (!sessionId || !detail || detail.session_id !== sessionId) return
    setReviewSubmitting(true)
    setReviewError(null)
    reviewPrescription(
      sessionId,
      { action: 'request_more_info' },
      { stateVersion: detail.state_version },
    )
      .then(() => {
        setReviewSubmitting(false)
        void refreshDetail()
      })
      .catch((err: unknown) => {
        setReviewSubmitting(false)
        if (err instanceof ApiRequestError) setReviewError(err)
      })
  }, [sessionId, detail, refreshDetail])

  const handleRejectSubmit = useCallback(
    (feedback: string) => {
      if (!sessionId || !detail || detail.session_id !== sessionId) return
      setReviewSubmitting(true)
      setReviewError(null)
      reviewPrescription(
        sessionId,
        { action: 'reject', feedback: feedback || undefined },
        { stateVersion: detail.state_version },
      )
        .then(() => {
          setReviewSubmitting(false)
          setRejectModalOpen(false)
          void refreshDetail()
        })
        .catch((err: unknown) => {
          setReviewSubmitting(false)
          if (err instanceof ApiRequestError) setReviewError(err)
          setRejectModalOpen(false)
        })
    },
    [sessionId, detail, refreshDetail],
  )

  const handleReviewRetry = useCallback(() => {
    setReviewError(null)
    void refreshDetail()
  }, [refreshDetail])

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
        <div className="xh-case-header">
          {detail ? <PatientBar detail={detail} /> : null}
          <Button
            className="xh-context-trigger"
            icon={<FileSearchOutlined />}
            aria-label="打开诊疗摘要"
            onClick={() => setContextOpen(true)}
          >
            诊疗摘要
          </Button>
        </div>

        <div className="xh-progress-region">
          <StepBar
            currentStage={detail?.current_stage ?? null}
            agentRuntime={detail?.agent_runtime}
            readModel={detail?.read_model}
            agentRuns={streamHook.agentRuns}
          />
        </div>

        <div className="xh-workspace-columns">
          <section className="xh-conversation-pane" aria-label="问诊对话">
            <div className="xh-pane-heading">
              <div>
                <Text className="xh-section-kicker">CONSULTATION</Text>
                <Title level={5}>
                  <MessageOutlined aria-hidden="true" />
                  问诊对话
                </Title>
              </div>
              <Text type="secondary">对话记录实时保存</Text>
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
            {loading && !detail ? (
              <div className="xh-centered-state xh-detail-loading">
                <Spin />
              </div>
            ) : null}
            <div className="xh-message-surface">
              <MessageList
                messages={messages}
                loading={msgLoading}
                error={msgError}
                onRetry={() => sessionId && void loadMessages(sessionId)}
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
            aria-label="诊疗摘要"
          >
            <div className="xh-context-pane-heading">
              <div>
                <Text className="xh-section-kicker">CLINICAL SUMMARY</Text>
                <Title level={5}>诊疗摘要</Title>
              </div>
              <Button
                className="xh-context-close"
                type="text"
                icon={<CloseOutlined />}
                aria-label="关闭诊疗摘要"
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
              />
              {detail ? (
                <LangGraphAdvanceBar
                  detail={detail}
                  onRefresh={refreshDetail}
                  onAdvanced={async () => {
                    await refreshDetail()
                    if (sessionId) await loadMessages(sessionId)
                  }}
                  onRecovered={async () => {
                    await refreshDetail()
                    if (sessionId) await loadMessages(sessionId)
                  }}
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
            aria-label="关闭诊疗摘要"
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
    </>
  )
}

export default ChatPanel
