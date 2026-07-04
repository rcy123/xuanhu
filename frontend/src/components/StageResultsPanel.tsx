/**
 * 悬壶 WebUI —— 阶段结果面板（P8-3）
 *
 * 根据 SessionDetail 的阶段结果字段条件渲染只读子卡片：
 * - SufficiencyCard：完备性判断报告
 * - SyndromeCard：辨证结论
 * - FormulaCard：基础方 + 加减方 + 待确认处方
 * - SafetyReviewCard：安全审核结果
 *
 * P8-3 只读；P8-4 添加确认/修改/否决按钮。
 */

import { Card, Descriptions, Tag, Typography, Table, Empty } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type {
  SessionDetail,
  Formula,
  SafetyIssue,
  HerbItem,
  Severity,
} from '@/types/api'

const { Text, Title } = Typography

interface StageResultsPanelProps {
  detail: SessionDetail | null
  /** review.required 事件携带的待确认处方（优先于 detail.modified_formula 展示）。 */
  pendingReviewFormula?: Formula | null
  /** safety.blocked 事件携带的阻断信息。 */
  blockedIssues?: SafetyIssue[] | null
  rollbackTarget?: string | null
}

// ---------------------------------------------------------------------------
// 子卡片
// ---------------------------------------------------------------------------

function SufficiencyCard({ report }: { report: Record<string, unknown> }) {
  const sufficient = report.sufficient as boolean | undefined
  const summary = report.summary as string | undefined
  const missingFields = report.missing_fields as string[] | undefined

  return (
    <Card
      size="small"
      title={
        <span>
          🔍 完备性判断报告
        </span>
      }
      style={{ marginBottom: 'var(--xh-space-l)' }}
    >
      {sufficient !== undefined ? (
        <div style={{ marginBottom: 8 }}>
          <Tag color={sufficient ? 'success' : 'warning'}>
            {sufficient ? '信息充分' : '信息不充分'}
          </Tag>
        </div>
      ) : null}
      {summary ? (
        <Text style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{summary}</Text>
      ) : null}
      {missingFields && missingFields.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            缺失字段：{missingFields.join(', ')}
          </Text>
        </div>
      ) : null}
    </Card>
  )
}

function SyndromeCard({ result }: { result: Record<string, unknown> }) {
  const pattern = result.pattern as string | undefined
  const organs = result.organs as string[] | undefined
  const evidence = result.evidence as string | undefined
  const basis = result.basis as string | undefined

  return (
    <Card
      size="small"
      title={
        <span>
          📋 辨证结论
        </span>
      }
      style={{ marginBottom: 'var(--xh-space-l)' }}
    >
      {pattern ? (
        <Descriptions column={1} size="small" style={{ marginBottom: 8 }}>
          <Descriptions.Item label="证型">{pattern}</Descriptions.Item>
          {organs ? (
            <Descriptions.Item label="病位">
              {organs.join('、')}
            </Descriptions.Item>
          ) : null}
        </Descriptions>
      ) : null}
      {evidence ? (
        <div style={{ marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>辨证依据：</Text>
          <Text style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{evidence}</Text>
        </div>
      ) : null}
      {basis ? (
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>文献参考：</Text>
          <Text style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{basis}</Text>
        </div>
      ) : null}
    </Card>
  )
}

function FormulaCard({
  baseFormula,
  modifiedFormula,
  pendingReviewFormula,
}: {
  baseFormula?: Formula | null
  modifiedFormula?: Formula | null
  pendingReviewFormula?: Formula | null
}) {
  const hasBase = baseFormula?.composition && baseFormula.composition.length > 0
  const hasModified = modifiedFormula?.composition && modifiedFormula.composition.length > 0
  const hasPending = pendingReviewFormula?.composition && pendingReviewFormula.composition.length > 0

  if (!hasBase && !hasModified && !hasPending) return null

  const herbColumns: ColumnsType<HerbItem> = [
    { title: '药材', dataIndex: 'herb', key: 'herb' },
    {
      title: '剂量',
      key: 'dose',
      render: (_, r) => (r.dose != null ? `${r.dose}${r.unit ?? ''}` : '-'),
    },
    {
      title: '备注',
      dataIndex: 'note',
      key: 'note',
      render: (v: string | null | undefined) => v ?? '-',
    },
  ]

  return (
    <Card
      size="small"
      title={
        <span>
          📜 处方
        </span>
      }
      style={{ marginBottom: 'var(--xh-space-l)' }}
    >
      <div style={{ display: 'flex', gap: 'var(--xh-space-l)', flexWrap: 'wrap' }}>
        {hasBase ? (
          <div style={{ flex: 1, minWidth: 280 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              参考基础方
              {baseFormula!.name ? `：${baseFormula!.name}` : ''}
            </Text>
            <Table
              dataSource={baseFormula!.composition}
              columns={herbColumns}
              rowKey="herb"
              size="small"
              pagination={false}
              style={{ marginTop: 4 }}
            />
            {baseFormula!.rationale ? (
              <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                {baseFormula!.rationale}
              </Text>
            ) : null}
          </div>
        ) : null}
        {hasModified ? (
          <div style={{ flex: 1, minWidth: 280 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              加减方
              {modifiedFormula!.name ? `：${modifiedFormula!.name}` : ''}
            </Text>
            <Table
              dataSource={modifiedFormula!.composition}
              columns={herbColumns}
              rowKey="herb"
              size="small"
              pagination={false}
              style={{ marginTop: 4 }}
            />
            {modifiedFormula!.rationale ? (
              <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                {modifiedFormula!.rationale}
              </Text>
            ) : null}
          </div>
        ) : null}
      </div>
      {hasPending ? (
        <div
          style={{
            marginTop: 'var(--xh-space-l)',
            padding: 'var(--xh-space-l)',
            border: '2px solid var(--xh-primary)',
            borderRadius: 'var(--xh-radius-card)',
            background: 'var(--xh-bg-card)',
          }}
          data-testid="pending-review-formula"
        >
          <Title level={5} style={{ color: 'var(--xh-primary)', marginTop: 0 }}>
            待确认处方
            {pendingReviewFormula!.name ? `：${pendingReviewFormula!.name}` : ''}
          </Title>
          <Table
            dataSource={pendingReviewFormula!.composition}
            columns={herbColumns}
            rowKey="herb"
            size="small"
            pagination={false}
          />
          {pendingReviewFormula!.rationale ? (
            <Text style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
              {pendingReviewFormula!.rationale}
            </Text>
          ) : null}
          <div style={{ marginTop: 8 }}>
            <Text type="warning" style={{ fontSize: 12 }}>
              ⚠ 以上处方仅供参考，需经执业中医师审核确认后方可使用
            </Text>
          </div>
          {/* 注意：P8-3 不添加确认/修改/否决按钮 */}
        </div>
      ) : null}
    </Card>
  )
}

function SafetyReviewCard({
  safetyReview,
  blockedIssues,
  rollbackTarget,
}: {
  safetyReview?: Record<string, unknown> | null
  blockedIssues?: SafetyIssue[] | null
  rollbackTarget?: string | null
}) {
  const passed = safetyReview?.passed as boolean | undefined
  const issues = (blockedIssues ?? (safetyReview?.issues as SafetyIssue[] | undefined)) ?? []
  const hasIssues = issues.length > 0
  const isBlocked = !passed || hasIssues

  if (!safetyReview && !blockedIssues) return null

  const severityColor = (s: Severity | undefined): string => {
    switch (s) {
      case 'blocker':
        return 'red'
      case 'high':
        return 'volcano'
      case 'warning':
        return 'orange'
      case 'info':
        return 'blue'
      default:
        return 'default'
    }
  }

  return (
    <Card
      size="small"
      title={
        <span>
          🛡 安全审核
        </span>
      }
      style={{
        marginBottom: 'var(--xh-space-l)',
        borderLeft: isBlocked ? '3px solid var(--xh-error)' : '3px solid var(--xh-success)',
      }}
    >
      {isBlocked ? (
        <>
          <div style={{ marginBottom: 8 }}>
            <Tag icon={<CloseCircleOutlined />} color="error">
              审核未通过
            </Tag>
          </div>
          {issues.map((issue, i) => (
            <div
              key={i}
              style={{
                padding: '8px 12px',
                marginBottom: 8,
                background: 'var(--xh-bg-card)',
                borderRadius: 'var(--xh-radius-card)',
                border: '1px solid var(--xh-border)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <Tag color={severityColor(issue.severity)}>
                  {issue.severity === 'blocker'
                    ? '阻断'
                    : issue.severity === 'high'
                      ? '高危'
                      : issue.severity === 'warning'
                        ? '警告'
                        : '信息'}
                </Tag>
                {issue.herb ? <Text strong>{issue.herb}</Text> : null}
              </div>
              <Text style={{ fontSize: 13 }}>{issue.message}</Text>
              {issue.detail ? (
                <div style={{ marginTop: 4 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {issue.detail}
                  </Text>
                </div>
              ) : null}
              {issue.suggestion ? (
                <div style={{ marginTop: 4 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    建议：{issue.suggestion}
                  </Text>
                </div>
              ) : null}
            </div>
          ))}
          <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
            <WarningOutlined style={{ color: 'var(--xh-error)' }} />
            <Text type="danger" style={{ fontSize: 12 }}>
              {rollbackTarget
                ? `系统将自动回退调整（目标：${rollbackTarget === 'prescription' ? '开方阶段' : '加减方阶段'}）`
                : '已阻断，系统将回退调整'}
            </Text>
          </div>
          {/* 严禁"接受风险继续"按钮 */}
        </>
      ) : (
        <>
          <div style={{ marginBottom: 8 }}>
            <Tag icon={<CheckCircleOutlined />} color="success">
              审核通过
            </Tag>
          </div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            安全审核已通过，处方可进入下一步。
          </Text>
        </>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function StageResultsPanel({
  detail,
  pendingReviewFormula,
  blockedIssues,
  rollbackTarget,
}: StageResultsPanelProps) {
  if (!detail) return null

  const hasSufficiency = detail.sufficiency_report != null
  const hasSyndrome = detail.syndrome_result != null
  const hasFormula =
    detail.base_formula != null ||
    detail.modified_formula != null ||
    pendingReviewFormula != null
  const hasSafety =
    detail.safety_review != null || blockedIssues != null

  if (!hasSufficiency && !hasSyndrome && !hasFormula && !hasSafety) return null

  return (
    <div
      data-testid="stage-results-panel"
      style={{
        padding: 'var(--xh-space-l)',
        background: 'var(--xh-bg-page)',
        borderBottom: '1px solid var(--xh-border)',
        maxHeight: 360,
        overflow: 'auto',
      }}
    >
      {hasSufficiency ? (
        <SufficiencyCard report={detail.sufficiency_report!} />
      ) : null}
      {hasSyndrome ? (
        <SyndromeCard result={detail.syndrome_result!} />
      ) : null}
      {hasFormula ? (
        <FormulaCard
          baseFormula={detail.base_formula}
          modifiedFormula={detail.modified_formula}
          pendingReviewFormula={pendingReviewFormula}
        />
      ) : null}
      {hasSafety ? (
        <SafetyReviewCard
          safetyReview={detail.safety_review}
          blockedIssues={blockedIssues}
          rollbackTarget={rollbackTarget}
        />
      ) : null}
    </div>
  )
}

export default StageResultsPanel
