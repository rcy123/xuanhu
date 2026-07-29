/**
 * 悬壶 WebUI —— 侧边栏会话列表（P8-2）
 *
 * 新建会话按钮 + 会话列表项（患者摘要 + 时间 + 状态 Tag）。
 * 选中高亮；空态/加载态/错误态（含重试）。
 */

import { useMemo, useState } from 'react'
import { Button, Empty, Input, Spin, Tag, Typography } from 'antd'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons'
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
      className="xh-session-item"
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
    >
      <div className="xh-session-item-main">
        <Text
          strong
          className="xh-session-patient"
        >
          {patientSummary(session) || '未命名患者'}
        </Text>
        <Tag color={tag.color} className="xh-session-status">
          {tag.label}
        </Tag>
      </div>
      <div className="xh-session-complaint">
        <Text type="secondary">
          {session.chief_complaint
            ? session.chief_complaint.length > 18
              ? `${session.chief_complaint.slice(0, 18)}…`
              : session.chief_complaint
            : '—'}
        </Text>
      </div>
      <div className="xh-session-meta">
          <Text type="secondary">
            {formatTime(session.updated_at)}
          </Text>
          <Tag
            color={session.agent_runtime === 'langgraph' ? 'geekblue' : 'default'}
            className="xh-runtime-tag"
            data-testid={`runtime-${session.session_id}`}
          >
            {session.agent_runtime === 'langgraph' ? 'LangGraph v2' : 'Legacy'}
          </Tag>
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
  const [query, setQuery] = useState('')
  const filteredSessions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    if (!normalized) return sessions
    return sessions.filter((session) => {
      const searchable = [
        patientSummary(session),
        session.chief_complaint ?? '',
        session.current_stage,
      ].join(' ').toLocaleLowerCase()
      return searchable.includes(normalized)
    })
  }, [query, sessions])

  return (
    <div data-testid="session-list" className="xh-session-list">
      <Button
        type="primary"
        icon={<PlusOutlined />}
        block
        size="large"
        className="xh-create-session"
        onClick={onCreate}
      >
        新建问诊
      </Button>
      <Input
        allowClear
        prefix={<SearchOutlined />}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索患者或主诉"
        className="xh-session-search"
        aria-label="搜索会话"
      />
      <div className="xh-session-list-label">
        <Text type="secondary">最近会话</Text>
        <Text type="secondary">{filteredSessions.length}</Text>
      </div>
      <div className="xh-session-list-scroll">
        {loading && sessions.length === 0 ? (
          <div className="xh-centered-state">
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
        {!loading && !error && sessions.length > 0 && filteredSessions.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary">没有匹配的会话</Text>}
          />
        ) : null}
        {filteredSessions.map((s) => (
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
