/**
 * 悬壶 WebUI —— 否决 Modal（P8-4）
 *
 * 医师填写 feedback 后调用 POST /review(action=reject)。
 */

import { useState, useEffect } from 'react'
import { Modal, Input, Typography } from 'antd'
import type { ButtonProps } from 'antd'

const { Text } = Typography

export interface RejectModalProps {
  open: boolean
  submitting: boolean
  onCancel: () => void
  onSubmit: (feedback: string) => void
}

export function RejectModal({ open, submitting, onCancel, onSubmit }: RejectModalProps) {
  const [feedback, setFeedback] = useState('')

  useEffect(() => {
    if (open) setFeedback('')
  }, [open])

  return (
    <Modal
      title="否决处方"
      open={open}
      onCancel={onCancel}
      onOk={() => onSubmit(feedback.trim())}
      okText="确认否决"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{
        danger: true,
        disabled: submitting,
        'data-testid': 'reject-submit-btn',
      } as ButtonProps}
      destroyOnHidden
    >
      <div style={{ marginBottom: 'var(--xh-space-m)' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          否决后系统将回退到开方阶段重新生成处方。请填写否决原因（可选）：
        </Text>
      </div>
      <Input.TextArea
        placeholder="如：辨证结论存疑，患者舌象不符合风寒表证"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={4}
        data-testid="reject-feedback"
      />
    </Modal>
  )
}

export default RejectModal
