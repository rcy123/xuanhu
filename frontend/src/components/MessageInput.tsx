/**
 * 悬壶 WebUI —— 问诊输入框（P8-2）
 *
 * TextArea + 发送按钮；提交中 loading/disabled；Enter 发送，Shift+Enter 换行；
 * 空内容不提交；错误提示（trace_id + 重试按钮）。
 */

import { useEffect, useRef, useState } from 'react'
import { Button, Input } from 'antd'
import { SendOutlined } from '@ant-design/icons'
import { ErrorBanner } from './ErrorBanner'

interface MessageInputProps {
  submitting: boolean
  error: unknown
  disabled?: boolean
  disabledReason?: string
  onSubmit: (content: string) => void
  onRetry?: () => void
  /** 上次失败的内容，供重试时回填。 */
  lastContent?: string
}

const MAX = 5000

export function MessageInput({
  submitting,
  error,
  disabled,
  disabledReason,
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
    <div className="xh-message-composer">
      {error ? (
        <div className="xh-composer-error">
          <ErrorBanner error={error as never} onRetry={onRetry} />
        </div>
      ) : null}
      <div className="xh-composer-row">
        <Input.TextArea
          data-testid="message-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? (disabledReason ?? '当前阶段不可输入问诊消息') : '回复问诊问题或补充症状…'}
          autoSize={{ minRows: 1, maxRows: 4 }}
          maxLength={MAX}
          disabled={submitting || disabled}
          className="xh-composer-input"
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          onClick={submit}
          loading={submitting}
          disabled={!filled || disabled}
          size="large"
          className="xh-send-button"
        >
          发送
        </Button>
      </div>
      <div className="xh-composer-hint">
        <span>{disabled ? (disabledReason ?? '当前流程阶段已锁定问诊输入') : 'Enter 发送 · Shift + Enter 换行'}</span>
        <span>{value.length}/{MAX}</span>
      </div>
    </div>
  )
}

export default MessageInput
