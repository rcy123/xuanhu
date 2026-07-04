import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { FormulaEditModal } from './FormulaEditModal'
import type { Formula } from '@/types/api'
import { ApiRequestError } from '@/api/errors'

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

function makeFormula(): Formula {
  return {
    name: '四君子汤',
    composition: [
      { herb: '人参', dose: 9, unit: 'g' },
      { herb: '白术', dose: 9, unit: 'g' },
    ],
    rationale: '健脾益气',
  }
}

describe('FormulaEditModal', () => {
  it('打开时从 initialFormula 初始化字段', () => {
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const nameInput = screen.getByTestId('formula-edit-name') as HTMLInputElement
    expect(nameInput.value).toBe('四君子汤')

    const herbNames = screen.getAllByTestId('formula-edit-herb-name') as HTMLInputElement[]
    expect(herbNames.length).toBe(2)
    expect(herbNames[0].value).toBe('人参')
    expect(herbNames[1].value).toBe('白术')
  })

  it('添加药材行', () => {
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const addBtn = screen.getByTestId('formula-edit-add-herb')
    fireEvent.click(addBtn)
    const rows = screen.getAllByTestId('formula-edit-herb-row')
    expect(rows.length).toBe(3)
  })

  it('删除药材行（至少保留 1 行）', () => {
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const removeBtns = screen.getAllByTestId('formula-edit-remove-herb')
    fireEvent.click(removeBtns[0])
    const rows = screen.getAllByTestId('formula-edit-herb-row')
    expect(rows.length).toBe(1)
    // 只剩 1 行时删除按钮 disabled
    const remainingBtn = screen.getByTestId('formula-edit-remove-herb') as HTMLButtonElement
    expect(remainingBtn.disabled).toBe(true)
  })

  it('composition 为空的校验：至少 1 味药', () => {
    const onSubmit = vi.fn()
    render(
      <FormulaEditModal
        open={true}
        initialFormula={null}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    )
    // 默认有一行空药材
    const submitBtn = screen.getByTestId('formula-edit-submit')
    fireEvent.click(submitBtn)

    expect(screen.getByTestId('formula-edit-validation')).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('有效提交调用 onSubmit 并传递 FormulaOverride', () => {
    const onSubmit = vi.fn()
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    )
    const submitBtn = screen.getByTestId('formula-edit-submit')
    fireEvent.click(submitBtn)

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const override = onSubmit.mock.calls[0][0]
    expect(override.composition).toHaveLength(2)
    expect(override.composition[0].herb).toBe('人参')
    expect(override.composition[0].dose).toBe(9)
  })

  it('关闭时调用 onCancel', () => {
    const onCancel = vi.fn()
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        onCancel={onCancel}
        onSubmit={vi.fn()}
      />,
    )
    // AntD Modal 关闭按钮
    const closeBtn = document.querySelector('.ant-modal-close') as HTMLElement
    fireEvent.click(closeBtn)
    expect(onCancel).toHaveBeenCalled()
  })

  it('reviewError 显示二次安全审核失败信息', () => {
    const error = new ApiRequestError({
      code: 'SAFETY_REVIEW_BLOCKED',
      userMessage: '安全审核阻断',
      status: 409,
      retryable: false,
      issues: [
        { severity: 'blocker', herb: '附子', message: '超剂量' },
        { severity: 'high', message: '十八反' },
      ],
    })
    render(
      <FormulaEditModal
        open={true}
        initialFormula={makeFormula()}
        submitting={false}
        reviewError={error}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByText(/二次安全审核未通过/)).toBeInTheDocument()
    expect(screen.getByText(/超剂量/)).toBeInTheDocument()
    expect(screen.getByText(/请修改处方后重新提交/)).toBeInTheDocument()
  })
})