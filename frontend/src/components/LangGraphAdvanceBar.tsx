import { useRef, useState } from 'react'
import { Alert, Button, Space, Typography } from 'antd'
import { ArrowRightOutlined } from '@ant-design/icons'
import { advanceSession } from '@/api'
import { ApiRequestError } from '@/api/errors'
import type { AdvanceData, SessionDetail } from '@/types/api'
import { canAdvanceLangGraph } from '@/utils/agent'
import { generateIdempotencyKey } from '@/utils/id'

const { Text } = Typography

interface LangGraphAdvanceBarProps {
  detail: SessionDetail
  onAdvanced: (result: AdvanceData) => Promise<void> | void
}

export function LangGraphAdvanceBar({ detail, onAdvanced }: LangGraphAdvanceBarProps) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const pendingKey = useRef<string | null>(null)

  if (detail.agent_runtime !== 'langgraph') return null

  const canAdvance = canAdvanceLangGraph(detail)
  const unresolved = detail.read_model.unresolved
  const handleAdvance = async () => {
    setSubmitting(true)
    setError(null)
    pendingKey.current ??= generateIdempotencyKey()
    try {
      const result = await advanceSession(
        detail.session_id,
        {},
        { idempotencyKey: pendingKey.current, stateVersion: detail.state_version },
      )
      pendingKey.current = null
      await onAdvanced(result)
    } catch (caught: unknown) {
      setError(caught instanceof ApiRequestError ? caught : new ApiRequestError({
        code: 'ADVANCE_FAILED',
        userMessage: '推进失败，请稍后重试',
        status: 0,
        retryable: true,
        cause: caught,
      }))
    } finally {
      setSubmitting(false)
    }
  }

  const hint = detail.current_stage === 'safety'
    ? 'L4 推理已完成，当前停在 Safety 边界，等待后续安全审核。'
    : canAdvance
      ? '红旗与问诊完备性门禁均已通过，可进入辨证与方药草案。'
      : '需先完成问诊，并通过红旗与完备性门禁后才能推进。'

  return (
    <div data-testid="langgraph-advance-bar" style={{ padding: '8px var(--xh-space-l)' }}>
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Text type="secondary">LangGraph 非临床限量灰度 · {hint}</Text>
        <div data-testid="langgraph-read-model-summary">
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text strong data-testid="langgraph-graph-revision">
              权威 Read Model · 图修订 {detail.read_model.graph.revision}
            </Text>
            {unresolved.length > 0 ? (
              <>
                <Text type="warning">未解决项（{unresolved.length}）</Text>
                <ul
                  aria-label="LangGraph 未解决项"
                  data-testid="langgraph-unresolved-items"
                  style={{ margin: 0, paddingInlineStart: 24 }}
                >
                  {unresolved.map((item, index) => (
                    <li
                      key={`${item.source}:${item.kind}:${item.key}:${index}`}
                      data-testid="langgraph-unresolved-item"
                    >
                      <Text code>{item.source}</Text>
                      {' · '}
                      <Text code>{item.kind}</Text>
                      {' · '}
                      <Text code>{item.key}</Text>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <Text type="secondary" data-testid="langgraph-unresolved-empty">
                未解决项：无
              </Text>
            )}
          </Space>
        </div>
        {error ? <Alert type="error" showIcon message={error.userMessage} /> : null}
        {detail.current_stage === 'inquiry' ? (
          <Button
            type="primary"
            icon={<ArrowRightOutlined />}
            disabled={!canAdvance}
            loading={submitting}
            onClick={() => void handleAdvance()}
            data-testid="langgraph-advance-button"
          >
            进入辨证开方
          </Button>
        ) : null}
      </Space>
    </div>
  )
}
