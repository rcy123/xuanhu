import type {
  AgentRuntime,
  Formula,
  HerbItem,
  SessionReadModel,
} from '@/types/api'

/** Minimal empty projection used while constructing fixtures or initial UI state. */
export function emptySessionReadModel(
  agentRuntime: AgentRuntime,
  revision: number,
): SessionReadModel {
  return {
    schema_version: 'session-read-model.v1',
    agent_runtime: agentRuntime,
    graph: { revision },
    gates: [],
    artifacts: [],
    review_required: false,
    unresolved: [],
  }
}

function optionalString(
  value: unknown,
): { valid: true; value: string | null | undefined } | { valid: false } {
  if (value === undefined || value === null || typeof value === 'string') {
    return { valid: true, value }
  }
  return { valid: false }
}

function herbItem(value: unknown): HerbItem | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const item = value as Record<string, unknown>
  if (typeof item.herb !== 'string' || item.herb.length === 0) return null
  if (
    item.dose !== undefined
    && item.dose !== null
    && typeof item.dose !== 'number'
  ) return null
  const unit = optionalString(item.unit)
  const note = optionalString(item.note)
  if (!unit.valid || !note.valid) return null
  return {
    herb: item.herb,
    dose: item.dose as number | null | undefined,
    unit: unit.value ?? undefined,
    note: note.value,
  }
}

/**
 * Restore the pending Formula Draft after refresh from the integrity-checked
 * read model. Raw checkpoints and state_snapshot are intentionally ignored.
 */
export function pendingFormulaFromReadModel(readModel: SessionReadModel): Formula | null {
  if (!readModel.review_required) return null
  const artifact = readModel.artifacts.find(
    (item) => item.artifact_type === 'formula_draft'
      && item.status === 'current'
      && item.decision === 'completed'
      && item.review_required,
  )
  const candidate = artifact?.output.candidate_formula
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null
  const raw = candidate as Record<string, unknown>
  if (!Array.isArray(raw.composition)) return null
  const composition = raw.composition.map(herbItem)
  if (composition.length === 0 || composition.some((item) => item === null)) return null
  const name = optionalString(raw.name)
  const source = optionalString(raw.source)
  const rationale = optionalString(raw.rationale)
  if (!name.valid || !source.valid || !rationale.valid) return null
  return {
    name: name.value,
    composition: composition as HerbItem[],
    source: source.value ?? null,
    rationale: rationale.value,
  }
}
