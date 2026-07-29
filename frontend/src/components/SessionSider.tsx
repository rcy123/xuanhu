/**
 * 悬壶 WebUI —— 侧边栏容器（P8-2）
 *
 * 组合 useSessions + SessionList + CreateSessionModal。
 * 创建会话成功后通过 onCreated(id) 通知父层（用于路由跳转）。
 */

import { useState } from 'react'
import { Layout, Typography } from 'antd'
import { HistoryOutlined } from '@ant-design/icons'
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
  const [modalOpen, setModalOpen] = useState(false)

  const handleCreate = async (body: SessionCreateRequest): Promise<string> => {
    const id = await sessionsHook.createSession(body)
    onCreated(id)
    return id
  }

  return (
    <Sider
      width={280}
      className="xh-session-sider"
    >
      <div className="xh-session-sider-heading">
        <div>
          <Text className="xh-section-kicker">CONSULTATIONS</Text>
          <Text strong>问诊会话</Text>
        </div>
        <HistoryOutlined aria-hidden="true" />
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
