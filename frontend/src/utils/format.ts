/**
 * 悬壶 WebUI —— 格式化工具
 */

import type { SessionListItem } from '@/types/api'

/** 格式化 ISO 时间戳为 "MM-DD HH:mm" 显示。 */
export function formatTime(iso: string | undefined | null): string {
  if (!iso) return ''
  // 直接从 ISO 字符串提取墙钟时间，避免 Date 对象使用运行时本地时区
  // 例: "2026-07-03T10:35:00+08:00" → "07-03 10:35"
  const m = /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (!m) return ''
  return `${m[1]}-${m[2]} ${m[3]}:${m[4]}`
}

/** 患者摘要文本（name + gender + age），无信息时返回空字符串。 */
export function patientSummary(session: SessionListItem): string {
  const parts: string[] = []
  const p = session.patient_info
  if (p.name) parts.push(p.name)
  if (p.gender && p.gender !== 'unknown') {
    const map: Record<string, string> = { male: '男', female: '女' }
    parts.push(map[p.gender] ?? p.gender)
  }
  if (p.age != null) parts.push(`${p.age}岁`)
  return parts.join(' · ')
}