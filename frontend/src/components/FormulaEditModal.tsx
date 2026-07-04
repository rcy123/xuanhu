/**
 * 悬壶 WebUI —— 处方编辑 Modal（P8-4）
 *
 * 医师可编辑 name、composition（每项 herb/dose/unit/note）、rationale。
 * composition 至少 1 味药，提交前校验。
 */

import { useState, useCallback, useEffect } from 'react'
import { Modal, Input, InputNumber, Button, Typography, Alert } from 'antd'
import type { ButtonProps } from 'antd'
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons'
import type { Formula, FormulaOverride, HerbItem } from '@/types/api'
import type { ApiRequestError } from '@/api/errors'

const { Text } = Typography

export interface FormulaEditModalProps {
  open: boolean
  /** 初始值来源：优先 pendingReviewFormula，其次 detail.modified_formula */
  initialFormula: Formula | null
  submitting: boolean
  /** 二次安全审核失败时携带的 issues（SAFETY_REVIEW_BLOCKED）。不关闭弹窗，允许再改。 */
  reviewError?: ApiRequestError | null
  onCancel: () => void
  onSubmit: (override: FormulaOverride, feedback?: string) => void
}

interface EditHerbItem extends HerbItem {
  key: string
}

let _herbKeyCounter = 0
function nextHerbKey(): string {
  return `herb-${++_herbKeyCounter}`
}

function toEditItems(composition: HerbItem[] | undefined): EditHerbItem[] {
  if (!composition || composition.length === 0) {
    return [{ herb: '', dose: undefined, unit: 'g', note: '', key: nextHerbKey() }]
  }
  return composition.map((h) => ({ ...h, key: nextHerbKey() }))
}

export function FormulaEditModal({
  open,
  initialFormula,
  submitting,
  reviewError,
  onCancel,
  onSubmit,
}: FormulaEditModalProps) {
  const [name, setName] = useState('')
  const [herbs, setHerbs] = useState<EditHerbItem[]>([])
  const [rationale, setRationale] = useState('')
  const [feedback, setFeedback] = useState('')
  const [validationMsg, setValidationMsg] = useState<string | null>(null)

  // 弹窗打开时从 initialFormula 初始化
  useEffect(() => {
    if (open) {
      setName(initialFormula?.name ?? '')
      setHerbs(toEditItems(initialFormula?.composition))
      setRationale(initialFormula?.rationale ?? '')
      setFeedback('')
      setValidationMsg(null)
    }
  }, [open, initialFormula])

  const updateHerb = useCallback((key: string, field: keyof EditHerbItem, value: unknown) => {
    setHerbs((prev) =>
      prev.map((h) => (h.key === key ? { ...h, [field]: value } : h)),
    )
  }, [])

  const removeHerb = useCallback((key: string) => {
    setHerbs((prev) => {
      if (prev.length <= 1) return prev
      return prev.filter((h) => h.key !== key)
    })
  }, [])

  const addHerb = useCallback(() => {
    setHerbs((prev) => [...prev, { herb: '', dose: undefined, unit: 'g', note: '', key: nextHerbKey() }])
  }, [])

  const handleSubmit = () => {
    // 验证：至少一味药有名称
    const validHerbs = herbs.filter((h) => h.herb.trim() !== '')
    if (validHerbs.length === 0) {
      setValidationMsg('处方至少需要一味药材')
      return
    }
    setValidationMsg(null)

    const override: FormulaOverride = {
      name: name.trim() || undefined,
      composition: validHerbs.map((h) => ({
        herb: h.herb.trim(),
        dose: h.dose ?? undefined,
        unit: h.unit || 'g',
        note: h.note?.trim() || undefined,
      })),
      rationale: rationale.trim() || undefined,
    }
    onSubmit(override, feedback.trim() || undefined)
  }

  return (
    <Modal
      title="修改处方"
      open={open}
      onCancel={onCancel}
      onOk={handleSubmit}
      okText="保存修改"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{ disabled: submitting, 'data-testid': 'formula-edit-submit' } as ButtonProps}
      width={640}
      destroyOnClose
    >
      {reviewError ? (
        <Alert
          type="error"
          message="二次安全审核未通过"
          description={
            <div>
              <Text>{reviewError.userMessage}</Text>
              {reviewError.issues && Array.isArray(reviewError.issues) ? (
                <ul style={{ marginTop: 8, paddingLeft: 20 }}>
                  {(reviewError.issues as Array<{ severity: string; message: string }>).map((issue, i) => (
                    <li key={i}>
                      <Text type="danger">
                        [{issue.severity}] {issue.message}
                      </Text>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div style={{ marginTop: 8 }}>
                <Text type="secondary">请修改处方后重新提交</Text>
              </div>
            </div>
          }
          style={{ marginBottom: 'var(--xh-space-m)' }}
          showIcon
        />
      ) : null}

      <div style={{ marginBottom: 'var(--xh-space-m)' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>方名</Text>
        <Input
          placeholder="如：麻杏石甘汤加减"
          value={name}
          onChange={(e) => setName(e.target.value)}
          data-testid="formula-edit-name"
          style={{ marginTop: 4 }}
        />
      </div>

      <div style={{ marginBottom: 'var(--xh-space-m)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>药材组成</Text>
          <Button size="small" icon={<PlusOutlined />} onClick={addHerb} data-testid="formula-edit-add-herb">
            添加药材
          </Button>
        </div>
        {validationMsg ? (
          <Text type="danger" style={{ fontSize: 12 }} data-testid="formula-edit-validation">
            {validationMsg}
          </Text>
        ) : null}
        {herbs.map((herb) => (
          <div
            key={herb.key}
            style={{
              display: 'flex',
              gap: 8,
              marginBottom: 8,
              alignItems: 'center',
            }}
            data-testid="formula-edit-herb-row"
          >
            <Input
              placeholder="药材名"
              value={herb.herb}
              onChange={(e) => updateHerb(herb.key, 'herb', e.target.value)}
              style={{ flex: 2 }}
              data-testid="formula-edit-herb-name"
            />
            <InputNumber
              placeholder="剂量"
              value={herb.dose ?? undefined}
              onChange={(v) => updateHerb(herb.key, 'dose', v)}
              style={{ flex: 1 }}
              min={0}
              data-testid="formula-edit-herb-dose"
            />
            <Input
              placeholder="单位"
              value={herb.unit}
              onChange={(e) => updateHerb(herb.key, 'unit', e.target.value)}
              style={{ width: 60 }}
              data-testid="formula-edit-herb-unit"
            />
            <Input
              placeholder="备注"
              value={herb.note ?? ''}
              onChange={(e) => updateHerb(herb.key, 'note', e.target.value)}
              style={{ flex: 1 }}
            />
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={() => removeHerb(herb.key)}
              disabled={herbs.length <= 1}
              data-testid="formula-edit-remove-herb"
            />
          </div>
        ))}
      </div>

      <div style={{ marginBottom: 'var(--xh-space-m)' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>加减理由</Text>
        <Input.TextArea
          placeholder="修改处方的理由（可选）"
          value={rationale}
          onChange={(e) => setRationale(e.target.value)}
          rows={3}
          data-testid="formula-edit-rationale"
          style={{ marginTop: 4 }}
        />
      </div>

      <div>
        <Text type="secondary" style={{ fontSize: 12 }}>修改备注（可选）</Text>
        <Input.TextArea
          placeholder="备注信息将随审核记录保存"
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          rows={2}
          data-testid="formula-edit-feedback"
          style={{ marginTop: 4 }}
        />
      </div>
    </Modal>
  )
}

export default FormulaEditModal
