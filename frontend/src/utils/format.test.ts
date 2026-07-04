import { describe, expect, it } from 'vitest'
import { formatTime, patientSummary } from './format'
import type { SessionListItem } from '@/types/api'

function makeSession(over: Partial<SessionListItem> = {}): SessionListItem {
  return {
    session_id: 's-1',
    patient_info: {},
    current_stage: 'inquiry',
    status: 'active',
    pending_review: false,
    created_at: '2026-07-03T10:30:00+08:00',
    updated_at: '2026-07-03T10:35:00+08:00',
    ...over,
  }
}

describe('formatTime', () => {
  it('格式化 ISO 为 MM-DD HH:mm', () => {
    expect(formatTime('2026-07-03T10:35:00+08:00')).toBe('07-03 10:35')
  })

  it('空值返回空串', () => {
    expect(formatTime(null)).toBe('')
    expect(formatTime(undefined)).toBe('')
  })

  it('非法时间返回空串', () => {
    expect(formatTime('not-a-date')).toBe('')
  })
})

describe('patientSummary', () => {
  it('拼接 name + gender + age', () => {
    const s = makeSession({
      patient_info: { name: '李明', gender: 'male', age: 35 },
    })
    expect(patientSummary(s)).toBe('李明 · 男 · 35岁')
  })

  it('unknown 性别不显示', () => {
    const s = makeSession({ patient_info: { name: '张华', gender: 'unknown' } })
    expect(patientSummary(s)).toBe('张华')
  })

  it('无信息返回空串', () => {
    const s = makeSession()
    expect(patientSummary(s)).toBe('')
  })
})
