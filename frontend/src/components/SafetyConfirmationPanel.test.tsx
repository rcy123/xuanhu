import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '@/api/index'
import { ApiRequestError } from '@/api/errors'
import type { SafetyFactAssertion } from '@/types/api'
import { SafetyConfirmationPanel } from './SafetyConfirmationPanel'

const assertion: SafetyFactAssertion = {
  schema_version: 'safety-fact-assertion.v1',
  assertion_id: 'assertion-1',
  session_id: 'session-1',
  field_name: 'allergy',
  value: { collection_status: 'explicitly_none', values: null },
  value_digest: 'a'.repeat(64),
  status: 'proposed',
  source_kind: 'deterministic_reply_binding',
  source_message_id: 'message-1',
  extraction_run_id: 'run-1',
  template_version: 'intake_extraction_v2.jinja2',
  evidence_spans: [{
    source_message_id: 'message-1',
    start_char: 0,
    end_char: 2,
    quote_digest: 'b'.repeat(64),
    reply_to_question_message_id: 'question-1',
    reply_dimension: 'safety.allergy_status',
  }],
  evidence_digest: 'c'.repeat(64),
  proposed_at: '2026-07-29T10:00:00Z',
}

describe('SafetyConfirmationPanel', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listSafetyAssertions').mockResolvedValue({ items: [assertion] })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('confirms with the JWT identity (no manual reviewer entry)', async () => {
    const confirm = vi.spyOn(api, 'confirmSafetyAssertion').mockResolvedValue({
      ...assertion,
      status: 'confirmed',
      confirmed_at: '2026-07-29T10:01:00Z',
    })
    vi.mocked(api.listSafetyAssertions)
      .mockResolvedValueOnce({ items: [assertion] })
      .mockResolvedValueOnce({ items: [] })
    const onChanged = vi.fn().mockResolvedValue(undefined)
    const onPendingChange = vi.fn()

    render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        pendingHint
        blocksFreeInput
        onChanged={onChanged}
        onPendingChange={onPendingChange}
      />,
    )

    expect(await screen.findByTestId('safety-confirmation-panel')).toBeInTheDocument()
    expect(screen.getByText('明确回答：无')).toBeInTheDocument()
    expect(screen.getByTestId('safety-confirmation-blocked-input')).toBeInTheDocument()
    expect(screen.queryByTestId('safety-reviewer-id')).not.toBeInTheDocument()

    const confirmButton = screen.getByTestId('safety-confirm-assertion-1')
    expect(confirmButton).toBeEnabled()
    fireEvent.click(confirmButton)

    await waitFor(() => {
      expect(confirm).toHaveBeenCalledWith(
        'session-1',
        'assertion-1',
        {},
        {
          idempotencyKey: expect.any(String),
        },
      )
      expect(onChanged).toHaveBeenCalledTimes(1)
    })
    expect(onPendingChange).toHaveBeenLastCalledWith(0)
  })

  it('rejects the extraction with an auditable reason code', async () => {
    const reject = vi.spyOn(api, 'rejectSafetyAssertion').mockResolvedValue({
      ...assertion,
      status: 'rejected',
      rejected_at: '2026-07-29T10:01:00Z',
    })
    vi.mocked(api.listSafetyAssertions)
      .mockResolvedValueOnce({ items: [assertion] })
      .mockResolvedValueOnce({ items: [] })

    render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        onChanged={vi.fn()}
      />,
    )

    await screen.findByTestId('safety-confirmation-panel')
    fireEvent.click(screen.getByTestId('safety-reject-assertion-1'))

    await waitFor(() => {
      expect(reject).toHaveBeenCalledWith(
        'session-1',
        'assertion-1',
        { reason_code: 'EXTRACTION_REJECTED' },
        {
          idempotencyKey: expect.any(String),
        },
      )
    })
  })

  it('reuses the same decision key after an uncertain response loss', async () => {
    const lostResponse = new ApiRequestError({
      code: 'NETWORK_ERROR',
      userMessage: 'response lost',
      status: 0,
      retryable: true,
    })
    const confirm = vi.spyOn(api, 'confirmSafetyAssertion')
      .mockRejectedValueOnce(lostResponse)
      .mockResolvedValueOnce({ ...assertion, status: 'confirmed' })
    vi.mocked(api.listSafetyAssertions)
      .mockResolvedValueOnce({ items: [assertion] })
      .mockResolvedValueOnce({ items: [] })

    render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        onChanged={vi.fn()}
      />,
    )
    await screen.findByTestId('safety-confirmation-panel')
    fireEvent.click(screen.getByTestId('safety-confirm-assertion-1'))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('safety-confirm-assertion-1')).toBeEnabled())

    fireEvent.click(screen.getByTestId('safety-confirm-assertion-1'))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))

    expect(confirm.mock.calls[1][3]?.idempotencyKey).toBe(
      confirm.mock.calls[0][3]?.idempotencyKey,
    )
  })

  it('does not render a review card when there are no proposed facts', async () => {
    vi.mocked(api.listSafetyAssertions).mockResolvedValue({ items: [] })
    render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        onChanged={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()
    })
  })

  it('excludes red flags because they are resolved by triage/recovery', async () => {
    vi.mocked(api.listSafetyAssertions).mockResolvedValue({
      items: [{
        ...assertion,
        assertion_id: 'red-flag-1',
        field_name: 'red_flag',
        value: { category: 'dyspnea', severity: 'high' },
      }],
    })
    const onPendingChange = vi.fn()

    render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        pendingHint
        onChanged={vi.fn()}
        onPendingChange={onPendingChange}
      />,
    )

    await waitFor(() => expect(onPendingChange).toHaveBeenLastCalledWith(0))
    expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('safety-confirm-red-flag-1')).not.toBeInTheDocument()
  })

  it('ignores an old confirmation response after switching sessions', async () => {
    let resolveConfirmation!: (value: SafetyFactAssertion) => void
    const confirmation = new Promise<SafetyFactAssertion>((resolve) => {
      resolveConfirmation = resolve
    })
    vi.spyOn(api, 'confirmSafetyAssertion').mockReturnValue(confirmation)
    vi.mocked(api.listSafetyAssertions).mockImplementation(async (sessionId) => (
      sessionId === 'session-1' ? { items: [assertion] } : { items: [] }
    ))
    const oldPendingChange = vi.fn()
    const newPendingChange = vi.fn()
    const oldChanged = vi.fn()
    const { rerender } = render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        onChanged={oldChanged}
        onPendingChange={oldPendingChange}
      />,
    )

    await screen.findByTestId('safety-confirmation-panel')
    fireEvent.click(screen.getByTestId('safety-confirm-assertion-1'))

    rerender(
      <SafetyConfirmationPanel
        sessionId="session-2"
        refreshKey={1}
        enabled
        onChanged={vi.fn()}
        onPendingChange={newPendingChange}
      />,
    )
    await waitFor(() => expect(newPendingChange).toHaveBeenLastCalledWith(0))

    resolveConfirmation({ ...assertion, status: 'confirmed' })
    await waitFor(() => expect(api.confirmSafetyAssertion).toHaveBeenCalledTimes(1))
    await Promise.resolve()

    expect(oldPendingChange).not.toHaveBeenCalledWith(0)
    expect(oldChanged).not.toHaveBeenCalled()
    expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()
  })

  it('reloads when the authoritative state version changes in the same inquiry', async () => {
    vi.mocked(api.listSafetyAssertions)
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [assertion] })
    const onPendingChange = vi.fn()
    const { rerender } = render(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={1}
        enabled
        onChanged={vi.fn()}
        onPendingChange={onPendingChange}
      />,
    )

    await waitFor(() => expect(api.listSafetyAssertions).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('safety-confirmation-panel')).not.toBeInTheDocument()

    rerender(
      <SafetyConfirmationPanel
        sessionId="session-1"
        refreshKey={2}
        enabled
        onChanged={vi.fn()}
        onPendingChange={onPendingChange}
      />,
    )

    expect(await screen.findByTestId('safety-confirmation-panel')).toBeInTheDocument()
    expect(onPendingChange).toHaveBeenLastCalledWith(1)
  })
})
