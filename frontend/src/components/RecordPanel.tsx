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
  CalendarOutlined,
  CompassOutlined,
  EditOutlined,
  SaveOutlined,
  ExportOutlined,
  CloseOutlined,
  FileTextOutlined,
  HistoryOutlined,
  MessageOutlined,
  MedicineBoxOutlined,
  ReadOutlined,
} from '@ant-design/icons'
import type { SessionDetail, RecordResponse, RecordUpdateRequest } from '@/types/api'
import type { ApiRequestError } from '@/api/errors'
import { ErrorBanner } from './ErrorBanner'

const { Text, Paragraph } = Typography

interface RecordSection {
  label: string
  content: string
}

const HIDDEN_RECORD_SECTIONS = new Set(['安全审核', '医师复核'])
const SUMMARY_RECORD_SECTIONS = new Set(['主诉', '现病史', '中医诊断', '治则治法'])
const FORMULA_RECORD_SECTIONS = new Set(['处方', '处方名', '组成', '加减'])

function overviewVisual(label: string) {
  switch (label) {
    case '主诉':
      return { tone: 'complaint', icon: <MessageOutlined aria-hidden="true" /> }
    case '现病史':
      return { tone: 'history', icon: <HistoryOutlined aria-hidden="true" /> }
    case '中医诊断':
      return { tone: 'diagnosis', icon: <ReadOutlined aria-hidden="true" /> }
    case '治则治法':
      return { tone: 'principle', icon: <CompassOutlined aria-hidden="true" /> }
    default:
      return { tone: 'default', icon: <FileTextOutlined aria-hidden="true" /> }
  }
}

/**
 * 将后端按“标题：内容”生成的病历文本拆成可阅读的临床章节。
 * 已存量病历中的安全审核、医师复核仍保留在原始数据中，但不再作为病历正文展示。
 */
function parseRecordSections(recordText: string): RecordSection[] {
  const sections: RecordSection[] = []
  let current: RecordSection | null = null

  for (const rawLine of recordText.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue

    const matched = line.match(/^([^：:]{1,16})[：:]\s*(.*)$/)
    if (matched) {
      const label = matched[1].trim()
      const content = matched[2].trim()
      current = { label, content }
      sections.push(current)
      continue
    }

    if (current) {
      current.content = `${current.content}${current.content ? '\n' : ''}${line}`
    } else {
      current = { label: '病历记录', content: line }
      sections.push(current)
    }
  }

  return sections.filter((section) => !HIDDEN_RECORD_SECTIONS.has(section.label))
}

function formatRecordDate(value?: string | null): string {
  if (!value) return '暂未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  }).format(date)
}

function patientDetails(detail: SessionDetail): string {
  const details: string[] = []
  const { gender, age } = detail.patient_info
  if (gender && gender !== 'unknown') {
    details.push(gender === 'male' ? '男' : gender === 'female' ? '女' : gender)
  }
  if (age != null) details.push(`${age}岁`)
  return details.join(' · ')
}

function displayRecordJson(recordJson: Record<string, unknown>): Record<string, unknown> {
  const systemFields = new Set([
    'safety_review',
    'doctor_review',
    'authority_refs',
    'record_id',
    'session_id',
  ])
  return Object.fromEntries(
    Object.entries(recordJson).filter(([key]) => !systemFields.has(key)),
  )
}

export interface RecordPanelProps {
  detail: SessionDetail
  record: RecordResponse | null
  /** 仅在医师点击“生成病历”后展示 record 阶段的生成卡片。 */
  generationRequested?: boolean
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
  generationRequested = false,
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
  const recordSections = record ? parseRecordSections(record.record_text) : []
  const embeddedDisclaimer = recordSections.find((section) => section.label === '免责声明')?.content
  const visibleSections = recordSections.filter((section) => section.label !== '免责声明')
  const summarySections = visibleSections.filter((section) => SUMMARY_RECORD_SECTIONS.has(section.label))
  const formulaSections = visibleSections.filter((section) => FORMULA_RECORD_SECTIONS.has(section.label))
  const detailSections = visibleSections.filter(
    (section) => !SUMMARY_RECORD_SECTIONS.has(section.label) && !FORMULA_RECORD_SECTIONS.has(section.label),
  )
  const patientName = detail.patient_info.name?.trim() || '未命名患者'
  const patientMeta = patientDetails(detail)
  const recordDate = formatRecordDate(detail.patient_info.visit_time || record?.created_at || detail.created_at)

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
    if (!generationRequested) return null
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

              <div className="xh-record-identity" data-testid="record-identity">
                <div className="xh-record-patient-mark" aria-hidden="true">
                  {patientName.slice(0, 1)}
                </div>
                <div className="xh-record-patient-info">
                  <Text strong>{patientName}</Text>
                  {patientMeta ? <Text type="secondary">{patientMeta}</Text> : null}
                </div>
                <div className="xh-record-date">
                  <CalendarOutlined aria-hidden="true" />
                  <span>病历日期 {recordDate}</span>
                </div>
              </div>

              <div className="xh-record-content" data-testid="record-text">
                {summarySections.length > 0 ? (
                  <section className="xh-record-overview" aria-label="诊疗概览">
                    <div className="xh-record-overview-heading">
                      <div>
                        <FileTextOutlined aria-hidden="true" />
                        <Text strong>诊疗概览</Text>
                      </div>
                      <Text type="secondary">本次问诊核心信息</Text>
                    </div>
                    {summarySections.map((section) => (
                      <div
                        className={`xh-record-overview-item is-${overviewVisual(section.label).tone}`}
                        key={section.label}
                      >
                        <div className="xh-record-overview-label">
                          <span className="xh-record-overview-icon">
                            {overviewVisual(section.label).icon}
                          </span>
                          <Text type="secondary">{section.label}</Text>
                        </div>
                        <Paragraph>{section.content || '未记录'}</Paragraph>
                      </div>
                    ))}
                  </section>
                ) : null}

                {formulaSections.length > 0 ? (
                  <section className="xh-record-formula" aria-label="处方信息">
                    <div className="xh-record-section-heading">
                      <MedicineBoxOutlined aria-hidden="true" />
                      <Text strong>处方信息</Text>
                    </div>
                    <div className="xh-record-formula-grid">
                      {formulaSections.map((section) => (
                        <div className="xh-record-formula-item" key={section.label}>
                          <Text type="secondary">{section.label}</Text>
                          <Paragraph>{section.content || '未记录'}</Paragraph>
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null}

                {detailSections.length > 0 ? (
                  <section className="xh-record-sections" aria-label="诊疗详情">
                    {detailSections.map((section) => (
                      <div className="xh-record-section" key={section.label}>
                        <div className="xh-record-section-heading">
                          <span aria-hidden="true" />
                          <Text strong>{section.label}</Text>
                        </div>
                        <Paragraph>{section.content || '未记录'}</Paragraph>
                      </div>
                    ))}
                  </section>
                ) : null}

                {visibleSections.length === 0 ? (
                  <Paragraph className="xh-record-fallback">暂无可展示的病历正文</Paragraph>
                ) : null}
              </div>

              <Collapse
                size="small"
                items={[
                  {
                    key: 'json',
                    label: '查看结构化病历数据',
                    children: (
                      <pre
                        style={{ fontSize: 12, whiteSpace: 'pre-wrap', margin: 0 }}
                        data-testid="record-json-view"
                      >
                        {JSON.stringify(displayRecordJson(record.record_json), null, 2)}
                      </pre>
                    ),
                  },
                ]}
                style={{ marginBottom: 'var(--xh-space-m)' }}
              />

              {record.disclaimer || embeddedDisclaimer ? (
                <Alert
                  type="info"
                  message="免责声明"
                  description={record.disclaimer || embeddedDisclaimer}
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
