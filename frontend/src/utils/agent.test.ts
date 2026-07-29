import { describe, expect, it } from 'vitest'
import type { SessionDetail } from '@/types/api'
import {
  agentNameToStage,
  canAdvanceLangGraph,
  langGraphDisposition,
} from './agent'

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
    expect(agentNameToStage('reasoning')).toBe('syndrome')
    expect(agentNameToStage('syndrome_draft')).toBe('syndrome')
    expect(agentNameToStage('formula_draft')).toBe('formula')
    expect(agentNameToStage('prescription')).toBe('prescription')
    expect(agentNameToStage('modification')).toBe('modification')
    expect(agentNameToStage('safety')).toBe('safety')
  })

  it('未知 agent 返回 null', () => {
    expect(agentNameToStage('supervisor')).toBeNull()
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

describe('langGraphDisposition', () => {
  it('maps authoritative gate outcomes to the stable v2 dispositions', () => {
    expect(langGraphDisposition(langGraphDetail())).toBe('ready')

    const needsInput = langGraphDetail()
    needsInput.read_model.gates[1] = {
      ...needsInput.read_model.gates[1],
      decision: 'failed',
      details: { disposition: 'incomplete' },
    }
    expect(langGraphDisposition(needsInput)).toBe('needs_input')

    const triageHold = langGraphDetail()
    triageHold.read_model.gates[0] = {
      ...triageHold.read_model.gates[0],
      decision: 'blocked',
      details: { disposition: 'emergency_referral' },
    }
    expect(langGraphDisposition(triageHold)).toBe('triage_hold')

    expect(langGraphDisposition({
      ...langGraphDetail(),
      recovery_status: 'manual_required',
    })).toBe('manual_required')
  })
})
