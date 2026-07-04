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

import { Layout, Typography } from 'antd'
import { Link, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { useEffect, useMemo } from 'react'
import { useSessions } from '@/hooks/useSessions'
import { useSessionDetail } from '@/hooks/useSessionDetail'
import { useMessages } from '@/hooks/useMessages'
import { SessionSider } from '@/components/SessionSider'
import { ChatPanel } from '@/components/ChatPanel'

const { Header } = Layout
const { Title, Text } = Typography

function DisclaimerBar() {
  return (
    <div
      style={{
        background: 'var(--xh-border)',
        color: 'var(--xh-text)',
        padding: '4px var(--xh-space-l)',
        fontSize: 12,
        textAlign: 'center',
      }}
    >
      辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。
    </div>
  )
}

function BrandHeader() {
  return (
    <Header
      style={{
        background: 'var(--xh-bg-card)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 var(--xh-space-l)',
        borderBottom: '1px solid var(--xh-border)',
        height: 56,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 20 }}>🌿</span>
        <Title
          level={4}
          style={{
            margin: 0,
            fontFamily: 'var(--xh-font-serif)',
            color: 'var(--xh-primary)',
          }}
        >
          悬壶
        </Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Xuanhu
        </Text>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        中医 AI 辅助诊疗工作台
      </Text>
    </Header>
  )
}

function Workbench() {
  const sessionsHook = useSessions()
  const detailHook = useSessionDetail()
  const messagesHook = useMessages()
  const refreshSessions = sessionsHook.refresh
  const navigate = useNavigate()
  const params = useParams<{ id?: string }>()

  const selectedId = params.id ?? null

  // 路由切换后刷新一次列表，确保新建或直接访问的会话能在侧栏同步出来。
  useEffect(() => {
    if (selectedId) {
      void refreshSessions()
    }
  }, [selectedId, refreshSessions])

  const handleSelect = (id: string) => {
    navigate(`/sessions/${id}`)
  }

  const handleCreated = (id: string) => {
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
    <Layout style={{ height: '100vh' }}>
      <BrandHeader />
      <DisclaimerBar />
      <Layout>
        <SessionSider
          sessionsHook={sessionsHook}
          selectedId={selectedId}
          onSelect={handleSelect}
          onCreated={handleCreated}
        />
        {chatPanel}
      </Layout>
    </Layout>
  )
}

function PlaceholderHome() {
  return (
    <div style={{ padding: 32, textAlign: 'center' }}>
      <Title level={3} style={{ fontFamily: 'var(--xh-font-serif)' }}>
        悬壶工作台
      </Title>
      <Text type="secondary">
        请访问 <Link to="/workbench">工作台</Link> 开始问诊。
      </Text>
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
