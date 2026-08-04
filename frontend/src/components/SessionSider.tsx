/**
 * 悬壶 WebUI —— 侧边栏容器（P8-2）
 *
 * 组合 useSessions + SessionList + CreateSessionModal。
 * 创建会话成功后通过 onCreated(id) 通知父层（用于路由跳转）。
 * 支持折叠：收起后每个历史会话缩成小块（患者姓名首字）。
 */

import { useState } from 'react'
import { Button, Layout, Tooltip, Typography } from 'antd'
import {
  HistoryOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons'
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
  const [collapsed, setCollapsed] = useState(false)

  const handleCreate = async (body: SessionCreateRequest): Promise<string> => {
    const id = await sessionsHook.createSession(body)
    onCreated(id)
    return id
  }

  return (
    <Sider
      width={280}
      collapsedWidth={64}
      collapsed={collapsed}
      className="xh-session-sider"
    >
      <div className="xh-session-sider-heading">
        {collapsed ? null : (
          <div>
            <Text className="xh-section-kicker">CONSULTATIONS</Text>
            <Text strong>问诊会话</Text>
          </div>
        )}
        {collapsed ? <HistoryOutlined aria-hidden="true" /> : null}
        <Tooltip title={collapsed ? '展开会话栏' : '收起会话栏'} placement="right">
          <Button
            type="text"
            size="small"
            className="xh-session-collapse"
            aria-label={collapsed ? '展开会话栏' : '收起会话栏'}
            data-testid="session-collapse-toggle"
            onClick={() => setCollapsed((prev) => !prev)}
          >
            {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          </Button>
        </Tooltip>
      </div>
      <SessionList
        sessions={sessionsHook.sessions}
        loading={sessionsHook.loading}
        error={sessionsHook.error}
        selectedId={selectedId}
        collapsed={collapsed}
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
