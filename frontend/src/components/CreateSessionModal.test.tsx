import { describe, expect, it, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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

function form(): HTMLFormElement {
  const forms = document.querySelectorAll('form')
  if (forms.length === 0) throw new Error('form not found')
  return forms[forms.length - 1] as HTMLFormElement
}

function chiefComplaintInput(): HTMLTextAreaElement {
  const inputs = document.querySelectorAll('#chief_complaint')
  if (inputs.length === 0) throw new Error('chief_complaint input not found')
  return inputs[inputs.length - 1] as HTMLTextAreaElement
}

function chiefComplaintHelp(): HTMLElement | null {
  const helps = document.querySelectorAll('#chief_complaint_help')
  return helps.length > 0 ? (helps[helps.length - 1] as HTMLElement) : null
}

describe('CreateSessionModal', () => {
  it('未填主诉时校验失败，不调用 onSubmit', async () => {
    const onSubmit = vi.fn()
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={() => {}} onSubmit={onSubmit} />,
    )
    fireEvent.submit(form())
    await waitFor(() => {
      expect(chiefComplaintHelp()?.textContent).toContain('请输入主诉内容')
    })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('填写主诉后提交调用 onSubmit 并关闭', async () => {
    const onSubmit = vi.fn<(b: SessionCreateRequest) => Promise<string>>().mockResolvedValue('s-1')
    const onClose = vi.fn()
    wrap(
      <CreateSessionModal open={true} creating={false} onClose={onClose} onSubmit={onSubmit} />,
    )
    const textarea = chiefComplaintInput()
    expect(textarea).not.toBeNull()
    fireEvent.change(textarea, { target: { value: '头痛三天' } })
    await waitFor(() => {
      expect(textarea.value).toBe('头痛三天')
    })
    fireEvent.submit(form())
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].chief_complaint).toBe('头痛三天')
    expect(onClose).toHaveBeenCalled()
  }, 15000)
})
