import { useEffect, useState } from 'react'
import { Input, Modal, Typography } from 'antd'
import type { ButtonProps } from 'antd'

const { Text } = Typography

export interface RequestMoreInfoModalProps {
  open: boolean
  submitting: boolean
  onCancel: () => void
  onSubmit: (feedback: string) => void
}

export function RequestMoreInfoModal({
  open,
  submitting,
  onCancel,
  onSubmit,
}: RequestMoreInfoModalProps) {
  const [feedback, setFeedback] = useState('')

  useEffect(() => {
    if (open) setFeedback('')
  }, [open])

  return (
    <Modal
      title="补充辨证信息"
      open={open}
      onCancel={onCancel}
      onOk={() => onSubmit(feedback.trim())}
      okText="保存并进入辨证"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{
        disabled: submitting || !feedback.trim(),
        'data-testid': 'request-more-info-submit-btn',
      } as ButtonProps}
      destroyOnHidden
    >
      <div style={{ marginBottom: 'var(--xh-space-m)' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          保存后将进入辨证阶段，补充内容会带入新的辨证和开方；当前处方与安全审核结果将失效。
        </Text>
      </div>
      <Text strong style={{ display: 'block', marginBottom: 6, fontSize: 12 }}>
        补充信息 <Text type="danger">*</Text>
      </Text>
      <Input.TextArea
        placeholder="如：舌质淡红、苔薄白，脉浮；近期未服用其他药物"
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
        rows={4}
        data-testid="request-more-info-feedback"
      />
    </Modal>
  )
}

export default RequestMoreInfoModal
