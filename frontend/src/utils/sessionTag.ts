/**
 * 悬壶 WebUI —— 会话状态 → Tag 颜色映射
 *
 * 对齐 UI 设计文档 §4.1.3 状态标签配色。
 * 基于 current_stage + status + pending_review 推导。
 */

import type { SessionListItem } from '@/types/api'

export interface TagInfo {
  label: string
  color: string
}

const STAGE_LABEL: Record<string, string> = {
  inquiry: '问诊中',
  sufficiency: '完备性',
  syndrome: '辨证中',
  formula: '方药草案',
  prescription: '开方中',
  modification: '加减中',
  safety: '安全审核',
  review: '待确认',
  record: '病历生成中',
  done: '已完成',
  blocked: '阻塞',
}

/** 获取会话状态标签信息。 */
export function sessionTag(session: SessionListItem): TagInfo {
  const { current_stage, status } = session

  // 终态
  if (status === 'done') return { label: '已完成', color: 'success' }
  if (status === 'terminated') return { label: '已终止', color: 'default' }
  if (status === 'blocked') return { label: '阻塞', color: 'error' }

  // pending_review: 安全审核通过后等待医师确认
  if (status === 'pending_review' || current_stage === 'review' || session.pending_review) {
    return { label: '待确认处方', color: '#c04040' } // 暗砂红
  }

  // 按阶段
  switch (current_stage) {
    case 'inquiry':
    case 'sufficiency':
      return { label: '问诊中', color: '#3d5a4b' } // 墨绿
    case 'syndrome':
    case 'formula':
    case 'prescription':
    case 'modification':
    case 'safety':
      return { label: '辨证中', color: '#6b7d8a' } // 信息色
    case 'record':
      return { label: '病历生成中', color: 'processing' }
    default:
      break
  }

  return { label: STAGE_LABEL[current_stage] ?? current_stage, color: 'default' }
}
