/**
 * 悬壶 WebUI —— 通用错误条（P8-2）
 *
 * 展示 ApiRequestError.userMessage + trace_id；retryable 时提供重试按钮。
 * 基于 error.code 补充用户可理解的提示文案。
 */

import { Alert, Button, Typography } from 'antd'
import type { ApiRequestError } from '@/api/errors'
import { ErrorCode } from '@/types/api'

const { Text } = Typography

interface ErrorBannerProps {
  error: ApiRequestError | null
  onRetry?: () => void
  /** 是否显示重试按钮；默认当 error.retryable 为 true 时显示。 */
  showRetry?: boolean
  banner?: boolean
}

/** 按错误码补充更友好的中文文案。 */
function describeMessage(err: ApiRequestError): string {
  switch (err.code) {
    case ErrorCode.SESSION_BUSY:
      return '会话正在处理其他请求，请稍后重试。'
    case ErrorCode.INVALID_STATE_VERSION:
      return '会话状态已更新，正在同步，请重试。'
    case ErrorCode.SESSION_NOT_FOUND:
      return '会话不存在或已终止。'
    case ErrorCode.SESSION_TERMINATED:
      return '会话已被终止。'
    case ErrorCode.MODEL_GATEWAY_UNAVAILABLE:
      return 'AI 模型服务暂不可用，请稍后重试。'
    default:
      return err.userMessage || '请求失败，请重试。'
  }
}

export function ErrorBanner({ error, onRetry, showRetry, banner = true }: ErrorBannerProps) {
  if (!error) return null
  const message = describeMessage(error)
  const retryVisible = showRetry ?? error.retryable
  return (
    <Alert
      type="error"
      banner={banner}
      showIcon
      title={message}
      description={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          {error.traceId ? (
            <Text type="secondary" style={{ fontSize: 11 }}>
              追踪号：<span data-testid="error-trace-id">{error.traceId}</span>
            </Text>
          ) : (
            <span />
          )}
          {retryVisible && onRetry ? (
            <Button size="small" data-testid="error-retry" onClick={onRetry}>
              重试
            </Button>
          ) : null}
        </div>
      }
    />
  )
}

export default ErrorBanner
