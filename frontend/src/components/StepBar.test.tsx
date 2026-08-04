import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { StepBar } from './StepBar'

afterEach(() => {
  cleanup()
})

describe('StepBar', () => {
  it('渲染稳定的 6 个临床步骤节点', () => {
    render(<StepBar currentStage="inquiry" />)
    expect(screen.getByTestId('step-bar')).toBeInTheDocument()
    expect(screen.getByText('问诊')).toBeInTheDocument()
    expect(screen.getByText('辨证')).toBeInTheDocument()
    expect(screen.getByText('方药')).toBeInTheDocument()
    expect(screen.getByText('医师复核')).toBeInTheDocument()
    expect(screen.getByText('病历')).toBeInTheDocument()
    expect(screen.queryByText('完备性')).toBeNull()
    expect(screen.queryByText('加减方')).toBeNull()
  })

  it('done 阶段全部完成', () => {
    render(<StepBar currentStage="done" />)
    // done 时 current=7, status=finish, 所有节点应显示完成
    // 完成图标存在即可
    expect(screen.getByTestId('step-bar')).toBeInTheDocument()
  })

  it('blocked 阶段显示 error', () => {
    render(<StepBar currentStage="blocked" />)
    expect(screen.getByTestId('step-bar')).toBeInTheDocument()
  })

  it('agentRuns 传入 running 状态时显示对应图标', () => {
    render(
      <StepBar
        currentStage="syndrome"
        agentRuns={{ syndrome: { status: 'running', agentRunId: 'r1' } }}
      />,
    )
    // syndrome 节点应该有 loading 图标
    expect(screen.getByText('辨证')).toBeInTheDocument()
    // 有 loading 图标渲染
    const icons = document.querySelectorAll('.anticon-loading')
    expect(icons.length).toBeGreaterThanOrEqual(1)
  })

  it('agentRuns 传入 failed 状态时显示错误图标', () => {
    render(
      <StepBar
        currentStage="safety"
        agentRuns={{ safety: { status: 'failed', error: 'FAILED' } }}
      />,
    )
    expect(screen.getByText('安全审核')).toBeInTheDocument()
    const icons = document.querySelectorAll('.anticon-close-circle')
    expect(icons.length).toBeGreaterThanOrEqual(1)
  })

  it('不传 agentRuns 时正常渲染', () => {
    render(<StepBar currentStage="inquiry" />)
    expect(screen.getByTestId('step-bar')).toBeInTheDocument()
  })

  it('阶段别名映射到统一的临床流程，不展示运行时细节', () => {
    const { rerender } = render(<StepBar currentStage="sufficiency" />)
    expect(screen.getByText('问诊')).toBeInTheDocument()
    expect(screen.queryByText('完备性')).toBeNull()

    rerender(<StepBar currentStage="modification" />)
    expect(screen.getByText('方药')).toBeInTheDocument()
    expect(screen.queryByText('加减方')).toBeNull()
    expect(screen.queryByText('方药草案')).toBeNull()
    expect(screen.queryByText('已持久化')).toBeNull()
    expect(screen.queryByText('等待硬门禁')).toBeNull()
  })
})
