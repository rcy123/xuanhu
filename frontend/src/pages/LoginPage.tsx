/**
 * 悬壶 WebUI —— 医师登录页（阶段 1 加固）
 *
 * POST /api/v1/auth/login 换取 JWT，存入 sessionStorage（非 localStorage）。
 * 登录成功后跳转工作台；失败展示统一错误（不区分账号/密码错误）。
 */

import { useState } from 'react'
import { Alert, Button, Form, Input, Typography } from 'antd'
import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons'
import { useLocation, useNavigate } from 'react-router-dom'
import { ApiRequestError } from '@/api/errors'
import { login } from '@/api/index'
import { setAuthSession } from '@/api/auth'
import './styles/login.css'

const { Title, Text } = Typography

interface LoginFormValues {
  username: string
  password: string
}

export interface LoginPageProps {
  /** 管理员入口仅改变文案与登录后的目的地；服务端仍负责角色校验。 */
  mode?: 'doctor' | 'admin'
}

interface LoginLocationState {
  authError?: string
}

export function LoginPage({ mode = 'doctor' }: LoginPageProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const isAdminLogin = mode === 'admin'
  const authError = (location.state as LoginLocationState | null)?.authError

  const onFinish = async (values: LoginFormValues) => {
    setSubmitting(true)
    setError(null)
    try {
      const result = await login(values.username, values.password)
      if (!result.user || (result.user.role !== 'doctor' && result.user.role !== 'admin')) {
        throw new Error('登录响应缺少账户角色信息，请联系系统管理员。')
      }
      setAuthSession(result.access_token, result.user)
      // 管理员无论从哪个登录入口认证，都进入账户管理；普通医师进入临床工作台。
      navigate(result.user.role === 'admin' ? '/admin/users' : '/workbench', { replace: true })
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
    <main
      className="xh-login"
      data-testid={isAdminLogin ? 'admin-login-page' : 'login-page'}
    >
      <section className="xh-login-card">
        <div className="xh-login-brand" aria-hidden="true">
          悬
        </div>
        <Title level={3}>悬壶 · {isAdminLogin ? '管理员登录' : '医师登录'}</Title>
        <Text type="secondary">
          {isAdminLogin ? '账户与医师用户管理' : '中医 AI 辅助诊疗工作台'}
        </Text>

        {authError ? (
          <Alert
            type="warning"
            showIcon
            message={authError}
            data-testid="login-auth-notice"
          />
        ) : null}

        <Form<LoginFormValues>
          layout="vertical"
          onFinish={(values) => void onFinish(values)}
          disabled={submitting}
          requiredMark={false}
        >
          <Form.Item
            name="username"
            label="登录名"
            rules={[{ required: true, message: '请输入登录名' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="登录名（拼音/工号）"
              autoComplete="username"
              data-testid="login-username"
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
          登录信息仅保存在本次会话中，关闭浏览器后需重新登录。
        </div>
      </section>
    </main>
  )
}

export default LoginPage
