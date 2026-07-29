import { useRef, useState } from 'react'
import { Alert, Button, Space, Tag, Typography } from 'antd'
import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import { advanceSession, recoverSession } from '@/api'
import { ApiRequestError } from '@/api/errors'
import type { AdvanceData, RecoveryData, SessionDetail } from '@/types/api'
import {
  canAdvanceLangGraph,
  langGraphDisposition,
  type LangGraphDisposition,
} from '@/utils/agent'
import { generateIdempotencyKey } from '@/utils/id'

const { Text } = Typography

const DISPOSITION_META: Record<
  LangGraphDisposition,
  { label: string; color: string; description: string }
> = {
  ready: {
    label: 'ready',
    color: 'success',
    description: '确定性 Triage 与 Completeness Gate 均已通过。',
  },
  needs_input: {
    label: 'needs_input',
    color: 'processing',
    description: '仍需补充问诊信息，当前不能推进临床推理。',
  },
  triage_hold: {
    label: 'triage_hold',
    color: 'error',
    description: '红旗分诊已阻断自动流程，请按分诊要求人工处理。',
  },
  manual_required: {
    label: 'manual_required',
    color: 'warning',
    description: '存在冲突、停滞或执行故障，需要人工恢复或处置。',
  },
}

interface LangGraphAdvanceBarProps {
  detail: SessionDetail
  onAdvanced: (result: AdvanceData) => Promise<void> | void
  onRefresh?: () => Promise<unknown> | void
  onRecovered?: (result: RecoveryData) => Promise<void> | void
}

interface PendingAdvanceRequest {
  sessionId: string
  idempotencyKey: string
  stateVersion: number
}

export function LangGraphAdvanceBar({
  detail,
  onAdvanced,
  onRefresh,
  onRecovered,
}: LangGraphAdvanceBarProps) {
  const [submitting, setSubmitting] = useState(false)
  const [recoverySubmitting, setRecoverySubmitting] = useState(false)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const pendingAdvance = useRef<PendingAdvanceRequest | null>(null)
  const pendingRecoveryKey = useRef<string | null>(null)

  if (detail.agent_runtime !== 'langgraph') return null

  const canAdvance = canAdvanceLangGraph(detail)
  const isProductStageAction = detail.current_stage === 'safety'
    || detail.current_stage === 'record'
  const canRunStageAction = detail.status === 'active'
    && detail.recovery_status === 'normal'
    && isProductStageAction
  const disposition = langGraphDisposition(detail) ?? 'manual_required'
  const canRecover = disposition === 'manual_required'
    && detail.status !== 'terminated'
    && !detail.blocked_reason?.startsWith('triage_hold:')
    && (detail.status === 'blocked' || detail.recovery_status !== 'normal')
  const dispositionMeta = DISPOSITION_META[disposition]
  const unresolved = detail.read_model.unresolved
  const handleAdvance = async () => {
    setSubmitting(true)
    setError(null)
    if (pendingAdvance.current?.sessionId !== detail.session_id) {
      pendingAdvance.current = {
        sessionId: detail.session_id,
        idempotencyKey: generateIdempotencyKey(),
        stateVersion: detail.state_version,
      }
    }
    const request = pendingAdvance.current
    try {
      const result = await advanceSession(
        request.sessionId,
        {},
        { idempotencyKey: request.idempotencyKey, stateVersion: request.stateVersion },
      )
      pendingAdvance.current = null
      await onAdvanced(result)
    } catch (caught: unknown) {
      const commandError = caught instanceof ApiRequestError ? caught : new ApiRequestError({
        code: 'ADVANCE_FAILED',
        userMessage: '推进失败，请稍后重试',
        status: 0,
        retryable: true,
        cause: caught,
      })
      const outcomeUncertain = commandError.status === 0
        || commandError.status >= 500
        || commandError.code === 'HTTP_COMMAND_RECOVERY_REQUIRED'
        || commandError.code === 'BAD_RESPONSE'
      if (!outcomeUncertain) pendingAdvance.current = null
      if (commandError.code === 'INVALID_STATE_VERSION') {
        try {
          await onRefresh?.()
        } catch {
          // Preserve the original command error; the clinician can retry refresh separately.
        }
      }
      setError(commandError)
    } finally {
      setSubmitting(false)
    }
  }

  const handleRecover = async () => {
    setRecoverySubmitting(true)
    setError(null)
    pendingRecoveryKey.current ??= generateIdempotencyKey()
    try {
      const result = await recoverSession(
        detail.session_id,
        { action: 'retry_current_stage' },
        { idempotencyKey: pendingRecoveryKey.current },
      )
      pendingRecoveryKey.current = null
      await onRecovered?.(result)
    } catch (caught: unknown) {
      setError(caught instanceof ApiRequestError ? caught : new ApiRequestError({
        code: 'RECOVERY_FAILED',
        userMessage: '恢复失败，请按错误提示人工处置',
        status: 0,
        retryable: true,
        cause: caught,
      }))
    } finally {
      setRecoverySubmitting(false)
    }
  }

  const hint = detail.current_stage === 'safety'
    ? '推理草案已持久化，可执行确定性 Safety 硬门禁；前端不会绕过安全审核。'
    : detail.current_stage === 'record'
      ? '医师复核已通过，可从权威处方、安全结果与复核引用确定性生成病历。'
      : canAdvance
      ? '红旗与问诊完备性门禁均已通过，可进入辨证与方药草案。'
      : '需先完成问诊，并通过红旗与完备性门禁后才能推进。'

  const actionLabel = detail.current_stage === 'safety'
    ? '执行安全审核'
    : detail.current_stage === 'record'
      ? '生成病历'
      : '进入辨证开方'

  return (
    <div data-testid="langgraph-advance-bar" className="xh-runtime-control">
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Space wrap>
          <Text strong>流程状态</Text>
          <Tag color={dispositionMeta.color} data-testid="langgraph-disposition">
            {dispositionMeta.label}
          </Tag>
          <Text type="secondary">{dispositionMeta.description}</Text>
        </Space>
        <Text type="secondary">{hint}</Text>
        <div data-testid="langgraph-read-model-summary">
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text strong data-testid="langgraph-graph-revision">
              权威 Read Model · 图修订 {detail.read_model.graph.revision}
              {detail.read_model.graph.status
                ? ` · ${detail.read_model.graph.status}`
                : ''}
            </Text>
            {unresolved.length > 0 ? (
              <>
                <Text type="warning">未解决项（{unresolved.length}）</Text>
                <ul
                  aria-label="LangGraph 未解决项"
                  data-testid="langgraph-unresolved-items"
                  style={{ margin: 0, paddingInlineStart: 24 }}
                >
                  {unresolved.map((item, index) => (
                    <li
                      key={`${item.source}:${item.kind}:${item.key}:${index}`}
                      data-testid="langgraph-unresolved-item"
                    >
                      <Text code>{item.source}</Text>
                      {' · '}
                      <Text code>{item.kind}</Text>
                      {' · '}
                      <Text code>{item.key}</Text>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <Text type="secondary" data-testid="langgraph-unresolved-empty">
                未解决项：无
              </Text>
            )}
          </Space>
        </div>
        {detail.read_model.review_required ? (
          <Alert
            type="info"
            showIcon
            message="医师复核要求已从权威 Read Model 恢复"
            description="刷新页面不会丢失待复核草案；只有后端 Safety 与 Doctor Review 硬门禁完成后才会开放确认操作。"
            data-testid="langgraph-review-restored"
          />
        ) : null}
        {error ? <Alert type="error" showIcon message={error.userMessage} /> : null}
        {canRecover ? (
          <Alert
            type="warning"
            showIcon
            message="LangGraph 控制游标需要恢复"
            description="恢复只重建当前运行阶段，不会切换到 Legacy，也不会跳过 Safety 或医师复核硬门禁。"
            action={(
              <Button
                icon={<ReloadOutlined />}
                loading={recoverySubmitting}
                onClick={() => void handleRecover()}
                data-testid="langgraph-recover-button"
              >
                恢复当前阶段
              </Button>
            )}
            data-testid="langgraph-recovery-required"
          />
        ) : null}
        {detail.current_stage === 'inquiry' || isProductStageAction ? (
          <Button
            type="primary"
            icon={<ArrowRightOutlined />}
            disabled={detail.current_stage === 'inquiry' ? !canAdvance : !canRunStageAction}
            loading={submitting}
            onClick={() => void handleAdvance()}
            data-testid="langgraph-advance-button"
          >
            {actionLabel}
          </Button>
        ) : null}
      </Space>
    </div>
  )
}
