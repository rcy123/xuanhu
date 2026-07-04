import { describe, expect, it } from 'vitest'
import { agentNameToStage } from './agent'

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
