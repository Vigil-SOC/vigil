import { useCallback, useEffect, useState } from 'react'
import { configApi, type AutonomyRatchet } from '../../services/api'
import { HoldButton } from '../../shared/HoldButton'
import { InfoTip } from '../../shared/InfoTip'
import { useToast } from '../../shell/toast'

const POLL_MS = 20_000
const MIN_REASON = 12

function errorText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
  const detail = data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string }
    if (first?.msg) return first.msg
  }
  const message = (error as { message?: string })?.message
  return message && message.trim() ? message : fallback
}

/** Naive timestamps from the API are UTC. */
function tightenedAt(iso: string | null): string {
  if (!iso) return ''
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(iso)
  const at = Date.parse(hasZone ? iso : `${iso}Z`)
  if (Number.isNaN(at)) return ''
  return `Tightened ${new Date(at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`
}

/**
 * The human-on-the-loop hold. Vigil tightens itself from analyst disagreement;
 * this card is how a person gives autonomy back.
 */
export function AutonomyRatchet() {
  const [state, setState] = useState<AutonomyRatchet | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { notify } = useToast()

  const load = useCallback(async () => {
    try {
      const read = await configApi.getRatchet()
      if (read.data.due && !read.data.holding) {
        try {
          const synced = await configApi.syncRatchet()
          setState(synced.data)
        } catch {
          setState(read.data)
        }
      } else {
        setState(read.data)
      }
      setError(null)
    } catch {
      // A quiet miss: Home still works when the ratchet cannot be read.
      setState(null)
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(id)
  }, [load])

  if (!state || (!state.holding && !state.due)) return null

  const promote = async () => {
    const text = reason.trim()
    if (text.length < MIN_REASON || busy) return
    setBusy(true)
    setError(null)
    const restoring = state?.will_restore_act
    try {
      const res = await configApi.promoteRatchet(text)
      setState(res.data)
      setReason('')
      notify(
        'ok',
        restoring
          ? 'Promoted. Vigil can act on its own again.'
          : 'Acknowledged. Approvals stay as they were.',
      )
    } catch (err) {
      setError(errorText(err, 'Could not promote autonomy'))
    } finally {
      setBusy(false)
    }
  }

  const rate = Math.round(state.disagree_rate * 100)
  const title = state.holding ? 'Vigil tightened itself' : 'Vigil is ready to tighten itself'
  const ready = reason.trim().length >= MIN_REASON && !busy

  return (
    <section className="home-ratchet" aria-label="Autonomy ratchet">
      <div className="home-ratchet-head">
        <h2>{title}</h2>
        <InfoTip
          label="How the autonomy ratchet is decided"
          align="start"
          source="Graded AI decisions and rejected response proposals"
          calculation={`${state.disagreements} disagreements of ${state.graded} graded, ${state.rejections} rejections, last ${state.window_hours}h. Tightens at ${rate}% across at least ${state.min_graded} grades (${state.min_disagreements} disagreements), or ${state.min_rejections} rejections at or above ${state.review_threshold.toFixed(2)}.`}
          limit="The system only tightens. A promotion starts a fresh window."
        />
      </div>
      {state.reason && <p className="home-ratchet-reason">{state.reason}</p>}
      {state.demoted_at && state.holding && <p className="home-ratchet-when">{tightenedAt(state.demoted_at)}</p>}
      {state.evidence.length > 0 && (
        <ul className="home-ratchet-evidence">
          {state.evidence.map((item) => (
            <li key={`${item.kind}-${item.id}`}>
              <span>{item.kind === 'rejection' ? 'Proposal' : 'Decision'}</span>
              {item.summary}
            </li>
          ))}
        </ul>
      )}
      {state.blocked_by_environment && (
        <p className="home-ratchet-note">
          The environment is forcing approval, so a promotion cannot restore Act until that changes.
        </p>
      )}
      {state.holding && state.may_promote && (
        <form
          className="home-ratchet-promote"
          onSubmit={(event) => {
            event.preventDefault()
          }}
        >
          <label>
            {state.will_restore_act
              ? 'Why are you giving autonomy back?'
              : 'Why are you clearing this notice?'}
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="A short sentence an auditor can read later"
              rows={2}
            />
          </label>
          <HoldButton
            label={state.will_restore_act ? 'Promote autonomy' : 'Clear the notice'}
            disabled={!ready}
            onConfirm={() => void promote()}
          />
        </form>
      )}
      {state.due && !state.holding && (
        <p className="home-ratchet-note">The daemon commits this on its next pass. Home catches it up when it can.</p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
