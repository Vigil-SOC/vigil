import type { LinkedProposal } from '../../services/api'
import { STAGE_NAME } from './ruleStage'

export const proposalHref = (id: string) => `/detections?tab=proposed&proposal=${encodeURIComponent(id)}`
export const ruleHref = (id: number) => `/detections?rules=${id}`

/** A proposal's state in a word: rejected (by a person, or withdrawn by Vigil) or open. */
export const proposalWord = (p: LinkedProposal) => (p.state === 'rejected' ? 'Rejected' : 'Open')

/** An attached copy's stage in a word, as the Rules tab names it. */
export function copyStage(r: LinkedProposal['rules'][number]): string {
  if (r.status === 'removed') return 'Removed'
  return r.stage ? STAGE_NAME[r.stage] : 'Saved'
}

