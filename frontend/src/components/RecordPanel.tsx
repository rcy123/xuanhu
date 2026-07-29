/**
 * 悬壶 WebUI —— 病历 Panel（P8-4）
 *
 * 根据会话阶段展示：
 * - record：病历生成中（骨架屏）
 * - done：病历文本 + 结构化 JSON + 免责声明 + 编辑 + 导出
 */

import { useState, useEffect, useCallback } from 'react'
import { Card, Skeleton, Typography, Button, Space, Collapse, Input, Alert, Descriptions } from 'antd'
import {
  EditOutlined,
  SaveOutlined,
  ExportOutlined,
  CloseOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import type { SessionDetail, RecordResponse, RecordUpdateRequest } from '@/types/api'
import type { ApiRequestError } from '@/api/errors'
import { ErrorBanner } from './ErrorBanner'

const { Text, Paragraph } = Typography

export interface RecordPanelProps {
  detail: SessionDetail
  record: RecordResponse | null
  loading: boolean
  error: ApiRequestError | null
  editing: boolean
  saving: boolean
  saveError: ApiRequestError | null
  /** 导出失败错误，由 ChatPanel 管理。 */
  exportError: ApiRequestError | null
  onEdit: () => void
  onCancelEdit: () => void
  onSave: (body: RecordUpdateRequest) => void
  onExport: (format: 'txt' | 'json' | 'md') => void
  onRetry: () => void
  /** 关闭导出错误提示。 */
  onExportErrorDismiss: () => void
}

export function RecordPanel({
  detail,
  record,
  loading,
  error,
  editing,
  saving,
  saveError,
  exportError,
  onEdit,
  onCancelEdit,
  onSave,
  onExport,
  onRetry,
  onExportErrorDismiss,
}: RecordPanelProps) {
  const [editText, setEditText] = useState('')
  const [editJson, setEditJson] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)

  // 进入编辑态时初始化
  useEffect(() => {
    if (editing && record) {
      setEditText(record.record_text ?? '')
      setEditJson(JSON.stringify(record.record_json, null, 2))
      setJsonError(null)
    }
  }, [editing, record])

  const handleSave = useCallback(() => {
    // 解析 JSON
    let parsedJson: Record<string, unknown> | undefined
    try {
      parsedJson = JSON.parse(editJson) as Record<string, unknown>
      setJsonError(null)
    } catch {
      setJsonError('JSON 格式无效，请修正后重试')
      return
    }
    onSave({
      record_text: editText || undefined,
      record_json: parsedJson,
    })
  }, [editText, editJson, onSave])

  // ---------- record 阶段：生成中 ----------
  if (detail.current_stage === 'record') {
    return (
      <Card
        size="small"
        title={<span><FileTextOutlined /> 病历</span>}
        className="xh-summary-card xh-record-card"
        data-testid="record-panel"
      >
        <Skeleton active paragraph={{ rows: 6 }} />
        <div style={{ textAlign: 'center', marginTop: 'var(--xh-space-m)' }}>
          <Text type="secondary">正在汇总生成病历...</Text>
        </div>
      </Card>
    )
  }

  // ---------- done 阶段：展示病历 ----------
  if (detail.current_stage === 'done') {
    return (
      <Card
        size="small"
        title={<span><FileTextOutlined /> 中医病历</span>}
        className="xh-summary-card xh-record-card"
        data-testid="record-panel"
        extra={
          editing ? (
            <Space wrap>
              <Button
                size="small"
                icon={<CloseOutlined />}
                onClick={onCancelEdit}
                disabled={saving}
              >
                取消
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<SaveOutlined />}
                onClick={handleSave}
                loading={saving}
                disabled={jsonError !== null}
                data-testid="record-save-btn"
              >
                保存病历
              </Button>
            </Space>
          ) : (
            <Space wrap>
              <Button
                size="small"
                icon={<ExportOutlined />}
                onClick={() => onExport('txt')}
                data-testid="record-export-txt"
              >
                导出 TXT
              </Button>
              <Button
                size="small"
                icon={<ExportOutlined />}
                onClick={() => onExport('json')}
                data-testid="record-export-json"
              >
                导出 JSON
              </Button>
              <Button
                size="small"
                icon={<ExportOutlined />}
                onClick={() => onExport('md')}
                data-testid="record-export-md"
              >
                导出 MD
              </Button>
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={onEdit}
                data-testid="record-edit-btn"
              >
                编辑病历
              </Button>
            </Space>
          )
        }
      >
        {error ? (
          <ErrorBanner error={error} onRetry={onRetry} />
        ) : loading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : record ? (
          editing ? (
            /* ---------- 编辑态 ---------- */
            <div>
              {saveError ? (
                <ErrorBanner error={saveError} onRetry={handleSave} />
              ) : null}
              <div style={{ marginBottom: 'var(--xh-space-m)' }}>
                <Text type="secondary" style={{ fontSize: 12 }}>病历文本（record_text）</Text>
                <Input.TextArea
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  rows={12}
                  data-testid="record-edit-text"
                  style={{ marginTop: 4 }}
                />
              </div>
              <div style={{ marginBottom: 'var(--xh-space-m)' }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  结构化 JSON（record_json）
                </Text>
                <Input.TextArea
                  value={editJson}
                  onChange={(e) => {
                    setEditJson(e.target.value)
                    setJsonError(null)
                  }}
                  rows={10}
                  data-testid="record-edit-json"
                  style={{ marginTop: 4, fontFamily: 'monospace', fontSize: 12 }}
                />
                {jsonError ? (
                  <Text type="danger" style={{ fontSize: 12 }} data-testid="record-json-error">
                    {jsonError}
                  </Text>
                ) : null}
              </div>
            </div>
          ) : (
            /* ---------- 展示态 ---------- */
            <div>
              {exportError ? (
                <div style={{ marginBottom: 'var(--xh-space-m)' }} data-testid="record-export-error">
                  <ErrorBanner
                    error={exportError}
                    onRetry={onExportErrorDismiss}
                    showRetry={false}
                  />
                  <Space style={{ marginTop: 'var(--xh-space-s)' }}>
                    <Button
                      size="small"
                      type="primary"
                      icon={<ExportOutlined />}
                      onClick={() => onExport('txt')}
                      data-testid="record-export-retry-txt"
                    >
                      重新导出 TXT
                    </Button>
                    <Button
                      size="small"
                      icon={<CloseOutlined />}
                      onClick={onExportErrorDismiss}
                      data-testid="record-export-dismiss"
                    >
                      关闭
                    </Button>
                  </Space>
                </div>
              ) : null}
              {record.edited_by_doctor ? (
                <Descriptions size="small" style={{ marginBottom: 'var(--xh-space-m)' }}>
                  <Descriptions.Item label="版本">
                    v{record.version}
                  </Descriptions.Item>
                  <Descriptions.Item label="编辑状态">
                    <Text type="warning">已由医师编辑</Text>
                  </Descriptions.Item>
                </Descriptions>
              ) : null}

              <Paragraph
                style={{ whiteSpace: 'pre-wrap', fontSize: 14, lineHeight: 1.8 }}
                data-testid="record-text"
              >
                {record.record_text}
              </Paragraph>

              <Collapse
                size="small"
                items={[
                  {
                    key: 'json',
                    label: '查看结构化 JSON',
                    children: (
                      <pre
                        style={{ fontSize: 12, whiteSpace: 'pre-wrap', margin: 0 }}
                        data-testid="record-json-view"
                      >
                        {JSON.stringify(record.record_json, null, 2)}
                      </pre>
                    ),
                  },
                ]}
                style={{ marginBottom: 'var(--xh-space-m)' }}
              />

              {record.disclaimer ? (
                <Alert
                  type="info"
                  message="免责声明"
                  description={record.disclaimer}
                  showIcon
                  style={{ marginTop: 'var(--xh-space-m)' }}
                  data-testid="record-disclaimer"
                />
              ) : null}
            </div>
          )
        ) : (
          <div style={{ textAlign: 'center', padding: 'var(--xh-space-xl)' }}>
            <Text type="secondary">暂无病历数据</Text>
          </div>
        )}
      </Card>
    )
  }

  // ---------- 其他阶段：不渲染 ----------
  return null
}

export default RecordPanel
