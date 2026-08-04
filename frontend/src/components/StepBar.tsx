/**
 * 悬壶 WebUI —— 步骤条（P8-3 增强）
 *
 * 使用稳定的临床流程展示 current_stage，避免不同执行运行时改变步骤数量或标题。
 * agentRuns 仅用于展示当前步骤的运行/失败状态，不暴露内部编排差异。
 */

import { CheckOutlined, LoadingOutlined, CloseCircleFilled } from '@ant-design/icons'
import type { Stage } from '@/types/api'
import { agentNameToStage } from '@/utils/agent'
import type { AgentRunState } from '@/hooks/useSessionStream'

interface StepBarProps {
  currentStage: Stage | null
  /** per-agent 运行状态（key = agent_name e.g. "syndrome", "safety"）。 */
  agentRuns?: Record<string, AgentRunState>
}

const CLINICAL_STEP_NODES = [
  { stage: 'inquiry', label: '问诊' },
  { stage: 'syndrome', label: '辨证' },
  { stage: 'formula', label: '方药' },
  { stage: 'safety', label: '安全审核' },
  { stage: 'review', label: '医师复核' },
  { stage: 'record', label: '病历' },
] as const

type ClinicalStepStage = (typeof CLINICAL_STEP_NODES)[number]['stage']

function normalizeClinicalStage(stage: Stage): ClinicalStepStage | null {
  switch (stage) {
    case 'inquiry':
    case 'sufficiency':
      return 'inquiry'
    case 'syndrome':
      return 'syndrome'
    case 'formula':
    case 'prescription':
    case 'modification':
      return 'formula'
    case 'safety':
      return 'safety'
    case 'review':
      return 'review'
    case 'record':
    case 'done':
      return 'record'
    default:
      return null
  }
}

function clinicalStepIndex(stage: Stage): number | undefined {
  const normalized = normalizeClinicalStage(stage)
  if (!normalized) return undefined
  const index = CLINICAL_STEP_NODES.findIndex((node) => node.stage === normalized)
  return index >= 0 ? index : undefined
}

/** 把 agentRuns 转换为 stage → status 的映射。 */
function resolveAgentStatus(
  agentRuns: Record<string, AgentRunState> | undefined,
): Partial<Record<ClinicalStepStage, AgentRunState['status']>> {
  if (!agentRuns) return {}
  const result: Partial<Record<ClinicalStepStage, AgentRunState['status']>> = {}
  const priority: Record<AgentRunState['status'], number> = {
    done: 1,
    running: 2,
    failed: 3,
  }
  for (const [agentName, run] of Object.entries(agentRuns)) {
    const stage = agentNameToStage(agentName)
    const clinicalStage = stage ? normalizeClinicalStage(stage) : null
    if (
      clinicalStage
      && (!result[clinicalStage] || priority[run.status] >= priority[result[clinicalStage]])
    ) {
      result[clinicalStage] = run.status
    }
  }
  return result
}

export function StepBar({
  currentStage,
  agentRuns,
}: StepBarProps) {
  const step = currentStage ? clinicalStepIndex(currentStage) : undefined
  let current = step ?? 0
  if (currentStage === 'done') {
    current = CLINICAL_STEP_NODES.length
  } else if (currentStage === 'blocked') {
    current = step ?? 0
  }

  const agentStatus = resolveAgentStatus(agentRuns)

  const items = CLINICAL_STEP_NODES.map((n, idx) => {
    const status = agentStatus[n.stage]
    const isCurrent = currentStage !== 'done' && idx === current
    const isComplete = currentStage === 'done' || idx < current
    const isFailed = status === 'failed' || (currentStage === 'blocked' && idx === current)
    const isRunning = status === 'running' && isCurrent
    const state = isFailed
      ? 'error'
      : isRunning
        ? 'running'
        : isComplete
          ? 'complete'
          : isCurrent
            ? 'current'
            : 'pending'

    const marker = isFailed
      ? <CloseCircleFilled />
      : isRunning
        ? <LoadingOutlined />
        : isComplete
          ? <CheckOutlined />
          : idx + 1

    return {
      ...n,
      marker,
      state,
    }
  })

  return (
    <nav
      data-testid="step-bar"
      className="xh-step-bar"
      aria-label="诊疗流程"
    >
      <ol className="xh-clinical-flow">
        {items.map((item, idx) => (
          <li
            key={item.stage}
            className={`xh-flow-step is-${item.state}`}
            aria-current={item.state === 'current' || item.state === 'running' ? 'step' : undefined}
          >
            <span className="xh-flow-marker" aria-hidden="true">{item.marker}</span>
            <span className="xh-flow-label">{item.label}</span>
            {idx < items.length - 1 ? <span className="xh-flow-separator" aria-hidden="true">›</span> : null}
          </li>
        ))}
      </ol>
    </nav>
  )
}

export default StepBar
