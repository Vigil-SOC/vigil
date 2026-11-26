import type { AgreementTally, Ceiling, ModelAgreement, WorkflowAgreement } from '../services/api'

/** One workflow's stored entry in the ceiling (core/response/ceiling.py workflow_entry). */
export interface WorkflowTier {
  tier: 'act' | 'assist'
  floor: number | null
  /** 0–1: a dismissal or containment scoring under it, or unscored, waits for a person. Absent or null: no bar. */
  bar?: number | null
  evidence: {
    agreed: number
    disagreed: number
    agreement_rate: number | null
    window_end: string
    false_close_rate: number | null
    answered: number
    wilson_low: number | null
    wilson_high: number | null
  } | null
  /** a principal id, or "system" when Vigil lowered it */
  by: string
  /** that principal's name, read at request time; null when blank or erased */
  by_label: string | null
  at: string
  /** the model the tier was earned (or lost) on; absent when it was not resolved */
  model?: string
  /** set when Vigil lowered it on a model change: the model it was on before */
  from_model?: string
  /** the model-change judgement's reason, naming both models */
  reason?: string
}

export function workflowTier(ceiling: Ceiling | null | undefined, id: string | null | undefined): WorkflowTier | undefined {
  return ceiling && id ? (ceiling.workflows[id] as unknown as WorkflowTier | undefined) : undefined
}

/** Whether runs of a workflow act on their own: its own tier, else the fleet's; stop and the lock beat both. */
export function actsByDefault(ceiling: Ceiling | null | undefined, id: string | null | undefined): boolean {
  if (!ceiling || !id || ceiling.stopped || ceiling.environment_locked) return false
  const entry = workflowTier(ceiling, id)
  return entry ? entry.tier === 'act' : ceiling.act_reversible
}

/** The header chip's Act: the fleet (either act flag), or any one workflow, may act and nothing is stopped or locked. */
export function anyActs(ceiling: Ceiling): boolean {
  if (ceiling.stopped || ceiling.environment_locked) return false
  return ceiling.act_reversible || ceiling.act_irreversible || Object.values(ceiling.workflows).some((e) => (e as Partial<WorkflowTier>).tier === 'act')
}

/** "N reviews · X% agree" for a workflow with scored closures; null when nothing was scored. */
export function agreementText(row: Pick<WorkflowAgreement, 'agreed' | 'disagreed' | 'agreement_rate'> | null | undefined): { text: string; pct: number } | null {
  const scored = row ? row.agreed + row.disagreed : 0
  if (!row || !scored || row.agreement_rate == null) return null
  const pct = Math.round(row.agreement_rate * 100)
  return { text: `${scored} ${scored === 1 ? 'review' : 'reviews'} · ${pct}% agree`, pct }
}

const asPct = (rate: number) => Math.round(rate * 100)

/** "30 days · 4% false close · 25 answered · 1–9%". Null when the rate is absent. */
export function falseCloseText(
  row: Pick<AgreementTally, 'false_close_rate' | 'answered' | 'wilson_low' | 'wilson_high'> | null | undefined,
): string | null {
  if (!row || row.false_close_rate == null) return null
  const range =
    row.wilson_low != null && row.wilson_high != null
      ? ` · ${asPct(row.wilson_low)}–${asPct(row.wilson_high)}%`
      : ''
  return `30 days · ${asPct(row.false_close_rate)}% false close · ${row.answered} answered${range}`
}

/** Agreements given without opening the evidence, over those plus the ones that did. Null when neither was counted. */
export function uncheckedText(row: Pick<WorkflowAgreement, 'agreed' | 'agreed_unchecked'> | null | undefined): string | null {
  const agreed = row?.agreed ?? 0
  const unchecked = row?.agreed_unchecked ?? 0
  const total = agreed + unchecked
  if (!row || total === 0) return null
  return `${asPct(unchecked / total)}% agreed without the evidence`
}

/** A workflow's per-model tallies, the model its tier is on first, then by most scored closes. */
export function modelsInOrder(row: WorkflowAgreement | undefined, inForce: string | undefined): ModelAgreement[] {
  const scored = (m: ModelAgreement) => m.agreed + m.disagreed
  return [...(row?.by_model ?? [])].sort(
    (a, b) => Number(b.model === inForce) - Number(a.model === inForce) || scored(b) - scored(a),
  )
}

/** Why a model change dropped a workflow: the bracketed tail of the judgement's reason, else the whole reason. */
export function dropWhy(reason: string | null | undefined): string | undefined {
  return reason?.match(/\(([^()]*)\)$/)?.[1] ?? reason ?? undefined
}
