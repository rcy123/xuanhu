/**
 * 阶段映射工具测试
 */

import { describe, expect, it } from 'vitest'
import { STEP_NODES, stageLabel, stageMeta } from '@/utils/stage'

describe('stage utils', () => {
  it('stageLabel 返回 UI 显示文本', () => {
    expect(stageLabel('inquiry')).toBe('问诊')
    expect(stageLabel('safety')).toBe('安全审核')
    expect(stageLabel('done')).toBe('病历已生成')
    expect(stageLabel('blocked')).toBe('阻塞')
  })

  it('stageMeta 返回终态标记', () => {
    expect(stageMeta('done').terminal).toBe(true)
    expect(stageMeta('blocked').terminal).toBe(true)
    expect(stageMeta('inquiry').terminal).toBeUndefined()
  })

  it('STEP_NODES 为 7 个 Agent 节点', () => {
    expect(STEP_NODES).toHaveLength(7)
    expect(STEP_NODES[0]).toEqual({ stage: 'inquiry', label: '问诊' })
    expect(STEP_NODES[6]).toEqual({ stage: 'review', label: '医师确认' })
  })
})
