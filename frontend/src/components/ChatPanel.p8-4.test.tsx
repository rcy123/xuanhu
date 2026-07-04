/**
 * 悬壶 WebUI —— ChatPanel P8-4 集成测试
 *
 * 测试医师确认三路径、病历集成。不测完整 E2E（P8-5）。
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { ChatPanel } from './ChatPanel'
import type { UseSessionDetailResult } from '@/hooks/useSessionDetail'
import type { UseMessagesResult } from '@/hooks/useMessages'
import * as api from '@/api/index'
import type { SessionDetail } from '@/types/api'

// Mock SSE
vi.mock('@/api/sse', () => ({
  connectSessionStream: vi.fn(() => ({
    close: vi.fn(),
    closed: false,
    lastEventId: null,
  })),
}))

function makeDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_id: 's1',
    status: 'active',
    current_stage: 'inquiry',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 1,
    patient_info: { name: '测试患者', gender: 'male', age: 35 },
    chief_complaint: '头痛',
    created_at: '2026-07-04T10:00:00+08:00',
    updated_at: '2026-07-04T10:00:00+08:00',
    ...overrides,
  }
}

function makeDetailHook(overrides: Partial<UseSessionDetailResult> = {}): UseSessionDetailResult {
  return {
    sessionId: 's1',
    detail: makeDetail(),
    loading: false,
    error: null,
    selectSession: vi.fn(),
    refreshDetail: vi.fn().mockResolvedValue(makeDetail()),
    ...overrides,
  }
}

function makeMessagesHook(overrides: Partial<UseMessagesResult> = {}): UseMessagesResult {
  return {
    messages: [],
    loading: false,
    error: null,
    submitting: false,
    submitError: null,
    loadMessages: vi.fn().mockResolvedValue(undefined),
    submit: vi.fn().mockResolvedValue(true),
    clear: vi.fn(),
    ...overrides,
  }
}

describe('ChatPanel P8-4 集成', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    document.body.innerHTML = ''
  })

  it('review 阶段且 pending_review=true 时显示 ReviewActionsBar', () => {
    const detail = makeDetail({
      current_stage: 'review',
      pending_review: true,
      safety_review: { passed: true, issues: [] },
      modified_formula: {
        name: '待确认方',
        composition: [{ herb: '麻黄', dose: 6, unit: 'g' }],
      },
    })
    const detailHook = makeDetailHook({ detail })

    render(
      <ChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.getByTestId('review-actions-bar')).toBeInTheDocument()
  })

  it('阻断态（safety_review.passed=false）不显示确认/修改按钮', () => {
    const detail = makeDetail({
      current_stage: 'review',
      pending_review: true,
      safety_review: {
        passed: false,
        issues: [{ severity: 'blocker', message: '十八反' }],
      },
      modified_formula: {
        composition: [{ herb: '甘草', dose: 9, unit: 'g' }],
      },
    })
    const detailHook = makeDetailHook({ detail })

    render(
      <ChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.queryByTestId('review-confirm-btn')).not.toBeInTheDocument()
    expect(screen.queryByTestId('review-modify-btn')).not.toBeInTheDocument()
    // 否决按钮仍可显示
    expect(screen.getByTestId('review-reject-btn')).toBeInTheDocument()
  })

  it('record 阶段显示 RecordPanel 生成中', () => {
    const detail = makeDetail({
      current_stage: 'record',
      status: 'active',
      pending_review: false,
    })
    const detailHook = makeDetailHook({ detail })

    render(
      <ChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.getByTestId('record-panel')).toBeInTheDocument()
    expect(screen.getByText(/正在汇总生成病历/)).toBeInTheDocument()
  })

  it('done 阶段拉取病历并展示', async () => {
    const recordData = {
      id: 'rec-1',
      session_id: 's1',
      version: 1,
      record_text: '主诉：头痛\n辨证：风寒',
      record_json: { chief_complaint: '头痛' },
      disclaimer: '本记录由AI辅助生成',
      edited_by_doctor: false,
      created_at: '2026-07-04T10:30:00+08:00',
      updated_at: '2026-07-04T10:30:00+08:00',
    }

    const getRecordSpy = vi.spyOn(api, 'getRecord').mockResolvedValue(recordData)

    const detail = makeDetail({
      current_stage: 'done',
      status: 'done',
      pending_review: false,
    })
    const detailHook = makeDetailHook({ detail })

    render(
      <ChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    await waitFor(() => {
      expect(getRecordSpy).toHaveBeenCalledWith('s1', 'latest')
    })

    await waitFor(() => {
      expect(screen.getByTestId('record-panel')).toBeInTheDocument()
      expect(screen.getByTestId('record-text')).toBeInTheDocument()
    })

    getRecordSpy.mockRestore()
  })
})