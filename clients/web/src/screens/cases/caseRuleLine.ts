import type { Schema } from '../../services/apiTypes'

export type CaseRuleCard = Schema<'CaseRuleCard'>

/** A rule's worst problem, as the one line under an alert says it. */
const PROBLEM: Record<string, string> = {
  fails_to_run: 'its query errors in the SIEM',
  data_silent: 'its data stopped arriving',
  missing_field: 'it searches a field the index doesn’t have',
  never_fired: 'it had never fired before',
  disabled: 'it is turned off in the SIEM',
  no_fire_data: 'the SIEM reports no fire counts for it',
  noisy: 'it went noisy, far above its usual',
  quiet: 'it is over its alerts-per-day limit',
  accurate: 'most of its judged alerts weren’t real',
  silent: 'it fired regularly, then went silent',
  drift: 'it was changed in the SIEM, not in its repo',
}

const CHECK_NAMES: Record<string, string> = {
  runs: 'Runs', sees: 'Sees', quiet: 'Quiet', accurate: 'Accurate', noisy: 'Noisy', silent: 'Silent', caught: 'Caught', drift: 'Drift',
}

export interface RuleLine {
  /** Proven, or no failing check and at least one passed. */
  ok: boolean
  text: string
  /** The hover: the rule's name and each stored check's result. */
  title: string
  /** False for a removed rule: Detections no longer lists it. */
  link: boolean
}

/** The one line an alert's rule gets in the Case rail: removed, switched off, proven,
 * passing, or its worst problem. */
export function ruleLine(card: CaseRuleCard): RuleLine {
  // No failing check: proven when it caught an attack, passing only when a check
  // actually passed (a check not yet run is not a pass).
  const noFail = card.worst === 'caught' || card.worst === 'fires'
  const passed = Object.values(card.checks ?? {}).some((v) => (v as { result?: string })?.result === 'pass')
  const removed = card.status === 'removed'
  const off = card.stage === 'off'
  const healthy = !removed && !off && noFail && (card.caught || passed)
  const text = removed
    ? `Rule: it was removed from ${card.source.startsWith('repo:') ? 'its repo' : 'the SIEM'}`
    : off
      ? 'Rule is switched off'
      : (noFail
        ? card.caught ? 'Proven rule · caught an attack' : passed ? 'Rule passes its checks' : 'Rule fires · no check has passed yet'
        : `Rule: ${PROBLEM[card.worst] ?? card.worst.replace(/_/g, ' ')}`) + (card.stage === 'shadow' ? ' · in shadow' : '')
  const checks = Object.entries(card.checks ?? {})
    .filter(([k]) => k in CHECK_NAMES)
    .map(([k, v]) => `${CHECK_NAMES[k]} ${(v as { result?: string })?.result ?? '—'}`)
  const notes = [
    card.latest_version && card.latest_version !== card.version ? `fired from v${card.version}; now v${card.latest_version}` : '',
    card.by_name ? `${card.by_name} matched by the rule's name only` : '',
  ].filter(Boolean)
  return { ok: healthy, text, title: [card.name, checks.join(' · '), ...notes].filter(Boolean).join('\n'), link: !removed }
}

/** Each alert's rule card, by finding id. */
export function cardsByFinding(cards: CaseRuleCard[]): Record<string, CaseRuleCard> {
  const out: Record<string, CaseRuleCard> = {}
  for (const card of cards) for (const id of card.findings ?? []) out[id] ??= card
  return out
}
