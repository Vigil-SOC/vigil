import type { NeedsYouItem } from '../services/api'

const DECISION_KINDS: NeedsYouItem['kind'][] = ['approval', 'checkpoint', 'parked']

/**
 * Whether closing a recommendation's case clears it without skipping a real decision:
 * it names exactly one case, and nothing else waiting asks a question of that case.
 */
export function closesOnCase(item: NeedsYouItem, all: NeedsYouItem[]): boolean {
  const ids = item.case_ids ?? []
  if (item.kind !== 'recommendation' || ids.length !== 1) return false
  return !all.some((other) => DECISION_KINDS.includes(other.kind) && (other.case_ids ?? [other.case_id]).includes(ids[0]))
}
