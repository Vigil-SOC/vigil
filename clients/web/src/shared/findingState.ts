// A finding's terminal state (core.findings.alert_outcomes) in the board's five
// words: New, Scoring, Case NNNN, Resolved, Dropped. Both finding popups and the
// Dashboard table read it from here.
export type TerminalState =
  | 'waiting'
  | 'scoring'
  | 'working'
  | 'needs_you'
  | 'resolved_auto'
  | 'resolved_person'
  | 'dismissed'

/** The board's word; a live case reads as its id. */
export function stateWord(state: string | null | undefined, caseId?: string | null): string {
  switch (state) {
    case 'waiting':
      return 'New'
    case 'scoring':
      return 'Scoring'
    case 'working':
    case 'needs_you':
      return caseId ? `Case ${caseId}` : 'In a case'
    case 'resolved_auto':
    case 'resolved_person':
      return 'Resolved'
    case 'dismissed':
      return 'Dropped'
    default:
      return '—'
  }
}

/** Board order, for sorting: New first, Dropped last. */
export const STATE_RANK: Record<TerminalState, number> = {
  waiting: 0,
  scoring: 1,
  working: 2,
  needs_you: 2,
  resolved_auto: 3,
  resolved_person: 3,
  dismissed: 4,
}
