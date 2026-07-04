/**
 * 悬壶 WebUI —— 步骤条（只读，P8-2）
 *
 * 基于 current_stage 计算 current index。不接 SSE、不支持点击跳转（P8-3 再做）。
 */

import { Steps } from 'antd'
import type { Stage } from '@/types/api'
import { STEP_NODES, stageMeta } from '@/utils/stage'

interface StepBarProps {
  currentStage: Stage | null
}

export function StepBar({ currentStage }: StepBarProps) {
  const step = currentStage ? stageMeta(currentStage).step : undefined
  // 终态：done 显示全部完成；blocked 显示当前阶段为 error
  let current = step ?? 0
  let status: 'process' | 'finish' | 'error' = 'process'
  if (currentStage === 'done') {
    current = STEP_NODES.length
    status = 'finish'
  } else if (currentStage === 'blocked') {
    current = step ?? 0
    status = 'error'
  }

  const items = STEP_NODES.map((n) => ({ title: n.label }))

  return (
    <div
      data-testid="step-bar"
      style={{
        background: 'var(--xh-bg-card)',
        border: '1px solid var(--xh-border)',
        borderRadius: 'var(--xh-radius-card)',
        padding: 'var(--xh-space-l)',
        marginBottom: 'var(--xh-space-l)',
      }}
    >
      <Steps current={current} status={status} size="small" items={items} style={{ padding: 0 }} />
    </div>
  )
}

export default StepBar