/**
 * 悬壶 WebUI —— 医师登录页（阶段 1 加固）
 *
 * POST /api/v1/auth/login 换取 JWT，存入 sessionStorage（非 localStorage）。
 * 登录成功后跳转工作台；失败展示统一错误（不区分账号/密码错误）。
 */

import { useState } from 'react'
import { Alert, Button, Form, Input, Typography } from 'antd'
import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { ApiRequestError } from '@/api/errors'
import { login } from '@/api/index'
import { setAuthToken } from '@/api/auth'
import './styles/login.css'

const { Title, Text } = Typography

interface LoginFormValues {
  doctorId: string
  password: string
}

export function LoginPage() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const onFinish = async (values: LoginFormValues) => {
    setSubmitting(true)
    setError(null)
    try {
      const result = await login(values.doctorId, values.password)
      setAuthToken(result.access_token)
      navigate('/', { replace: true })
    } catch (caught: unknown) {
      setError(caught)
    } finally {
      setSubmitting(false)
    }
  }

  const errorMessage =
    error instanceof ApiRequestError
      ? error.userMessage
      : error instanceof Error
        ? error.message
        : '登录失败，请检查账号密码后重试'

  return (
    <main className="xh-login" data-testid="login-page">
      <section className="xh-login-card">
        <div className="xh-login-brand" aria-hidden="true">
          悬
        </div>
        <Title level={3}>悬壶 · 医师登录</Title>
        <Text type="secondary">中医 AI 辅助诊疗工作台</Text>

        <Form<LoginFormValues>
          layout="vertical"
          onFinish={(values) => void onFinish(values)}
          disabled={submitting}
          requiredMark={false}
        >
          <Form.Item
            name="doctorId"
            label="医师标识"
            rules={[{ required: true, message: '请输入医师标识' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="医师唯一标识（doctors.id）"
              autoComplete="username"
              data-testid="login-doctor-id"
            />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="登录密码"
              autoComplete="current-password"
              data-testid="login-password"
            />
          </Form.Item>

          {error ? (
            <Alert
              type="error"
              showIcon
              message={errorMessage}
              data-testid="login-error"
            />
          ) : null}

          <Button
            type="primary"
            htmlType="submit"
            block
            loading={submitting}
            data-testid="login-submit"
          >
            登录
          </Button>
        </Form>

        <div className="xh-login-footnote">
          <SafetyCertificateOutlined aria-hidden="true" />
          登录信息仅保存在本次会话中，工作台关闭后需重新登录。
        </div>
      </section>
    </main>
  )
}

export default LoginPage
