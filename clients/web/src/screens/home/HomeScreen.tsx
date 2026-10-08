import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ConsoleScreenProps } from '../../shared/types'
import { approvalsApi, configApi, triageApi, type NeedsYouItem } from '../../services/api'
import { HoldButton } from '../../shared/HoldButton'
import { useToast } from '../../shell/toast'
import './home.css'

const POLL_MS = 20_000
const VISIBLE = 4
const HIDDEN_KEY = 'vigil.home.setup.hidden'

type SetupStep = {
  id: string
  title: string
  state_line: string
  done: boolean
  href: string
}

type SetupSteps = {
  steps: SetupStep[]
  alerts_exist: number
  demo_enabled: boolean
}

const STEP_ACTION: Record<string, string> = {
  connect_tools: 'Set up',
  notify: 'Set up',
  rules: 'Link',
  per_agent: 'Pick',
}

function readHidden(): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(HIDDEN_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : []
  } catch {
    return []
  }
}

function headline(count: number): string {
  if (count === 0) return 'Board clear.'
  if (count === 1) return '1 decision waits on you. Everything else is running.'
  return `${count} decisions wait on you. Everything else is running.`
}

/** Naive timestamps from the API are UTC. `Date.parse` would read them as local. */
export function parseCreatedAt(createdAt: string): number {
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(createdAt)
  return Date.parse(hasZone ? createdAt : `${createdAt}Z`)
}

function waited(createdAt: string): string {
  const then = parseCreatedAt(createdAt)
  if (Number.isNaN(then)) return ''
  const minutes = Math.max(0, Math.floor((Date.now() - then) / 60_000))
  if (minutes < 1) return 'Waited under a minute'
  if (minutes < 60) return `Waited ${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  return rem ? `Waited ${hours}h ${rem}m` : `Waited ${hours}h`
}

function errorText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
  if (typeof data?.detail === 'string' && data.detail.trim()) return data.detail
  const message = (error as { message?: string })?.message
  return message && message.trim() ? message : fallback
}

function DecisionCard({
  item,
  busy,
  onApprove,
  onReject,
  onOpenCase,
}: {
  item: NeedsYouItem
  busy: boolean
  onApprove: (item: NeedsYouItem, fused: boolean) => void
  onReject: (item: NeedsYouItem, reason: string) => void
  onOpenCase: (id: string) => void
}) {
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const caseId = item.case_id
  const meta = [item.kind, item.case_id, waited(item.created_at)].filter(Boolean).join(' · ')

  return (
    <article className="card">
      <div className="card-b">
        <h3>{item.title}</h3>
        <p className="home-meta">{meta}</p>
        {item.reason && <p className="home-reason">{item.reason}</p>}
        <div className="home-actions">
          {item.reversibility === 'reversible' ? (
            <button type="button" className="btn primary" disabled={busy} onClick={() => onApprove(item, true)}>
              Approve
            </button>
          ) : (
            <HoldButton label="Approve" disabled={busy} onConfirm={() => onApprove(item, false)} />
          )}
          {rejecting ? (
            <form
              className="home-reject"
              onSubmit={(event) => {
                event.preventDefault()
                const text = reason.trim()
                if (!text) return
                onReject(item, text)
              }}
            >
              <textarea
                className="feedback-box"
                aria-label="Rejection reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why is this being rejected?"
                autoFocus
              />
              <button type="submit" className="btn danger" disabled={busy || !reason.trim()}>
                Reject
              </button>
            </form>
          ) : (
            <button type="button" className="btn danger" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </button>
          )}
          {caseId && (
            <Link
              className="btn ghost"
              to={`/cases?case=${encodeURIComponent(caseId)}`}
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey) return // new tab or window
                event.preventDefault()
                onOpenCase(caseId)
              }}
            >
              Open case
            </Link>
          )}
        </div>
      </div>
    </article>
  )
}

export default function HomeScreen({ openCase }: ConsoleScreenProps) {
  const [items, setItems] = useState<NeedsYouItem[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [share, setShare] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [setup, setSetup] = useState<SetupSteps | null>(null)
  const [setupError, setSetupError] = useState<string | null>(null)
  const [hidden, setHidden] = useState<string[]>(() => readHidden())
  const [demoMessage, setDemoMessage] = useState<string | null>(null)
  const [demoBusy, setDemoBusy] = useState(false)
  const busyRef = useRef<string | null>(null)
  const loadTicket = useRef(0)
  const { notify, notifyUndoable, pending, settled } = useToast()

  const load = useCallback(async () => {
    const ticket = ++loadTicket.current
    const needs = approvalsApi.needsYou().then(
      (res) => ({ ok: true as const, data: res.data }),
      (err: unknown) => ({ ok: false as const, err }),
    )
    const shareRead = triageApi.get().then(
      (res) => {
        const value = res.data.strip.picked_up.share
        return typeof value === 'number' ? value : null
      },
      () => null,
    )
    const [needsResult, shareValue] = await Promise.all([needs, shareRead])
    if (ticket !== loadTicket.current) return
    setShare(shareValue)
    if (!needsResult.ok) {
      setError(errorText(needsResult.err, 'Could not load what needs you'))
      return
    }
    setItems(needsResult.data.items)
    setCount(needsResult.data.count)
    setError(null)
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => {
      void load()
    }, POLL_MS)
    return () => window.clearInterval(id)
  }, [load])

  useEffect(() => {
    let cancelled = false
    configApi
      .getSetupSteps()
      .then((res) => {
        if (!cancelled) setSetup(res.data)
      })
      .catch((err) => {
        if (!cancelled) setSetupError(errorText(err, 'Could not load setup steps'))
      })
    return () => {
      cancelled = true
    }
  }, [])

  // a fused commit just landed or failed (possibly after Home was left and reopened): refresh
  const seenSettled = useRef(settled)
  useEffect(() => {
    if (seenSettled.current === settled) return
    seenSettled.current = settled
    void load()
  }, [settled, load])

  const dismiss = (id: string) => {
    const next = hidden.includes(id) ? hidden : [...hidden, id]
    sessionStorage.setItem(HIDDEN_KEY, JSON.stringify(next))
    setHidden(next)
  }

  const exploreDemo = async () => {
    setDemoBusy(true)
    try {
      const res = await configApi.setDemoMode(true)
      const message = res.data?.message
      setDemoMessage(typeof message === 'string' ? message : '')
      setSetup((current) => (current ? { ...current, demo_enabled: true } : current))
    } catch (err) {
      setSetupError(errorText(err, 'Could not enable demo mode'))
    } finally {
      setDemoBusy(false)
    }
  }

  // Reversible approve and every reject commit through the toast's undo fuse, which owns the
  // call, so it survives leaving Home. A held (irreversible) approve commits at once.
  const fuse = (id: string, verb: string, past: string, title: string, commit: () => Promise<unknown>) =>
    notifyUndoable({
      key: id,
      text: `${verb}: ${title}`,
      commit,
      doneText: `${past}: ${title}`,
      failText: (err) => errorText(err, 'Could not update that decision'),
    })

  const approve = (item: NeedsYouItem, fused: boolean) => {
    const commit = () => approvalsApi.approve(item.source_id)
    if (fused) return fuse(item.source_id, 'Approving', 'Approved', item.title, commit)
    void run(item.source_id, commit, `Approved: ${item.title}`)
  }

  const reject = (item: NeedsYouItem, reason: string) =>
    fuse(item.source_id, 'Rejecting', 'Rejected', item.title, () => approvalsApi.reject(item.source_id, reason))

  const run = async (id: string, act: () => Promise<unknown>, doneText: string) => {
    if (busyRef.current) return
    busyRef.current = id
    setBusy(id)
    try {
      await act()
      notify('ok', doneText)
      await load()
    } catch (err) {
      setError(errorText(err, 'Could not update that decision'))
    } finally {
      busyRef.current = null
      setBusy(null)
    }
  }

  // cards whose fuse is running stay out of the list and the count, so a poll can't bring them back
  const shown = items.filter((it) => !pending.includes(it.source_id))
  const shownCount = count === null ? null : Math.max(0, count - (items.length - shown.length))
  const visible = showAll ? shown : shown.slice(0, VISIBLE)
  const moreWaiting = shown.length - visible.length
  const openSteps = (setup?.steps ?? []).filter((step) => !step.done && !hidden.includes(step.id))
  const noAlerts = setup !== null && setup.alerts_exist === 0

  return (
    <div className="home-screen">
      {shownCount !== null && <p className="home-headline">{headline(shownCount)}</p>}
      {share !== null && (
        <p className="home-share">{(share * 100).toFixed(1)}% of alerts picked up automatically today</p>
      )}
      {error && (
        <p className="section" role="alert">
          {error}
        </p>
      )}
      <section className="home-setup section" aria-label="Setup">
        <div className="home-head">
          <h2>Get more from Vigil</h2>
          <Link to="/settings?section=integrations">Browse integrations →</Link>
        </div>
        {setupError && <p role="alert">{setupError}</p>}
        {openSteps.length > 0 && (
          <ul className="home-steps">
            {openSteps.map((step) => (
              <li key={step.id} className="home-step">
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.state_line}</p>
                </div>
                <div className="home-step-actions">
                  <Link className="btn primary" to={step.href}>
                    {STEP_ACTION[step.id] ?? 'Open'}
                  </Link>
                  <button type="button" className="btn ghost" onClick={() => dismiss(step.id)}>
                    Not now
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {noAlerts && (
          <div className="home-alerts">
            <Link className="btn primary" to="/settings?section=data">
              Connect data
            </Link>
            {!setup.demo_enabled && (
              <button type="button" className="btn" disabled={demoBusy} onClick={() => void exploreDemo()}>
                Explore with demo data
              </button>
            )}
            {demoMessage && <p className="home-meta">{demoMessage}</p>}
          </div>
        )}
      </section>
      <section className="section" aria-label="Needs your attention">
        <div className="home-head">
          <h2>Needs your attention</h2>
          <Link to="/cases">Cases →</Link>
        </div>
        <div className="home-cards">
          {visible.map((item) => (
            <DecisionCard
              key={item.source_id}
              item={item}
              busy={busy !== null}
              onApprove={approve}
              onReject={reject}
              onOpenCase={openCase}
            />
          ))}
        </div>
        {moreWaiting > 0 && (
          <button type="button" className="btn ghost" style={{ marginTop: 12 }} onClick={() => setShowAll(true)}>
            {moreWaiting} more waiting
          </button>
        )}
      </section>
    </div>
  )
}
