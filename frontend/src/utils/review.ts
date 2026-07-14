import type { SafetyIssue, SessionDetail } from '@/types/api'

/** Return true when review actions must be hidden behind the safety boundary. */
export function isReviewBlocked(
  detail: SessionDetail,
  blockedIssues?: SafetyIssue[] | null,
): boolean {
  const issues = blockedIssues ?? (detail.safety_review?.issues as SafetyIssue[] | undefined) ?? []
  if (detail.safety_review && !detail.safety_review.passed) return true
  return issues.some((issue) => issue.severity === 'blocker' || issue.severity === 'high')
}
