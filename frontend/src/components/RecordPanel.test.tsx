import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { RecordPanel } from './RecordPanel'
import type { SessionDetail, RecordResponse } from '@/types/api'
import { ApiRequestError } from '@/api/errors'
import { emptySessionReadModel } from '@/utils/readModel'

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

function makeDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_id: 's1',
    status: 'done',
    current_stage: 'done',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 10,
    agent_runtime: 'legacy',
    read_model: emptySessionReadModel('legacy', 10),
    patient_info: {},
    created_at: '2026-07-04T10:00:00+08:00',
    updated_at: '2026-07-04T10:00:00+08:00',
    ...overrides,
  }
}

function makeRecord(overrides: Partial<RecordResponse> = {}): RecordResponse {
  return {
    id: 'rec-1',
    session_id: 's1',
    version: 1,
    record_text: '主诉：头痛3天\n现病史：患者3天前无明显诱因出现头痛...',
    record_json: {
      chief_complaint: '头痛3天',
      present_illness: '患者3天前无明显诱因出现头痛...',
    },
    disclaimer: '本记录由悬壶AI辅助生成，仅供参考。',
    edited_by_doctor: false,
    created_at: '2026-07-04T10:00:00+08:00',
    updated_at: '2026-07-04T10:00:00+08:00',
    ...overrides,
  }
}

describe('RecordPanel', () => {
  it('record 阶段显示生成中骨架', () => {
    const detail = makeDetail({ current_stage: 'record', status: 'active' })
    render(
      <RecordPanel
        detail={detail}
        record={null}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('record-panel')).toBeInTheDocument()
    expect(screen.getByText(/正在汇总生成病历/)).toBeInTheDocument()
  })

  it('done 阶段展示病历文本、JSON、免责声明', () => {
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('record-panel')).toBeInTheDocument()
    expect(screen.getByTestId('record-text')).toHaveTextContent('头痛3天')
    expect(screen.getByTestId('record-disclaimer')).toBeInTheDocument()
    // 导出按钮
    expect(screen.getByTestId('record-export-txt')).toBeInTheDocument()
    expect(screen.getByTestId('record-export-json')).toBeInTheDocument()
    expect(screen.getByTestId('record-export-md')).toBeInTheDocument()
    expect(screen.getByTestId('record-edit-btn')).toBeInTheDocument()
  })

  it('点击编辑进入编辑态', () => {
    const onEdit = vi.fn()
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={onEdit}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('record-edit-btn'))
    expect(onEdit).toHaveBeenCalled()
  })

  it('编辑态显示双区文本 + JSON 编辑器', () => {
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={true}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('record-edit-text')).toBeInTheDocument()
    expect(screen.getByTestId('record-edit-json')).toBeInTheDocument()
    expect(screen.getByTestId('record-save-btn')).toBeInTheDocument()
  })

  it('编辑态 JSON 非法时禁用保存', () => {
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={true}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    // 填入非法 JSON
    const jsonInput = screen.getByTestId('record-edit-json') as HTMLTextAreaElement
    fireEvent.change(jsonInput, { target: { value: '{invalid' } })
    fireEvent.click(screen.getByTestId('record-save-btn'))
    expect(screen.getByTestId('record-json-error')).toBeInTheDocument()
  })

  it('编辑态保存触发 onSave 带 record_text + record_json', () => {
    const onSave = vi.fn()
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={true}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={onSave}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('record-save-btn'))
    expect(onSave).toHaveBeenCalledTimes(1)
    const body = onSave.mock.calls[0][0]
    expect(body).toHaveProperty('record_text')
    expect(body).toHaveProperty('record_json')
  })

  it('导出按钮点击触发 onExport', () => {
    const onExport = vi.fn()
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={onExport}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('record-export-txt'))
    expect(onExport).toHaveBeenCalledWith('txt')
    fireEvent.click(screen.getByTestId('record-export-json'))
    expect(onExport).toHaveBeenCalledWith('json')
    fireEvent.click(screen.getByTestId('record-export-md'))
    expect(onExport).toHaveBeenCalledWith('md')
  })

  it('exportError 显示导出错误提示', () => {
    const error = new ApiRequestError({
      code: 'EXPORT_FORMAT_UNSUPPORTED',
      userMessage: '不支持的导出格式',
      status: 400,
      retryable: false,
    })
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={error}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('record-export-error')).toBeInTheDocument()
    expect(screen.getByText(/不支持的导出格式/)).toBeInTheDocument()
    expect(screen.getByTestId('record-export-retry-txt')).toBeInTheDocument()
  })

  it('点击关闭按钮触发 onExportErrorDismiss', () => {
    const onExportErrorDismiss = vi.fn()
    const error = new ApiRequestError({
      code: 'RECORD_NOT_FOUND',
      userMessage: '病历不存在',
      status: 404,
      retryable: false,
    })
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={error}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={onExportErrorDismiss}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('record-export-dismiss'))
    expect(onExportErrorDismiss).toHaveBeenCalledTimes(1)
  })

  it('exportError 状态下点击重新导出触发 onExport 并清空旧错误', () => {
    const onExport = vi.fn()
    const onExportErrorDismiss = vi.fn()
    const error = new ApiRequestError({
      code: 'EXPORT_FORMAT_UNSUPPORTED',
      userMessage: '不支持的导出格式',
      status: 400,
      retryable: false,
    })
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord()}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={error}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={onExport}
        onExportErrorDismiss={onExportErrorDismiss}
        onRetry={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('record-export-retry-txt'))
    expect(onExport).toHaveBeenCalledWith('txt')
  })

  it('edited_by_doctor=true 显示版本与编辑状态', () => {
    render(
      <RecordPanel
        detail={makeDetail()}
        record={makeRecord({ edited_by_doctor: true, version: 2 })}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByText(/已由医师编辑/)).toBeInTheDocument()
  })

  it('loading 时显示骨架屏', () => {
    render(
      <RecordPanel
        detail={makeDetail()}
        record={null}
        loading={true}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByTestId('record-panel')).toBeInTheDocument()
  })

  it('error 显示 ErrorBanner + 重试', () => {
    const onRetry = vi.fn()
    const error = new ApiRequestError({
      code: 'RECORD_NOT_FOUND',
      userMessage: '病历不存在',
      status: 404,
      retryable: false,
    })
    render(
      <RecordPanel
        detail={makeDetail()}
        record={null}
        loading={false}
        error={error}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={onRetry}
      />,
    )
    expect(screen.getByText(/病历不存在/)).toBeInTheDocument()
  })

  it('非 record/done 阶段不渲染', () => {
    const detail = makeDetail({ current_stage: 'inquiry' })
    const { container } = render(
      <RecordPanel
        detail={detail}
        record={null}
        loading={false}
        error={null}
        editing={false}
        saving={false}
        saveError={null}
        exportError={null}
        onEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSave={vi.fn()}
        onExport={vi.fn()}
        onExportErrorDismiss={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(container.firstChild).toBeNull()
  })
})
