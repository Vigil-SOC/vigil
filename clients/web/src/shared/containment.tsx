import { CURRENT, undoable, useOwnAccount, vendorName, type Containment } from './containmentRead'
import './kit.css'
import './containment.css'

/** What a pending containment would act on: the vendor records or carriers it resolved to, or why none can carry it. */
export function ContainmentTargets({ containment, picked, onPick, group }: {
  containment: Containment
  picked: string | null
  onPick: (id: string) => void
  /** Names the radio group; the action's id. */
  group: string
}) {
  const own = useOwnAccount()
  const { candidates, missing } = containment
  if (candidates.length === 0) return <p className="contain-missing">{missing || 'The target has not been resolved to a vendor ID yet.'}</p>
  const several = candidates.length > 1
  return (
    <div className="contain-targets">
      {several && <p className="contain-ask">{candidates.length} matches. Pick the one to act on.</p>}
      <ul aria-label="What this acts on">
        {candidates.map((c) => {
          const line = (
            <>
              <span className="contain-name">{c.name}</span>
              <span className="contain-meta">
                {c.carrier ?? vendorName(c.vendor)}
                {String(c.vendor_id) !== String(c.name) && <> · <code>{String(c.vendor_id)}</code></>}
                {c.state && ` · ${CURRENT[c.state] ?? c.state.replace(/_/g, ' ')}`}
                {` · ${undoable(c) ? 'Vigil can undo this' : 'Vigil cannot undo this'}`}
              </span>
            </>
          )
          return (
            <li key={c.id}>
              {several ? (
                <label>
                  <input type="radio" name={`contain-${group}`} checked={picked === c.id} onChange={() => onPick(c.id)} />
                  <span className="contain-line">{line}</span>
                </label>
              ) : (
                <span className="contain-line">{line}</span>
              )}
              {own(c) && <p className="contain-own">This is your own account.</p>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** A recorded state's chip tone and word (#5059 execution_result.state). */
const STATE: Record<string, { tone: string; word: string }> = {
  held: { tone: 'fair', word: 'Held' },
  running: { tone: 'live', word: 'Running' },
  requested: { tone: 'live', word: 'Requested' },
  contained: { tone: 'good', word: 'Contained' },
  disabled: { tone: 'good', word: 'Disabled' },
  released: { tone: 'idle', word: 'Released' },
  enabled: { tone: 'idle', word: 'Enabled' },
  blocked: { tone: 'good', word: 'Blocked' },
  unblocked: { tone: 'idle', word: 'Unblocked' },
  revoked: { tone: 'good', word: 'Revoked' },
  failed: { tone: 'needs', word: 'Failed' },
}

export function ContainmentState({ state }: { state: string }) {
  const shown = STATE[state] ?? { tone: 'idle', word: state.replace(/_/g, ' ') }
  return (
    <span className={`state-pill ${shown.tone}`}>
      <span className="state-dot" aria-hidden="true" />
      {shown.word}
    </span>
  )
}
