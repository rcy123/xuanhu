import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { RejectModal } from './RejectModal'

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('RejectModal', () => {
  it('打开时 feedback 为空', () => {
    render(
      <RejectModal
        open={true}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const textarea = screen.getByTestId('reject-feedback') as HTMLTextAreaElement
    expect(textarea.value).toBe('')
  })

  it('提交 feedback 调用 onSubmit', () => {
    const onSubmit = vi.fn()
    render(
      <RejectModal
        open={true}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    )
    const textarea = screen.getByTestId('reject-feedback') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '辨证存疑' } })
    fireEvent.click(screen.getByTestId('reject-submit-btn'))
    expect(onSubmit).toHaveBeenCalledWith('辨证存疑')
  })

  it('空 feedback 提交为 ""', () => {
    const onSubmit = vi.fn()
    render(
      <RejectModal
        open={true}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.click(screen.getByTestId('reject-submit-btn'))
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByTestId('reject-submit-btn')).toBeDisabled()
  })

  it('关闭时调用 onCancel', () => {
    const onCancel = vi.fn()
    render(
      <RejectModal
        open={true}
        submitting={false}
        onCancel={onCancel}
        onSubmit={vi.fn()}
      />,
    )
    const closeBtn = document.querySelector('.ant-modal-close') as HTMLElement
    fireEvent.click(closeBtn)
    expect(onCancel).toHaveBeenCalled()
  })

  it('submitting 时提交按钮 disabled', () => {
    render(
      <RejectModal
        open={true}
        submitting={true}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const btn = screen.getByTestId('reject-submit-btn') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})
