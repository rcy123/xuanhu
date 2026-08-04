import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { ThinkingHint } from './ThinkingHint'

function wrap(node: React.ReactNode) {
  return render(
    <ConfigProvider>
      <AntdApp>{node}</AntdApp>
    </ConfigProvider>,
  )
}

describe('ThinkingHint', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('非激活时不渲染', () => {
    wrap(<ThinkingHint active={false} />)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('激活且无 agent 时显示通用短语', () => {
    wrap(<ThinkingHint active agent={null} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getAllByText('正在思考…').length).toBeGreaterThan(0)
  })

  it('按 agent 名称显示对应短语', () => {
    wrap(<ThinkingHint active agent="intake" />)
    expect(screen.getAllByText('正在理解患者描述…').length).toBeGreaterThan(0)
  })

  it('agent 变化时切换到对应短语组', () => {
    const { rerender } = wrap(<ThinkingHint active agent="intake" />)
    expect(screen.getAllByText('正在理解患者描述…').length).toBeGreaterThan(0)
    rerender(
      <ConfigProvider>
        <AntdApp>
          <ThinkingHint active agent="question_composer" />
        </AntdApp>
      </ConfigProvider>,
    )
    expect(screen.getAllByText('正在分析问诊进度…').length).toBeGreaterThan(0)
  })

  it('短语按间隔循环切换', () => {
    wrap(<ThinkingHint active agent="intake" />)
    expect(screen.getAllByText('正在理解患者描述…').length).toBeGreaterThan(0)
    act(() => vi.advanceTimersByTime(2_200))
    expect(screen.getAllByText('正在梳理症状要点…').length).toBeGreaterThan(0)
    act(() => vi.advanceTimersByTime(2_200))
    expect(screen.getAllByText('正在提取关键信息…').length).toBeGreaterThan(0)
    // 循环回到第一条
    act(() => vi.advanceTimersByTime(2_200))
    expect(screen.getAllByText('正在核对问诊要素…').length).toBeGreaterThan(0)
    act(() => vi.advanceTimersByTime(2_200))
    expect(screen.getAllByText('正在理解患者描述…').length).toBeGreaterThan(0)
  })

  it('失活后停止循环', () => {
    const { rerender } = wrap(<ThinkingHint active agent="intake" />)
    rerender(
      <ConfigProvider>
        <AntdApp>
          <ThinkingHint active={false} agent="intake" />
        </AntdApp>
      </ConfigProvider>,
    )
    expect(screen.queryByRole('status')).toBeNull()
  })
})
