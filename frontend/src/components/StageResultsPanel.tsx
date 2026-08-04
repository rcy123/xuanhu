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

import { useState } from 'react'
import { Button, Card, Descriptions, Tag, Typography, Table } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  FileDoneOutlined,
  MedicineBoxOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
  UpOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type {
  SessionDetail,
  Formula,
  SafetyIssue,
  HerbItem,
  Severity,
  SafetyReview,
  SufficiencyMissingItem,
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

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function isSufficiencyMissingItem(value: unknown): value is SufficiencyMissingItem {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return ['key', 'label', 'reason', 'suggested_question'].every(
    (field) => typeof item[field] === 'string' && item[field].trim().length > 0,
  )
}

const LEGACY_MISSING_ITEM_LABELS: Record<string, string> = {
  'chief_complaint.symptom': '主要不适',
  'chief_complaint.course': '病程',
  'present_illness.change': '病情变化',
  'ten_questions.cold_heat': '寒热情况',
  'ten_questions.sweat': '出汗情况',
  'ten_questions.head_body': '头身情况',
  'ten_questions.stool_urine': '二便情况',
  'ten_questions.diet': '饮食情况',
  'ten_questions.chest_abdomen': '胸腹情况',
  'ten_questions.thirst': '口渴情况',
  'ten_questions.sleep': '睡眠情况',
  'ten_questions.menses_leukorrhea': '月经带下',
  'ten_questions.pain': '疼痛情况',
  'ten_questions.respiratory': '呼吸情况',
  'safety.allergy_status': '过敏史',
  'safety.medication_status': '当前用药',
  'safety.major_condition_status': '重要疾病史',
  'safety.pregnancy_status': '妊娠情况',
  'safety.lactation_status': '哺乳情况',
  past_history: '既往病史',
  four_diagnosis: '四诊信息',
  'patient.sex': '性别',
  'patient.age': '年龄',
  'patient.menopause_status': '绝经情况',
}

function dimensionLabel(key: string): string {
  return LEGACY_MISSING_ITEM_LABELS[key] ?? '待补充信息'
}

function legacyMissingItem(key: string): SufficiencyMissingItem {
  return {
    key,
    label: dimensionLabel(key),
    reason: '该项问诊信息尚未完整。',
    suggested_question: '请补充与当前症状相关的问诊信息。',
  }
}

function SufficiencyCard({ report }: { report: Record<string, unknown> }) {
  const [collapsed, setCollapsed] = useState(false)
  const sufficient = typeof report.sufficient === 'boolean' ? report.sufficient : undefined
  const summary = report.summary as string | undefined
  const covered = readStringArray(report.covered)
  const missingItems = Array.isArray(report.missing_items)
    ? report.missing_items.filter(isSufficiencyMissingItem)
    : []
  const legacyMissing = readStringArray(report.missing_fields ?? report.missing)
  const displayMissingItems = missingItems.length > 0 ? missingItems : legacyMissing.map(legacyMissingItem)
  const collectedItems = Array.from(new Set(covered)).map((key) => ({ key, label: dimensionLabel(key) }))
  const missingCount = displayMissingItems.length
  const statusText = sufficient
    ? '信息已满足进入下一步条件'
    : missingCount > 0
      ? `待补充 ${missingCount} 项`
      : '尚未满足进入下一步条件'

  return (
    <Card
      size="small"
      className={`xh-summary-card xh-sufficiency-card ${sufficient ? 'is-success' : ''}`}
      title={
        <span>
          <SearchOutlined /> 完备性判断报告
        </span>
      }
      extra={
        sufficient !== undefined ? (
          <Button
            aria-label={collapsed ? '展开完备性报告' : '收起完备性报告'}
            className="xh-sufficiency-toggle"
            icon={collapsed ? <DownOutlined /> : <UpOutlined />}
            size="small"
            type="text"
            onClick={() => setCollapsed((value) => !value)}
          />
        ) : null
      }
    >
      {collapsed ? (
        <Tag className="xh-sufficiency-collapsed-status" color={sufficient ? 'success' : 'error'}>
          {statusText}
        </Tag>
      ) : (
        <>
          <div className="xh-sufficiency-overview">
            {sufficient !== undefined ? (
              <Tag color={sufficient ? 'success' : 'error'}>{statusText}</Tag>
            ) : null}
            {collectedItems.length > 0 ? (
              <Text type="secondary" className="xh-sufficiency-covered">
                已收集 {collectedItems.length} 项
              </Text>
            ) : null}
          </div>
          {sufficient ? (
            <div className="xh-sufficiency-ready">
              <CheckCircleOutlined aria-hidden="true" />
              <div>
                <Text strong>问诊信息已满足进入下一步条件</Text>
                <Text type="secondary">可继续进入后续诊疗流程。</Text>
              </div>
            </div>
          ) : null}
          {summary ? (
            <Text className="xh-sufficiency-summary">{summary}</Text>
          ) : null}
          {collectedItems.length > 0 ? (
            <section className="xh-sufficiency-collected" aria-label="已收集信息">
              <Text className="xh-sufficiency-section-title">已收集信息</Text>
              <div className="xh-sufficiency-collected-list">
                {collectedItems.map((item) => (
                  <span key={item.key} className="xh-sufficiency-collected-item">
                    <CheckCircleOutlined aria-hidden="true" />
                    {item.label}
                  </span>
                ))}
              </div>
            </section>
          ) : null}
          {!sufficient && displayMissingItems.length > 0 ? (
            <section className="xh-sufficiency-uncollected" aria-label="未收集信息">
              <Text className="xh-sufficiency-section-title">未收集信息</Text>
              <div className="xh-sufficiency-uncollected-list">
                {displayMissingItems.map((item) => (
                  <span key={item.key} className="xh-sufficiency-uncollected-item">
                    <CloseCircleOutlined aria-hidden="true" />
                    {item.label}
                  </span>
                ))}
              </div>
            </section>
          ) : null}
        </>
      )}
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
      className="xh-summary-card"
      title={
        <span>
          <FileDoneOutlined /> 辨证结论
        </span>
      }
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
      className="xh-summary-card"
      title={
        <span>
          <MedicineBoxOutlined /> 处方
        </span>
      }
    >
      <div className="xh-formula-grid">
        {hasBase ? (
          <div className="xh-formula-column">
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
          <div className="xh-formula-column">
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
          className="xh-pending-formula"
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
  safetyReview?: SafetyReview | null
  blockedIssues?: SafetyIssue[] | null
  rollbackTarget?: string | null
}) {
  const passed = safetyReview?.passed as boolean | undefined
  const issues = (blockedIssues ?? (safetyReview?.issues as SafetyIssue[] | undefined)) ?? []
  const hasIssues = issues.length > 0
  // 只有显式 passed=false 或存在 issues 才判为未通过；safetyReview 缺失时
  // （如 safety.blocked 事件携带空 issues 的误报）不展示失败状态。
  const isBlocked = hasIssues || passed === false

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
      className={`xh-summary-card ${isBlocked ? 'is-danger' : 'is-success'}`}
      title={
        <span>
          <SafetyCertificateOutlined /> 安全审核
        </span>
      }
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
              {rollbackTarget && rollbackTarget !== 'none'
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
      className="xh-stage-results"
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
