/**
 * 悬壶 WebUI —— 侧边栏会话列表（P8-2）
 *
 * 新建会话按钮 + 会话列表项（患者摘要 + 时间 + 状态 Tag）。
 * 选中高亮；空态/加载态/错误态（含重试）。
 * collapsed 模式：每个会话缩成小块（患者姓名首字），hover 显示完整信息。
 * 删除按钮仅前端隐藏展示，不删除数据库记录。
 */

import { useMemo, useState } from 'react'
import { Button, Empty, Input, Spin, Tag, Tooltip, Typography } from 'antd'
import { DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
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
  collapsed?: boolean
  onSelect: (id: string) => void
  onRefresh: () => void
  onCreate: () => void
}

/** 患者名字首字（无名字时用「患」）。 */
function patientInitial(session: SessionListItem): string {
  const name = session.patient_info?.name?.trim()
  return name ? name.slice(0, 1) : '患'
}

function SessionItem({
  session,
  selected,
  collapsed,
  onSelect,
  onRemove,
}: {
  session: SessionListItem
  selected: boolean
  collapsed: boolean
  onSelect: (id: string) => void
  onRemove: (id: string) => void
}) {
  const tag = sessionTag(session)

  if (collapsed) {
    const summary = patientSummary(session) || '未命名患者'
    return (
      <Tooltip
        title={`${summary}${session.chief_complaint ? ` · ${session.chief_complaint}` : ''}`}
        placement="right"
        mouseEnterDelay={0.2}
      >
        <div
          className="xh-session-chip"
          role="button"
          tabIndex={0}
          data-session-id={session.session_id}
          data-selected={selected || undefined}
          data-status-color={tag.color}
          aria-label={`会话 ${summary}`}
          onClick={() => onSelect(session.session_id)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onSelect(session.session_id)
            }
          }}
        >
          <span className="xh-session-chip-initial">{patientInitial(session)}</span>
          <Button
            type="text"
            size="small"
            className="xh-session-chip-remove"
            aria-label={`删除会话 ${summary}`}
            data-testid={`remove-${session.session_id}`}
            onClick={(event) => {
              // 仅前端隐藏展示，不删除数据库记录。
              event.stopPropagation()
              onRemove(session.session_id)
            }}
          >
            <DeleteOutlined />
          </Button>
        </div>
      </Tooltip>
    )
  }

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
          <Button
            type="text"
            size="small"
            className="xh-session-remove"
            aria-label={`删除会话 ${patientSummary(session) || session.session_id}`}
            data-testid={`remove-${session.session_id}`}
            onClick={(event) => {
              // 仅前端隐藏展示，不删除数据库记录。
              event.stopPropagation()
              onRemove(session.session_id)
            }}
          >
            <DeleteOutlined />
          </Button>
      </div>
    </div>
  )
}

export function SessionList({
  sessions,
  loading,
  error,
  selectedId,
  collapsed = false,
  onSelect,
  onRefresh,
  onCreate,
}: SessionListProps) {
  const [query, setQuery] = useState('')
  // 仅前端隐藏的会话 id（不删除数据库，刷新后恢复显示）。
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<string>>(() => new Set())

  const handleRemove = (id: string) => {
    setHiddenIds((prev) => {
      const next = new Set(prev)
      next.add(id)
      return next
    })
  }

  const visibleSessions = useMemo(
    () => sessions.filter((session) => !hiddenIds.has(session.session_id)),
    [sessions, hiddenIds],
  )

  const filteredSessions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    if (!normalized) return visibleSessions
    return visibleSessions.filter((session) => {
      const searchable = [
        patientSummary(session),
        session.chief_complaint ?? '',
        session.current_stage,
      ].join(' ').toLocaleLowerCase()
      return searchable.includes(normalized)
    })
  }, [query, visibleSessions])

  return (
    <div data-testid="session-list" className={`xh-session-list${collapsed ? ' is-collapsed' : ''}`}>
      <Button
        type="primary"
        icon={<PlusOutlined />}
        block={!collapsed}
        size="large"
        className="xh-create-session"
        onClick={onCreate}
        aria-label={collapsed ? '新建问诊' : undefined}
      >
        {collapsed ? null : '新建问诊'}
      </Button>
      {collapsed ? null : (
        <Input
          allowClear
          prefix={<SearchOutlined />}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索患者或主诉"
          className="xh-session-search"
          aria-label="搜索会话"
        />
      )}
      {collapsed ? null : (
        <div className="xh-session-list-label">
          <Text type="secondary">最近会话</Text>
          <Text type="secondary">{filteredSessions.length}</Text>
        </div>
      )}
      <div className="xh-session-list-scroll">
        {loading && sessions.length === 0 ? (
          <div className="xh-centered-state">
            <Spin />
          </div>
        ) : null}
        {error && !loading ? (
          <ErrorBanner error={error as never} onRetry={onRefresh} />
        ) : null}
        {!loading && !error && visibleSessions.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary">暂无会话</Text>}
          />
        ) : null}
        {!loading && !error && visibleSessions.length > 0 && filteredSessions.length === 0 ? (
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
            collapsed={collapsed}
            onSelect={onSelect}
            onRemove={handleRemove}
          />
        ))}
      </div>
    </div>
  )
}

export default SessionList
