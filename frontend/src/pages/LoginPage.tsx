/**
 * 悬壶 WebUI —— 医师登录页（升级：editorial 分屏 + 品牌资产融合）
 *
 * POST /api/v1/auth/login 换取 JWT，存入 sessionStorage（非 localStorage）。
 * 登录成功后跳转工作台；失败展示统一错误（不区分账号/密码错误）。
 *
 * 设计要点（按 design-taste-frontend skill）：
 * - 左侧品牌面板：水墨渐变 + 真实 logo 资产 + 编辑式排版（Noto Serif SC）
 * - 右侧表单面板：纸质卡片 + 精炼表单 + 朱砂主按钮
 * - CTA 对比度：#FFFEFA 文字 on #C8442C 按钮 = ~6.0:1（WCAG AA）
 * - Reduced motion 已通过全局 @media 在 tokens.css 中处理
 */

import { useState } from 'react'
import { Alert, Button, Form, Input, Typography } from 'antd'
import {
  ArrowRightOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons'
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
      {/* 左侧：水墨品牌面板 */}
      <aside className="xh-login-brand-panel" aria-hidden={false}>
        <span className="xh-login-brand-vert" aria-hidden="true">
          悬壶济世 · 大医精诚
        </span>

        <header className="xh-login-brand-head">
          <div className="xh-login-mark">
            <img src="/xuanhu-mark.png" alt="" />
          </div>
          <div className="xh-login-mark-text">
            <span className="xh-login-mark-name">悬壶</span>
            <span className="xh-login-mark-sub">Xuanhu Clinical</span>
          </div>
        </header>

        <div className="xh-login-brand-art">
          <img src="/xuanhu-brand.png" alt="悬壶品牌图：葫芦水墨、松针绿叶、朱砂方印" />
          <span className="xh-login-brand-art-seal" aria-hidden="true">
            <i>悬</i>
            <i>壶</i>
          </span>
        </div>

        <div className="xh-login-brand-quote">
          <div className="xh-login-brand-quote-line" aria-hidden="true" />
          <div>
            <h2 className="xh-login-brand-title">
              {isAdminLogin ? '悬壶 · 账户中枢' : '悬壶 · 临床工作台'}
            </h2>
            <p className="xh-login-brand-sub">
              问诊、辨证、安全审核与医师复核，归置在同一份可追溯的工作流中。
              愿你笔下每一味，皆有据可循。
            </p>
          </div>
        </div>

        <footer className="xh-login-brand-foot">
          <span className="xh-login-brand-seal" aria-hidden="true">壶</span>
          <span>中医智慧 · 临床决策辅助</span>
        </footer>
      </aside>

      {/* 右侧：表单面板 */}
      <section className="xh-login-form-panel">
        {isAdminLogin ? (
          <a className="xh-login-back-link" href="/login">
            ← 医师入口
          </a>
        ) : (
          <a className="xh-login-back-link" href="/admin/login">
            管理员入口 →
          </a>
        )}

        <div className="xh-login-card">
          <span className="xh-login-card-seal" aria-hidden="true">
            壶
          </span>

          <div className="xh-login-mode-row">
            <span className="xh-login-mode-dot" aria-hidden="true" />
            <span>{isAdminLogin ? 'ADMIN LOGIN' : 'DOCTOR LOGIN'}</span>
            <span className="xh-login-mode-divider" />
            <span>v 1.0</span>
          </div>

          <Title level={1} className="xh-login-title">
            {isAdminLogin ? '管理员登录' : '医师登录'}
          </Title>
          <Text className="xh-login-subtitle">
            {isAdminLogin
              ? '账户与医师用户管理'
              : '中医 AI 辅助诊疗工作台'}
          </Text>

          {authError ? (
            <Alert
              type="warning"
              showIcon
              message={authError}
              style={{ marginBottom: 16 }}
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
                placeholder="登录名（拼音 / 工号）"
                autoComplete="username"
                size="large"
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
                size="large"
                data-testid="login-password"
              />
            </Form.Item>

            {error ? (
              <Alert
                type="error"
                showIcon
                message={errorMessage}
                style={{ marginBottom: 12 }}
                data-testid="login-error"
              />
            ) : null}

            <Button
              type="primary"
              htmlType="submit"
              block
              loading={submitting}
              size="large"
              icon={<ArrowRightOutlined />}
              iconPlacement="end"
              data-testid="login-submit"
            >
              {isAdminLogin ? '进入账户中枢' : '进入工作台'}
            </Button>
          </Form>

          <div className="xh-login-footnote">
            <SafetyCertificateOutlined
              className="xh-login-footnote-icon"
              aria-hidden="true"
            />
            <span>登录信息仅保存在本次会话中，关闭浏览器后需重新登录。</span>
          </div>
        </div>
      </section>
    </main>
  )
}

export default LoginPage