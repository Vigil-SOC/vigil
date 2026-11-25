import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import {
  recheckReviewApi,
  type RecheckCall,
  type RecheckQueueItem,
  type RecheckTicket,
} from '../../services/api'
import { errorText } from '../../shared/errorText'
import { EmptyState } from '../../shared/ui'
import type { ConsoleScreenProps } from '../../shared/types'
import { utcDayClock } from '../../shared/utc'
import './review.css'

const CALLS: { call: RecheckCall; label: string }[] = [
  { call: 'real', label: 'Real' },
  { call: 'not real', label: 'Not real' },
  { call: "can't tell", label: "Can't tell" },
]

function verdict(value: string | null): string {
  if (value === 'real') return 'Real'
  if (value === 'not real') return 'Not real'
  if (value === 'undetermined') return 'Undetermined'
  return value || '—'
}

function Row({ item, current, onOpen }: { item: RecheckQueueItem; current: boolean; onOpen: () => void }) {
  const label = item.title || item.ticket_id
  return (
    <button type="button" className="rev-row" aria-current={current || undefined} onClick={onOpen}>
      <b>{label}</b>
      <span>{item.source}{item.closed_at ? ` · ${utcDayClock(item.closed_at) || item.closed_at}` : ''}</span>
    </button>
  )
}

export default function ReviewScreen(_props: ConsoleScreenProps) {
  const { hasPermission } = useAuth()
  const canWrite = hasPermission('cases.write')
  const [pending, setPending] = useState<RecheckQueueItem[]>([])
  const [reviewed, setReviewed] = useState<RecheckQueueItem[]>([])
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [ticket, setTicket] = useState<RecheckTicket | null>(null)
  const [ticketError, setTicketError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback((quiet = false) => {
    if (!quiet) setPhase('loading')
    recheckReviewApi.queue().then(
      (res) => {
        setPending(res.data.pending)
        setReviewed(res.data.reviewed)
        setError(null)
        setPhase('ready')
      },
      (e) => {
        setError(errorText(e, 'Could not load the review queue.'))
        if (!quiet) setPhase('error')
      },
    )
  }, [])

  useEffect(load, [load])

  useEffect(() => {
    if (selected === null) {
      setTicket(null)
      return
    }
    let live = true
    setTicket(null)
    setTicketError(null)
    recheckReviewApi.get(selected).then(
      (res) => { if (live) setTicket(res.data) },
      (e) => { if (live) setTicketError(errorText(e, 'Could not open this ticket.')) },
    )
    return () => { live = false }
  }, [selected])

  const confirm = async (call: RecheckCall) => {
    if (selected === null) return
    setBusy(true)
    try {
      const res = await recheckReviewApi.confirm(selected, call)
      setTicket(res.data)
      load(true)
    } catch (e) {
      setTicketError(errorText(e, 'Could not record the call.'))
    } finally {
      setBusy(false)
    }
  }

  const openCase = async () => {
    if (selected === null) return
    setBusy(true)
    try {
      const res = await recheckReviewApi.openCase(selected)
      setTicket((prev) => prev && { ...prev, case_id: res.data.case_id, can_open: false })
      load(true)
    } catch (e) {
      setTicketError(errorText(e, 'Could not open a Case.'))
    } finally {
      setBusy(false)
    }
  }

  const empty = phase === 'ready' && pending.length === 0 && reviewed.length === 0

  return (
    <div className="rev-page">
      {phase === 'loading' && <EmptyState icon="clock" title="Loading the queue" loading />}
      {phase === 'error' && <EmptyState icon="alert" title="Could not load the queue" body={error} error />}
      {empty && (
        <EmptyState
          icon="check"
          title="Nothing to review"
          body="A finished re-check has nothing in the queue."
        />
      )}
      {phase === 'ready' && !empty && (
        <div className="rev-layout">
          <div className="rev-list">
            <div className="rev-group">
              <div className="rev-label">To review</div>
              {pending.length === 0 && <p className="rev-note">Nothing left to review.</p>}
              {pending.map((item) => (
                <Row key={item.id} item={item} current={item.id === selected} onOpen={() => setSelected(item.id)} />
              ))}
            </div>
            <div className="rev-group">
              <div className="rev-label">Reviewed</div>
              {reviewed.length === 0 && <p className="rev-note">No call recorded yet.</p>}
              {reviewed.map((item) => (
                <Row key={item.id} item={item} current={item.id === selected} onOpen={() => setSelected(item.id)} />
              ))}
            </div>
          </div>
          <section className="rev-card" aria-label="Ticket">
            {selected === null && <p className="rev-note">Choose a ticket. The verdicts stay hidden until you record a call.</p>}
            {selected !== null && !ticket && !ticketError && <p className="rev-note">Loading…</p>}
            {ticketError && <p className="rev-note" role="alert">{ticketError}</p>}
            {ticket?.removed && <p className="rev-note">{ticket.notice || 'removed with its import'}</p>}
            {ticket && !ticket.removed && (
              <>
                <h2>{ticket.title || ticket.ticket_id}</h2>
                <p className="rev-meta">{ticket.source}{ticket.closed_at ? ` · closed ${utcDayClock(ticket.closed_at) || ticket.closed_at}` : ''}</p>
                {ticket.alerts_hidden && <p className="rev-note">Hidden without automation.read.</p>}
                {ticket.alerts?.map((alert, i) => (
                  <article key={i} className="rev-alert" tabIndex={0}>
                    <b>{alert.title || 'Alert'}</b>
                    <p className="rev-meta">
                      {[alert.time && (utcDayClock(alert.time) || alert.time), alert.rule, alert.severity, alert.entity_keys.join(', ')]
                        .filter(Boolean)
                        .join(' · ')}
                    </p>
                    {alert.description && <p className="rev-prose">{alert.description}</p>}
                  </article>
                ))}
                {ticket.evidence_hidden && <p className="rev-note">Hidden without cases.read.</p>}
                {ticket.evidence && (
                  <>
                    <p className="rev-note">This evidence came from Vigil's run.</p>
                    {ticket.evidence.length === 0 && <p className="rev-note">No tool results.</p>}
                    {ticket.evidence.map((row, i) => (
                      <article key={i} className="rev-evidence" tabIndex={0}>
                        <b>{row.tool || 'Tool'}</b>
                        <pre className="rev-result">{row.result}</pre>
                      </article>
                    ))}
                  </>
                )}
                {ticket.recorded && (
                  <div className="rev-found">
                    <div>Recorded call: {verdict(ticket.call)}</div>
                    <div>Closure: {verdict(ticket.closure_verdict)}</div>
                    <div>Vigil: <span className="vigil">{verdict(ticket.vigil_verdict)}</span></div>
                    {ticket.reviewers_differ && <div className="differ">Reviewers differ.</div>}
                    {ticket.summary && (
                      <article className="rev-alert" tabIndex={0}>
                        <b>Vigil's write-up</b>
                        <p className="rev-prose">{ticket.summary}</p>
                      </article>
                    )}
                    {ticket.proposed_actions && ticket.proposed_actions.length > 0 && (
                      <div>
                        <b>Proposed actions</b>
                        {ticket.proposed_actions.map((action, i) => (
                          <p key={i} className="rev-meta">
                            {[action.action_type, action.target, action.reason].filter(Boolean).join(' · ')}
                          </p>
                        ))}
                      </div>
                    )}
                    {ticket.run_id && (
                      <Link to={`/workflows?run=${encodeURIComponent(ticket.run_id)}`}>Run {ticket.run_id}</Link>
                    )}
                    {ticket.case_id && (
                      <Link to={`/cases?case=${encodeURIComponent(ticket.case_id)}`}>Case {ticket.case_id}</Link>
                    )}
                  </div>
                )}
                {canWrite && (
                  <div className="rev-calls">
                    {CALLS.map((one) => (
                      <button key={one.call} type="button" className="btn" disabled={busy} onClick={() => void confirm(one.call)}>
                        {one.label}
                      </button>
                    ))}
                    {ticket.can_open && !ticket.case_id && (
                      <button type="button" className="btn primary" disabled={busy} onClick={() => void openCase()}>
                        Open a Case
                      </button>
                    )}
                  </div>
                )}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
