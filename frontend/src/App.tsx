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
import { Link, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { useSessions } from '@/hooks/useSessions'
import { useSessionDetail } from '@/hooks/useSessionDetail'
import { useMessages } from '@/hooks/useMessages'
import { SessionSider } from '@/components/SessionSider'
import { ChatPanel } from '@/components/ChatPanel'
import './styles/workbench.css'

const { Title, Text } = Typography

function DisclaimerBar() {
  return (
    <div className="xh-disclaimer">
      <SafetyCertificateOutlined aria-hidden="true" />
      辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。
    </div>
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
  const messagesHook = useMessages()
  const refreshSessions = sessionsHook.refresh
  const navigate = useNavigate()
  const params = useParams<{ id?: string }>()
  const [navigationOpen, setNavigationOpen] = useState(false)

  const selectedId = params.id ?? null

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
      />
    ),
    [selectedId, detailHook, messagesHook],
  )

  return (
    <div className="xh-app-shell">
      <BrandHeader onOpenNavigation={() => setNavigationOpen(true)} />
      <DisclaimerBar />
      <div className="xh-app-body">
        <aside
          className={`xh-session-rail${navigationOpen ? ' is-open' : ''}`}
          aria-label="问诊会话导航"
        >
          <SessionSider
            sessionsHook={sessionsHook}
            selectedId={selectedId}
            onSelect={handleSelect}
            onCreated={handleCreated}
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

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<PlaceholderHome />} />
      <Route path="/workbench" element={<Workbench />} />
      <Route path="/sessions/:id" element={<Workbench />} />
    </Routes>
  )
}
