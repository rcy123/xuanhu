/**
 * 悬壶 WebUI —— 侧边栏会话列表（P8-2）
 *
 * 新建会话按钮 + 会话列表项（患者摘要 + 时间 + 状态 Tag）。
 * 选中高亮；空态/加载态/错误态（含重试）。
 */

import { Button, Empty, Spin, Tag, Typography } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import type { SessionListItem } from '@/types/api'
import { formatTime, patientSummary } from '@/utils/format'
import { sessionTag } from '@/utils/sessionTag'
import { ErrorBanner } from './ErrorBanner'

const { Text } = Typography

interface SessionListProps {
  sessions: SessionListItem[]
  loading: boolean
  error: unknown
  selectedId: string | null
  onSelect: (id: string) => void
  onRefresh: () => void
  onCreate: () => void
}

function SessionItem({
  session,
  selected,
  onSelect,
}: {
  session: SessionListItem
  selected: boolean
  onSelect: (id: string) => void
}) {
  const tag = sessionTag(session)
  return (
    <div
      role="button"
      tabIndex={0}
      data-session-id={session.session_id}
      data-selected={selected || undefined}
      onClick={() => onSelect(session.session_id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(session.session_id)
        }
      }}
      style={{
        padding: '10px 12px',
        borderRadius: 'var(--xh-radius-card)',
        cursor: 'pointer',
        border: selected ? '2px solid var(--xh-secondary)' : '1px solid var(--xh-border)',
        background: selected ? 'var(--xh-bg-card)' : 'transparent',
        marginBottom: 8,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text
          strong
          style={{ fontSize: 13, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}
        >
          {patientSummary(session) || '未命名患者'}
        </Text>
        <Tag color={tag.color} style={{ margin: 0, fontSize: 11 }}>
          {tag.label}
        </Tag>
      </div>
      <div style={{ marginTop: 4 }}>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {session.chief_complaint
            ? session.chief_complaint.length > 18
              ? `${session.chief_complaint.slice(0, 18)}…`
              : session.chief_complaint
            : '—'}
        </Text>
      </div>
      <div style={{ marginTop: 2 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {formatTime(session.updated_at)}
          </Text>
          <Tag
            color={session.agent_runtime === 'langgraph' ? 'geekblue' : 'default'}
            style={{ margin: 0, fontSize: 10, lineHeight: '16px' }}
            data-testid={`runtime-${session.session_id}`}
          >
            {session.agent_runtime === 'langgraph' ? 'LangGraph v2' : 'Legacy'}
          </Tag>
        </div>
      </div>
    </div>
  )
}

export function SessionList({
  sessions,
  loading,
  error,
  selectedId,
  onSelect,
  onRefresh,
  onCreate,
}: SessionListProps) {
  return (
    <div data-testid="session-list" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Button type="primary" icon={<PlusOutlined />} block style={{ marginBottom: 12 }} onClick={onCreate}>
        新建问诊
      </Button>
      <div style={{ flex: 1, overflow: 'auto' }}>
        {loading && sessions.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : null}
        {error && !loading ? (
          <ErrorBanner error={error as never} onRetry={onRefresh} />
        ) : null}
        {!loading && !error && sessions.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary">暂无会话</Text>}
          />
        ) : null}
        {sessions.map((s) => (
          <SessionItem
            key={s.session_id}
            session={s}
            selected={s.session_id === selectedId}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  )
}

export default SessionList
