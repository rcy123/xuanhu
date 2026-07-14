import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, fireEvent, screen, waitFor } from '@testing-library/react'
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
  vi.unstubAllEnvs()
})

describe('CreateSessionModal', () => {
  it('keeps the LangGraph create option hidden when the feature flag is off by default', () => {
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={() => {}} onSubmit={vi.fn()} />,
    )

    expect(currentModal().querySelector('#agent_runtime')).not.toBeInTheDocument()
    expect(screen.queryByText(/LangGraph/)).not.toBeInTheDocument()
  })

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
    expect(onSubmit.mock.calls[0][0].agent_runtime).toBe('legacy')
    expect(onClose).toHaveBeenCalled()
  })

  it('shows a non-clinical limited-rollout option and submits LangGraph only when enabled', async () => {
    vi.stubEnv('VITE_LANGGRAPH_UI_ENABLED', 'true')
    const onSubmit = vi.fn<(b: SessionCreateRequest) => Promise<string>>().mockResolvedValue('s-lg')
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={() => {}} onSubmit={onSubmit} />,
    )

    expect(screen.getByText('问诊运行时（非临床 / 限量灰度）')).toBeInTheDocument()
    expect(screen.getByText(/LangGraph 仅用于非临床验证与限量灰度/)).toBeInTheDocument()

    const runtimeSelect = screen.getByTestId('agent-runtime-select')
    const runtimeControl = runtimeSelect.querySelector('.ant-select-content')
    if (!runtimeControl) throw new Error('agent_runtime select not found')
    fireEvent.mouseDown(runtimeControl)
    const langGraphOption = await screen.findByText('LangGraph（非临床 · 限量灰度）', {
      selector: '.ant-select-item-option-content',
    })
    fireEvent.click(langGraphOption)
    await waitFor(() => expect(runtimeSelect).toHaveTextContent('LangGraph（非临床 · 限量灰度）'))

    fireEvent.change(chiefComplaintInput(), { target: { value: 'non-clinical validation' } })
    fireEvent.click(okButton())

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0][0].agent_runtime).toBe('langgraph')
  })
})
