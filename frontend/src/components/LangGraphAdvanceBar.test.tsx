import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '@/api'
import { ApiRequestError } from '@/api/errors'
import type { SessionDetail } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'
import { LangGraphAdvanceBar } from './LangGraphAdvanceBar'

const idMocks = vi.hoisted(() => {
  let sequence = 0
  return {
    generate: vi.fn(() => `advance-idem-${++sequence}`),
    reset: () => { sequence = 0 },
  }
})

vi.mock('@/utils/id', () => ({ generateIdempotencyKey: idMocks.generate }))

function detail(ready = true): SessionDetail {
  const readModel = emptySessionReadModel('langgraph', 2)
  readModel.gates = ready
    ? [
        {
          gate_id: 'triage-gate',
          gate_name: 'triage',
          policy_version: 'triage-policy.v1',
          input_state_version: 1,
          decision: 'passed',
          details: { disposition: 'continue' },
        },
        {
          gate_id: 'completeness-gate',
          gate_name: 'completeness',
          policy_version: 'completeness-policy.v1',
          input_state_version: 1,
          decision: 'passed',
          details: { disposition: 'ready' },
        },
      ]
    : []
  return {
    session_id: 'session-1',
    status: 'active',
    current_stage: 'inquiry',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 2,
    agent_runtime: 'langgraph',
    read_model: readModel,
    patient_info: {},
    created_at: '',
    updated_at: '',
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  idMocks.generate.mockClear()
  idMocks.reset()
})

describe('LangGraphAdvanceBar', () => {
  it('is hidden for legacy sessions', () => {
    const legacy = { ...detail(), agent_runtime: 'legacy' as const, read_model: emptySessionReadModel('legacy', 2) }
    render(<LangGraphAdvanceBar detail={legacy} onAdvanced={() => {}} />)
    expect(screen.queryByTestId('langgraph-advance-bar')).not.toBeInTheDocument()
  })

  it('keeps advance disabled until authoritative gates are ready', () => {
    render(<LangGraphAdvanceBar detail={detail(false)} onAdvanced={() => {}} />)
    expect(screen.getByTestId('langgraph-advance-button')).toBeDisabled()
    expect(screen.getByTestId('langgraph-disposition')).toHaveTextContent('needs_input')
  })

  it('renders triage_hold and manual_required from persisted authority', () => {
    const triage = detail(false)
    triage.read_model.gates = [
      {
        gate_id: 'triage-gate',
        gate_name: 'triage',
        policy_version: 'triage-policy.v1',
        input_state_version: 1,
        decision: 'blocked',
        details: { disposition: 'emergency_referral' },
      },
    ]
    const { rerender } = render(
      <LangGraphAdvanceBar detail={triage} onAdvanced={() => {}} />,
    )
    expect(screen.getByTestId('langgraph-disposition')).toHaveTextContent('triage_hold')

    rerender(
      <LangGraphAdvanceBar
        detail={{ ...detail(), recovery_status: 'manual_required' }}
        onAdvanced={() => {}}
      />,
    )
    expect(screen.getByTestId('langgraph-disposition')).toHaveTextContent('manual_required')
  })

  it('recovers a blocked LangGraph control cursor with a stable public key', async () => {
    const blocked = detail()
    blocked.current_stage = 'blocked'
    blocked.status = 'blocked'
    blocked.recovery_status = 'manual_required'
    blocked.blocked_reason = 'safety_rule_blocked'
    const recover = vi.spyOn(api, 'recoverSession').mockResolvedValue({
      session_id: blocked.session_id,
      current_stage: 'safety',
      status: 'active',
      recovery_status: 'normal',
      action: 'retry_current_stage',
      updated_at: '2026-07-28T00:00:00Z',
    })
    const onRecovered = vi.fn()

    render(
      <LangGraphAdvanceBar
        detail={blocked}
        onAdvanced={() => {}}
        onRecovered={onRecovered}
      />,
    )
    fireEvent.click(screen.getByTestId('langgraph-recover-button'))

    await waitFor(() => expect(onRecovered).toHaveBeenCalledOnce())
    expect(recover).toHaveBeenCalledWith(
      blocked.session_id,
      { action: 'retry_current_stage' },
      { idempotencyKey: 'advance-idem-1' },
    )
    expect(screen.getByTestId('langgraph-recovery-required')).toHaveTextContent(
      '不会切换到 Legacy',
    )
  })

  it('does not offer runtime recovery for a triage hold', () => {
    const triage = detail(false)
    triage.current_stage = 'blocked'
    triage.status = 'blocked'
    triage.recovery_status = 'manual_required'
    triage.blocked_reason = 'triage_hold:emergency_referral'
    triage.read_model.gates = [
      {
        gate_id: 'triage-gate',
        gate_name: 'triage',
        policy_version: 'triage-policy.v1',
        input_state_version: 1,
        decision: 'blocked',
        details: { disposition: 'emergency_referral' },
      },
    ]

    render(<LangGraphAdvanceBar detail={triage} onAdvanced={() => {}} />)
    expect(screen.getByTestId('langgraph-disposition')).toHaveTextContent('triage_hold')
    expect(screen.queryByTestId('langgraph-recover-button')).not.toBeInTheDocument()
  })

  it('restores the review-required status from the read model', () => {
    const review = detail()
    review.current_stage = 'safety'
    review.read_model.review_required = true
    render(<LangGraphAdvanceBar detail={review} onAdvanced={() => {}} />)
    expect(screen.getByTestId('langgraph-review-restored')).toHaveTextContent(
      '医师复核要求已从权威 Read Model 恢复',
    )
  })

  it('exposes explicit Safety and Record product actions', () => {
    const safety = detail()
    safety.current_stage = 'safety'
    const { rerender } = render(
      <LangGraphAdvanceBar detail={safety} onAdvanced={() => {}} />,
    )
    expect(screen.getByTestId('langgraph-advance-button')).toHaveTextContent('执行安全审核')

    rerender(
      <LangGraphAdvanceBar
        detail={{ ...safety, current_stage: 'record', state_version: 5 }}
        onAdvanced={() => {}}
      />,
    )
    expect(screen.getByTestId('langgraph-advance-button')).toHaveTextContent('生成病历')
  })

  it.each(['safety', 'record'] as const)(
    'disables the %s product action while recovery is required',
    (stage) => {
      const recoveryRequired = detail()
      recoveryRequired.current_stage = stage
      recoveryRequired.recovery_status = 'manual_required'

      render(
        <LangGraphAdvanceBar
          detail={recoveryRequired}
          onAdvanced={() => {}}
        />,
      )

      expect(screen.getByTestId('langgraph-disposition')).toHaveTextContent('manual_required')
      expect(screen.getByTestId('langgraph-advance-button')).toBeDisabled()
    },
  )

  it('renders the authoritative revision and refreshes non-sensitive unresolved items on rerender', () => {
    const initial = detail(false)
    initial.read_model.unresolved = [
      { source: 'completeness', kind: 'missing_required', key: 'ten_questions' },
    ]
    const { rerender } = render(
      <LangGraphAdvanceBar detail={initial} onAdvanced={() => {}} />,
    )

    expect(screen.getByTestId('langgraph-graph-revision')).toHaveTextContent('图修订 2')
    expect(screen.getByTestId('langgraph-unresolved-item')).toHaveTextContent(
      'completeness · missing_required · ten_questions',
    )

    const refreshed = detail(false)
    refreshed.read_model.graph = { ...refreshed.read_model.graph, revision: 7 }
    refreshed.read_model.unresolved = [
      {
        source: 'safety_confirmation',
        kind: 'unconfirmed_safety_fact',
        key: 'allergy',
      },
    ]
    rerender(<LangGraphAdvanceBar detail={refreshed} onAdvanced={() => {}} />)

    expect(screen.getByTestId('langgraph-graph-revision')).toHaveTextContent('图修订 7')
    expect(screen.queryByText('ten_questions')).not.toBeInTheDocument()
    expect(screen.getByTestId('langgraph-unresolved-item')).toHaveTextContent(
      'safety_confirmation · unconfirmed_safety_fact · allergy',
    )
  })

  it('shows an explicit empty state when the authoritative model has no unresolved items', () => {
    render(<LangGraphAdvanceBar detail={detail()} onAdvanced={() => {}} />)
    expect(screen.getByTestId('langgraph-unresolved-empty')).toHaveTextContent('未解决项：无')
  })

  it('advances with state version and a stable public idempotency key', async () => {
    const advance = vi.spyOn(api, 'advanceSession').mockResolvedValue({
      session_id: 'session-1',
      current_stage: 'safety',
      from_stage: 'inquiry',
      state_version: 4,
    })
    const onAdvanced = vi.fn()
    render(<LangGraphAdvanceBar detail={detail()} onAdvanced={onAdvanced} />)

    fireEvent.click(screen.getByTestId('langgraph-advance-button'))

    await waitFor(() => expect(onAdvanced).toHaveBeenCalledOnce())
    expect(advance).toHaveBeenCalledWith(
      'session-1',
      {},
      { idempotencyKey: 'advance-idem-1', stateVersion: 2 },
    )
  })

  it('reuses the same key when the clinician retries a failed request', async () => {
    const advance = vi.spyOn(api, 'advanceSession')
      .mockRejectedValueOnce(new ApiRequestError({
        code: 'NETWORK_ERROR',
        userMessage: '网络失败',
        status: 0,
        retryable: true,
      }))
      .mockResolvedValueOnce({
        session_id: 'session-1',
        current_stage: 'safety',
        from_stage: 'inquiry',
        state_version: 4,
      })
    const { rerender } = render(
      <LangGraphAdvanceBar detail={detail()} onAdvanced={() => {}} />,
    )

    fireEvent.click(screen.getByTestId('langgraph-advance-button'))
    await screen.findByText('网络失败')
    const refreshed = detail()
    refreshed.state_version = 3
    rerender(<LangGraphAdvanceBar detail={refreshed} onAdvanced={() => {}} />)
    fireEvent.click(screen.getByTestId('langgraph-advance-button'))

    await waitFor(() => expect(advance).toHaveBeenCalledTimes(2))
    expect(advance.mock.calls[0][2]).toEqual({
      idempotencyKey: 'advance-idem-1',
      stateVersion: 2,
    })
    expect(advance.mock.calls[1][2]).toEqual({
      idempotencyKey: 'advance-idem-1',
      stateVersion: 2,
    })
  })

  it('starts a fresh command after a deterministic state-version rejection', async () => {
    const advance = vi.spyOn(api, 'advanceSession')
      .mockRejectedValueOnce(new ApiRequestError({
        code: 'INVALID_STATE_VERSION',
        userMessage: '状态已更新',
        status: 409,
        retryable: true,
      }))
      .mockResolvedValueOnce({
        session_id: 'session-1',
        current_stage: 'safety',
        from_stage: 'inquiry',
        state_version: 4,
      })
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <LangGraphAdvanceBar
        detail={detail()}
        onAdvanced={() => {}}
        onRefresh={onRefresh}
      />,
    )

    fireEvent.click(screen.getByTestId('langgraph-advance-button'))
    await screen.findByText('状态已更新')
    expect(onRefresh).toHaveBeenCalledOnce()

    const refreshed = detail()
    refreshed.state_version = 3
    rerender(
      <LangGraphAdvanceBar
        detail={refreshed}
        onAdvanced={() => {}}
        onRefresh={onRefresh}
      />,
    )
    fireEvent.click(screen.getByTestId('langgraph-advance-button'))

    await waitFor(() => expect(advance).toHaveBeenCalledTimes(2))
    expect(advance.mock.calls[0][2]).toEqual({
      idempotencyKey: 'advance-idem-1',
      stateVersion: 2,
    })
    expect(advance.mock.calls[1][2]).toEqual({
      idempotencyKey: 'advance-idem-2',
      stateVersion: 3,
    })
  })
})
