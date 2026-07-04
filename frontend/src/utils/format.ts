/**
 * 悬壶 WebUI —— 格式化工具
 */

import type { SessionListItem } from '@/types/api'

/** 格式化 ISO 时间戳为 "MM-DD HH:mm" 显示。 */
export function formatTime(iso: string | undefined | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return ''
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return ''
  }
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