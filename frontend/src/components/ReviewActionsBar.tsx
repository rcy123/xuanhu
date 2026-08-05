/** 医师处方复核操作区。 */

import { Button, Modal } from 'antd'
import {
  CheckOutlined,
  EditOutlined,
  CloseOutlined,
  QuestionCircleOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import type { SessionDetail, Formula, SafetyIssue } from '@/types/api'
import type { ApiRequestError } from '@/api/errors'
import { isReviewBlocked } from '@/utils/review'
import { ErrorBanner } from './ErrorBanner'

export interface ReviewActionsBarProps {
  detail: SessionDetail
  pendingReviewFormula: Formula | null
  blockedIssues?: SafetyIssue[] | null
  submitting: boolean
  error: ApiRequestError | null
  onConfirm: () => void
  onModify: () => void
  onReject: () => void
  onRequestMoreInfo?: () => void
  onRetry: () => void
}

export function ReviewActionsBar({
  detail,
  pendingReviewFormula,
  blockedIssues,
  submitting,
  error,
  onConfirm,
  onModify,
  onReject,
  onRequestMoreInfo,
  onRetry,
}: ReviewActionsBarProps) {
  // 渲染条件与后端 _prepared_from_current 对齐：
  // - review/pending_review：常规复核
  // - blocked/safety_rule_blocked：安全拦截后仍可修改处方（modify → 二次安全审核），
  //   否则医生被困在 recover→advance 循环里无法调剂量。
  const blockedSafety = detail.current_stage === 'blocked'
    && detail.blocked_reason === 'safety_rule_blocked'
  if (
    !blockedSafety
    && (detail.current_stage !== 'review' || !detail.pending_review)
  ) {
    return null
  }

  const blocked = isReviewBlocked(detail, blockedIssues)
  const hasFormula = pendingReviewFormula?.composition && pendingReviewFormula.composition.length > 0

  const handleConfirmClick = () => {
    Modal.confirm({
      title: '确认处方',
      content: '确认后将进入病历生成阶段，请确认处方无误。',
      okText: '确认',
      cancelText: '取消',
      okButtonProps: { disabled: submitting },
      onOk: onConfirm,
    })
  }

  const handleRequestMoreInfoClick = () => {
    if (!onRequestMoreInfo) return
    onRequestMoreInfo()
  }

  return (
    <div data-testid="review-actions-bar" className="xh-review-actions">
      {error ? <ErrorBanner error={error} onRetry={onRetry} /> : null}

      {!hasFormula ? (
        <div style={{ color: 'var(--xh-text-secondary)', fontSize: 13 }}>
          暂无待确认处方
        </div>
      ) : (
        <div>
          <div className="xh-review-actions-heading">
            <span className="xh-review-actions-icon" aria-hidden="true">
              <SafetyCertificateOutlined />
            </span>
            <div>
              <strong>处方复核</strong>
              <span>核对方药、剂量与安全审核结论</span>
            </div>
          </div>
          <div className="xh-review-actions-grid">
            {detail.agent_runtime === 'langgraph' && onRequestMoreInfo ? (
              <Button
                icon={<QuestionCircleOutlined />}
                onClick={handleRequestMoreInfoClick}
                loading={submitting}
                data-testid="review-request-more-info-btn"
                className="xh-review-action-button is-return"
                title="补充信息后重新辨证开方"
              >
                补充信息
              </Button>
            ) : null}
            <Button
              icon={<CloseOutlined />}
              danger
              ghost
              onClick={onReject}
              loading={submitting}
              data-testid="review-reject-btn"
              className="xh-review-action-button is-reject"
              title="回到辨证阶段，重新开方"
            >
              否决并重开
            </Button>
            <Button
              icon={<EditOutlined />}
              onClick={onModify}
              loading={submitting}
              data-testid="review-modify-btn"
              className="xh-review-action-button is-modify"
              title="修改后重新执行安全审核"
            >
              修改并复审
            </Button>
            {!blocked ? (
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={handleConfirmClick}
                loading={submitting}
                data-testid="review-confirm-btn"
                className="xh-review-action-button is-confirm"
                title="确认当前安全审核通过的处方"
              >
                确认处方
              </Button>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}

export default ReviewActionsBar
