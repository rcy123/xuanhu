/**
 * 悬壶 WebUI —— 侧边栏容器（P8-2）
 *
 * 组合 useSessions + SessionList + CreateSessionModal。
 * 创建会话成功后通过 onCreated(id) 通知父层（用于路由跳转）。
 */

import { useState } from 'react'
import { Layout, theme, Typography } from 'antd'
import type { UseSessionsResult } from '@/hooks/useSessions'
import type { SessionCreateRequest } from '@/types/api'
import { SessionList } from './SessionList'
import { CreateSessionModal } from './CreateSessionModal'

const { Sider } = Layout
const { Text } = Typography

interface SessionSiderProps {
  sessionsHook: UseSessionsResult
  selectedId: string | null
  onSelect: (id: string) => void
  onCreated: (id: string) => void
}

export function SessionSider({ sessionsHook, selectedId, onSelect, onCreated }: SessionSiderProps) {
  const { token } = theme.useToken()
  const [modalOpen, setModalOpen] = useState(false)

  const handleCreate = async (body: SessionCreateRequest): Promise<string> => {
    const id = await sessionsHook.createSession(body)
    onCreated(id)
    return id
  }

  return (
    <Sider
      width={280}
      style={{
        background: token.colorBgContainer,
        borderRight: '1px solid var(--xh-border)',
        overflow: 'auto',
        padding: 'var(--xh-space-l)',
      }}
    >
      <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontSize: 18 }}>🌿</span>
        <Text
          strong
          style={{ fontFamily: 'var(--xh-font-serif)', color: 'var(--xh-primary)' }}
        >
          悬壶会话
        </Text>
      </div>
      <SessionList
        sessions={sessionsHook.sessions}
        loading={sessionsHook.loading}
        error={sessionsHook.error}
        selectedId={selectedId}
        onSelect={onSelect}
        onRefresh={sessionsHook.refresh}
        onCreate={() => setModalOpen(true)}
      />
      <CreateSessionModal
        open={modalOpen}
        creating={sessionsHook.creating}
        onClose={() => setModalOpen(false)}
        onSubmit={handleCreate}
      />
    </Sider>
  )
}

export default SessionSider