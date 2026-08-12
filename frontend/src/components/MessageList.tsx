/**
 * 悬壶 WebUI —— 消息列表（P8-2）
 *
 * 按时间升序渲染气泡（agent 左、doctor/patient_proxy 右）。
 * 空态、加载、错误态（含 trace_id）。自动滚动到底部。
 *
 * 问诊回退（message rollback）：inquiry 阶段且会话 active 时，每条消息
 * hover 显示「回退到此」——删除该消息及其之后的所有问诊记录并重建事实。
 */

import { useEffect, useRef } from 'react'
import { Button, Empty, Popconfirm, Spin, Typography } from 'antd'
import { MedicineBoxOutlined, RollbackOutlined, UserOutlined } from '@ant-design/icons'
import type { MessageItem } from '@/types/api'
import { ErrorBanner } from './ErrorBanner'

const { Text } = Typography

interface MessageListProps {
  messages: MessageItem[]
  loading: boolean
  error: unknown
  onRetry: () => void
  /** 是否允许回退（仅 inquiry 阶段 + active 会话时为 true）。 */
  canRollback?: boolean
  /** 回退请求进行中（禁用所有回退按钮）。 */
  rollbackPending?: boolean
  /** 点击确认回退到指定消息。 */
  onRollback?: (messageId: string, content: string) => void
}

interface MessageBubbleProps {
  msg: MessageItem
  canRollback: boolean
  rollbackPending: boolean
  onRollback?: (messageId: string, content: string) => void
}

function MessageBubble({ msg, canRollback, rollbackPending, onRollback }: MessageBubbleProps) {
  const isAgent = msg.role === 'agent'
  const author = isAgent ? '悬壶助手' : '医师'
  return (
    <div
      data-message-id={msg.id}
      data-role={msg.role}
      data-stage={msg.stage ?? undefined}
      className={`xh-message-row ${isAgent ? 'is-agent' : 'is-clinician'}`}
    >
      <div className="xh-message-cluster">
        <span className="xh-message-avatar" aria-hidden="true">
          {isAgent ? (
            <MedicineBoxOutlined />
          ) : (
            <UserOutlined />
          )}
        </span>
        <div className="xh-message-bubble">
          <div className="xh-message-author">
            <span>{author}</span>
            {canRollback && onRollback ? (
              <Popconfirm
                title="回退到此消息？"
                description="将删除本条及之后的所有问诊记录，已提取的病情信息会同步回退，且不可撤销。"
                okText="确认回退"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => onRollback(msg.id, msg.content)}
                disabled={rollbackPending}
              >
                <Button
                  type="text"
                  size="small"
                  icon={<RollbackOutlined />}
                  className="xh-message-rollback-btn"
                  disabled={rollbackPending}
                  aria-label={`回退到此：${msg.content.slice(0, 20)}`}
                  data-testid={`rollback-to-${msg.id}`}
                >
                  回退到此
                </Button>
              </Popconfirm>
            ) : null}
          </div>
          <Text className="xh-message-content">
            {msg.content}
          </Text>
        </div>
      </div>
    </div>
  )
}

export function MessageList({
  messages,
  loading,
  error,
  onRetry,
  canRollback = false,
  rollbackPending = false,
  onRollback,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages])

  return (
    <div
      data-testid="message-list"
      className="xh-message-list"
    >
      {loading && messages.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : error && messages.length === 0 ? (
        <ErrorBanner error={error as never} onRetry={onRetry} />
      ) : messages.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1 }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary">暂无问诊记录，开始第一条对话</Text>}
          />
        </div>
      ) : (
        <>
          {loading ? (
            <div className="xh-message-refresh-hint" role="status">
              <Spin size="small" />
              <Text type="secondary">正在同步最新对话…</Text>
            </div>
          ) : null}
          {error ? <ErrorBanner error={error as never} onRetry={onRetry} /> : null}
          {messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              msg={msg}
              canRollback={canRollback}
              rollbackPending={rollbackPending}
              onRollback={onRollback}
            />
          ))}
        </>
      )}
      <div ref={bottomRef} />
    </div>
  )
}

export default MessageList
