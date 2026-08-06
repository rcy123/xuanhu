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
import { Button, Card, Tag, Typography, Table } from 'antd'
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
  BaseFormulaAlternative,
} from '@/types/api'

const { Text } = Typography

interface StageResultsPanelProps {
  detail: SessionDetail | null
  /** review.required 事件携带的待确认处方（优先于 detail.modified_formula 展示）。 */
  pendingReviewFormula?: Formula | null
  /** safety.blocked 事件携带的阻断信息。 */
  blockedIssues?: SafetyIssue[] | null
  rollbackTarget?: string | null
  /** P1 多方案：候选基础方方案列表。 */
  baseFormulaAlternatives?: BaseFormulaAlternative[] | null
  /** P1 多方案：医师选择方案的回调。 */
  onSelectAlternative?: (index: number) => void
  /** P1 多方案：是否正在提交选择。 */
  alternativeSubmitting?: boolean
  /** P1 多方案：当前选中的方案索引（用于高亮）。 */
  selectedAlternativeIndex?: number | null
  onHoverAlternative?: (index: number | null) => void
}

// ---------------------------------------------------------------------------
// 子卡片
// ---------------------------------------------------------------------------

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function readNonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim().length > 0 ? value : undefined
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
  const [collapsed, setCollapsed] = useState(false)
  // LangGraph 的辨证产物使用 syndrome / syndrome_basis /
  // treatment_principle；保留旧字段兼容已存的 legacy 会话。
  const syndrome = readNonEmptyString(result.syndrome) ?? readNonEmptyString(result.pattern)
  const organs = readStringArray(result.organs)
  const syndromeBasis = readStringArray(result.syndrome_basis)
  const legacyEvidence = readNonEmptyString(result.evidence)
  const evidenceItems = syndromeBasis.length > 0
    ? syndromeBasis
    : legacyEvidence
      ? [legacyEvidence]
      : []
  const treatmentPrinciple = readNonEmptyString(result.treatment_principle)
  const differential = readStringArray(result.differential)
  const legacyBasis = readNonEmptyString(result.basis)
  const collapsedSummary = syndrome ?? '已生成辨证结论'

  return (
    <Card
      size="small"
      className="xh-summary-card xh-syndrome-card"
      title={
        <span>
          <FileDoneOutlined /> 辨证结论
        </span>
      }
      extra={
        <Button
          aria-label={collapsed ? '展开辨证结论' : '收起辨证结论'}
          className="xh-syndrome-toggle"
          icon={collapsed ? <DownOutlined /> : <UpOutlined />}
          size="small"
          type="text"
          onClick={() => setCollapsed((value) => !value)}
        />
      }
    >
      {collapsed ? (
        <div className="xh-syndrome-collapsed" aria-label="已收起的辨证结论">
          <span>证型</span>
          <Text strong>{collapsedSummary}</Text>
        </div>
      ) : (
        <div className="xh-syndrome-content">
          {syndrome ? (
            <section className="xh-syndrome-hero">
              <span className="xh-syndrome-eyebrow">辨证证型</span>
              <Text className="xh-syndrome-pattern">{syndrome}</Text>
              {organs.length > 0 ? (
                <div className="xh-syndrome-organs">
                  {organs.map((organ) => <span key={organ}>{organ}</span>)}
                </div>
              ) : null}
            </section>
          ) : null}
          {evidenceItems.length > 0 ? (
            <section className="xh-syndrome-section">
              <Text className="xh-syndrome-section-title">辨证依据</Text>
              <ul className="xh-syndrome-evidence-list">
                {evidenceItems.map((item, index) => (
                  <li key={`${item}-${index}`}>{item}</li>
                ))}
              </ul>
            </section>
          ) : null}
          {treatmentPrinciple ? (
            <section className="xh-syndrome-treatment">
              <Text className="xh-syndrome-section-title">治则治法</Text>
              <Text>{treatmentPrinciple}</Text>
            </section>
          ) : null}
          {differential.length > 0 ? (
            <section className="xh-syndrome-section">
              <Text className="xh-syndrome-section-title">鉴别考虑</Text>
              <Text className="xh-syndrome-secondary-copy">{differential.join('；')}</Text>
            </section>
          ) : null}
          {legacyBasis ? (
            <section className="xh-syndrome-section">
              <Text className="xh-syndrome-section-title">文献参考</Text>
              <Text className="xh-syndrome-secondary-copy">{legacyBasis}</Text>
            </section>
          ) : null}
        </div>
      )}
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
  const [collapsed, setCollapsed] = useState(false)
  const [baseExpanded, setBaseExpanded] = useState(false)
  const [rationaleExpanded, setRationaleExpanded] = useState(false)
  const hasBase = baseFormula?.composition && baseFormula.composition.length > 0
  const hasModified = modifiedFormula?.composition && modifiedFormula.composition.length > 0
  const hasPending = pendingReviewFormula?.composition && pendingReviewFormula.composition.length > 0

  if (!hasBase && !hasModified && !hasPending) return null

  const currentFormula = hasPending
    ? pendingReviewFormula!
    : hasModified
      ? modifiedFormula!
      : baseFormula!
  const currentLabel = hasPending ? '待确认方案' : hasModified ? '当前加减方案' : '当前方案'
  const currentHerbs = new Map(currentFormula.composition.map((item) => [item.herb, item]))
  const baseHerbs = new Map((hasBase ? baseFormula!.composition : []).map((item) => [item.herb, item]))
  const formulaChanged = hasBase && (
    baseFormula!.composition.length !== currentFormula.composition.length
    || baseFormula!.composition.some((item) => {
      const current = currentHerbs.get(item.herb)
      return !current || current.dose !== item.dose || current.unit !== item.unit
    })
  )
  const showBaseReference = hasBase && formulaChanged
  const adjustmentItems = showBaseReference
    ? [
        ...currentFormula.composition
          .filter((item) => !baseHerbs.has(item.herb))
          .map((item) => ({ key: `add-${item.herb}`, tone: 'add', label: `新增 ${item.herb}` })),
        ...baseFormula!.composition
          .filter((item) => !currentHerbs.has(item.herb))
          .map((item) => ({ key: `remove-${item.herb}`, tone: 'remove', label: `去除 ${item.herb}` })),
        ...currentFormula.composition.flatMap((item) => {
          const base = baseHerbs.get(item.herb)
          if (!base || (base.dose === item.dose && base.unit === item.unit)) return []
          const baseDose = base.dose != null ? `${base.dose}${base.unit ?? ''}` : '-'
          const currentDose = item.dose != null ? `${item.dose}${item.unit ?? ''}` : '-'
          return [{ key: `dose-${item.herb}`, tone: 'change', label: `${item.herb} ${baseDose} → ${currentDose}` }]
        }),
      ]
    : []

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
      className="xh-summary-card xh-formula-card"
      title={
        <span>
          <MedicineBoxOutlined /> 处方
        </span>
      }
      extra={
        <Button
          aria-label={collapsed ? '展开处方' : '收起处方'}
          className="xh-formula-toggle"
          icon={collapsed ? <DownOutlined /> : <UpOutlined />}
          size="small"
          type="text"
          onClick={() => setCollapsed((value) => !value)}
        />
      }
    >
      {collapsed ? (
        <div className="xh-formula-collapsed" aria-label="已收起的处方">
          <span>{currentLabel}</span>
          <Text strong>{currentFormula.name ?? '未命名处方'}</Text>
          <span>{currentFormula.composition.length} 味药</span>
          {hasPending ? <Text type="warning">待医师确认</Text> : null}
        </div>
      ) : (
        <>
          <section className="xh-current-formula">
            <div className="xh-current-formula-heading">
              <div>
                <span>{currentLabel}</span>
                <Text strong>{currentFormula.name ?? '未命名处方'}</Text>
              </div>
              <span className="xh-formula-herb-count">{currentFormula.composition.length} 味药</span>
            </div>
            {adjustmentItems.length > 0 ? (
              <div className="xh-formula-adjustments" aria-label="处方调整">
                {adjustmentItems.map((item) => (
                  <span key={item.key} className={`is-${item.tone}`}>{item.label}</span>
                ))}
              </div>
            ) : null}
            <Table
              className="xh-current-formula-table"
              dataSource={currentFormula.composition}
              columns={herbColumns}
              rowKey="herb"
              size="small"
              pagination={false}
            />
            {currentFormula.rationale ? (
              <div className="xh-formula-rationale">
                <div>
                  <Text className="xh-formula-section-title">方义说明</Text>
                  <Button
                    aria-label={rationaleExpanded ? '收起方义说明' : '展开方义说明'}
                    className="xh-formula-disclosure"
                    size="small"
                    type="text"
                    onClick={() => setRationaleExpanded((value) => !value)}
                  >
                    {rationaleExpanded ? '收起' : '查看'}
                  </Button>
                </div>
                {rationaleExpanded ? <Text>{currentFormula.rationale}</Text> : null}
              </div>
            ) : null}
          </section>
          {showBaseReference ? (
            <section className="xh-base-formula">
              <div className="xh-base-formula-heading">
                <div>
                  <Text>基方：{baseFormula!.name ?? '未命名处方'}</Text>
                  <Text type="secondary">{baseFormula!.composition.length} 味药</Text>
                </div>
                <Button
                  aria-label={baseExpanded ? '收起基方' : '展开基方'}
                  className="xh-formula-disclosure"
                  size="small"
                  type="text"
                  onClick={() => setBaseExpanded((value) => !value)}
                >
                  {baseExpanded ? '收起' : '查看基方'}
                </Button>
              </div>
              {baseExpanded ? (
                <Table
                  dataSource={baseFormula!.composition}
                  columns={herbColumns}
                  rowKey="herb"
                  size="small"
                  pagination={false}
                />
              ) : null}
            </section>
          ) : null}
          {hasPending ? (
            <div
              className="xh-pending-formula"
              data-testid="pending-review-formula"
            >
              <div>
                <Text strong>待医师确认</Text>
                <Text type="secondary">
                  当前方案：{currentFormula.name ?? '未命名处方'} · {currentFormula.composition.length} 味药
                </Text>
              </div>
              <Text type="warning">⚠ 以上处方仅供参考，需经执业中医师审核确认后方可使用</Text>
              {/* 注意：P8-3 不添加确认/修改/否决按钮 */}
            </div>
          ) : null}
        </>
      )}
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
// P1 多方案：候选方案选择卡片
// ---------------------------------------------------------------------------

function AlternativeSelectionCard({
  alternatives,
  selectedIndex,
  submitting,
  onSelect,
  onHover,
}: {
  alternatives: BaseFormulaAlternative[]
  selectedIndex: number | null
  submitting: boolean
  onSelect: (index: number) => void
  onHover?: (index: number | null) => void
}) {
  const herbColumns: ColumnsType<HerbItem> = [
    { title: '药材', dataIndex: 'herb', key: 'herb' },
    {
      title: '剂量',
      key: 'dose',
      render: (_: unknown, r: HerbItem) => (r.dose != null ? `${r.dose}${r.unit ?? ''}` : '-'),
    },
    {
      title: '备注',
      dataIndex: 'note',
      key: 'note',
      render: (v: string | null | undefined) => v ?? '-',
    },
  ]

  const confidenceColor = (c: number): 'green' | 'orange' | 'red' => {
    if (c >= 0.7) return 'green'
    if (c >= 0.5) return 'orange'
    return 'red'
  }

  return (
    <Card
      size="small"
      className="xh-summary-card xh-alternatives-card"
      title={
        <span>
          <CheckCircleOutlined /> 请选择基础方案
        </span>
      }
    >
      <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
        AI 生成了 {alternatives.length} 套侧重不同的基础方案，请选择一套继续加减
      </Text>
      <div className="xh-alternatives-list">
        {alternatives.map((alt) => {
          const isSelected = selectedIndex === alt.index
          return (
            <Card
              key={alt.index}
              size="small"
              className={`xh-alternative-item${isSelected ? ' is-selected' : ''}`}
              onMouseEnter={() => onHover?.(alt.index)}
              onMouseLeave={() => onHover?.(null)}
              title={
                <span>
                  方案{alt.index + 1}：{alt.formula.name ?? '未命名方'}
                </span>
              }
              extra={
                <Tag color={confidenceColor(alt.confidence)}>
                  置信度 {(alt.confidence * 100).toFixed(0)}%
                </Tag>
              }
            >
              <div className="xh-alternative-angle">
                <Text strong>侧重：</Text>
                <Text>{alt.angle}</Text>
              </div>
              <div className="xh-alternative-rationale">
                <Text strong>方义：</Text>
                <Text>{alt.rationale}</Text>
              </div>
              <div className="xh-alternative-composition">
                <Text strong style={{ display: 'block', marginBottom: 4 }}>
                  药味组成：
                </Text>
                <Table
                  columns={herbColumns}
                  dataSource={alt.formula.composition.map((item, idx) => ({ ...item, key: idx }))}
                  pagination={false}
                  size="small"
                />
              </div>
              <div style={{ marginTop: 8, textAlign: 'right' }}>
                <Button
                  type={isSelected ? 'primary' : 'default'}
                  disabled={submitting}
                  loading={submitting && isSelected}
                  onClick={() => onSelect(alt.index)}
                >
                  {isSelected ? '已选中' : '选择此方案'}
                </Button>
              </div>
            </Card>
          )
        })}
      </div>
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
  baseFormulaAlternatives,
  onSelectAlternative,
  alternativeSubmitting = false,
  selectedAlternativeIndex = null,
  onHoverAlternative,
}: StageResultsPanelProps) {
  if (!detail) return null

  const hasAlternatives =
    baseFormulaAlternatives != null && baseFormulaAlternatives.length > 0

  const hasSufficiency = detail.sufficiency_report != null
  const hasSyndrome = detail.syndrome_result != null
  const hasFormula =
    !hasAlternatives &&
    (detail.base_formula != null ||
      detail.modified_formula != null ||
      pendingReviewFormula != null)
  const hasSafety =
    detail.safety_review != null || blockedIssues != null

  if (!hasSufficiency && !hasSyndrome && !hasFormula && !hasSafety && !hasAlternatives) return null

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
      {hasAlternatives ? (
        <AlternativeSelectionCard
          alternatives={baseFormulaAlternatives!}
          selectedIndex={selectedAlternativeIndex}
          submitting={alternativeSubmitting}
          onSelect={(index) => onSelectAlternative?.(index)}
          onHover={onHoverAlternative}
        />
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
