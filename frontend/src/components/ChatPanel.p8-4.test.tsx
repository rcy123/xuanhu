/**
 * 悬壶 WebUI —— ChatPanel P8-4 集成测试
 *
 * 测试医师确认三路径、病历集成。不测完整 E2E（P8-5）。
 */

import type { ComponentProps } from 'react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react'
import { ChatPanel } from './ChatPanel'
import type { UseSessionDetailResult } from '@/hooks/useSessionDetail'
import type { UseMessagesResult } from '@/hooks/useMessages'
import type { UseCommandReconciliationResult } from '@/hooks/useCommandReconciliation'
import * as api from '@/api/index'
import * as sse from '@/api/sse'
import { ApiRequestError } from '@/api/errors'
import type { SessionDetail } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'

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
    agent_runtime: 'legacy',
    read_model: emptySessionReadModel('legacy', 1),
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
    pendingSubmission: null,
    lastFailedContent: null,
    loadMessages: vi.fn().mockResolvedValue(null),
    submit: vi.fn().mockResolvedValue(true),
    retryPending: vi.fn().mockResolvedValue(true),
    settleMessage: vi.fn(),
    clear: vi.fn(),
    ...overrides,
  }
}

function makeCommandReconciler(): UseCommandReconciliationResult {
  return {
    outstanding: [],
    hasOutstanding: false,
    attention: [],
    hasAttention: false,
    isOutstandingFor: vi.fn().mockReturnValue(false),
    registerAccepted: vi.fn(),
    handleCommandEvent: vi.fn(),
    reconcileAll: vi.fn().mockResolvedValue(undefined),
    retryStatus: vi.fn().mockResolvedValue(undefined),
    getEntry: vi.fn(),
    setHandlers: vi.fn(),
    clear: vi.fn(),
  }
}

const commandReconciler = makeCommandReconciler()

// ChatPanel 现在要求 commandReconciler prop；P8-4 用例不测 R7，统一注入空 mock。
function TestChatPanel(props: Omit<ComponentProps<typeof ChatPanel>, 'commandReconciler'>) {
  return <ChatPanel {...props} commandReconciler={commandReconciler} />
}

describe('ChatPanel P8-4 集成', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(api, 'listSafetyAssertions').mockResolvedValue({ items: [] })
  })

  afterEach(() => {
    cleanup()
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('locks free-form input while only a doctor safety confirmation is pending', async () => {
    const readModel = emptySessionReadModel('langgraph', 2)
    readModel.unresolved = [{
      source: 'safety_confirmation',
      kind: 'unconfirmed_safety_fact',
      key: 'allergy',
    }]
    vi.mocked(api.listSafetyAssertions).mockResolvedValue({
      items: [{
        schema_version: 'safety-fact-assertion.v1',
        assertion_id: 'assertion-1',
        session_id: 's1',
        field_name: 'allergy',
        value: { collection_status: 'explicitly_none', values: null },
        value_digest: 'a'.repeat(64),
        status: 'proposed',
        source_kind: 'deterministic_reply_binding',
        source_message_id: 'message-1',
        extraction_run_id: 'run-1',
        template_version: 'intake_extraction_v2.jinja2',
        evidence_spans: [],
        evidence_digest: 'b'.repeat(64),
        proposed_at: '2026-07-29T10:00:00Z',
      }],
    })
    const detail = makeDetail({
      agent_runtime: 'langgraph',
      read_model: readModel,
      state_version: 2,
    })

    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ detail })}
        messagesHook={makeMessagesHook({ messages: [] })}
      />,
    )

    expect(await screen.findByTestId('safety-confirmation-panel')).toBeInTheDocument()
    expect(screen.getByTestId('safety-confirmation-blocked-input')).toBeInTheDocument()
    expect(screen.getByTestId('message-input')).toBeDisabled()
    expect(screen.getByTestId('safety-reviewer-id')).toHaveValue('')
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
      <TestChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.getByTestId('review-actions-bar')).toBeInTheDocument()
  })

  it('将问诊对话与诊疗摘要拆分为独立工作区', () => {
    const detail = makeDetail({
      syndrome_result: {
        pattern: '风寒束表',
        evidence: '恶寒、无汗、脉浮紧',
      },
    })

    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ detail })}
        messagesHook={makeMessagesHook()}
      />,
    )

    const conversation = screen.getByRole('region', { name: '问诊对话' })
    expect(conversation).toBeInTheDocument()
    expect(within(conversation).getByTestId('step-bar')).toBeInTheDocument()
    expect(within(conversation).queryByText('CONSULTATION')).not.toBeInTheDocument()
    expect(within(conversation).queryByText('对话记录实时保存')).not.toBeInTheDocument()
    const summary = screen.getByRole('complementary', { name: '诊疗摘要' })
    expect(summary).toBeInTheDocument()
    expect(within(summary).queryByText('CLINICAL SUMMARY')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '打开诊疗摘要' }))
    expect(summary).toHaveClass('is-open')
  })

  it('问诊结束后将诊疗工作台切换为主工作区', () => {
    const detail = makeDetail({
      current_stage: 'review',
      pending_review: true,
      modified_formula: {
        name: '待确认方',
        composition: [{ herb: '麻黄', dose: 6, unit: 'g' }],
      },
    })
    const { container } = render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ detail })}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(container.querySelector('.xh-workspace-columns')).toHaveClass('is-clinical-workspace')
    expect(screen.getByRole('region', { name: '问诊记录' })).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '诊疗工作台' })).toBeInTheDocument()
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
      <TestChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.queryByTestId('review-confirm-btn')).not.toBeInTheDocument()
    // 修改处方保留：提交后重新执行 Safety 硬门禁
    expect(screen.getByTestId('review-modify-btn')).toBeInTheDocument()
    // 否决按钮仍可显示
    expect(screen.getByTestId('review-reject-btn')).toBeInTheDocument()
  })

  it('LangGraph 补充信息动作调用 request_more_info', async () => {
    const reviewSpy = vi.spyOn(api, 'reviewPrescription').mockResolvedValue({
      session_id: 's1',
      action: 'request_more_info',
      current_stage: 'syndrome',
      status: 'active',
      pending_review: false,
      review_id: 'review-1',
      state_version: 6,
      updated_at: '2026-07-04T10:30:00+08:00',
    })
    const detail = makeDetail({
      agent_runtime: 'langgraph',
      read_model: emptySessionReadModel('langgraph', 5),
      current_stage: 'review',
      pending_review: true,
      state_version: 5,
      safety_review: { passed: true, issues: [] },
      modified_formula: {
        name: '待确认方',
        composition: [{ herb: '麻黄', dose: 6, unit: 'g' }],
      },
    })

    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ detail })}
        messagesHook={makeMessagesHook()}
      />,
    )
    fireEvent.click(screen.getByTestId('review-request-more-info-btn'))
    fireEvent.change(screen.getByTestId('request-more-info-feedback'), {
      target: { value: '请补充舌象和脉象' },
    })
    fireEvent.click(screen.getByTestId('request-more-info-submit-btn'))

    await waitFor(() => {
      expect(reviewSpy).toHaveBeenCalledWith(
        's1',
        { action: 'request_more_info', feedback: '请补充舌象和脉象' },
        // R7: 医师确认操作现在携带 idempotencyKey（幂等保留），断言 stateVersion 即可。
        expect.objectContaining({ stateVersion: 5 }),
      )
    })
    reviewSpy.mockRestore()
  })

  it('record 阶段在未点击生成病历前不显示 RecordPanel', () => {
    const detail = makeDetail({
      current_stage: 'record',
      status: 'active',
      pending_review: false,
    })
    const detailHook = makeDetailHook({ detail })

    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={detailHook}
        messagesHook={makeMessagesHook()}
      />,
    )

    expect(screen.queryByTestId('record-panel')).not.toBeInTheDocument()
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
      <TestChatPanel
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

  it('releases the input lock when a stale safety hint resolves to an empty list', async () => {
    const readModel = emptySessionReadModel('langgraph', 2)
    readModel.unresolved = [{
      source: 'safety_confirmation',
      kind: 'unconfirmed_safety_fact',
      key: 'allergy',
    }]
    vi.mocked(api.listSafetyAssertions).mockResolvedValue({ items: [] })
    const detail = makeDetail({
      agent_runtime: 'langgraph',
      read_model: readModel,
      state_version: 2,
    })

    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ detail })}
        messagesHook={makeMessagesHook()}
      />,
    )

    await waitFor(() => expect(api.listSafetyAssertions).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('message-input')).toBeEnabled())
    expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()
  })

  it('locks new input while an uncertain message command is pending exact replay', () => {
    const pendingError = new ApiRequestError({
      code: 'NETWORK_ERROR',
      userMessage: 'response lost',
      status: 0,
      retryable: true,
    })
    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook()}
        messagesHook={makeMessagesHook({
          submitError: pendingError,
          pendingSubmission: {
            content: 'original answer',
            replyToMessageId: 'question-1',
            idempotencyKey: 'fixed-key',
            stateVersion: 1,
          },
          lastFailedContent: 'original answer',
        })}
      />,
    )

    expect(screen.getByTestId('message-input')).toBeDisabled()
    expect(screen.getByTestId('message-input')).toHaveAttribute('placeholder')
    expect(screen.getByTestId('error-retry')).toBeInTheDocument()
  })

  it('does not render actionable controls for detail from another session', () => {
    const staleDetail = makeDetail({ session_id: 's1' })
    const submit = vi.fn().mockResolvedValue(true)
    render(
      <TestChatPanel
        sessionId="s2"
        detailHook={makeDetailHook({ sessionId: 's2', detail: staleDetail })}
        messagesHook={makeMessagesHook({ submit })}
      />,
    )

    expect(screen.getByTestId('session-detail-boundary-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('message-input')).not.toBeInTheDocument()
    expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()
    expect(submit).not.toHaveBeenCalled()
  })

  it.each(['intake', 'safety_confirmation'])(
    'refreshes detail and messages when another window finishes %s',
    async (agentName) => {
    let onEvent: ((event: import('@/types/api').SessionEvent) => void) | undefined
    vi.mocked(sse.connectSessionStream).mockImplementation((_id, handlers) => {
      onEvent = handlers.onEvent
      return { close: vi.fn(), closed: false, lastEventId: null }
    })
    const refreshDetail = vi.fn().mockResolvedValue(makeDetail({ state_version: 2 }))
    const loadMessages = vi.fn().mockResolvedValue([])
    render(
      <TestChatPanel
        sessionId="s1"
        detailHook={makeDetailHook({ refreshDetail })}
        messagesHook={makeMessagesHook({ loadMessages })}
      />,
    )
    refreshDetail.mockClear()
    loadMessages.mockClear()

    await act(async () => {
      onEvent?.({
        event_id: `event-${agentName}-finished`,
        event_type: 'agent.finished',
        payload: { agent_name: agentName },
      })
    })

    await waitFor(() => expect(refreshDetail).toHaveBeenCalledTimes(1))
    expect(loadMessages).toHaveBeenCalledWith('s1')
    },
  )
})
