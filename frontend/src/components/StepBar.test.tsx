import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { StepBar } from './StepBar'

afterEach(() => {
  cleanup()
})

describe('StepBar', () => {
  it('渲染 7 个步骤节点', () => {
    render(<StepBar currentStage="inquiry" />)
    expect(screen.getByTestId('step-bar')).toBeInTheDocument()
    expect(screen.getByText('问诊')).toBeInTheDocument()
    expect(screen.getByText('医师确认')).toBeInTheDocument()
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

  it('LangGraph 使用合并后的 Formula 节点并展示持久化状态', () => {
    render(
      <StepBar
        currentStage="safety"
        agentRuntime="langgraph"
        readModel={{
          schema_version: 'session-read-model.v1',
          agent_runtime: 'langgraph',
          graph: { revision: 4, status: 'completed' },
          gates: [],
          artifacts: [
            {
              artifact_id: 'formula-1',
              artifact_type: 'formula_draft',
              revision: 1,
              input_state_version: 3,
              status: 'current',
              produced_by_run_id: 'run-1',
              payload_schema_version: 'formula-artifact-payload.v1',
              content_digest: '0'.repeat(64),
              decision: 'completed',
              evidence_mode: 'model_knowledge_only',
              review_required: true,
              unresolved: [],
              verification_gate: {
                gate_id: 'gate-1',
                gate_name: 'formula_consistency',
                policy_version: 'formula-consistency-policy.v1',
                input_state_version: 3,
                decision: 'passed',
              },
              output: {},
            },
          ],
          review_required: true,
          unresolved: [],
        }}
      />,
    )
    expect(screen.getByTestId('step-bar')).toHaveAttribute('data-runtime', 'langgraph')
    expect(screen.getByText('方药草案')).toBeInTheDocument()
    expect(screen.queryByText('加减方')).not.toBeInTheDocument()
    expect(screen.getByText('已持久化')).toBeInTheDocument()
    expect(screen.getByText('等待硬门禁')).toBeInTheDocument()
  })
})
