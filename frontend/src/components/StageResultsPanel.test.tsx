import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { StageResultsPanel } from './StageResultsPanel'
import type { SessionDetail, Formula, SafetyReview } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'

function makeDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_id: 's1',
    status: 'active',
    current_stage: 'inquiry',
    pending_review: false,
    recovery_status: 'normal',
    rollback_counts: {},
    state_version: 1,
    agent_runtime: 'legacy',
    read_model: emptySessionReadModel('legacy', 1),
    patient_info: {},
    created_at: '2026-07-03T10:00:00+08:00',
    updated_at: '2026-07-03T10:00:00+08:00',
    ...overrides,
  }
}

describe('StageResultsPanel', () => {
  it('全空 detail 不渲染', () => {
    const { container } = render(<StageResultsPanel detail={makeDetail()} />)
    expect(container.firstChild).toBeNull()
  })

  it('detail=null 不渲染', () => {
    const { container } = render(<StageResultsPanel detail={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('sufficiency_report 渲染完备性卡片', () => {
    const detail = makeDetail({
      sufficiency_report: { sufficient: true, summary: '信息充分' },
    })
    render(<StageResultsPanel detail={detail} />)
    expect(screen.getByText(/完备性判断报告/)).toBeInTheDocument()
    expect(screen.getAllByText('信息充分').length).toBeGreaterThan(0)
  })

  it('展示结构化的待补充信息，不暴露技术字段名', () => {
    const detail = makeDetail({
      sufficiency_report: {
        sufficient: false,
        covered: ['chief_complaint.symptom'],
        missing: ['four_diagnosis'],
        missing_items: [
          {
            key: 'four_diagnosis',
            label: '四诊信息',
            reason: '望、闻、问、切相关信息尚未完整。',
            suggested_question: '请补充舌象、面色、声音或脉象等四诊信息。',
          },
        ],
      },
    })

    render(<StageResultsPanel detail={detail} />)

    expect(screen.getByText('待补充 1 项')).toBeInTheDocument()
    expect(screen.getByText('已收集信息')).toBeInTheDocument()
    expect(screen.getByText('未收集信息')).toBeInTheDocument()
    expect(screen.getByText('主要不适')).toBeInTheDocument()
    expect(screen.getByText('四诊信息')).toBeInTheDocument()
    expect(screen.queryByText(/建议问诊：/)).toBeNull()
    expect(screen.queryByText(/望、闻、问、切相关信息尚未完整/)).toBeNull()
    expect(screen.queryByText('four_diagnosis')).toBeNull()
  })

  it('收纳时仅展示完备性状态，展开后展示已收集与待补充信息', () => {
    const detail = makeDetail({
      sufficiency_report: {
        sufficient: false,
        covered: ['chief_complaint.symptom'],
        missing: ['four_diagnosis'],
        missing_items: [
          {
            key: 'four_diagnosis',
            label: '四诊信息',
            reason: '望、闻、问、切相关信息尚未完整。',
            suggested_question: '请补充舌象、面色、声音或脉象等四诊信息。',
          },
        ],
      },
    })
    const { container } = render(<StageResultsPanel detail={detail} />)
    const panel = within(container)

    fireEvent.click(panel.getByRole('button', { name: '收起完备性报告' }))
    expect(panel.getByText('待补充 1 项')).toBeInTheDocument()
    expect(panel.queryByText('主要不适')).toBeNull()
    expect(panel.queryByText('四诊信息')).toBeNull()

    fireEvent.click(panel.getByRole('button', { name: '展开完备性报告' }))
    expect(panel.getByText('主要不适')).toBeInTheDocument()
    expect(panel.getByText('四诊信息')).toBeInTheDocument()
  })

  it('旧报告降级展示中文待补充项目，不暴露技术字段名', () => {
    const detail = makeDetail({
      sufficiency_report: {
        sufficient: false,
        missing: ['safety.allergy_status'],
      },
    })

    render(<StageResultsPanel detail={detail} />)

    expect(screen.getByText('过敏史')).toBeInTheDocument()
    expect(screen.queryByText('safety.allergy_status')).toBeNull()
  })

  it('syndrome_result 渲染辨证卡片', () => {
    const detail = makeDetail({
      syndrome_result: {
        pattern: '脾胃虚弱',
        organs: ['脾', '胃'],
        evidence: '舌淡苔白',
        basis: '《中医诊断学》',
      },
    })
    render(<StageResultsPanel detail={detail} />)
    expect(screen.getByText(/辨证结论/)).toBeInTheDocument()
    expect(screen.getByText('脾胃虚弱')).toBeInTheDocument()
  })

  it('base_formula + modified_formula 渲染处方卡片', () => {
    const base: Formula = {
      name: '四君子汤',
      composition: [{ herb: '人参', dose: 9, unit: 'g' }],
    }
    const modified: Formula = {
      name: '四君子汤加减',
      composition: [{ herb: '人参', dose: 6, unit: 'g' }, { herb: '黄芪', dose: 12, unit: 'g' }],
    }
    const detail = makeDetail({ base_formula: base, modified_formula: modified })
    render(<StageResultsPanel detail={detail} />)
    expect(screen.getByText(/处方/)).toBeInTheDocument()
    expect(screen.getByText('参考基础方：四君子汤')).toBeInTheDocument()
    expect(screen.getByText('加减方：四君子汤加减')).toBeInTheDocument()
  })

  it('pendingReviewFormula 渲染待确认处方，断言无确认按钮', () => {
    const pendingFormula: Formula = {
      name: '待确认方',
      composition: [{ herb: '甘草', dose: 6, unit: 'g' }],
    }
    render(
      <StageResultsPanel
        detail={makeDetail()}
        pendingReviewFormula={pendingFormula}
      />,
    )
    expect(screen.getByTestId('pending-review-formula')).toBeInTheDocument()
    expect(screen.getByText('待确认处方：待确认方')).toBeInTheDocument()
    expect(screen.getByText(/仅供参考/)).toBeInTheDocument()
    // 断言无确认/修改/否决按钮
    expect(screen.queryByText('确认处方')).toBeNull()
    expect(screen.queryByText('修改处方')).toBeNull()
    expect(screen.queryByText('否决')).toBeNull()
  })

  it('safety_review.passed=false 渲染阻断，断言无接受风险按钮', () => {
    const safetyReview: SafetyReview = {
      passed: false,
      issues: [
        {
          severity: 'blocker',
          herb: '甘草',
          message: '十八反：甘草与海藻同用',
          rollback_target: 'prescription',
        },
      ],
    }
    const detail = makeDetail({ safety_review: safetyReview })
    render(<StageResultsPanel detail={detail} />)
    expect(screen.getAllByText(/安全审核/).length).toBeGreaterThan(0)
    expect(screen.getByText('审核未通过')).toBeInTheDocument()
    expect(screen.getByText(/十八反/)).toBeInTheDocument()
    // 断言无"接受风险继续"按钮
    expect(screen.queryByText('接受风险')).toBeNull()
    expect(screen.queryByText('继续')).toBeNull()
    expect(screen.queryByText('accept')).toBeNull()
  })

  it('safety_review.passed=true 渲染通过', () => {
    const safetyReview: SafetyReview = { passed: true, issues: [] }
    const detail = makeDetail({ safety_review: safetyReview })
    render(<StageResultsPanel detail={detail} />)
    expect(screen.getByText(/审核通过/)).toBeInTheDocument()
  })

  it('blockedIssues 渲染阻断卡片', () => {
    const issues = [
      {
        severity: 'blocker' as const,
        message: '剂量超限',
        rollback_target: 'modification' as const,
      },
    ]
    render(
      <StageResultsPanel
        detail={makeDetail()}
        blockedIssues={issues}
        rollbackTarget="modification"
      />,
    )
    expect(screen.getAllByText(/安全审核/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('审核未通过').length).toBeGreaterThan(0)
    expect(screen.getByText('剂量超限')).toBeInTheDocument()
    expect(screen.getByText(/加减方阶段/)).toBeInTheDocument()
  })
})
