/**
 * 悬壶 WebUI —— 新建会话弹窗（P8-2）
 *
 * 主诉必填（1-5000 字符），可选患者信息（name/gender/age）。
 * 提交调 useSessions.createSession，成功后关闭。
 */

import { useState } from 'react'
import { Modal, Form, Input, Select, InputNumber, Typography } from 'antd'
import type { SessionCreateRequest } from '@/types/api'

const { Text } = Typography

interface CreateSessionModalProps {
  open: boolean
  creating: boolean
  onClose: () => void
  onSubmit: (body: SessionCreateRequest) => Promise<string>
}

interface FormValues {
  chief_complaint?: string
  patient_name?: string
  gender?: 'male' | 'female' | 'unknown'
  age?: number | null
}

export function CreateSessionModal({ open, creating, onClose, onSubmit }: CreateSessionModalProps) {
  const [form] = Form.useForm()
  const [error, setError] = useState<string | null>(null)

  const handleFinish = async (values: FormValues) => {
    try {
      setError(null)
      const body: SessionCreateRequest = {
        chief_complaint: values.chief_complaint?.trim() || null,
        patient_info: {
          name: values.patient_name?.trim() || null,
          gender: values.gender ?? 'unknown',
          age: values.age ?? null,
        },
      }
      await onSubmit(body)
      form.resetFields()
      onClose()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '创建会话失败')
    }
  }

  const handleCancel = () => {
    form.resetFields()
    setError(null)
    onClose()
  }

  return (
    <Modal
      title="新建问诊会话"
      open={open}
      onOk={() => form.submit()}
      onCancel={handleCancel}
      confirmLoading={creating}
      okText="创建"
      cancelText="取消"
      destroyOnHidden
      width={640}
      className="xh-create-session-modal"
    >
      <Form
        form={form}
        layout="vertical"
        style={{ marginTop: 16 }}
        onFinish={handleFinish}
      >
        <Form.Item
          name="chief_complaint"
          label="主诉"
          rules={[
            { required: true, message: '请输入主诉内容' },
            { max: 5000, message: '主诉最多 5000 字符' },
          ]}
        >
          <Input.TextArea
            rows={3}
            placeholder="患者主诉，如：头痛三天，伴有恶寒发热"
            maxLength={5000}
            showCount
          />
        </Form.Item>
        <div className="xh-patient-fields">
          <Form.Item name="patient_name" label="患者姓名">
            <Input placeholder="选填" maxLength={100} />
          </Form.Item>
          <Form.Item name="gender" label="性别">
            <Select placeholder="未知" allowClear>
              <Select.Option value="male">男</Select.Option>
              <Select.Option value="female">女</Select.Option>
              <Select.Option value="unknown">未知</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item name="age" label="年龄">
            <InputNumber min={0} max={130} placeholder="选填" style={{ width: '100%' }} />
          </Form.Item>
        </div>
        <div className="xh-runtime-note" data-testid="runtime-managed-notice">
          运行时由后端发布配置决定
        </div>
        {error ? (
          <Text type="danger" style={{ fontSize: 12 }}>
            {error}
          </Text>
        ) : null}
      </Form>
    </Modal>
  )
}

export default CreateSessionModal
