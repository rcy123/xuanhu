/**
 * 悬壶 WebUI —— 工作台 App Shell（P8-2）
 *
 * 按 UI 设计文档 §3 信息架构搭建：
 * - 左侧 280px 侧边栏（会话列表 + 新建会话）
 * - 右侧主区域（品牌标题 + 全局免责声明 + 步骤条 + 问诊对话 + 输入栏）
 *
 * P8-2 实现真实 API 驱动的会话列表、创建会话、消息历史、问诊输入。
 * 路由：/ 与 /workbench（空会话态）、/sessions/:id（选中会话详情）。
 * 不接 SSE、不做 review/病历（P8-3/P8-4）。
 */

import { Button, Typography } from 'antd'
import { MenuOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useEffect, useMemo, useState, type ReactElement } from 'react'
import {
  clearAuthSession,
  getAuthUser,
  isAuthenticated,
  setAuthExpiredHandler,
} from '@/api/auth'
import { LoginPage } from '@/pages/LoginPage'
import { AdminUsersPage } from '@/pages/AdminUsersPage'
import { useSessions } from '@/hooks/useSessions'
import { useSessionDetail } from '@/hooks/useSessionDetail'
import { useMessages } from '@/hooks/useMessages'
import { useCommandReconciliation } from '@/hooks/useCommandReconciliation'
import { SessionSider } from '@/components/SessionSider'
import { ChatPanel } from '@/components/ChatPanel'
import './styles/workbench.css'

const { Title, Text } = Typography

function DisclaimerFooter() {
  return (
    <footer className="xh-disclaimer">
      <SafetyCertificateOutlined aria-hidden="true" />
      辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。
    </footer>
  )
}

function BrandHeader({ onOpenNavigation }: { onOpenNavigation: () => void }) {
  return (
    <header className="xh-brand-header">
      <div className="xh-brand-cluster">
        <Button
          className="xh-mobile-menu"
          type="text"
          icon={<MenuOutlined />}
          aria-label="打开会话导航"
          onClick={onOpenNavigation}
        />
        <div className="xh-brand-mark" aria-hidden="true">
          悬
        </div>
        <div className="xh-brand-copy">
          <div className="xh-brand-title-row">
            <Title level={4}>悬壶</Title>
            <Text>Xuanhu</Text>
          </div>
          <Text type="secondary">中医 AI 辅助诊疗工作台</Text>
        </div>
      </div>
      <div className="xh-header-context" aria-label="当前工作区">
        <span className="xh-header-context-dot" />
        临床工作区
      </div>
    </header>
  )
}

function Workbench() {
  const sessionsHook = useSessions()
  const detailHook = useSessionDetail()
  const navigate = useNavigate()
  const params = useParams<{ id?: string }>()
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [sessionSiderCollapsed, setSessionSiderCollapsed] = useState(false)

  const selectedId = params.id ?? null
  // R7：异步命令终态对账由本层持有，供 useMessages 登记已接受（202）消息命令；
  // 终态刷新处理器由 ChatPanel 通过 setHandlers 注册（拥有读模型刷新能力）。
  const commandReconciler = useCommandReconciliation()
  const messagesHook = useMessages({
    onCommandAccepted: (accepted, idempotencyKey) => {
      if (selectedId) commandReconciler.registerAccepted(accepted, selectedId, idempotencyKey)
    },
  })
  const refreshSessions = sessionsHook.refresh

  // 路由切换后刷新一次列表，确保新建或直接访问的会话能在侧栏同步出来。
  useEffect(() => {
    if (selectedId) {
      void refreshSessions()
    }
  }, [selectedId, refreshSessions])

  const handleSelect = (id: string) => {
    setNavigationOpen(false)
    navigate(`/sessions/${id}`)
  }

  const handleCreated = (id: string) => {
    setNavigationOpen(false)
    navigate(`/sessions/${id}`)
  }

  const chatPanel = useMemo(
    () => (
      <ChatPanel
        sessionId={selectedId}
        detailHook={detailHook}
        messagesHook={messagesHook}
        commandReconciler={commandReconciler}
      />
    ),
    [selectedId, detailHook, messagesHook, commandReconciler],
  )

  return (
    <div className="xh-app-shell">
      <BrandHeader onOpenNavigation={() => setNavigationOpen(true)} />
      <div className="xh-app-body">
        <aside
          className={`xh-session-rail${sessionSiderCollapsed ? ' is-collapsed' : ''}${navigationOpen ? ' is-open' : ''}`}
          aria-label="问诊会话导航"
        >
          <SessionSider
            sessionsHook={sessionsHook}
            selectedId={selectedId}
            onSelect={handleSelect}
            onCreated={handleCreated}
            collapsed={sessionSiderCollapsed}
            onCollapsedChange={setSessionSiderCollapsed}
          />
        </aside>
        <button
          type="button"
          className={`xh-navigation-scrim${navigationOpen ? ' is-open' : ''}`}
          aria-label="关闭会话导航"
          onClick={() => setNavigationOpen(false)}
        />
        {chatPanel}
      </div>
      <DisclaimerFooter />
    </div>
  )
}

function PlaceholderHome() {
  return (
    <div className="xh-home">
      <div className="xh-home-card">
        <div className="xh-home-mark" aria-hidden="true">悬</div>
        <Text className="xh-home-eyebrow">XUANHU CLINICAL COPILOT</Text>
        <Title level={2}>悬壶工作台</Title>
        <Text type="secondary">
          请访问工作台，将问诊、辨证、安全审核与医师复核放在一个清晰、可追溯的临床工作流中。
        </Text>
        <Link className="xh-home-action" to="/workbench">进入工作台</Link>
      </div>
    </div>
  )
}

/** 认证守卫：未登录跳转登录页。 */
function RequireAuth({ children }: { children: ReactElement }) {
  const location = useLocation()
  if (!isAuthenticated()) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  // 管理员 token 不具有临床问诊权限；避免其在工作台中遇到服务端 403。
  if (getAuthUser()?.role === 'admin') {
    return <Navigate to="/admin/users" replace />
  }
  return children
}

/**
 * 管理端的本地体验守卫。服务端 /admin/* 接口仍是权限的唯一权威来源。
 * 已知为普通医师的会话保留其 token 并回到工作台；缺失或损坏身份信息的会话则
 * 清空，避免旧版前端遗留的 token 被错误视为管理员会话。
 */
function RequireAdmin({ children }: { children: ReactElement }) {
  const location = useLocation()
  if (!isAuthenticated()) {
    return <Navigate to="/admin/login" replace state={{ from: location.pathname }} />
  }

  const user = getAuthUser()
  if (!user) {
    clearAuthSession()
    return (
      <Navigate
        to="/admin/login"
        replace
        state={{ authError: '登录会话信息不完整，请重新登录。' }}
      />
    )
  }
  if (user.role !== 'admin') {
    return <Navigate to="/workbench" replace />
  }
  return children
}

export default function App() {
  useEffect(() => {
    // 认证失效（401）时跳转登录页。
    setAuthExpiredHandler(() => {
      const isAdminPath = window.location.pathname.startsWith('/admin')
      const loginPath = isAdminPath ? '/admin/login' : '/login'
      if (window.location.pathname !== loginPath) {
        window.location.assign(loginPath)
      }
    })
    return () => setAuthExpiredHandler(null)
  }, [])

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/admin/login" element={<LoginPage mode="admin" />} />
      <Route path="/" element={<PlaceholderHome />} />
      <Route
        path="/admin"
        element={(
          <RequireAdmin>
            <Navigate to="/admin/users" replace />
          </RequireAdmin>
        )}
      />
      <Route
        path="/admin/users"
        element={(
          <RequireAdmin>
            <AdminUsersPage />
          </RequireAdmin>
        )}
      />
      <Route
        path="/workbench"
        element={(
          <RequireAuth>
            <Workbench />
          </RequireAuth>
        )}
      />
      <Route
        path="/sessions/:id"
        element={(
          <RequireAuth>
            <Workbench />
          </RequireAuth>
        )}
      />
    </Routes>
  )
}
