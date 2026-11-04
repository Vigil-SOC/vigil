import type { Ceiling, WorkflowAgreement } from '../services/api'

/** One workflow's stored entry in the ceiling (core/response/ceiling.py workflow_entry). */
export interface WorkflowTier {
  tier: 'act' | 'assist'
  floor: number | null
  evidence: { agreed: number; disagreed: number; agreement_rate: number | null; window_end: string } | null
  /** a principal id, or "system" when Vigil lowered it */
  by: string
  /** that principal's name, read at request time; null when blank or erased */
  by_label: string | null
  at: string
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

/** The header chip's Act: the fleet, or any one workflow, may act and nothing is stopped or locked. */
export function anyActs(ceiling: Ceiling): boolean {
  if (ceiling.stopped || ceiling.environment_locked) return false
  return ceiling.act_reversible || Object.values(ceiling.workflows).some((e) => (e as Partial<WorkflowTier>).tier === 'act')
}

/** "N reviews · X% agree" for a workflow with scored closures; null when nothing was scored. */
export function agreementText(row: Pick<WorkflowAgreement, 'agreed' | 'disagreed' | 'agreement_rate'> | null | undefined): { text: string; pct: number } | null {
  const scored = row ? row.agreed + row.disagreed : 0
  if (!row || !scored || row.agreement_rate == null) return null
  const pct = Math.round(row.agreement_rate * 100)
  return { text: `${scored} ${scored === 1 ? 'review' : 'reviews'} · ${pct}% agree`, pct }
}
