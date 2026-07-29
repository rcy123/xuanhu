/**
 * 悬壶 WebUI —— 步骤条（P8-3 增强）
 *
 * 基于 current_stage 计算 current index。
 * 新增 agentRuns prop：按 Agent 节点显示运行状态（running/done/failed）。
 * 不接 SSE、不支持点击跳转（P8-4 再做）。
 */

import { Steps } from 'antd'
import { LoadingOutlined, CheckCircleFilled, CloseCircleFilled } from '@ant-design/icons'
import type { AgentRuntime, SessionReadModel, Stage } from '@/types/api'
import { stageStepIndex, stepNodesForRuntime } from '@/utils/stage'
import { agentNameToStage } from '@/utils/agent'
import type { AgentRunState } from '@/hooks/useSessionStream'

interface StepBarProps {
  currentStage: Stage | null
  agentRuntime?: AgentRuntime
  readModel?: SessionReadModel
  /** per-agent 运行状态（key = agent_name e.g. "syndrome", "safety"）。 */
  agentRuns?: Record<string, AgentRunState>
}

/** 把 agentRuns 转换为 stage → status 的映射。 */
function resolveAgentStatus(
  agentRuns: Record<string, AgentRunState> | undefined,
): Record<string, AgentRunState['status']> {
  if (!agentRuns) return {}
  const result: Record<string, AgentRunState['status']> = {}
  for (const [agentName, run] of Object.entries(agentRuns)) {
    const stage = agentNameToStage(agentName)
    if (stage) {
      result[stage] = run.status
    }
  }
  return result
}

export function StepBar({
  currentStage,
  agentRuntime = 'legacy',
  readModel,
  agentRuns,
}: StepBarProps) {
  const nodes = stepNodesForRuntime(agentRuntime)
  const step = currentStage ? stageStepIndex(currentStage, agentRuntime) : undefined
  let current = step ?? 0
  let overallStatus: 'process' | 'finish' | 'error' = 'process'
  if (currentStage === 'done') {
    current = nodes.length
    overallStatus = 'finish'
  } else if (currentStage === 'blocked') {
    current = step ?? 0
    overallStatus = 'error'
  }

  const agentStatus = resolveAgentStatus(agentRuns)

  const artifactTypes = new Set(readModel?.artifacts.map((item) => item.artifact_type) ?? [])
  const items = nodes.map((n) => {
    const status = agentStatus[n.stage]
    let icon: React.ReactNode | undefined
    if (status === 'running') {
      icon = <LoadingOutlined style={{ color: 'var(--xh-primary)' }} />
    } else if (status === 'done') {
      icon = <CheckCircleFilled style={{ color: 'var(--xh-success)' }} />
    } else if (status === 'failed') {
      icon = <CloseCircleFilled style={{ color: 'var(--xh-error)' }} />
    }
    // 已完成阶段（index < current）用默认 ✓ 图标
    // 仅在当前或未来阶段显示 agent 状态图标
    const idx = nodes.findIndex((x) => x.stage === n.stage)
    const showAgentIcon = idx >= current && icon !== undefined
    let description: string | undefined
    if (agentRuntime === 'langgraph') {
      if (status === 'running') description = '运行中'
      else if (status === 'failed') description = '执行失败'
      else if (
        (n.stage === 'syndrome' && artifactTypes.has('syndrome_draft'))
        || (n.stage === 'formula' && artifactTypes.has('formula_draft'))
      ) description = '已持久化'
      else if (n.stage === 'review' && readModel?.review_required) description = '等待硬门禁'
    }

    return {
      title: n.label,
      description,
      icon: showAgentIcon ? icon : undefined,
    }
  })

  return (
    <div
      data-testid="step-bar"
      data-runtime={agentRuntime}
      style={{
        background: 'var(--xh-bg-card)',
        border: '1px solid var(--xh-border)',
        borderRadius: 'var(--xh-radius-card)',
        padding: 'var(--xh-space-l)',
        marginBottom: 'var(--xh-space-l)',
      }}
    >
      <Steps current={current} status={overallStatus} size="small" items={items} style={{ padding: 0 }} />
    </div>
  )
}

export default StepBar
