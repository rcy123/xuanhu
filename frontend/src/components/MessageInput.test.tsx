import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { MessageInput } from './MessageInput'

function wrap(node: React.ReactNode) {
  return render(
    <ConfigProvider>
      <AntdApp>{node}</AntdApp>
    </ConfigProvider>,
  )
}

describe('MessageInput', () => {
  it('输入内容并点击发送触发 onSubmit，提交后清空', () => {
    const onSubmit = vi.fn()
    wrap(<MessageInput submitting={false} error={null} onSubmit={onSubmit} onRetry={() => {}} />)
    const input = screen.getByTestId('message-input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '头痛三天' } })
    fireEvent.click(screen.getByText('发送'))
    expect(onSubmit).toHaveBeenCalledWith('头痛三天')
  })

  it('空内容不触发提交', () => {
    const onSubmit = vi.fn()
    wrap(<MessageInput submitting={false} error={null} onSubmit={onSubmit} onRetry={() => {}} />)
    const buttons = screen.getAllByText('发送')
    fireEvent.click(buttons[0])
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submitting 时发送按钮 disabled', () => {
    const onSubmit = vi.fn()
    wrap(<MessageInput submitting={true} error={null} onSubmit={onSubmit} onRetry={() => {}} />)
    const buttons = screen.getAllByText('发送')
    const btn = buttons[buttons.length - 1]
    // Button is rendered as <button> with <span> inside; check the parent button
    const parentBtn = btn.closest('button')
    expect(parentBtn).toBeDisabled()
  })

  it('disabled 时输入框禁用', () => {
    wrap(
      <MessageInput submitting={false} error={null} disabled onSubmit={() => {}} onRetry={() => {}} />,
    )
    const inputs = screen.getAllByTestId('message-input')
    const input = inputs[inputs.length - 1]
    expect(input).toHaveAttribute('disabled')
  })
})
