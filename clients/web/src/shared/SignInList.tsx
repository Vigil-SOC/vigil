import type { SignIn } from '../services/api'

const stamp = (iso: string) =>
  new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

const method = (m: string) => (m === 'mfa' ? 'Password and authenticator app' : 'Password')

/**
 * A person's sign-ins as a row stack: start, last action, browser and OS, device,
 * address and how it signed in; ended rows say when, and why. `onEnd` adds an End
 * button to open rows (the current one is never offered: that is Sign out).
 */
export default function SignInList({
  signIns,
  onEnd,
  busy = false,
}: {
  signIns: SignIn[]
  onEnd?: (s: SignIn) => void
  busy?: boolean
}) {
  if (signIns.length === 0) {
    return <div className="text-sm text-tx-3 py-6 text-center">No sign-ins in the last 30 days.</div>
  }
  return (
    <ul
      className="flex flex-col gap-px m-0 p-0 list-none rounded-[10px] overflow-x-hidden overflow-y-auto max-h-[52vh]"
      style={{ background: 'var(--line)' }}
    >
      {signIns.map((s) => {
        const ended = !!s.ended_at
        return (
          <li
            key={s.sign_in_id}
            className={`flex flex-wrap items-start justify-between gap-x-3 gap-y-2 px-3 py-2.5 bg-bg-2 ${ended ? 'text-tx-3' : ''}`}
          >
            <div className="min-w-0 flex-1 basis-56">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <span className={ended ? '' : 'text-tx'}>{s.browser}</span>
                {s.device && <span className="text-xs text-tx-3">{s.device}</span>}
                {s.current && <span className="chip">This sign-in</span>}
              </div>
              <div className="mt-0.5 text-xs text-tx-3 break-words">
                {ended
                  ? `Ended ${stamp(s.ended_at!)} · last active ${stamp(s.last_action_at)} · ${s.end_reason_text}`
                  : `Started ${stamp(s.started_at)} · last action ${stamp(s.last_action_at)}`}
              </div>
              <div className="text-xs text-tx-3 break-words">
                {[s.address, method(s.auth_method)].filter(Boolean).join(' · ')}
              </div>
            </div>
            {onEnd && !ended && !s.current && (
              <button className="btn ghost" disabled={busy} onClick={() => onEnd(s)}>
                End
              </button>
            )}
          </li>
        )
      })}
    </ul>
  )
}
