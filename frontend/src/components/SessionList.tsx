/**
 * 悬壶 WebUI —— 侧边栏会话列表（P8-2）
 *
 * 新建会话按钮 + 会话列表项（患者摘要 + 时间 + 状态 Tag）。
 * 选中高亮；空态/加载态/错误态（含重试）。
 * collapsed 模式：每个会话缩成患者头像入口，hover 显示完整信息。
 * 删除按钮仅前端隐藏展示，不删除数据库记录。
 */

import { useMemo, useState, type CSSProperties } from 'react'
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

function patientName(session: SessionListItem): string {
  return session.patient_info?.name?.trim() || '未命名患者'
}

function patientDetails(session: SessionListItem): string {
  const details: string[] = []
  const { age, gender } = session.patient_info
  if (gender && gender !== 'unknown') {
    const genderLabel: Record<string, string> = { male: '男', female: '女' }
    details.push(genderLabel[gender] ?? gender)
  }
  if (age != null) details.push(`${age}岁`)
  return details.join(' · ')
}

function patientInitial(session: SessionListItem): string {
  const name = session.patient_info?.name?.trim()
  return name ? name.slice(0, 1) : '患'
}

function sessionAccentColor(color: string): string {
  const semanticColors: Record<string, string> = {
    default: 'var(--xh-border-strong)',
    error: 'var(--xh-error)',
    processing: 'var(--xh-secondary)',
    success: 'var(--xh-success)',
    warning: 'var(--xh-warning)',
  }
  return semanticColors[color] ?? color
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
  const summary = patientSummary(session) || '未命名患者'
  const complaint = session.chief_complaint?.trim() || '暂无主诉'
  const updatedAt = formatTime(session.updated_at)
  const accentStyle = {
    '--xh-session-accent': sessionAccentColor(tag.color),
  } as CSSProperties

  if (collapsed) {
    return (
      <div className="xh-session-chip-shell">
        <Tooltip
          title={(
            <div className="xh-session-chip-tooltip">
              <div className="xh-session-chip-tooltip-heading">
                <strong>{summary}</strong>
                <span>{tag.label}</span>
              </div>
              <div>主诉：{complaint}</div>
              {updatedAt ? <small>更新于 {updatedAt}</small> : null}
            </div>
          )}
          placement="right"
          mouseEnterDelay={0.2}
        >
          <button
            type="button"
            className="xh-session-chip"
            data-session-id={session.session_id}
            data-selected={selected || undefined}
            aria-current={selected ? 'page' : undefined}
            aria-label={`会话 ${summary}，${tag.label}，主诉 ${complaint}`}
            style={accentStyle}
            onClick={() => onSelect(session.session_id)}
          >
            <span className="xh-session-chip-avatar" aria-hidden="true">
              {patientInitial(session)}
            </span>
            <span className="xh-session-chip-status" aria-hidden="true" />
          </button>
        </Tooltip>
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
    )
  }

  return (
    <div
      className="xh-session-item"
      data-selected={selected || undefined}
      style={accentStyle}
    >
      <button
        type="button"
        className="xh-session-item-select"
        data-session-id={session.session_id}
        aria-current={selected ? 'page' : undefined}
        aria-label={`会话 ${summary}，${tag.label}，主诉 ${complaint}`}
        onClick={() => onSelect(session.session_id)}
      >
        <div className="xh-session-item-main">
          <span className="xh-session-identity">
            <strong className="xh-session-patient">{patientName(session)}</strong>
            {patientDetails(session) ? (
              <span className="xh-session-demographics">{patientDetails(session)}</span>
            ) : null}
          </span>
          <Tag color={tag.color} className="xh-session-status">
            {tag.label}
          </Tag>
        </div>
        <div className="xh-session-complaint" title={complaint}>
          <Text>{complaint}</Text>
        </div>
        <div className="xh-session-meta">
          <Text type="secondary">
            {updatedAt}
          </Text>
        </div>
      </button>
      <Button
        type="text"
        size="small"
        className="xh-session-remove"
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
