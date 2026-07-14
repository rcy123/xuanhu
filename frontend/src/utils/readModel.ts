import type { AgentRuntime, SessionReadModel } from '@/types/api'

/** Minimal empty projection used while constructing fixtures or initial UI state. */
export function emptySessionReadModel(
  agentRuntime: AgentRuntime,
  revision: number,
): SessionReadModel {
  return {
    schema_version: 'session-read-model.v1',
    agent_runtime: agentRuntime,
    graph: { revision },
    gates: [],
    artifacts: [],
    review_required: false,
    unresolved: [],
  }
}
