import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { isReviewBlocked } from '@/utils/review'
import { ReviewActionsBar } from './ReviewActionsBar'
import { ApiRequestError } from '@/api/errors'
import type { SessionDetail, Formula, SafetyIssue } from '@/types/api'
import { Modal } from 'antd'
import { emptySessionReadModel } from '@/utils/readModel'

function makeDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_id: 's1',
    status: 'active',
    current_stage: 'review',
    pending_review: true,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 5,
    agent_runtime: 'legacy',
    read_model: emptySessionReadModel('legacy', 5),
    patient_info: {},
    created_at: '2026-07-04T10:00:00+08:00',
    updated_at: '2026-07-04T10:00:00+08:00',
    safety_review: { passed: true, issues: [] },
    ...overrides,
  }
}

function makeFormula(): Formula {
  return {
    name: '待确认方',
    composition: [
      { herb: '麻黄', dose: 6, unit: 'g' },
      { herb: '桂枝', dose: 6, unit: 'g' },
    ],
  }
}

describe('isReviewBlocked', () => {
  it('安全通过且无 blocker/high issue → 不阻断', () => {
    const detail = makeDetail({
      safety_review: { passed: true, issues: [{ severity: 'warning', message: '提醒' }] },
    })
    expect(isReviewBlocked(detail)).toBe(false)
  })

  it('safety_review.passed === false → 阻断', () => {
    const detail = makeDetail({
      safety_review: { passed: false, issues: [{ severity: 'blocker', message: '十八反' }] },
    })
    expect(isReviewBlocked(detail)).toBe(true)
  })

  it('存在 blocker issue → 阻断', () => {
    const detail = makeDetail({
      safety_review: { passed: true, issues: [{ severity: 'blocker', message: '超剂量' }] },
    })
    expect(isReviewBlocked(detail)).toBe(true)
  })

  it('存在 high issue → 阻断', () => {
    const detail = makeDetail({
      safety_review: { passed: true, issues: [{ severity: 'high', message: '配伍禁忌' }] },
    })
    expect(isReviewBlocked(detail)).toBe(true)
  })

  it('blockedIssues 参数优先于 detail.safety_review.issues', () => {
    const detail = makeDetail({
      safety_review: { passed: true, issues: [] },
    })
    const blocked: SafetyIssue[] = [{ severity: 'blocker', message: '外部阻断' }]
    expect(isReviewBlocked(detail, blocked)).toBe(true)
  })

  it('blocked_reason=safety_rule_blocked 视为阻断（即使 SSE issues 为空）', () => {
    const detail = makeDetail({
      current_stage: 'blocked',
      blocked_reason: 'safety_rule_blocked',
      safety_review: { passed: true, issues: [] },
    })
    expect(isReviewBlocked(detail)).toBe(true)
    expect(isReviewBlocked(detail, null)).toBe(true)
  })
})

describe('ReviewActionsBar', () => {
  afterEach(() => {
    cleanup()
  })

  it('非 review 阶段或 pending_review=false 时不渲染', () => {
    const detail = makeDetail({ current_stage: 'inquiry', pending_review: false })
    const { container } = render(
      <ReviewActionsBar
        detail={detail}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('review 阶段且 pending_review=true 显示三按钮', () => {
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('review-confirm-btn')).toBeInTheDocument()
    expect(screen.getByTestId('review-modify-btn')).toBeInTheDocument()
    expect(screen.getByTestId('review-reject-btn')).toBeInTheDocument()
  })

  it('阻断态隐藏确认按钮但保留修改处方（修改后重新执行 Safety 硬门禁）', () => {
    const detail = makeDetail({
      safety_review: { passed: false, issues: [{ severity: 'blocker', message: '十八反' }] },
    })
    render(
      <ReviewActionsBar
        detail={detail}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.queryByTestId('review-confirm-btn')).not.toBeInTheDocument()
    expect(screen.getByTestId('review-modify-btn')).toBeInTheDocument()
    expect(screen.getByTestId('review-reject-btn')).toBeInTheDocument()
  })

  it('blocked/safety_rule_blocked 状态露出修改/否决出口（确认仍隐藏）', () => {
    const detail = makeDetail({
      current_stage: 'blocked',
      status: 'blocked',
      blocked_reason: 'safety_rule_blocked',
      pending_review: false,
      safety_review: { passed: true, issues: [] },
    })
    render(
      <ReviewActionsBar
        detail={detail}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    // 确认按钮必须隐藏：blocked/safety_rule_blocked 后端不允许 confirm。
    expect(screen.queryByTestId('review-confirm-btn')).not.toBeInTheDocument()
    expect(screen.getByTestId('review-modify-btn')).toBeInTheDocument()
    expect(screen.getByTestId('review-reject-btn')).toBeInTheDocument()
  })

  it('点击确认触发二次确认弹窗', () => {
    const onConfirm = vi.fn()
    // Mock Modal.confirm 以避免 DOM 弹窗
    const confirmSpy = vi.spyOn(Modal, 'confirm').mockImplementation(({ onOk }) => {
      onOk?.()
      return { destroy: vi.fn(), update: vi.fn() } as ReturnType<typeof Modal.confirm>
    })

    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={onConfirm}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByTestId('review-confirm-btn'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(onConfirm).toHaveBeenCalled()

    confirmSpy.mockRestore()
  })

  it('点击修改触发 onModify', () => {
    const onModify = vi.fn()
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={onModify}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('review-modify-btn'))
    expect(onModify).toHaveBeenCalled()
  })

  it('点击否决触发 onReject', () => {
    const onReject = vi.fn()
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={onReject}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('review-reject-btn'))
    expect(onReject).toHaveBeenCalled()
  })

  it('LangGraph 会话可退回补充问诊', () => {
    const onRequestMoreInfo = vi.fn()
    render(
      <ReviewActionsBar
        detail={makeDetail({
          agent_runtime: 'langgraph',
          read_model: emptySessionReadModel('langgraph', 5),
        })}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRequestMoreInfo={onRequestMoreInfo}
        onRetry={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByTestId('review-request-more-info-btn'))
    expect(onRequestMoreInfo).toHaveBeenCalled()
  })

  it('submitting 时按钮 loading', () => {
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={true}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    const confirmBtn = screen.getByTestId('review-confirm-btn') as HTMLButtonElement
    // AntD Button loading 态：disabled 属性
    expect(confirmBtn.disabled || confirmBtn.classList.contains('ant-btn-loading')).toBeTruthy()
  })

  it('error 显示 ErrorBanner + 重试按钮', () => {
    const onRetry = vi.fn()
    const error = new ApiRequestError({
      code: 'SESSION_BUSY',
      userMessage: '会话忙',
      status: 409,
      retryable: true,
    })
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={makeFormula()}
        submitting={false}
        error={error}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={onRetry}
      />,
    )
    expect(screen.getByText(/会话正在处理其他请求/)).toBeInTheDocument()
    expect(screen.getByTestId('error-retry')).toBeInTheDocument()
  })

  it('无 pendingReviewFormula 时显示提示文字', () => {
    render(
      <ReviewActionsBar
        detail={makeDetail()}
        pendingReviewFormula={null}
        submitting={false}
        error={null}
        onConfirm={vi.fn()}
        onModify={vi.fn()}
        onReject={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByText(/暂无待确认处方/)).toBeInTheDocument()
  })
})
