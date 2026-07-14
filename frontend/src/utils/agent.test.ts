import { describe, expect, it } from 'vitest'
import type { SessionDetail } from '@/types/api'
import { agentNameToStage, canAdvanceLangGraph } from './agent'

function langGraphDetail(): SessionDetail {
  return {
    session_id: 'session-1',
    status: 'active',
    current_stage: 'inquiry',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 2,
    agent_runtime: 'langgraph',
    read_model: {
      schema_version: 'session-read-model.v1',
      agent_runtime: 'langgraph',
      graph: { revision: 2 },
      gates: [
        {
          gate_id: 'gate-triage',
          gate_name: 'triage',
          policy_version: 'triage-policy.v1',
          input_state_version: 1,
          decision: 'passed',
          details: { disposition: 'continue' },
        },
        {
          gate_id: 'gate-completeness',
          gate_name: 'completeness',
          policy_version: 'completeness-policy.v1',
          input_state_version: 1,
          decision: 'passed',
          details: { disposition: 'ready' },
        },
      ],
      artifacts: [],
      review_required: false,
      unresolved: [],
    },
    patient_info: {},
    created_at: '',
    updated_at: '',
  }
}

describe('agentNameToStage', () => {
  it('映射已知 agent 到 stage', () => {
    expect(agentNameToStage('inquiry')).toBe('inquiry')
    expect(agentNameToStage('sufficiency')).toBe('sufficiency')
    expect(agentNameToStage('syndrome')).toBe('syndrome')
    expect(agentNameToStage('prescription')).toBe('prescription')
    expect(agentNameToStage('modification')).toBe('modification')
    expect(agentNameToStage('safety')).toBe('safety')
  })

  it('未知 agent 返回 null', () => {
    expect(agentNameToStage('supervisor')).toBeNull()
    expect(agentNameToStage('record')).toBeNull()
    expect(agentNameToStage('unknown')).toBeNull()
  })
})

describe('canAdvanceLangGraph', () => {
  it('requires current persisted triage and completeness gates', () => {
    expect(canAdvanceLangGraph(langGraphDetail())).toBe(true)
    expect(canAdvanceLangGraph({ ...langGraphDetail(), agent_runtime: 'legacy' })).toBe(false)
    expect(canAdvanceLangGraph({
      ...langGraphDetail(),
      read_model: { ...langGraphDetail().read_model, gates: [] },
    })).toBe(false)
  })
})
