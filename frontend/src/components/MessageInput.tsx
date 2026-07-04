/**
 * 悬壶 WebUI —— 问诊输入框（P8-2）
 *
 * TextArea + 发送按钮；提交中 loading/disabled；Enter 发送，Shift+Enter 换行；
 * 空内容不提交；错误提示（trace_id + 重试按钮）。
 */

import { useEffect, useRef, useState } from 'react'
import { Button, Input, Space } from 'antd'
import { SendOutlined } from '@ant-design/icons'
import { ErrorBanner } from './ErrorBanner'

interface MessageInputProps {
  submitting: boolean
  error: unknown
  disabled?: boolean
  onSubmit: (content: string) => void
  onRetry: () => void
  /** 上次失败的内容，供重试时回填。 */
  lastContent?: string
}

const MAX = 5000

export function MessageInput({
  submitting,
  error,
  disabled,
  onSubmit,
  onRetry,
  lastContent,
}: MessageInputProps) {
  const [value, setValue] = useState('')
  const restoredContent = useRef<string | null>(null)

  useEffect(() => {
    if (error && lastContent && !value && restoredContent.current !== lastContent) {
      setValue(lastContent)
      restoredContent.current = lastContent
    }
    if (!error) {
      restoredContent.current = null
    }
  }, [error, lastContent, value])

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed || submitting || disabled) return
    onSubmit(trimmed)
    setValue('')
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const filled = value.trim().length > 0

  return (
    <div style={{ borderTop: '1px solid var(--xh-border)', padding: 'var(--xh-space-l)' }}>
      {error ? (
        <div style={{ marginBottom: 8 }}>
          <ErrorBanner error={error as never} onRetry={onRetry} />
        </div>
      ) : null}
      <Space.Compact style={{ width: '100%' }}>
        <Input.TextArea
          data-testid="message-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? '当前阶段不可输入问诊消息' : '输入症状描述 / 回复问题…（Enter 发送，Shift+Enter 换行）'}
          autoSize={{ minRows: 2, maxRows: 6 }}
          maxLength={MAX}
          disabled={submitting || disabled}
          style={{ borderRadius: '6px 0 0 6px' }}
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          onClick={submit}
          loading={submitting}
          disabled={!filled || disabled}
          style={{ height: 'auto' }}
        >
          发送
        </Button>
      </Space.Compact>
    </div>
  )
}

export default MessageInput
