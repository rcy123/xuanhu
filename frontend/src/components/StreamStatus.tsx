/**
 * 悬壶 WebUI —— 流连接状态徽标（P8-3）
 *
 * 显示 SSE 连接健康：实时 / 同步中（轮询） / 已断开 / 连接中。
 * 运行中的 Agent 名称追加显示。
 */

import { Button, Space, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { StreamConnectionState } from '@/hooks/useSessionStream'

const { Text } = Typography

interface StreamStatusProps {
  state: StreamConnectionState
  lastError?: string | null
  runningAgent?: string | null
  onReconnect?: () => void
}

const STATE_META: Record<
  StreamConnectionState,
  { color: string; label: string; showReconnect: boolean }
> = {
  idle: { color: 'transparent', label: '', showReconnect: false },
  connecting: { color: '#bfbfbf', label: '连接中…', showReconnect: false },
  connected: { color: '#52c41a', label: '实时', showReconnect: false },
  polling: { color: '#faad14', label: '同步中（轮询）', showReconnect: true },
  disconnected: { color: '#ff4d4f', label: '已断开', showReconnect: true },
}

export function StreamStatus({ state, runningAgent, onReconnect }: StreamStatusProps) {
  if (state === 'idle') return null

  const meta = STATE_META[state]
  const dot = meta.color !== 'transparent' ? (
    <span
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        backgroundColor: meta.color,
        marginRight: 4,
      }}
    />
  ) : null

  return (
    <div
      data-testid="stream-status"
      className={`xh-stream-status is-${state}`}
    >
      <Space size={4}>
        {dot}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {meta.label}
        </Text>
        {runningAgent ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            · {runningAgent} 运行中
          </Text>
        ) : null}
      </Space>
      {meta.showReconnect && onReconnect ? (
        <Button
          size="small"
          type="link"
          icon={<ReloadOutlined />}
          data-testid="stream-reconnect"
          onClick={onReconnect}
        >
          重连
        </Button>
      ) : null}
    </div>
  )
}

export default StreamStatus
