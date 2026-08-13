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
import {
  ArrowRightOutlined,
  MenuOutlined,
  ReadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useEffect, useMemo, useState, type ReactElement } from 'react'
import {
  clearAuthSession,
  getAuthUser,
  isAuthenticated,
  setAuthExpiredHandler,
} from '@/api/auth'
import { LoginPage } from '@/pages/LoginPage'
import { DocsPage } from '@/pages/DocsPage'
import { AdminUsersPage } from '@/pages/AdminUsersPage'
import { useSessions } from '@/hooks/useSessions'
import { useSessionDetail } from '@/hooks/useSessionDetail'
import { useMessages } from '@/hooks/useMessages'
import { useCommandReconciliation } from '@/hooks/useCommandReconciliation'
import { SessionSider } from '@/components/SessionSider'
import { ChatPanel } from '@/components/ChatPanel'
import './styles/workbench.css'
import './pages/styles/home.css'

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
          <img src="/xuanhu-mark.png" alt="" />
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
      <header className="xh-home-nav">
        <div className="xh-home-nav-inner">
          <div className="xh-home-nav-brand">
            <div className="xh-home-nav-mark" aria-hidden="true">
              <img src="/xuanhu-mark.png" alt="" />
            </div>
            <div>
              <div className="xh-home-nav-name">悬壶</div>
              <span className="xh-home-nav-sub">Xuanhu Clinical</span>
            </div>
          </div>
          <nav className="xh-home-nav-actions" aria-label="顶部导航">
            <span className="xh-home-nav-version">v1.0</span>
          </nav>
        </div>
      </header>

      <main className="xh-home-main">
        <span className="xh-home-vert" aria-hidden="true">
          中医临床决策辅助 · 有据可循
        </span>

        <div className="xh-home-copy">
          <span className="xh-home-eyebrow">中医 AI 临床助理</span>
          <h1 className="xh-home-headline">
            悬壶<em>工作台</em>
          </h1>
          <p className="xh-home-body">
            请访问工作台，将问诊、辨证、安全审核与医师复核放在一个清晰、可追溯的临床工作流中。
            愿你笔下每一味，皆有据可循。
          </p>
          <div className="xh-home-cta-row">
            <Link className="xh-home-cta-primary" to="/workbench">
              进入工作台
              <ArrowRightOutlined className="xh-home-cta-arrow" />
            </Link>
            <Link className="xh-home-cta-secondary" to="/docs">
              <ReadOutlined className="xh-home-cta-doc-icon" />
              使用与设计文档
            </Link>
          </div>
        </div>

        <div className="xh-home-art" aria-hidden="true">
          <div className="xh-home-art-frame">
            <span className="xh-home-art-index">№ 001</span>
            <div className="xh-home-art-mat">
              <img
                className="xh-home-art-img"
                src="/xuanhu-brand.png"
                alt=""
              />
            </div>
            <span className="xh-home-art-seal">
              <i>悬</i>
              <i>壶</i>
            </span>
          </div>
          <span className="xh-home-art-caption">品牌图鉴 · 悬壶</span>
        </div>
      </main>

      <section className="xh-home-pillars" aria-label="工作流要点">
        <article className="xh-home-pillar">
          <span className="xh-home-pillar-num">壹 · 问诊对话</span>
          <h2 className="xh-home-pillar-title">主诉、四诊与辨证同步留痕</h2>
          <p className="xh-home-pillar-body">
            多轮问诊自动归档主诉与四诊要点，辨证思路逐句可追溯。
          </p>
        </article>
        <article className="xh-home-pillar">
          <span className="xh-home-pillar-num">贰 · 安全审核</span>
          <h2 className="xh-home-pillar-title">红线与禁忌内置</h2>
          <p className="xh-home-pillar-body">
            妊娠、肝肾等红线独立成栏，医师复核后再下笔。
          </p>
        </article>
        <article className="xh-home-pillar">
          <span className="xh-home-pillar-num">叁 · 病历收尾</span>
          <h2 className="xh-home-pillar-title">从初诊到处方，一处完成</h2>
          <p className="xh-home-pillar-body">
            方剂、医嘱、随访问卷一键归档，电子病历不再拼贴。
          </p>
        </article>
      </section>

      <footer className="xh-home-foot">
        <div className="xh-home-foot-meta">
          <span className="xh-home-foot-seal" aria-hidden="true">壶</span>
          <span>悬壶 · 中医 AI 临床助理</span>
        </div>
        <span>辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。</span>
      </footer>
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
      <Route path="/docs" element={<DocsPage />} />
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
