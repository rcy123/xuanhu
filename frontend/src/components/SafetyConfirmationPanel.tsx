import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Input, Spin, Tag, Typography } from 'antd'
import {
  CheckOutlined,
  CloseOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import {
  confirmSafetyAssertion,
  listSafetyAssertions,
  rejectSafetyAssertion,
} from '@/api/index'
import { ApiRequestError, TransportErrorCode } from '@/api/errors'
import type { SafetyFactAssertion, SafetyFactField } from '@/types/api'
import { generateIdempotencyKey } from '@/utils/id'

const { Text } = Typography

const FIELD_LABELS: Record<SafetyFactField, string> = {
  allergy: '过敏史',
  pregnancy: '妊娠状态',
  lactation: '哺乳状态',
  medications: '当前用药',
  major_conditions: '重大疾病史',
  contraindications: '禁忌信息',
  red_flag: '危险信号',
}

const SCALAR_LABELS: Record<string, string> = {
  pregnant: '已妊娠',
  not_pregnant: '未妊娠',
  possible: '可能妊娠',
  lactating: '正在哺乳',
  not_lactating: '未哺乳',
}

function assertionValueText(assertion: SafetyFactAssertion): string {
  const status = assertion.value.collection_status
  if (status === 'explicitly_none') return '明确回答：无'

  const values = assertion.value.values
  if (Array.isArray(values) && values.length > 0) {
    return values.map(String).join('、')
  }

  const scalar = assertion.value.value
  if (typeof scalar === 'string' && scalar.trim()) {
    return SCALAR_LABELS[scalar] ?? scalar
  }

  if (assertion.field_name === 'red_flag') {
    const category = assertion.value.category
    const severity = assertion.value.severity
    return [category, severity].filter((item) => typeof item === 'string').join(' · ') || '检测到危险信号'
  }

  return '已采集，内容需医生核对'
}

function errorText(error: unknown): string {
  if (error instanceof ApiRequestError) {
    return error.traceId
      ? `${error.userMessage}（trace_id: ${error.traceId}）`
      : error.userMessage
  }
  return error instanceof Error ? error.message : '操作失败，请重试'
}

function confirmableAssertions(items: SafetyFactAssertion[]): SafetyFactAssertion[] {
  // Red flags belong to the triage/recovery boundary, not generic fact review.
  return items.filter((item) => item.field_name !== 'red_flag')
}

interface PendingSafetyDecision {
  readonly sessionId: string
  readonly assertionId: string
  readonly action: 'confirm' | 'reject'
  readonly reviewerId: string
  readonly idempotencyKey: string
}

function isUncertainDecisionFailure(error: unknown): boolean {
  return error instanceof ApiRequestError && (
    error.code === TransportErrorCode.NETWORK_ERROR
    || error.code === TransportErrorCode.TIMEOUT
    || error.code === TransportErrorCode.ABORTED
    || error.code === TransportErrorCode.BAD_RESPONSE
  )
}

export interface SafetyConfirmationPanelProps {
  sessionId: string
  refreshKey: string | number
  enabled: boolean
  pendingHint?: boolean
  blocksFreeInput?: boolean
  /** null means not yet authoritative (loading or failed); 0 is a confirmed empty list. */
  onPendingChange?: (count: number | null) => void
  onChanged: () => Promise<void> | void
}

/**
 * Doctor-only boundary for model-extracted safety facts.
 *
 * The reviewer identifier is deliberately entered for every selected session;
 * it is never inferred or hard-coded because the backend writes it to audit
 * history as X-Doctor-Id.
 */
export function SafetyConfirmationPanel({
  sessionId,
  refreshKey,
  enabled,
  pendingHint = false,
  blocksFreeInput = false,
  onPendingChange,
  onChanged,
}: SafetyConfirmationPanelProps) {
  const [items, setItems] = useState<SafetyFactAssertion[]>([])
  const [reviewerId, setReviewerId] = useState('')
  const [loading, setLoading] = useState(false)
  const [actingId, setActingId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [pendingDecision, setPendingDecision] = useState<PendingSafetyDecision | null>(null)
  const mounted = useRef(true)
  const currentSessionId = useRef(sessionId)
  currentSessionId.current = sessionId

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    setReviewerId('')
    setItems([])
    setError(null)
    setSuccess(null)
    setPendingDecision(null)
  }, [sessionId])

  useEffect(() => {
    if (!enabled) {
      setItems([])
      onPendingChange?.(0)
      return
    }

    let cancelled = false
    setLoading(true)
    onPendingChange?.(null)
    setError(null)
    setSuccess(null)
    void listSafetyAssertions(sessionId, 'proposed')
      .then((result) => {
        if (cancelled) return
        const confirmable = confirmableAssertions(result.items)
        setItems(confirmable)
        onPendingChange?.(confirmable.length)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setItems([])
        setError(caught)
        onPendingChange?.(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [enabled, onPendingChange, refreshKey, sessionId])

  const decide = async (assertion: SafetyFactAssertion, action: 'confirm' | 'reject') => {
    const actor = reviewerId.trim()
    if (!actor || actingId) return
    const targetSessionId = sessionId
    const isCurrentSession = () => (
      mounted.current && currentSessionId.current === targetSessionId
    )
    const existingDecision = pendingDecision
    if (
      existingDecision
      && (
        existingDecision.sessionId !== targetSessionId
        || existingDecision.assertionId !== assertion.assertion_id
        || existingDecision.action !== action
        || existingDecision.reviewerId !== actor
      )
    ) return
    const decision: PendingSafetyDecision = existingDecision ?? Object.freeze({
      sessionId: targetSessionId,
      assertionId: assertion.assertion_id,
      action,
      reviewerId: actor,
      idempotencyKey: generateIdempotencyKey(),
    })
    if (!existingDecision) setPendingDecision(decision)

    setActingId(assertion.assertion_id)
    setError(null)
    setSuccess(null)
    try {
      if (action === 'confirm') {
        await confirmSafetyAssertion(
          targetSessionId,
          assertion.assertion_id,
          {},
          { doctorId: actor, idempotencyKey: decision.idempotencyKey },
        )
      } else {
        await rejectSafetyAssertion(
          targetSessionId,
          assertion.assertion_id,
          { reason_code: 'EXTRACTION_REJECTED' },
          { doctorId: actor, idempotencyKey: decision.idempotencyKey },
        )
      }
    } catch (caught: unknown) {
      if (!isCurrentSession()) return
      setError(caught)
      if (!isUncertainDecisionFailure(caught)) setPendingDecision(null)
      setActingId(null)
      return
    }

    if (!isCurrentSession()) return
    setPendingDecision(null)

    const remaining = items.filter((item) => item.assertion_id !== assertion.assertion_id)
    setItems(remaining)
    onPendingChange?.(remaining.length)
    setSuccess(action === 'confirm' ? '已确认并写入安全档案。' : '已驳回，系统将继续补问该项。')

    setLoading(true)
    try {
      const result = await listSafetyAssertions(targetSessionId, 'proposed')
      if (!isCurrentSession()) return
      const confirmable = confirmableAssertions(result.items)
      setItems(confirmable)
      onPendingChange?.(confirmable.length)
    } catch (caught: unknown) {
      if (!isCurrentSession()) return
      setError(new Error(`决定已提交，但刷新待确认列表失败：${errorText(caught)}`))
      onPendingChange?.(null)
    } finally {
      if (isCurrentSession()) setLoading(false)
    }

    if (!isCurrentSession()) return
    try {
      await onChanged()
    } catch (caught: unknown) {
      if (!isCurrentSession()) return
      setError(new Error(`决定已提交，但刷新问诊状态失败：${errorText(caught)}`))
    } finally {
      if (isCurrentSession()) setActingId(null)
    }
  }

  const retryLoad = () => {
    const targetSessionId = sessionId
    const isCurrentSession = () => (
      mounted.current && currentSessionId.current === targetSessionId
    )
    setLoading(true)
    setError(null)
    onPendingChange?.(null)
    void listSafetyAssertions(targetSessionId, 'proposed')
      .then((result) => {
        if (!isCurrentSession()) return
        const confirmable = confirmableAssertions(result.items)
        setItems(confirmable)
        onPendingChange?.(confirmable.length)
      })
      .catch((caught: unknown) => {
        if (isCurrentSession()) {
          setError(caught)
          onPendingChange?.(null)
        }
      })
      .finally(() => {
        if (isCurrentSession()) setLoading(false)
      })
  }

  if (!enabled) return null
  if (loading && items.length === 0) {
    return pendingHint ? (
      <div className="xh-safety-confirmation is-loading" data-testid="safety-confirmation-loading">
        <Spin size="small" />
        <Text>正在读取待确认的用药安全信息…</Text>
      </div>
    ) : null
  }
  if (items.length === 0 && error && pendingHint) {
    return (
      <section
        className="xh-safety-confirmation"
        aria-label="待确认的用药安全信息"
        data-testid="safety-confirmation-load-error"
      >
        <Alert
          type="error"
          showIcon
          message="待确认安全信息读取失败"
          description={errorText(error)}
          action={<Button size="small" onClick={retryLoad}>重试</Button>}
        />
      </section>
    )
  }
  if (items.length === 0) return null

  const hasReviewer = reviewerId.trim().length > 0

  return (
    <section
      className="xh-safety-confirmation"
      aria-label="待确认的用药安全信息"
      data-testid="safety-confirmation-panel"
    >
      <div className="xh-safety-confirmation-heading">
        <div>
          <Text strong>
            <SafetyCertificateOutlined aria-hidden="true" /> 待医生确认的安全信息
          </Text>
          <Text type="secondary">
            以下内容来自本轮回答，在医生确认前不会写入权威安全档案。
          </Text>
        </div>
        <Tag color="gold">{items.length} 项待确认</Tag>
      </div>

      {blocksFreeInput ? (
        <Alert
          type="warning"
          showIcon
          message="当前等待安全信息确认"
          description="请先确认或驳回下列事实；处理完成后系统会自动继续问诊。"
          data-testid="safety-confirmation-blocked-input"
        />
      ) : null}

      <div className="xh-safety-reviewer-field">
        <label htmlFor={`safety-reviewer-${sessionId}`}>复核医生标识（审计必填）</label>
        <Input
          id={`safety-reviewer-${sessionId}`}
          value={reviewerId}
          maxLength={128}
          autoComplete="off"
          placeholder="请输入工号或系统中的医生 ID"
          onChange={(event) => setReviewerId(event.target.value)}
          disabled={actingId != null || pendingDecision != null}
          data-testid="safety-reviewer-id"
        />
        <Text type="secondary">该标识将随确认结果写入审计记录，不会由前端代填。</Text>
      </div>

      <div className="xh-safety-assertion-list">
        {items.map((assertion) => {
          const busy = actingId === assertion.assertion_id
          const pendingThisAction = (action: 'confirm' | 'reject') => (
            pendingDecision == null
            || (
              pendingDecision.assertionId === assertion.assertion_id
              && pendingDecision.action === action
            )
          )
          return (
            <article className="xh-safety-assertion" key={assertion.assertion_id}>
              <div className="xh-safety-assertion-copy">
                <Text strong>{FIELD_LABELS[assertion.field_name]}</Text>
                <Text>{assertionValueText(assertion)}</Text>
              </div>
              <div className="xh-safety-assertion-actions">
                <Button
                  type="primary"
                  size="small"
                  icon={<CheckOutlined />}
                  loading={busy}
                  disabled={
                    !hasReviewer
                    || (actingId != null && !busy)
                    || !pendingThisAction('confirm')
                  }
                  onClick={() => void decide(assertion, 'confirm')}
                  data-testid={`safety-confirm-${assertion.assertion_id}`}
                >
                  确认
                </Button>
                <Button
                  danger
                  size="small"
                  icon={<CloseOutlined />}
                  disabled={
                    !hasReviewer
                    || actingId != null
                    || !pendingThisAction('reject')
                  }
                  onClick={() => void decide(assertion, 'reject')}
                  data-testid={`safety-reject-${assertion.assertion_id}`}
                >
                  驳回
                </Button>
              </div>
            </article>
          )
        })}
      </div>

      {error ? <Alert type="error" showIcon message={errorText(error)} /> : null}
      {success ? <Alert type="success" showIcon message={success} /> : null}
    </section>
  )
}

export default SafetyConfirmationPanel
