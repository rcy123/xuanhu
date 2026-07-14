import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '@/api'
import { ApiRequestError } from '@/api/errors'
import type { SessionDetail } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'
import { LangGraphAdvanceBar } from './LangGraphAdvanceBar'

vi.mock('@/utils/id', () => ({ generateIdempotencyKey: () => 'advance-idem-1' }))

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
  })

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
    render(<LangGraphAdvanceBar detail={detail()} onAdvanced={() => {}} />)

    fireEvent.click(screen.getByTestId('langgraph-advance-button'))
    await screen.findByText('网络失败')
    fireEvent.click(screen.getByTestId('langgraph-advance-button'))

    await waitFor(() => expect(advance).toHaveBeenCalledTimes(2))
    expect(advance.mock.calls[0][2]?.idempotencyKey).toBe('advance-idem-1')
    expect(advance.mock.calls[1][2]?.idempotencyKey).toBe('advance-idem-1')
  })
})
