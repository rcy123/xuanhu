/**
 * 悬壶 WebUI —— 医师确认操作区（P8-4）
 *
 * 在 review/pending_review 阶段展示确认/修改/否决按钮。
 * 阻断态（safety_review 未通过或存在 blocker/high issue）下隐藏确认/修改按钮。
 */

import { Button, Modal, Space } from 'antd'
import {
  CheckOutlined,
  EditOutlined,
  CloseOutlined,
  QuestionCircleOutlined,
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
  // 仅 review 阶段且 pending_review 为 true 时显示
  if (detail.current_stage !== 'review' || !detail.pending_review) return null

  const blocked = isReviewBlocked(detail, blockedIssues)
  const hasFormula = pendingReviewFormula?.composition && pendingReviewFormula.composition.length > 0

  const handleConfirmClick = () => {
    Modal.confirm({
      title: '确认处方',
      content: '确认处方后系统将进入病历生成阶段，请确认处方无误。',
      okText: '确认',
      cancelText: '取消',
      okButtonProps: { disabled: submitting },
      onOk: onConfirm,
    })
  }

  const handleRequestMoreInfoClick = () => {
    if (!onRequestMoreInfo) return
    Modal.confirm({
      title: '退回补充问诊',
      content: '当前处方及其下游安全结果将失效，会话将返回问诊阶段。确认继续吗？',
      okText: '确认退回',
      cancelText: '取消',
      okButtonProps: { disabled: submitting },
      onOk: onRequestMoreInfo,
    })
  }

  return (
    <div
      data-testid="review-actions-bar"
      className="xh-review-actions"
    >
      {error ? (
        <ErrorBanner error={error} onRetry={onRetry} />
      ) : null}

      {!hasFormula ? (
        <div style={{ color: 'var(--xh-text-secondary)', fontSize: 13 }}>
          暂无待确认处方
        </div>
      ) : (
        <div>
          <div style={{ fontSize: 13, marginBottom: 'var(--xh-space-m)', color: 'var(--xh-text-secondary)' }}>
            ⚠ 请仔细审核以上处方内容
          </div>
          <Space size="small" wrap>
            {detail.agent_runtime === 'langgraph' && onRequestMoreInfo ? (
              <Button
                icon={<QuestionCircleOutlined />}
                onClick={handleRequestMoreInfoClick}
                loading={submitting}
                data-testid="review-request-more-info-btn"
              >
                补充问诊
              </Button>
            ) : null}
            <Button
              icon={<CloseOutlined />}
              danger
              ghost
              onClick={onReject}
              loading={submitting}
              data-testid="review-reject-btn"
            >
              否决，重新开方
            </Button>
            {/* 修改处方是阻断态的合法出路：提交后重新执行 Safety 硬门禁，
                通过后才能确认（确认按钮仍隐藏）。否则医生被困在
                recover→advance 循环里无法改方子。 */}
            <Button
              icon={<EditOutlined />}
              onClick={onModify}
              loading={submitting}
              data-testid="review-modify-btn"
            >
              修改处方
            </Button>
            {!blocked ? (
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={handleConfirmClick}
                loading={submitting}
                data-testid="review-confirm-btn"
                style={{ background: 'var(--xh-primary)' }}
              >
                确认处方
              </Button>
            ) : null}
          </Space>
        </div>
      )}
    </div>
  )
}

export default ReviewActionsBar
