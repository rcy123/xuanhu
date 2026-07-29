import { describe, expect, it } from 'vitest'
import type { SessionReadModel } from '@/types/api'
import { emptySessionReadModel, pendingFormulaFromReadModel } from './readModel'

function formulaReadModel(): SessionReadModel {
  const readModel = emptySessionReadModel('langgraph', 4)
  readModel.review_required = true
  readModel.artifacts = [
    {
      artifact_id: 'formula-1',
      artifact_type: 'formula_draft',
      revision: 1,
      input_state_version: 3,
      status: 'current',
      produced_by_run_id: 'run-1',
      payload_schema_version: 'formula-artifact-payload.v1',
      content_digest: '0'.repeat(64),
      decision: 'completed',
      evidence_mode: 'model_knowledge_only',
      review_required: true,
      unresolved: [],
      verification_gate: {
        gate_id: 'gate-1',
        gate_name: 'formula_consistency',
        policy_version: 'formula-consistency-policy.v1',
        input_state_version: 3,
        decision: 'passed',
      },
      output: {
        candidate_formula: {
          name: '四君子汤加茯苓',
          composition: [
            { herb: '白术', dose: 9, unit: 'g', note: null },
            { herb: '茯苓', dose: 12, unit: 'g', note: null },
          ],
          rationale: '健脾渗湿',
        },
      },
    },
  ]
  return readModel
}

describe('pendingFormulaFromReadModel', () => {
  it('restores a verified current Formula Draft after refresh', () => {
    expect(pendingFormulaFromReadModel(formulaReadModel())).toEqual({
      name: '四君子汤加茯苓',
      composition: [
        { herb: '白术', dose: 9, unit: 'g', note: null },
        { herb: '茯苓', dose: 12, unit: 'g', note: null },
      ],
      source: null,
      rationale: '健脾渗湿',
    })
  })

  it('fails closed for non-review or malformed artifact payloads', () => {
    const noReview = formulaReadModel()
    noReview.review_required = false
    expect(pendingFormulaFromReadModel(noReview)).toBeNull()

    const malformed = formulaReadModel()
    malformed.artifacts[0].output = {
      candidate_formula: {
        composition: [{ herb: '', dose: '9', unit: 'g' }],
      },
    }
    expect(pendingFormulaFromReadModel(malformed)).toBeNull()
  })
})
