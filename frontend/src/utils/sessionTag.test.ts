import { describe, expect, it } from 'vitest'
import { sessionTag } from './sessionTag'
import type { SessionListItem } from '@/types/api'

function make(over: Partial<SessionListItem> = {}): SessionListItem {
  const result: SessionListItem = {
    session_id: 's',
    patient_info: {},
    current_stage: 'inquiry',
    status: 'active',
    agent_runtime: 'legacy',
    pending_review: false,
    created_at: '',
    updated_at: '',
    ...over,
  }
  return result
}

describe('sessionTag', () => {
  it('问诊阶段 → 问诊中/墨绿', () => {
    const t = sessionTag(make({ current_stage: 'inquiry' }))
    expect(t.label).toBe('问诊中')
    expect(t.color).toBe('#3d5a4b')
  })

  it('辨证阶段 → 辨证中/信息色', () => {
    const t = sessionTag(make({ current_stage: 'syndrome' }))
    expect(t.label).toBe('辨证中')
    expect(t.color).toBe('#6b7d8a')
  })

  it('LangGraph formula 阶段 → 辨证中/信息色', () => {
    const t = sessionTag(make({ current_stage: 'formula', agent_runtime: 'langgraph' }))
    expect(t.label).toBe('辨证中')
    expect(t.color).toBe('#6b7d8a')
  })

  it('pending_review → 待确认处方/暗砂红', () => {
    const t = sessionTag(make({ status: 'pending_review' }))
    expect(t.label).toBe('待确认处方')
    expect(t.color).toBe('#c04040')
  })

  it('done 状态 → 已完成/成功色', () => {
    const t = sessionTag(make({ status: 'done', current_stage: 'done' }))
    expect(t.label).toBe('已完成')
    expect(t.color).toBe('success')
  })

  it('terminated → 已终止/默认色', () => {
    const t = sessionTag(make({ status: 'terminated' }))
    expect(t.label).toBe('已终止')
    expect(t.color).toBe('default')
  })
})
