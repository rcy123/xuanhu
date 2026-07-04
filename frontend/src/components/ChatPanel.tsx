/**
 * 悬壶 WebUI —— 问诊对话主区（P8-3 增强）
 *
 * 组合 useSessionDetail + useMessages + useSessionStream + MessageList + MessageInput
 * + 患者信息条 + 步骤条（含 agent 运行状态）+ 阶段结果面板 + 流连接状态。
 *
 * 选中会话变化时：detail hook 自动拉取；本组件监听 sessionId 加载消息历史。
 * 提交消息：传当前 detail.state_version；版本冲突由 useMessages 刷新重提。
 * SSE 事件：触发 refreshDetail() / loadMessages() 以 GET 为权威来源。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Empty, Spin, Typography, Layout, theme } from 'antd'
import type { UseSessionDetailResult } from '@/hooks/useSessionDetail'
import type { UseMessagesResult } from '@/hooks/useMessages'
import { useSessionStream } from '@/hooks/useSessionStream'
import type { SessionDetail, Formula, SafetyIssue } from '@/types/api'
import { StepBar } from './StepBar'
import { MessageList } from './MessageList'
import { MessageInput } from './MessageInput'
import { ErrorBanner } from './ErrorBanner'
import { StreamStatus } from './StreamStatus'
import { StageResultsPanel } from './StageResultsPanel'

const { Content } = Layout
const { Text, Title } = Typography

interface ChatPanelProps {
  sessionId: string | null
  detailHook: UseSessionDetailResult
  messagesHook: UseMessagesResult
}

function PatientBar({ detail }: { detail: SessionDetail }) {
  const p = detail.patient_info
  const parts: string[] = []
  if (p.name) parts.push(p.name)
  if (p.gender && p.gender !== 'unknown') {
    parts.push(p.gender === 'male' ? '男' : '女')
  }
  if (p.age != null) parts.push(`${p.age}岁`)
  return (
    <div
      style={{
        padding: '8px var(--xh-space-l)',
        borderBottom: '1px solid var(--xh-border)',
        background: 'var(--xh-bg-card)',
      }}
    >
      <Text style={{ fontSize: 13 }}>
        患者：{parts.length > 0 ? parts.join(' · ') : '未填写'}
      </Text>
      {detail.chief_complaint ? (
        <>
          <Text type="secondary" style={{ margin: '0 8px' }}>|</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            主诉：{detail.chief_complaint}
          </Text>
        </>
      ) : null}
    </div>
  )
}

export function ChatPanel({ sessionId, detailHook, messagesHook }: ChatPanelProps) {
  const { token } = theme.useToken()
  const [lastSubmittedContent, setLastSubmittedContent] = useState<string | null>(null)
  const [pendingReviewFormula, setPendingReviewFormula] = useState<Formula | null>(null)
  const [blockedIssues, setBlockedIssues] = useState<SafetyIssue[] | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null)

  const { detail, loading, error, selectSession, refreshDetail } = detailHook
  const {
    messages,
    loading: msgLoading,
    error: msgError,
    submitting,
    submitError,
    loadMessages,
    submit,
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
      setLastSubmittedContent(null)
      setPendingReviewFormula(null)
      setBlockedIssues(null)
      setRollbackTarget(null)
      return
    }
    setLastSubmittedContent(null)
    setPendingReviewFormula(null)
    setBlockedIssues(null)
    setRollbackTarget(null)
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

  if (!sessionId) {
    return (
      <Content
        style={{
          background: 'var(--xh-bg-page)',
          padding: 'var(--xh-space-xxl)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          overflow: 'auto',
        }}
      >
        <Empty
          description={
            <div>
              <Title level={4} style={{ fontFamily: 'var(--xh-font-serif)' }}>
                开始一次问诊
              </Title>
              <Text type="secondary">
                请在左侧选择已有会话，或点击「新建问诊」创建会话。
              </Text>
            </div>
          }
        />
      </Content>
    )
  }

  // 当前阶段是否允许提交问诊消息：仅 inquiry 阶段允许
  const canSubmit = detail ? detail.current_stage === 'inquiry' : false

  const handleRefreshDetailForVersion = async (): Promise<number | undefined> => {
    const fresh = await refreshDetail()
    return fresh?.state_version
  }

  const submitContent = (content: string) => {
    if (!sessionId || !detail) return
    setLastSubmittedContent(content)
    void submit(
      sessionId,
      content,
      detail.state_version,
      handleRefreshDetailForVersion,
    ).then((ok) => {
      if (ok) {
        setLastSubmittedContent(null)
        // 提交成功后刷新会话详情（stage 可能推进，state_version 更新）
        void refreshDetail()
      }
    })
  }

  const retryLastSubmit = () => {
    if (!lastSubmittedContent) return
    submitContent(lastSubmittedContent)
  }

  return (
    <Layout style={{ background: 'var(--xh-bg-page)' }}>
      <Content style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <StreamStatus
          state={streamHook.connectionState}
          lastError={streamHook.lastError}
          runningAgent={runningAgent}
          onReconnect={streamHook.reconnect}
        />
        <div style={{ padding: 'var(--xh-space-l) var(--xh-space-l) 0' }}>
          <StepBar
            currentStage={detail?.current_stage ?? null}
            agentRuns={streamHook.agentRuns}
          />
        </div>
        {detail ? <PatientBar detail={detail} /> : null}
        <StageResultsPanel
          detail={detail}
          pendingReviewFormula={pendingReviewFormula}
          blockedIssues={blockedIssues}
          rollbackTarget={rollbackTarget}
        />
        {error ? (
          <div style={{ padding: 'var(--xh-space-l)' }}>
            <ErrorBanner error={error} onRetry={refreshDetail} />
          </div>
        ) : null}
        {loading && !detail ? (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
          </div>
        ) : null}
        <div
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            margin: 'var(--xh-space-l)',
            background: token.colorBgContainer,
            border: '1px solid var(--xh-border)',
            borderRadius: 'var(--xh-radius-card)',
            overflow: 'hidden',
            boxShadow: 'var(--xh-shadow-card)',
          }}
        >
          <MessageList
            messages={messages}
            loading={msgLoading}
            error={msgError}
            onRetry={() => sessionId && void loadMessages(sessionId)}
          />
          <MessageInput
            submitting={submitting}
            error={submitError}
            disabled={detail != null && !canSubmit}
            onRetry={retryLastSubmit}
            lastContent={lastSubmittedContent ?? undefined}
            onSubmit={submitContent}
          />
        </div>
      </Content>
    </Layout>
  )
}

export default ChatPanel
