import { useEffect, useRef, useState, type ReactNode } from 'react'
import api from '../../services/api'
import type { OverviewAgent } from '../../services/api'
import { Icon, type IconName } from '../../shared/icons'
import { LevelBadge } from '../../shared/LevelBadge'
import { useSourceBadge } from '../../shared/useSourceBadge'
import { Popup } from '../../shared/ui'
import { SevMark } from './AlertRail'
import './overviewDrill.css'

/* What a click on the Overview board opens: the alerts behind an outcome count, or one agent's record. */

const PAGE = 50

export interface OutcomeAlert {
  finding_id: string
  title: string | null
  description: string | null
  severity: string | null
  data_source: string
  created_at: string | null
  case_id: string | null
}

export interface OutcomeCase {
  case_id: string
  title: string | null
  status: string | null
  priority: string | null
  updated_at: string | null
  alerts: number
}

export interface OutcomeList {
  state: string
  label: string
  explain: string
  total: number
  offset: number
  items: OutcomeAlert[]
  cases: OutcomeCase[]
  by_source: { data_source: string; count: number }[]
}

export interface AgentRun {
  run_id: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  cost_usd: number
  outcome: string | null
  summary: string | null
  triggered_by: string | null
  case_id: string | null
  finding_id: string | null
}

export interface AgentRecord {
  workflow: {
    id: string
    name: string
    source: string
    description?: string | null
    use_case?: string | null
    run_kind?: string | null
    agents?: string[]
    tools_used?: string[]
    objectives?: string[]
    trigger_examples?: string[]
    agent?: { name?: string; id?: string } | null
    phases?: { name: string; agent_id?: string | null; purpose?: string | null; tools?: string[]; approval_required?: boolean }[] | null
  }
  live: AgentRun[]
  recent: AgentRun[]
  record: {
    days: number
    runs: number
    completed: number
    failed: number
    cancelled: number
    rate: number | null
    median_ms: number | null
    mean_cost_usd: number | null
  }
}

// What an empty outcome means, so a 0 reads as a fact rather than a gap.
const EMPTY: Record<string, string> = {
  resolved_auto: 'No case has been closed by an agent today yet.',
  resolved_person: 'No analyst has closed a case today yet.',
  working: 'No case is open right now.',
  needs_you: 'Nothing is waiting on a person. Approvals land here when an agent wants to act.',
  dismissed: 'Nothing has been dropped as noise today.',
  scoring: 'Nothing is waiting to be scored. New alerts are usually scored within a minute of arriving, so this is often empty.',
  waiting: 'Every alert today is in a case, queued or dropped.',
}

const humanize = (name: string) =>
  !name.includes(' ') && name.includes('-') ? name.charAt(0).toUpperCase() + name.slice(1).replace(/-/g, ' ') : name

const fmt = (n: number) => n.toLocaleString('en-US')

function when(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  if (Number.isNaN(t.getTime())) return iso
  const hm = t.toISOString().slice(11, 16)
  const sameDay = t.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10)
  return sameDay ? hm : `${t.getUTCDate()} ${t.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' })} ${hm}`
}

function duration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

// A run summary may be the agent's markdown ("### Triage …"): one plain line for the row.
const plain = (md: string | null) =>
  md ? md.replace(/^\s*#{1,6}\s+/gm, '').replace(/[*_`]+/g, '').replace(/\s+/g, ' ').trim() : ''

const money = (n: number | null) => (n === null || n === undefined ? '—' : `$${n.toFixed(2)}`)

function SourceMark({ source, size = 13 }: { source: string; size?: number }) {
  const badge = useSourceBadge()(source)
  return (
    <span className="ovd-src" title={badge.label}>
      <Icon name={badge.icon as IconName} size={size - 2} />
      <span>{badge.label}</span>
    </span>
  )
}

function useGet<T>(url: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    if (!url) return
    let live = true
    setError(false)
    api
      .get<T>(url)
      .then((res) => live && setData(res.data))
      .catch(() => live && setError(true))
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, ...deps])
  return { data, error, setData }
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="ovd-sec">
      <div className="ovd-sec-h">
        <h3>{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  )
}

/** The alerts behind one outcome count, today (UTC), and the cases they sit in. */
export function OutcomePanel({
  state,
  color,
  onClose,
  onOpenAlert,
  openCase,
  goTriage,
}: {
  state: string | null
  color?: string
  onClose: () => void
  onOpenAlert: (id: string) => void
  openCase: (id: string) => void
  goTriage: () => void
}) {
  const [extra, setExtra] = useState<OutcomeAlert[]>([])
  const [more, setMore] = useState(false)
  const { data, error } = useGet<OutcomeList>(state ? `/overview/outcomes/${encodeURIComponent(state)}?limit=${PAGE}` : null)
  // Bumped on every open, so a page still loading for an earlier one is dropped.
  const opened = useRef(0)
  useEffect(() => {
    opened.current += 1
    setExtra([])
  }, [state])
  const shown = data && data.state === state ? data : null
  const items = shown ? [...shown.items, ...extra] : []
  const loadMore = () => {
    if (!state) return
    const ticket = opened.current
    setMore(true)
    api
      .get<OutcomeList>(`/overview/outcomes/${encodeURIComponent(state)}?limit=${PAGE}&offset=${items.length}`)
      .then((res) => { if (ticket === opened.current) setExtra((x) => [...x, ...res.data.items]) })
      .finally(() => { if (ticket === opened.current) setMore(false) })
  }

  const title = shown ? (
    <span className="ovd-title">
      <b style={{ color }}>{fmt(shown.total)}</b> {shown.label}
      <span className="ovd-title-s">today</span>
    </span>
  ) : (
    'Outcome'
  )

  return (
    <Popup open={state !== null} onClose={onClose} title={title} width={820}>
      {error && <p role="alert">Couldn’t load these alerts.</p>}
      {!shown && !error && <p className="ovd-muted">Loading…</p>}
      {shown && (
        <div className="ovd">
          <p className="ovd-explain">{shown.explain}</p>
          {shown.total === 0 ? (
            <p className="ovd-empty">{EMPTY[shown.state] ?? 'Nothing here today.'}</p>
          ) : (
            <>
              {shown.by_source.length > 1 && (
                <div className="ovd-chips" aria-label="By source">
                  {shown.by_source.map((s) => (
                    <span key={s.data_source} className="ovd-chip">
                      <SourceMark source={s.data_source} />
                      <b>{fmt(s.count)}</b>
                    </span>
                  ))}
                </div>
              )}

              {shown.cases.length > 0 && (
                <Section title={`In ${shown.cases.length} case${shown.cases.length === 1 ? '' : 's'}`}>
                  <ul className="ovd-list">
                    {shown.cases.map((c) => (
                      <li key={c.case_id}>
                        <button type="button" className="ovd-row" onClick={() => openCase(c.case_id)}>
                          <span className="ovd-id">{c.case_id}</span>
                          <span className="ovd-t">{c.title || c.case_id}</span>
                          <span className="ovd-meta">
                            {c.priority && <span className={`ovd-prio ${c.priority.toLowerCase()}`}>{c.priority}</span>}
                            <span>{c.alerts} alert{c.alerts === 1 ? '' : 's'}</span>
                            <span>{c.status}</span>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </Section>
              )}

              <Section title="Alerts" aside={<span className="ovd-muted">Newest first · showing {fmt(items.length)} of {fmt(shown.total)}</span>}>
                <ul className="ovd-list">
                  {items.map((a) => (
                    <li key={a.finding_id}>
                      <button type="button" className="ovd-row alert" onClick={() => onOpenAlert(a.finding_id)}>
                        <time className="ovd-time" title={a.created_at ?? undefined}>{when(a.created_at)}</time>
                        <SevMark severity={a.severity} />
                        <span className="ovd-t" title={a.description ?? undefined}>{a.title || a.description || a.finding_id}</span>
                        <span className="ovd-meta">
                          <SourceMark source={a.data_source} />
                          {a.case_id && <span className="ovd-case">Case {a.case_id}</span>}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {items.length < shown.total && (
                  <button type="button" className="btn ghost ovd-more" onClick={loadMore} disabled={more}>
                    {more ? 'Loading…' : `Show ${Math.min(PAGE, shown.total - items.length)} more`}
                  </button>
                )}
              </Section>
            </>
          )}
          <div className="ovd-foot">
            <span className="ovd-muted">Click an alert for its details, or a case to open it.</span>
            <button type="button" className="btn ghost" onClick={goTriage}>
              Triage queue →
            </button>
          </div>
        </div>
      )}
    </Popup>
  )
}

const RUN_PILL: Record<string, string> = { completed: 'good', failed: 'poor', cancelled: '', running: 'ac', paused: 'fair' }
const RUN_WORD: Record<string, string> = { completed: 'Finished', failed: 'Failed', cancelled: 'Cancelled', running: 'Running', paused: 'Waiting on approval' }

function RunRow({ run, openRun, openCase }: { run: AgentRun; openRun: (id: string) => void; openCase: (id: string) => void }) {
  return (
    <li>
      <div
        role="button"
        tabIndex={0}
        className="ovd-row run"
        onClick={() => openRun(run.run_id)}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault()
            openRun(run.run_id)
          }
        }}
      >
        <span className={`ovd-pill ${RUN_PILL[run.status] ?? ''}`}>{RUN_WORD[run.status] ?? run.status}</span>
        <span className="ovd-t" title={plain(run.summary) || undefined}>{plain(run.summary) || run.outcome || `Run ${run.run_id.slice(0, 8)}`}</span>
        <span className="ovd-meta">
          <time title={run.started_at ?? undefined}>{when(run.started_at)}</time>
          {run.status !== 'running' && run.status !== 'paused' && <span>{duration(run.duration_ms)}</span>}
          {run.cost_usd > 0 && <span>{money(run.cost_usd)}</span>}
          {run.case_id && (
            <button
              type="button"
              className="ovd-case"
              onClick={(e) => {
                e.stopPropagation() // the row opens the run
                openCase(run.case_id!)
              }}
            >
              Case {run.case_id}
            </button>
          )}
        </span>
      </div>
    </li>
  )
}

/** One agent card opened: what the workflow does, what it is doing now, and how its runs went. */
export function AgentPanel({
  agent,
  onClose,
  openCase,
  openWorkflow,
  openRun,
}: {
  agent: OverviewAgent | null
  onClose: () => void
  openCase: (id: string) => void
  openWorkflow: (id: string) => void
  openRun: (id: string) => void
}) {
  const id = agent?.workflow_id ?? null
  const { data, error } = useGet<AgentRecord>(id ? `/overview/agents/${encodeURIComponent(id)}` : null)
  const shown = data && data.workflow.id === id ? data : null
  const w = shown?.workflow
  const name = humanize(w?.name || agent?.name || id || 'Agent')
  const steps = (w?.phases ?? []).filter(Boolean)
  const r = shown?.record

  return (
    <Popup open={agent !== null} onClose={onClose} title={<span className="ovd-title">{name}</span>} width={820}>
      {error && <p role="alert">Couldn’t load this agent.</p>}
      {!shown && !error && <p className="ovd-muted">Loading…</p>}
      {shown && w && r && agent && (
        <div className="ovd">
          <dl className="ovd-facts">
            <div>
              <dt>Running now</dt>
              <dd>{agent.running}{agent.current_step ? ` · ${agent.current_step}` : ''}</dd>
            </div>
            <div>
              <dt>Finishes its runs</dt>
              <dd className="ovd-rate">
                {r.rate === null ? '—' : `${(r.rate * 100).toFixed(1)}%`}
                <LevelBadge level={agent.level} variant="pill" />
              </dd>
            </div>
            <div>
              <dt>Runs, last {r.days} days</dt>
              <dd>
                {fmt(r.runs)}
                {r.failed + r.cancelled > 0 && <span className="ovd-muted"> · {r.failed} failed, {r.cancelled} cancelled</span>}
              </dd>
            </div>
            <div>
              <dt>Typical run</dt>
              <dd>
                {duration(r.median_ms)}
                {r.mean_cost_usd !== null && <span className="ovd-muted"> · {money(r.mean_cost_usd)} avg</span>}
              </dd>
            </div>
          </dl>

          <Section title="What it does" aside={<span className="ovd-tag">{w.source === 'custom' ? 'Custom' : 'Built-in'}</span>}>
            {w.description && <p className="ovd-p">{w.description}</p>}
            {w.use_case && w.use_case !== w.description && <p className="ovd-p ovd-muted">Use it for: {w.use_case}</p>}
            {w.agent?.name && <p className="ovd-p ovd-muted">Led by {w.agent.name}.</p>}
          </Section>

          {steps.length > 0 ? (
            <Section title={`Steps (${steps.length})`}>
              <ol className="ovd-steps">
                {steps.map((p, i) => (
                  <li key={`${p.name}-${i}`}>
                    <b>{p.name}</b>
                    {p.agent_id && <span className="ovd-muted"> · {humanize(p.agent_id)}</span>}
                    {p.approval_required && <span className="ovd-pill fair">Approval</span>}
                    {p.purpose && <p>{p.purpose}</p>}
                    {(p.tools ?? []).length > 0 && (
                      <div className="ovd-tools">
                        {(p.tools ?? []).map((t) => <code key={t}>{t}</code>)}
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </Section>
          ) : (w.objectives ?? []).length > 0 ? (
            <Section title="How it works">
              <ol className="ovd-steps">
                {(w.objectives ?? []).map((o) => <li key={o}><p>{o}</p></li>)}
              </ol>
            </Section>
          ) : null}

          {steps.length === 0 && (w.tools_used ?? []).length > 0 && (
            <Section title="Tools">
              <div className="ovd-tools">
                {(w.tools_used ?? []).map((t) => <code key={t}>{t}</code>)}
              </div>
            </Section>
          )}

          {shown.live.length > 0 && (
            <Section title="Running now">
              <ul className="ovd-list">
                {shown.live.map((run) => <RunRow key={run.run_id} run={run} openRun={openRun} openCase={openCase} />)}
              </ul>
            </Section>
          )}

          <Section title="Recent runs">
            {shown.recent.length === 0 ? (
              <p className="ovd-empty">No finished runs yet. Runs show here as soon as one ends.</p>
            ) : (
              <ul className="ovd-list">
                {shown.recent.map((run) => <RunRow key={run.run_id} run={run} openRun={openRun} openCase={openCase} />)}
              </ul>
            )}
          </Section>

          <div className="ovd-foot">
            <span className="ovd-muted">Click a run to follow it step by step.</span>
            <button type="button" className="btn primary" onClick={() => openWorkflow(w.id)}>
              Open in Agents &amp; workflows →
            </button>
          </div>
        </div>
      )}
    </Popup>
  )
}
