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

  return (
    <div
      data-testid="review-actions-bar"
      style={{
        padding: 'var(--xh-space-l)',
        borderTop: '1px solid var(--xh-border)',
        borderBottom: '1px solid var(--xh-border)',
        background: 'var(--xh-bg-card)',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--xh-space-m)',
      }}
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
          <Space size="middle">
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
            {!blocked ? (
              <Button
                icon={<EditOutlined />}
                onClick={onModify}
                loading={submitting}
                data-testid="review-modify-btn"
              >
                修改处方
              </Button>
            ) : null}
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
