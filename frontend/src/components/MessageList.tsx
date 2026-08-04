/**
 * 悬壶 WebUI —— 消息列表（P8-2）
 *
 * 按时间升序渲染气泡（agent 左、doctor/patient_proxy 右）。
 * 空态、加载、错误态（含 trace_id）。自动滚动到底部。
 */

import { useEffect, useRef } from 'react'
import { Empty, Spin, Typography } from 'antd'
import { MedicineBoxOutlined, UserOutlined } from '@ant-design/icons'
import type { MessageItem } from '@/types/api'
import { ErrorBanner } from './ErrorBanner'

const { Text } = Typography

interface MessageListProps {
  messages: MessageItem[]
  loading: boolean
  error: unknown
  onRetry: () => void
}

function MessageBubble({ msg }: { msg: MessageItem }) {
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
          </div>
          <Text className="xh-message-content">
            {msg.content}
          </Text>
        </div>
      </div>
    </div>
  )
}

export function MessageList({ messages, loading, error, onRetry }: MessageListProps) {
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
          {messages.map((msg) => <MessageBubble key={msg.id} msg={msg} />)}
        </>
      )}
      <div ref={bottomRef} />
    </div>
  )
}

export default MessageList
