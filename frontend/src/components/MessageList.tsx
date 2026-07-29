/**
 * 悬壶 WebUI —— 消息列表（P8-2）
 *
 * 按时间升序渲染气泡（agent 左、doctor/patient_proxy 右）。
 * 空态、加载、错误态（含 trace_id）。自动滚动到底部。
 */

import { useEffect, useRef } from 'react'
import { Empty, Spin, Typography } from 'antd'
import { RobotOutlined, UserOutlined } from '@ant-design/icons'
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
  return (
    <div
      data-message-id={msg.id}
      data-role={msg.role}
      className={`xh-message-row ${isAgent ? 'is-agent' : 'is-clinician'}`}
    >
      <div
        className="xh-message-bubble"
      >
        <div className="xh-message-author">
          {isAgent ? (
            <RobotOutlined style={{ fontSize: 12, color: 'var(--xh-secondary)' }} />
          ) : (
            <UserOutlined style={{ fontSize: 12, color: 'var(--xh-primary)' }} />
          )}
          <Text type="secondary">
            {isAgent ? (msg.agent_name ?? '悬壶') : '医师'}
          </Text>
        </div>
        <Text className="xh-message-content">
          {msg.content}
        </Text>
        {msg.stage ? (
          <div style={{ marginTop: 6 }}>
            <Text type="secondary" style={{ fontSize: 10 }}>
              {msg.stage}
            </Text>
          </div>
        ) : null}
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
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : error ? (
        <ErrorBanner error={error as never} onRetry={onRetry} />
      ) : messages.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1 }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary">暂无问诊记录，开始第一条对话</Text>}
          />
        </div>
      ) : (
        messages.map((msg) => <MessageBubble key={msg.id} msg={msg} />)
      )}
      <div ref={bottomRef} />
    </div>
  )
}

export default MessageList
