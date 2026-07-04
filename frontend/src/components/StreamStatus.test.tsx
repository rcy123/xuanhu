import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { StreamStatus } from './StreamStatus'

describe('StreamStatus', () => {
  afterEach(() => {
    cleanup()
  })

  it('connected 显示绿点实时', () => {
    render(<StreamStatus state="connected" />)
    expect(screen.getByTestId('stream-status')).toBeInTheDocument()
    expect(screen.getByText('实时')).toBeInTheDocument()
  })

  it('connecting 显示连接中', () => {
    render(<StreamStatus state="connecting" />)
    expect(screen.getByText('连接中…')).toBeInTheDocument()
  })

  it('polling 显示同步中（轮询）和重连按钮', () => {
    const onReconnect = vi.fn()
    render(<StreamStatus state="polling" onReconnect={onReconnect} />)
    expect(screen.getByText('同步中（轮询）')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('stream-reconnect'))
    expect(onReconnect).toHaveBeenCalled()
  })

  it('disconnected 显示已断开和重连按钮', () => {
    render(<StreamStatus state="disconnected" onReconnect={() => {}} />)
    expect(screen.getByText('已断开')).toBeInTheDocument()
    expect(screen.getByTestId('stream-reconnect')).toBeInTheDocument()
  })

  it('idle 不渲染', () => {
    const { container } = render(<StreamStatus state="idle" />)
    expect(container.firstChild).toBeNull()
  })

  it('runningAgent 非空时追加运行提示', () => {
    render(<StreamStatus state="connected" runningAgent="辨证" />)
    expect(screen.getByText('· 辨证 运行中')).toBeInTheDocument()
  })
})
