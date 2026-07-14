/**
 * 悬壶 WebUI —— agent_name → stage 映射工具
 *
 * 每个 agent_name 对应一个流程阶段；review/record 等无独立 agent，
 * 此映射供 StepBar 显示 agent 运行状态时使用。
 */

import type { AgentName, SessionDetail, Stage } from '@/types/api'

const AGENT_TO_STAGE: Partial<Record<AgentName, Stage>> = {
  inquiry: 'inquiry',
  sufficiency: 'sufficiency',
  syndrome: 'syndrome',
  prescription: 'prescription',
  modification: 'modification',
  safety: 'safety',
}

/**
 * 把 agent_name 映射到对应 stage。
 * supervisor/record 等无对应 stage 的返回 null。
 */
export function agentNameToStage(name: string): Stage | null {
  return AGENT_TO_STAGE[name as AgentName] ?? null
}

/** Only a persisted, current LangGraph completeness gate may enable advance. */
export function canAdvanceLangGraph(detail: SessionDetail): boolean {
  if (
    detail.agent_runtime !== 'langgraph'
    || detail.current_stage !== 'inquiry'
    || detail.status !== 'active'
  ) {
    return false
  }
  const triage = detail.read_model.gates.find((gate) => gate.gate_name === 'triage')
  const completeness = detail.read_model.gates.find((gate) => gate.gate_name === 'completeness')
  return Boolean(
    triage?.decision === 'passed'
      && triage.details?.disposition === 'continue'
      && completeness?.decision === 'passed'
      && completeness.details?.disposition === 'ready'
      && !detail.read_model.unresolved.some((item) => item.source === 'triage'),
  )
}
