import { useRef, useState } from 'react'
import { Alert, Button, Space, Typography } from 'antd'
import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import { advanceSession, recoverSession } from '@/api'
import { ApiRequestError } from '@/api/errors'
import { isAsyncCommandAccepted } from '@/types/api'
import type {
  AdvanceData,
  AsyncCommandAccepted,
  RecoveryData,
  SessionDetail,
} from '@/types/api'
import {
  canAdvanceLangGraph,
  langGraphDisposition,
} from '@/utils/agent'
import { generateIdempotencyKey } from '@/utils/id'

const { Text } = Typography

interface LangGraphAdvanceBarProps {
  detail: SessionDetail
  onAdvanced: (result: AdvanceData) => Promise<void> | void
  onRefresh?: () => Promise<unknown> | void
  onRecovered?: (result: RecoveryData) => Promise<void> | void
  onRecordGenerationStart?: () => void
  onRecordGenerationFailed?: () => void
  /** R7: 收到已接受（202）推进命令时登记对账（上层持有 reconciler）。 */
  onCommandAccepted?: (accepted: AsyncCommandAccepted, idempotencyKey: string) => void
  /** R7: 是否存在未决推进命令（有界对账中），用于禁用按钮。 */
  pending?: boolean
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
  onRecordGenerationStart,
  onRecordGenerationFailed,
  onCommandAccepted,
  pending = false,
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
  // reject 后回到 syndrome（重新辨证开方）也需要推进按钮，否则前端无路可走。
  const canRestartReasoning = detail.current_stage === 'syndrome'
    || detail.current_stage === 'modification'
  const canRunStageAction = detail.status === 'active'
    && detail.recovery_status === 'normal'
    && isProductStageAction
  const disposition = langGraphDisposition(detail) ?? 'manual_required'
  const canRecover = disposition === 'manual_required'
    && detail.status !== 'terminated'
    && !detail.blocked_reason?.startsWith('triage_hold:')
    && (detail.status === 'blocked' || detail.recovery_status !== 'normal')
  const handleAdvance = async () => {
    const generatingRecord = detail.current_stage === 'record'
    if (generatingRecord) onRecordGenerationStart?.()
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
      if (isAsyncCommandAccepted(result)) {
        // R7: 202 仅表示已接受，登记对账；不乐观刷新，终态由 reconciler 处理
        // （成功刷新读模型；失败展示有界错误）。pending prop 保持按钮禁用。
        onCommandAccepted?.(result, request.idempotencyKey)
        return
      }
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
      if (generatingRecord) onRecordGenerationFailed?.()
      setError(commandError)
    } finally {
      // submitting 仅覆盖「请求在途」；已接受（202）后的对账窗口由 pending prop
      // 负责禁用按钮，避免本组件局部 submitting 在对账终态后残留为 true。
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

  const hint = disposition === 'triage_hold'
    ? '发现需人工处理的风险项，自动流程已暂停。'
    : disposition === 'manual_required'
      ? '当前流程需要人工处置后继续。'
      : detail.current_stage === 'safety'
        ? '诊疗草案已生成，请执行安全审核。'
        : detail.current_stage === 'record'
          ? '医师复核已通过，可生成病历。'
          : canRestartReasoning
            ? '上一版方药已退回，请根据反馈重新生成诊疗方案。'
            : canAdvance
              ? '问诊信息已满足条件，可进入辨证开方。'
              : '请补充上方未收集信息后继续。'

  const actionLabel = detail.current_stage === 'safety'
    ? '执行安全审核'
    : detail.current_stage === 'record'
      ? '生成病历'
      : '进入辨证开方'

  return (
    <div data-testid="langgraph-advance-bar" className="xh-runtime-control">
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <div
          className={`xh-next-action-hint is-${disposition}`}
          data-testid="langgraph-next-action"
        >
          <Text>{hint}</Text>
        </div>
        {error ? <Alert type="error" showIcon message={error.userMessage} /> : null}
        {canRecover ? (
          <Alert
            type="warning"
            showIcon
            message="当前流程需要恢复"
            description="恢复只重新执行当前步骤，不会跳过安全审核或医师复核。"
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
        {detail.current_stage === 'inquiry' || canRestartReasoning || isProductStageAction ? (
          <Button
            type="primary"
            icon={<ArrowRightOutlined />}
            disabled={
              pending
              || (
                detail.current_stage === 'inquiry'
                  ? !canAdvance
                  : canRestartReasoning
                    ? detail.status !== 'active' || detail.recovery_status !== 'normal'
                    : !canRunStageAction
              )
            }
            loading={submitting || pending}
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
