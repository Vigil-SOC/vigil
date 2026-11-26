import { useCallback, useEffect, useRef, useState } from 'react'
import { approvalsApi, type ApprovalAction } from '../../services/api'
import { HoldButton } from '../../shared/HoldButton'
import { ContainmentState } from '../../shared/containment'
import { containmentOf, undoable, useOwnAccount, vendorName } from '../../shared/containmentRead'
import { errorDetail } from './caseFormat'

/** A row the backend carried out through a vendor: it was resolved to candidates (#5234). */
const carried = (a: ApprovalAction) => Boolean((a.parameters as { containment?: unknown } | null)?.containment)
/** Each kind's undo verb and the state it undoes; a kind with none (a session revoke) has no undo. */
const UNDO: Record<string, { verb: string; done: string }> = {
  isolate_host: { verb: 'Release', done: 'contained' },
  disable_user: { verb: 'Enable', done: 'disabled' },
  block_ip: { verb: 'Unblock', done: 'blocked' },
  block_domain: { verb: 'Unblock', done: 'blocked' },
}
const pastOf = (verb: string) => (verb.endsWith('e') ? `${verb}d` : `${verb}ed`)
/** Read again on the needs block's interval, so a requested act shows its vendor's answer. */
const POLL_MS = 20_000

type Result = { state?: string; error?: string; vendor?: string; vendor_id?: string | number; run_by?: string; undone_by?: string }
const resultOf = (a: ApprovalAction) => (a.execution_result ?? {}) as Result
const undoesOf = (a: ApprovalAction) => ((a.parameters ?? {}) as { undoes?: string }).undoes

/** An approved containment's state: held by a stop, running, or what the vendor answered. */
function stateOf(a: ApprovalAction): string {
  if (a.status === 'approved') return (a as { held?: boolean }).held ? 'held' : 'running'
  if (a.status === 'failed') return 'failed'
  return resultOf(a).state ?? 'requested'
}

const OPEN = new Set(['held', 'running', 'requested'])

/** The containments approved on this case, with their undo (Release, Enable, Unblock) and Run again (#5060, #5234). */
export function CaseContainments({ caseId, canDecide, refreshKey }: { caseId: string; canDecide: boolean; refreshKey: number }) {
  const [rows, setRows] = useState<ApprovalAction[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The optional reason typed before each act's undo hold.
  const [why, setWhy] = useState<Record<string, string>>({})
  const own = useOwnAccount()
  const ticket = useRef(0)

  const load = useCallback(async () => {
    const mine = ++ticket.current
    try {
      const res = await approvalsApi.list({ case_id: caseId, limit: 500 })
      if (mine === ticket.current) setRows(res.data.actions.filter((a) => carried(a) && a.status !== 'pending' && a.status !== 'rejected'))
    } catch {
      // a failed read keeps what is shown
    }
  }, [caseId])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [load, refreshKey])

  const act = async (id: string, call: () => Promise<unknown>) => {
    if (busy) return
    setBusy(id)
    setError(null)
    try {
      await call()
    } catch (e) {
      setError(errorDetail(e, 'Could not carry that out'))
    } finally {
      setBusy(null)
      await load()
    }
  }

  const acts = rows.filter((a) => !undoesOf(a))
  if (acts.length === 0) return null
  // Each act's newest lift: shown on the act, not as a row of its own.
  const lifts = new Map<string, ApprovalAction>()
  for (const a of rows) {
    const of = undoesOf(a)
    if (of && !lifts.has(of)) lifts.set(of, a) // newest first
  }

  return (
    <section className="case-needs case-contain" aria-label="Containment">
      <div className="needs-more">
        <h3>Containment · {acts.length}</h3>
        {error && <p role="alert" className="needs-error">{error}</p>}
        <ul aria-label="Containment actions">
          {acts.map((a) => {
            const result = resultOf(a)
            const state = stateOf(a)
            const lift = lifts.get(a.action_id ?? '')
            const liftState = lift ? stateOf(lift) : null
            const undo = UNDO[a.action_type ?? '']
            const verb = undo?.verb ?? ''
            const candidate = containmentOf((a.parameters as { containment?: unknown } | null)?.containment)?.candidates.find(
              (c) => c.vendor === result.vendor && String(c.vendor_id) === String(result.vendor_id),
            )
            const liftable = canDecide && !!undo && state === undo.done && (!candidate || undoable(candidate)) && !(liftState && OPEN.has(liftState))
            const id = a.action_id ?? ''
            return (
              <li key={id}>
                <div className="needs-row">
                  <span className="needs-row-title">{a.title}</span>
                  <ContainmentState state={state} />
                  {(liftable || (canDecide && state === 'failed')) && (
                    <div className="needs-actions">
                      {liftable && <HoldButton label={`Hold to ${verb.toLowerCase()}`} name={`Hold to ${verb.toLowerCase()} ${a.target}`} disabled={busy !== null} onConfirm={() => void act(id, () => approvalsApi.undo(id, why[id]?.trim() || undefined))} />}
                      {canDecide && state === 'failed' && (
                        <button
                          type="button"
                          className="btn neutral"
                          disabled={busy !== null}
                          onClick={() => void act(id, () => approvalsApi.approve(id, result.vendor && result.vendor_id !== undefined ? `${result.vendor}:${result.vendor_id}` : undefined))}
                        >
                          Run again
                        </button>
                      )}
                    </div>
                  )}
                </div>
                {result.vendor && (
                  <p className="needs-row-reason">
                    {candidate?.carrier ?? vendorName(result.vendor)}
                    {String(result.vendor_id ?? '') !== a.target && <> · <code>{String(result.vendor_id ?? '')}</code></>}
                    {candidate && candidate.name !== a.target && ` · ${candidate.name}`}
                    {a.approved_by && ` · approved by ${a.approved_by}`}
                  </p>
                )}
                {candidate && own(candidate) && <p className="contain-own">This is your own account.</p>}
                {liftable && (
                  <div className="contain-why">
                    <label htmlFor={`undo-why-${id}`}>Why? Optional; the next run reads it. For 7 days only a person may redo this act here.</label>
                    <textarea
                      id={`undo-why-${id}`}
                      className="feedback-box"
                      rows={2}
                      maxLength={2000}
                      value={why[id] ?? ''}
                      onChange={(event) => setWhy((all) => ({ ...all, [id]: event.target.value }))}
                      placeholder={`Why is ${a.target} being ${pastOf(verb).toLowerCase()}?`}
                      disabled={busy !== null}
                    />
                  </div>
                )}
                {state === 'failed' && result.error && <p className="needs-error">{result.vendor ? vendorName(result.vendor) : 'The vendor'} answered: {result.error}</p>}
                {state === 'held' && <p className="needs-row-reason">Held by the stop. Releasing it from the stop runs it.</p>}
                {lift && <LiftLine lift={lift} state={liftState!} verb={verb} />}
              </li>
            )
          })}
        </ul>
      </div>
    </section>
  )
}

/** The newest Release or Enable on an act: who did it and where it stands. */
function LiftLine({ lift, state, verb }: { lift: ApprovalAction; state: string; verb: string }) {
  const by = lift.approved_by ? ` by ${lift.approved_by}` : ''
  if (state === 'failed') return <p className="needs-error">{verb}{by} failed: {resultOf(lift).error ?? 'the vendor refused it'}</p>
  if (state === 'held') return <p className="needs-row-reason">{verb}{by} is held by the stop. Releasing it from the stop runs it.</p>
  if (OPEN.has(state)) return <p className="needs-row-reason">{verb}{by} requested; waiting for the vendor.</p>
  return <p className="needs-row-reason">{pastOf(verb)}{by}.</p>
}
