import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Space, Spin, Typography } from 'antd'
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
  // 推进命令「从点击到终态」的持续锁定：202 仅表示已接受，命令仍在处理。
  // submitting 只覆盖请求在途（finally 即清）；settling 覆盖整个对账窗口，
  // 直到上层 pending（reconciler 未决命令）从 true 回落为 false（终态/预算耗尽）
  // 才解除，避免 202 后按钮闪回可点击。
  const [settling, setSettling] = useState(false)
  const pendingAdvance = useRef<PendingAdvanceRequest | null>(null)
  const pendingRecoveryKey = useRef<string | null>(null)
  const prevPendingRef = useRef<boolean>(pending)

  // 对账终态：pending 由 true → false 表示该命令已 settle，解除按钮锁定。
  useEffect(() => {
    if (prevPendingRef.current === true && pending === false) {
      setSettling(false)
    }
    prevPendingRef.current = pending
  }, [pending])

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
    setSettling(true)  // 点击即锁定按钮（含对账窗口）
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
        // R7: 202 仅表示已接受，登记对账；不乐观刷新，终态由 reconciler 处理。
        // settling 保持 true，按钮持续转圈锁定，直到上层 pending 回落（终态）。
        onCommandAccepted?.(result, request.idempotencyKey)
        return
      }
      await onAdvanced(result)
      setSettling(false)
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
      setSettling(false)
    } finally {
      // submitting 仅覆盖「请求在途」；202 后的对账窗口由 settling + pending 负责。
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

  // 命令进行中（请求在途 + 对账窗口）：按钮隐藏，显示分阶段进行中提示。
  const advanceBusy = settling || pending
  // 分阶段文案：辨证 → 开方 → 生成完成
  const hasSyndrome = detail.syndrome_result != null
  const hasFormula = (
    detail.base_formula != null
    || detail.modified_formula != null
    || (detail.base_formula_alternatives != null && detail.base_formula_alternatives.length > 0)
  )
  const progressText = !hasSyndrome
    ? '正在辨证…'
    : !hasFormula
      ? '辨证完成，正在开方…'
      : '正在生成诊疗结果…'

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
          advanceBusy ? (
            // 生成中：按钮隐藏，仅展示进行中提示，避免重复点击。
            <div className="xh-advance-progress" data-testid="langgraph-advance-progress">
              <Spin size="small" />
              <Text type="secondary">{progressText}</Text>
            </div>
          ) : (
            <Button
              type="primary"
              icon={<ArrowRightOutlined />}
              disabled={
                detail.current_stage === 'inquiry'
                  ? !canAdvance
                  : canRestartReasoning
                    ? detail.status !== 'active' || detail.recovery_status !== 'normal'
                    : !canRunStageAction
              }
              loading={submitting}
              onClick={() => void handleAdvance()}
              data-testid="langgraph-advance-button"
            >
              {actionLabel}
            </Button>
          )
        ) : null}
      </Space>
    </div>
  )
}
