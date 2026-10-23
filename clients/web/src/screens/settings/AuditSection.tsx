import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AUDIT_PAGE, auditApi, type AuditEntry, type AuditFilter, type AuditTip, type AuditVerify } from '../../services/api'
import { FilterChip } from '../../shared/FilterChip'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { PageHead } from '../../shared/PageHead'
import { EmptyState, FilterButton } from '../../shared/ui'
import { utcDayClock } from '../../shared/utc'
import { inputCls } from '../cases/CaseSections'
import type { SectionProps } from './types'
import './audit.css'

export const AUDIT_DESC =
  'Every recorded action on one hash-chained trail: who did it, for whom, and what came of it. Filter it, open an entry, verify a range, or export it.'

const OUTCOMES = ['done', 'denied', 'outcome unknown', 'recorded late'] as const
const ATTRIBUTIONS = [
  { value: 'verified', label: 'Verified' },
  { value: 'matched by name', label: 'Matched by name' },
  { value: 'unverified legacy', label: 'Unverified legacy' },
]
const KINDS = [
  { value: 'person', label: 'People' },
  { value: 'service_account', label: 'Service accounts' },
  { value: 'agent', label: 'Agents' },
  { value: 'system', label: 'System' },
]
const TEXT_FIELDS = [
  { key: 'actor', label: 'Actor', hint: 'Principal id' },
  { key: 'person', label: 'Person', hint: 'Did it, or it was done for them' },
  { key: 'area', label: 'Area', hint: 'e.g. config, agent, case' },
  { key: 'object', label: 'Object', hint: 'e.g. a case id' },
] as const

/** What the controls hold; since and until are datetime-local values in the viewer's zone. */
export interface AuditDraft {
  since: string
  until: string
  actor: string
  person: string
  kind: string
  area: string
  object: string
  outcome: string
  attribution: string
}

export const EMPTY_DRAFT: AuditDraft = {
  since: '', until: '', actor: '', person: '', kind: '', area: '', object: '', outcome: '', attribution: '',
}

function isoOf(local: string): string {
  const t = local ? new Date(local) : null
  return t && !Number.isNaN(t.getTime()) ? t.toISOString() : ''
}

/** The controls as the trail filter: times go out as UTC ISO. */
export function auditFilter(draft: AuditDraft): AuditFilter {
  return { ...draft, since: isoOf(draft.since), until: isoOf(draft.until) }
}

const cap = (s: string) => s[0].toUpperCase() + s.slice(1)

function str(value: unknown): string {
  if (value == null || value === '') return ''
  if (typeof value === 'string') return value
  if (typeof value === 'object' && 'id' in value && typeof (value as { id: unknown }).id === 'string') return (value as { id: string }).id
  return JSON.stringify(value)
}

const attributionWord = (value: unknown) => str(value).replace(/_/g, ' ')

// What the entry acted on: its object, or the target an agent tool call names.
const subjectOf = (e: AuditEntry) => str(e.payload.object) || str(e.payload.target)

function actorOf(e: AuditEntry): string {
  return e.actor_text || str(e.payload.actor) || '—'
}

export default function AuditSection({ notify }: SectionProps) {
  const [draft, setDraft] = useState<AuditDraft>(EMPTY_DRAFT)
  const filter = useMemo(() => auditFilter(draft), [draft])
  const [query, setQuery] = useState(filter) // typed fields settle before a read
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [tip, setTip] = useState<AuditTip | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState('')
  const [more, setMore] = useState(false)
  const [moreError, setMoreError] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const [reload, setReload] = useState(0)
  const [exporting, setExporting] = useState(false)
  const ticket = useRef(0)

  useEffect(() => {
    const t = setTimeout(() => setQuery(filter), 300)
    return () => clearTimeout(t)
  }, [filter])

  const read = useCallback(async (after?: number) => {
    const mine = ++ticket.current
    if (after == null) setPhase('loading')
    setMoreError('')
    try {
      const res = await auditApi.entries(query, after)
      if (mine !== ticket.current) return
      const page = res.data.entries
      setEntries((rows) => (after == null ? page : [...rows, ...page]))
      setTip(res.data.tip)
      setMore(page.length >= AUDIT_PAGE)
      setPhase('ready')
    } catch (e) {
      if (mine !== ticket.current) return
      // a failed next page keeps the rows already shown
      if (after != null) return setMoreError(detailOf(e, 'The next page could not be read.'))
      setError(detailOf(e, 'The trail could not be read.'))
      setPhase('error')
    }
  }, [query])

  useEffect(() => {
    setOpen(null)
    read()
  }, [read, reload])

  const set = (patch: Partial<AuditDraft>) => setDraft((d) => ({ ...d, ...patch }))
  const typed = TEXT_FIELDS.filter((f) => draft[f.key].trim()).length + (draft.since ? 1 : 0) + (draft.until ? 1 : 0)
  const anyFilter = Object.values(draft).some((v) => v.trim())

  const exportTrail = async () => {
    setExporting(true)
    setQuery(filter) // what the controls say now, not the last settled read
    try {
      await auditApi.download(filter)
    } catch (e) {
      notify('err', detailOf(e, 'Export failed'))
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      <PageHead
        title="Audit"
        description={AUDIT_DESC}
        actions={
          <>
            <FilterButton
              activeCount={typed}
              onClearAll={() => set({ since: '', until: '', actor: '', person: '', area: '', object: '' })}
            >
              {TEXT_FIELDS.map((f) => (
                <label key={f.key} className="filter-grp">
                  <span className="filter-grp-label">{f.label}</span>
                  <input aria-label={`${f.key} filter`} placeholder={f.hint} className={inputCls} value={draft[f.key]} onChange={(e) => set({ [f.key]: e.target.value })} />
                </label>
              ))}
              <label className="filter-grp">
                <span className="filter-grp-label">Since</span>
                <input type="datetime-local" aria-label="Since" className={inputCls} value={draft.since} onChange={(e) => set({ since: e.target.value })} />
              </label>
              <label className="filter-grp">
                <span className="filter-grp-label">Until</span>
                <input type="datetime-local" aria-label="Until" className={inputCls} value={draft.until} onChange={(e) => set({ until: e.target.value })} />
              </label>
            </FilterButton>
            <button className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={() => setReload((k) => k + 1)}><Icon name="refresh" /></button>
            <button className="btn ghost" onClick={exportTrail} disabled={exporting}><Icon name="download" /> Export</button>
          </>
        }
      />

      <div className="audit-top">
        <VerifyCard tip={tip} />
        <FeedHealth />
      </div>

      <div className="audit-chips">
        <div className="chip-group" role="group" aria-label="Outcome">
          <FilterChip list label="Any outcome" active={!draft.outcome} onClick={() => set({ outcome: '' })} />
          {OUTCOMES.map((o) => (
            <FilterChip key={o} list label={cap(o)} active={draft.outcome === o} onClick={() => set({ outcome: draft.outcome === o ? '' : o })} />
          ))}
        </div>
        <div className="chip-group" role="group" aria-label="Attribution">
          <FilterChip list label="Any attribution" active={!draft.attribution} onClick={() => set({ attribution: '' })} />
          {ATTRIBUTIONS.map((a) => (
            <FilterChip key={a.value} list label={a.label} active={draft.attribution === a.value} onClick={() => set({ attribution: draft.attribution === a.value ? '' : a.value })} />
          ))}
        </div>
        <div className="chip-group" role="group" aria-label="Actor kind">
          <FilterChip list label="Everyone" active={!draft.kind} onClick={() => set({ kind: '' })} />
          {KINDS.map((k) => (
            <FilterChip key={k.value} list label={k.label} active={draft.kind === k.value} onClick={() => set({ kind: draft.kind === k.value ? '' : k.value })} />
          ))}
        </div>
      </div>

      <div className="audit-card">
        <div className="audit-grid" role="table" aria-label="Audit trail">
          <div className="ag-row ag-head" role="row">
            <span role="columnheader">Seq</span>
            <span role="columnheader">When (UTC)</span>
            <span role="columnheader">Actor</span>
            <span role="columnheader">What it was</span>
            <span role="columnheader">Outcome</span>
            <span role="columnheader">Marks</span>
          </div>
          <div role="rowgroup">
            {phase === 'loading' && <div className="ag-state"><EmptyState loading table compact icon="clock" title="Reading the trail…" /></div>}
            {phase === 'error' && (
              <div className="ag-state">
                <EmptyState error table icon="alert" title="Couldn’t read the trail" body={error} primary={{ label: 'Retry', onClick: () => setReload((k) => k + 1), icon: 'refresh' }} />
              </div>
            )}
            {phase === 'ready' && entries.length === 0 && (
              <div className="ag-state">
                <EmptyState
                  table
                  icon={anyFilter ? 'filter' : 'shield'}
                  title={anyFilter ? 'No entries match these filters' : 'The trail has no entries yet'}
                  primary={anyFilter ? { label: 'Clear filters', onClick: () => setDraft(EMPTY_DRAFT), icon: 'close' } : undefined}
                />
              </div>
            )}
            {phase === 'ready' && entries.map((e) => (
              <Fragment key={e.seq}>
                <div
                  role="row"
                  tabIndex={0}
                  aria-expanded={open === e.seq}
                  className={`ag-row ag-entry${open === e.seq ? ' open' : ''}`}
                  onClick={() => setOpen(open === e.seq ? null : e.seq)}
                  onKeyDown={(k) => { if (k.key === 'Enter' || k.key === ' ') { k.preventDefault(); setOpen(open === e.seq ? null : e.seq) } }}
                >
                  <span role="cell" className="ag-seq">{e.seq}</span>
                  <span role="cell" className="ag-when">{utcDayClock(e.occurred_at) ?? '—'}</span>
                  <span role="cell" className="ag-clip" title={str(e.payload.actor)}>{actorOf(e)}</span>
                  <span role="cell" className="ag-what">
                    <span className="ag-action" title={str(e.payload.action)}>{str(e.payload.action) || '—'}</span>
                    {subjectOf(e) && <span className="ag-sub" title={subjectOf(e)}>{subjectOf(e)}</span>}
                  </span>
                  <span role="cell" className="ag-clip">{str(e.payload.outcome) || '—'}</span>
                  <span role="cell" className="ag-marks"><Marks entry={e} /></span>
                </div>
                {open === e.seq && <EntryDetail entry={e} />}
              </Fragment>
            ))}
          </div>
        </div>
      </div>
      {phase === 'ready' && entries.length > 0 && (
        <div className="audit-pager">
          <span>{entries.length} shown, oldest first{tip ? ` · trail tip ${tip.seq}` : ''}</span>
          {moreError && <span className="audit-status">{moreError}</span>}
          {more && <button className="btn ghost" onClick={() => read(entries[entries.length - 1].seq)}>Show more</button>}
        </div>
      )}
    </>
  )
}

function Marks({ entry }: { entry: AuditEntry }) {
  const marks: string[] = []
  if (entry.late) marks.push('Recorded late')
  if (entry.payload.attribution != null) marks.push('Copied')
  if (entry.payload.awaiting === true) marks.push('Intent')
  return <>{marks.map((m) => <span key={m} className="ag-mark">{m}</span>)}</>
}

/** Entry detail, laid out like the case Record tab's card. */
function EntryDetail({ entry }: { entry: AuditEntry }) {
  const p = entry.payload
  const [unknown, setUnknown] = useState<boolean | null>(null)

  // An intent is "outcome unknown" while no result entry names it.
  useEffect(() => {
    if (p.awaiting !== true) return
    let live = true
    auditApi
      .entries({ outcome: 'outcome unknown' }, entry.seq - 1, 1)
      .then((res) => { if (live) setUnknown(res.data.entries[0]?.seq === entry.seq) })
      .catch(() => undefined)
    return () => { live = false }
  }, [entry.seq, p.awaiting])

  const fields: [string, string][] = [
    ['Seq', String(entry.seq)],
    ['Occurred (UTC)', utcDayClock(entry.occurred_at) ?? entry.occurred_at],
    ['Actor', entry.actor_text ? `${entry.actor_text} · ${str(p.actor)}` : str(p.actor)],
    ['On behalf of', [entry.operator_text, str(p.on_behalf_of)].filter(Boolean).join(' · ')],
    ['Action', str(p.action)],
    ['Object', str(p.object)],
    ...(p.target != null ? [['Target', str(p.target)] as [string, string]] : []),
    ['Outcome', str(p.outcome) || (unknown ? 'outcome unknown' : '')],
    ['Hash', entry.entry_hash],
  ]

  return (
    <div className="ag-detail" role="row">
      <div role="cell" className="ag-detail-card">
        {(entry.late || unknown || p.attribution != null) && (
          <div className="ag-notes">
            {entry.late && <p><b>Recorded late.</b> The trail could not take this entry when it happened; it was written afterwards with its original time.</p>}
            {unknown && <p><b>Outcome unknown.</b> This intent was recorded, and no result entry names it.</p>}
            {p.attribution != null && (
              <p>
                <b>Copied history · {attributionWord(p.attribution)}.</b> This entry was copied from an older record. It is
                tamper-evident only from when it was copied, not before.
              </p>
            )}
          </div>
        )}
        <dl className="ag-fields">
          {fields.map(([k, v]) => (
            <Fragment key={k}>
              <dt>{k}</dt>
              <dd className={k === 'Hash' || k === 'Seq' ? 'mono' : undefined}>{v || '—'}</dd>
            </Fragment>
          ))}
        </dl>
        <details className="ag-payload">
          <summary>Full entry</summary>
          <pre>{JSON.stringify(p, null, 2)}</pre>
        </details>
      </div>
    </div>
  )
}

/** Range verify; the result reads like Verify chain on the case Record tab. */
function VerifyCard({ tip }: { tip: AuditTip | null }) {
  const [first, setFirst] = useState('')
  const [last, setLast] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<{ level: 'good' | 'poor' | null; text: string } | null>(null)

  const run = async () => {
    const bound = (v: string) => (v ? Number(v) : undefined)
    const [lo, hi] = [bound(first), bound(last)]
    if ([lo, hi].some((n) => n !== undefined && !(Number.isInteger(n) && n >= 0))) {
      setStatus({ level: 'poor', text: 'Seq bounds are whole numbers, 0 or more' })
      return
    }
    setBusy(true)
    setStatus({ level: null, text: 'Verifying…' })
    try {
      const res = await auditApi.verify(lo, hi)
      setStatus(verifyLine(res.data))
    } catch (e) {
      setStatus({ level: 'poor', text: detailOf(e, 'Verify failed') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="audit-box" aria-label="Verify a range">
      <div className="audit-box-head">
        <h3>Verify a range</h3>
        <InfoTip label="About verify" text="Recomputes each entry’s hash from the one before it. Leave a bound empty to start at the first entry or stop at the tip." />
      </div>
      <div className="audit-verify">
        <label>From seq <input type="number" min={0} step={1} aria-label="First seq" className={inputCls} value={first} placeholder="first" onChange={(e) => setFirst(e.target.value)} /></label>
        <label>to <input type="number" min={0} step={1} aria-label="Last seq" className={inputCls} value={last} placeholder={tip ? String(tip.seq) : 'tip'} onChange={(e) => setLast(e.target.value)} /></label>
        <button type="button" className="audit-btn" onClick={run} disabled={busy}>Verify</button>
        <span className="audit-status" role="status" aria-live="polite">
          {status && (
            <>
              <LevelBadge level={status.level} variant="pill" />
              {status.text}
            </>
          )}
        </span>
      </div>
    </section>
  )
}

export function verifyLine(v: AuditVerify): { level: 'good' | 'poor'; text: string } {
  const span = `${v.first ?? '—'}–${v.last ?? '—'}`
  return v.ok
    ? { level: 'good', text: `Chain verified · seq ${span}` }
    : { level: 'poor', text: `Chain break at seq ${v.first_bad}${v.reason ? `: ${v.reason}` : ''}` }
}

/** Feed health: the feed (#2767) fills this once it ships. Until then nothing carries the trail out. */
function FeedHealth() {
  return (
    <section className="audit-box" aria-label="Feed health">
      <div className="audit-box-head">
        <h3>Feed health</h3>
        <span className="audit-off">Not on</span>
      </div>
      <p className="audit-note">No feed is configured. The trail has no outside proof unless it is exported or polled.</p>
    </section>
  )
}

function detailOf(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}
