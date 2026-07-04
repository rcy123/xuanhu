/**
 * 悬壶 WebUI —— agent_name → stage 映射工具
 *
 * 每个 agent_name 对应一个流程阶段；review/record 等无独立 agent，
 * 此映射供 StepBar 显示 agent 运行状态时使用。
 */

import type { Stage } from '@/types/api'
import type { AgentName } from '@/types/api'

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
