import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ConsoleScreenProps } from '../../shared/types'
import { approvalsApi, configApi, triageApi, type NeedsYouItem, type TriageRow } from '../../services/api'
import { HoldButton } from '../../shared/HoldButton'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
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
  custom_skill: 'Add',
}

const KIND_LABEL: Record<NeedsYouItem['kind'], string> = { approval: 'Approval', checkpoint: 'Checkpoint' }

type Pickup = { share: number; launched: number; today: number }

type Chip = { label: string; text: string }

/** Chips for the newest detection that timed out of the queue unworked today (UTC). None without one. */
export function dropChips(rows: TriageRow[], now = Date.now()): Chip[] {
  const dayStart = now - (now % 86_400_000)
  let newest: { row: TriageRow; at: number } | null = null
  for (const row of rows) {
    if (row.kind !== 'detection' || row.state !== 'expired' || !row.finding_id || !row.decided_at) continue
    const at = parseCreatedAt(row.decided_at)
    if (Number.isNaN(at) || at < dayStart || at >= dayStart + 86_400_000) continue
    if (!newest || at > newest.at) newest = { row, at }
  }
  if (!newest) return []
  const { finding_id: id, source } = newest.row
  return [
    { label: `/investigate ${id}`, text: `/investigate ${id}` },
    source
      ? { label: `Why was the ${source} alert dropped?`, text: `/ask Why was the ${source} alert dropped? Finding ${id}` }
      : { label: `Why was alert ${id} dropped?`, text: `/ask Why was alert ${id} dropped? Finding ${id}` },
  ]
}

const SETUP_TIP = {
  source: 'Setup steps (B8)',
  calculation: 'Fixed order; done steps and steps hidden with Not now are left out',
  limit: 'None',
}

const NEEDS_TIP = {
  source: 'Pending approvals and checkpoints (B1)',
  calculation: 'Oldest first, top four shown',
}

function readHidden(): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(HIDDEN_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : []
  } catch {
    return []
  }
}

const FIRST_RUN_HEADLINE = 'Nothing is connected yet, so nothing needs you'

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
  const meta = [caseId && `Case ${caseId}`, item.reason].filter(Boolean).join(' · ')

  return (
    <article className="home-card">
      <div className="home-card-top">
        <span className="home-waited">{waited(item.created_at)}</span>
        <span className="home-kind">{KIND_LABEL[item.kind] ?? item.kind}</span>
      </div>
      <h3>{item.title}</h3>
      {meta && (
        <p className="home-card-meta" title={meta}>
          {meta}
        </p>
      )}
      <div className="home-actions">
        {item.reversibility === 'reversible' ? (
          <button type="button" className="home-btn primary" disabled={busy} onClick={() => onApprove(item, true)}>
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
            <button type="submit" className="home-btn secondary" disabled={busy || !reason.trim()}>
              Reject
            </button>
          </form>
        ) : (
          <button type="button" className="home-btn secondary" disabled={busy} onClick={() => setRejecting(true)}>
            Reject
          </button>
        )}
        {caseId && (
          <Link
            className="home-btn ghost"
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
    </article>
  )
}

export default function HomeScreen({ openCase, startTour, fillCommand }: ConsoleScreenProps) {
  const [items, setItems] = useState<NeedsYouItem[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [share, setShare] = useState<Pickup | null>(null)
  const [chips, setChips] = useState<Chip[]>([])
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
        const { share: value, launched_or_merged: launched, created_today: today } = res.data.strip.picked_up
        return {
          pickup: typeof value === 'number' ? { share: value, launched, today } : null,
          chips: dropChips(res.data.rows ?? []),
        }
      },
      () => ({ pickup: null, chips: [] as Chip[] }),
    )
    const [needsResult, triage] = await Promise.all([needs, shareRead])
    if (ticket !== loadTicket.current) return
    setShare(triage.pickup)
    setChips(triage.chips)
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
  const rest = shown.slice(visible.length)
  const moreWaiting = rest.length
  const openSteps = (setup?.steps ?? []).filter((step) => !step.done && !hidden.includes(step.id))
  const noAlerts = setup !== null && setup.alerts_exist === 0
  const doneCount = (setup?.steps ?? []).filter((step) => step.done).length
  const stepCount = setup?.steps.length ?? 0
  // open steps first, then done ones, each in served order; the first open step is "next"
  const checklist = [
    ...(setup?.steps ?? []).filter((step) => !step.done),
    ...(setup?.steps ?? []).filter((step) => step.done),
  ]
  const nextStepId = checklist.find((step) => !step.done)?.id
  const shownChips = fillCommand ? chips : []
  const boardClear = shownCount !== null && !error && shown.length === 0 && !noAlerts

  const sectionHead = (title: string, sub: string | null, tip: ReactNode, link: ReactNode) => (
    <div className="home-head">
      <div className="home-head-title">
        <h2>{title}</h2>
        {sub && <span className="home-sub">{sub}</span>}
        {tip}
      </div>
      {link}
    </div>
  )

  return (
    <div className="home-screen">
      <div className="home-page">
        {(shownCount !== null || share !== null || shownChips.length > 0) && (
          <div className="home-top">
            {shownCount !== null && (setup !== null || setupError !== null) && (
              <p className="home-headline">{noAlerts ? FIRST_RUN_HEADLINE : headline(shownCount)}</p>
            )}
            {(share !== null || shownChips.length > 0) && (
              <div className="home-chips">
                {shownChips.map((chip) => (
                  <button key={chip.text} type="button" className="home-chip" onClick={() => fillCommand?.(chip.text)}>
                    {chip.label}
                  </button>
                ))}
                {share !== null && (
                  <p className="home-share">
                    {shownChips.length > 0 && (
                      <span className="home-dot" aria-hidden="true">
                        ·
                      </span>
                    )}
                    {Math.round(share.share * 100)}% of alerts picked up automatically today
                    <InfoTip
                      label="How the pickup share is calculated"
                      align="start"
                      source="Intake triggers"
                      calculation={`Launched or merged ÷ arrived today (UTC), ${share.launched} of ${share.today}`}
                      limit="None"
                    />
                  </p>
                )}
              </div>
            )}
          </div>
        )}
        {error && <p role="alert">{error}</p>}
        {!noAlerts && (
          <section aria-label="Setup">
            {sectionHead(
              'Get more from Vigil',
              'Fixed order · most impact first',
              <InfoTip label="How Get more from Vigil is calculated" align="start" {...SETUP_TIP} />,
              <Link to="/settings?section=integrations">Browse integrations →</Link>,
            )}
            {setupError && <p role="alert">{setupError}</p>}
            {openSteps.length > 0 ? (
              <ul className="home-steps" style={{ gridTemplateColumns: `repeat(${openSteps.length}, minmax(0, 1fr))` }}>
                {openSteps.map((step, index) => (
                  <li key={step.id} className="home-step">
                    <span className={`home-step-no${index === 0 ? ' first' : ''}`} aria-hidden="true">
                      {index + 1}
                    </span>
                    <h3>{step.title}</h3>
                    <p title={step.state_line}>{step.state_line}</p>
                    <div className="home-step-foot">
                      <button type="button" className="home-step-skip" onClick={() => dismiss(step.id)}>
                        Not now
                      </button>
                      <Link className="home-step-act" to={step.href}>
                        {STEP_ACTION[step.id] ?? 'Open'}
                        <Icon name="arrowR" size={13} />
                      </Link>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              setup !== null && <p className="home-steps-none">Nothing to suggest right now.</p>
            )}
          </section>
        )}
        <section aria-label="Needs your attention">
          {sectionHead(
            'Needs your attention',
            noAlerts || shownCount === null
              ? null
              : `${shownCount} open · oldest first${moreWaiting > 0 ? ` · showing ${VISIBLE}` : ''}`,
            <InfoTip label="How Needs your attention is calculated" align="start" {...NEEDS_TIP} />,
            <Link to="/cases">Cases →</Link>,
          )}
          {noAlerts && setup && (
            <>
          {setupError && <p role="alert">{setupError}</p>}
          <div className="home-first-grid">
            <div className="home-ready">
              <div className="home-ready-head">
                <div>
                  <h2>Get Vigil ready</h2>
                  <p>Tick steps off as you go.</p>
                </div>
                <span className="home-ready-count" aria-live="polite">
                  {doneCount} of {stepCount} done
                </span>
              </div>
              <div className="home-bar" aria-hidden="true">
                <span
                  className={doneCount === stepCount ? 'done' : undefined}
                  style={{ width: `${stepCount ? (doneCount / stepCount) * 100 : 0}%` }}
                />
              </div>
              <ul className="home-checks" aria-label="Setup steps">
                {checklist.map((step) => {
                  const next = step.id === nextStepId
                  return (
                    <li key={step.id} className={`home-check${step.done ? ' done' : ''}${next ? ' next' : ''}`}>
                      <span className="home-tick" aria-hidden="true">
                        {step.done && <Icon name="check" size={13} />}
                      </span>
                      <div>
                        <h3>
                          {step.title}
                          {step.done && <span className="sr-only"> (done)</span>}
                        </h3>
                        <p>{step.state_line}</p>
                      </div>
                      <Link className={next ? 'btn primary' : 'btn'} to={step.href}>
                        {step.done ? 'Edit' : (STEP_ACTION[step.id] ?? 'Open')}
                      </Link>
                    </li>
                  )
                })}
              </ul>
            </div>
            <div className="home-first-side">
              <div className="home-empty">
                <span className="home-empty-icon" aria-hidden="true">
                  <Icon name="link" size={20} />
                </span>
                <h3>No alerts yet</h3>
                <p>
                  Connect a SIEM, EDR or the LogLM pipeline and alerts start arriving within minutes. Decisions
                  that need you will show up here.
                </p>
                <Link className="btn primary" to="/settings?section=data">
                  <Icon name="plus" size={13} />
                  Connect data
                </Link>
              </div>
              <div className="home-notready">
                <h3>Not ready to connect yet?</h3>
                <p>
                  Load a day of demo data from a sample estate. Everything is labelled demo and can be cleared in
                  one click.
                </p>
                <div className="home-notready-actions">
                  {!setup.demo_enabled && (
                    <button type="button" className="btn" disabled={demoBusy} onClick={() => void exploreDemo()}>
                      <Icon name="play" size={13} />
                      Explore with demo data
                    </button>
                  )}
                  {startTour && (
                    <button type="button" className="btn ghost" onClick={startTour}>
                      Take the tour
                    </button>
                  )}
                </div>
                {demoMessage && <p className="home-meta">{demoMessage}</p>}
              </div>
            </div>
          </div>
            </>
          )}
          {boardClear && (
            <div className="home-clear">
              <h3>Board clear</h3>
              <p>Nothing waits on you. The fleet runs inside its limits; anything irreversible waits for a person.</p>
            </div>
          )}
          {visible.length > 0 && (
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
              {moreWaiting > 0 && (
                <aside className="home-more" aria-label="More waiting">
                  <h3>+{moreWaiting} more waiting</h3>
                  <ul>
                    {(Object.keys(KIND_LABEL) as NeedsYouItem['kind'][]).map((kind) => {
                      const n = rest.filter((it) => it.kind === kind).length
                      return (
                        n > 0 && (
                          <li key={kind}>
                            <span>{KIND_LABEL[kind]}</span>
                            <b>{n}</b>
                          </li>
                        )
                      )
                    })}
                  </ul>
                  <button type="button" className="home-btn primary" onClick={() => setShowAll(true)}>
                    Show all
                  </button>
                </aside>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
