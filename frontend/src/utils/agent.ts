/**
 * 悬壶 WebUI —— agent_name → stage 映射工具
 *
 * 每个 agent_name 对应一个流程阶段；review/record 等无独立 agent，
 * 此映射供 StepBar 显示 agent 运行状态时使用。
 */

import type { AgentName, SessionDetail, Stage } from '@/types/api'

const AGENT_TO_STAGE: Partial<Record<AgentName, Stage>> = {
  intake: 'inquiry',
  inquiry: 'inquiry',
  question_composer: 'inquiry',
  sufficiency: 'sufficiency',
  reasoning: 'syndrome',
  reasoning_subgraph: 'syndrome',
  syndrome: 'syndrome',
  syndrome_draft: 'syndrome',
  formula_draft: 'formula',
  prescription: 'prescription',
  modification: 'modification',
  safety: 'safety',
  record: 'record',
}

/**
 * 把 agent_name 映射到对应 stage。
 * supervisor/record 等无对应 stage 的返回 null。
 */
export function agentNameToStage(name: string): Stage | null {
  return AGENT_TO_STAGE[name as AgentName] ?? null
}

export type LangGraphDisposition =
  | 'ready'
  | 'needs_input'
  | 'triage_hold'
  | 'manual_required'

/**
 * Adapt persisted gate/read-model data to the four stable UI dispositions.
 * The client never promotes a failed/missing gate to ready.
 */
export function langGraphDisposition(detail: SessionDetail): LangGraphDisposition | null {
  if (detail.agent_runtime !== 'langgraph') return null

  const triage = detail.read_model.gates.find((gate) => gate.gate_name === 'triage')
  const completeness = detail.read_model.gates.find((gate) => gate.gate_name === 'completeness')
  const triageDisposition = triage?.details?.disposition
  const completenessDisposition = completeness?.details?.disposition

  if (triageDisposition === 'emergency_referral') return 'triage_hold'
  if (triageDisposition === 'manual_review') return 'manual_required'
  if (
    detail.recovery_status !== 'normal'
    || detail.status === 'blocked'
    || detail.status === 'terminated'
    || detail.read_model.graph.status === 'failed'
    || detail.read_model.graph.status === 'cancelled'
    || completenessDisposition === 'conflict'
    || completenessDisposition === 'stagnated'
  ) {
    return 'manual_required'
  }
  if (
    triage?.decision === 'blocked'
    || detail.read_model.unresolved.some((item) => item.source === 'triage')
  ) {
    return 'triage_hold'
  }
  if (
    triage?.decision === 'passed'
    && triageDisposition === 'continue'
    && completeness?.decision === 'passed'
    && completenessDisposition === 'ready'
    && !detail.read_model.unresolved.some(
      (item) => item.source === 'triage' || item.source === 'completeness',
    )
  ) {
    return 'ready'
  }
  return 'needs_input'
}

/** Only a persisted, current LangGraph completeness gate may enable advance. */
export function canAdvanceLangGraph(detail: SessionDetail): boolean {
  if (
    detail.agent_runtime !== 'langgraph'
    || detail.current_stage !== 'inquiry'
    || detail.status !== 'active'
    || detail.read_model.unresolved.some(
      (item) => item.source === 'safety_confirmation',
    )
  ) {
    return false
  }
  return langGraphDisposition(detail) === 'ready'
}
