import './kit.css'

export type PillTone = 'idle' | 'live' | 'needs' | 'closed'

const LIVE = new Set(['assigned', 'executing', 'review_submitted'])

/** Colour pair and word for a case's combined state; any state shows Needs you while a decision is pending. */
export function statePill(state: string, needs = false): { tone: PillTone; label: string } {
  if (needs || state === 'waiting_approval') return { tone: 'needs', label: 'Needs you' }
  if (state === 'closed') return { tone: 'closed', label: 'Closed' }
  const word = state.replace(/_/g, ' ')
  return { tone: LIVE.has(state) ? 'live' : 'idle', label: word.charAt(0).toUpperCase() + word.slice(1) }
}

/** The state chip; the caller writes the reason after it. */
export function StatePill({ state, needs, className }: { state: string; needs?: boolean; className?: string }) {
  const { tone, label } = statePill(state, needs)
  return (
    <span className={`state-pill ${tone}${className ? ` ${className}` : ''}`}>
      <span className="state-dot" aria-hidden="true" />
      {label}
    </span>
  )
}
