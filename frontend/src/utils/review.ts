import type { SafetyIssue, SessionDetail } from '@/types/api'

/** Return true when review actions must be hidden behind the safety boundary. */
export function isReviewBlocked(
  detail: SessionDetail,
  blockedIssues?: SafetyIssue[] | null,
): boolean {
  // blocked_reason='safety_rule_blocked' 时后端不允许 confirm（只能 modify/否决）。
  // SSE 丢失/未触发时 blockedIssues 可能为空，此判定兜底保证确认按钮始终隐藏。
  if (detail.blocked_reason === 'safety_rule_blocked') return true
  const issues = blockedIssues ?? (detail.safety_review?.issues as SafetyIssue[] | undefined) ?? []
  if (detail.safety_review && !detail.safety_review.passed) return true
  return issues.some((issue) => issue.severity === 'blocker' || issue.severity === 'high')
}
