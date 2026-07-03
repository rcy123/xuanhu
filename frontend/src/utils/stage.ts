/**
 * 阶段映射工具：后端 current_stage → UI 显示文本。
 * 与 UI 设计文档 §3.3 阶段映射表对齐。
 */

import type { Stage } from '@/types/api'

export interface StageMeta {
  /** UI 显示文本 */
  label: string
  /** 步骤条序号（0-based；review/record/done 不作为独立 Agent 节点） */
  step?: number
  /** 是否为终态 */
  terminal?: boolean
}

const STAGE_META: Record<Stage, StageMeta> = {
  inquiry: { label: '问诊', step: 0 },
  sufficiency: { label: '完备性', step: 1 },
  syndrome: { label: '辨证', step: 2 },
  prescription: { label: '开方', step: 3 },
  modification: { label: '加减方', step: 4 },
  safety: { label: '安全审核', step: 5 },
  review: { label: '医师确认', step: 6 },
  record: { label: '病历生成中' },
  done: { label: '病历已生成', terminal: true },
  blocked: { label: '阻塞', terminal: true },
}

export function stageMeta(stage: Stage): StageMeta {
  return STAGE_META[stage] ?? { label: stage }
}

export function stageLabel(stage: Stage): string {
  return stageMeta(stage).label
}

/** 步骤条节点（7 个 Agent 节点，与 UI §3.3 对齐）。 */
export const STEP_NODES: { stage: Stage; label: string }[] = [
  { stage: 'inquiry', label: '问诊' },
  { stage: 'sufficiency', label: '完备性' },
  { stage: 'syndrome', label: '辨证' },
  { stage: 'prescription', label: '开方' },
  { stage: 'modification', label: '加减方' },
  { stage: 'safety', label: '安全审核' },
  { stage: 'review', label: '医师确认' },
]
