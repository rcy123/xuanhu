import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, fireEvent, waitFor } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { CreateSessionModal } from './CreateSessionModal'
import type { SessionCreateRequest } from '@/types/api'

function wrap(node: React.ReactNode) {
  return render(
    <ConfigProvider>
      <AntdApp>{node}</AntdApp>
    </ConfigProvider>,
  )
}

function currentModal(): HTMLElement {
  const modals = document.querySelectorAll('.ant-modal')
  const modal = Array.from(modals)
    .reverse()
    .find((item) => item.querySelector('#chief_complaint'))
  if (!modal) throw new Error('modal not found')
  return modal as HTMLElement
}

function chiefComplaintInput(): HTMLTextAreaElement {
  const input = currentModal().querySelector('#chief_complaint')
  if (!input) throw new Error('chief_complaint input not found')
  return input as HTMLTextAreaElement
}

function okButton(): HTMLButtonElement {
  const button = currentModal().querySelector('.ant-modal-footer .ant-btn-primary')
  if (!button) throw new Error('ok button not found')
  return button as HTMLButtonElement
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('CreateSessionModal', () => {
  it('rejects empty chief complaint without calling onSubmit', async () => {
    const onSubmit = vi.fn()
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={() => {}} onSubmit={onSubmit} />,
    )

    const textarea = chiefComplaintInput()
    expect(textarea).toHaveAttribute('aria-required', 'true')
    fireEvent.click(okButton())

    await Promise.resolve()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits chief complaint and closes modal', async () => {
    const onSubmit = vi.fn<(b: SessionCreateRequest) => Promise<string>>().mockResolvedValue('s-1')
    const onClose = vi.fn()
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={onClose} onSubmit={onSubmit} />,
    )

    const textarea = chiefComplaintInput()
    fireEvent.change(textarea, { target: { value: 'headache for three days' } })
    await waitFor(() => {
      expect(textarea.value).toBe('headache for three days')
    })

    fireEvent.click(okButton())

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].chief_complaint).toBe('headache for three days')
    expect(onClose).toHaveBeenCalled()
  })
})
