import { useEffect, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { ConsoleScreenProps } from '../../shared/types'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi } from '../../services/api'
import { Empty, Failure } from '../shared/x1'
import RuleExceptions from './RuleExceptions'
import './detections.css'

type RuleHealth =
  | 'fails_to_run'
  | 'data_silent'
  | 'missing_field'
  | 'never_fired'
  | 'fires'
  | 'disabled'
  | 'no_fire_data'
type View = RuleHealth | 'attention' | 'all'
type Siem = 'elastic' | 'splunk' | 'wazuh'

interface RuleRow {
  rule_id: number
  name: string
  language: string
  tool: string
  techniques: string[]
  health: RuleHealth
  why: string
  /** The failing check's reason as stored, when it says more than ``why``. */
  raw: string | null
  fires: number
  checks_passing: number
  checks_run: number
}

interface SiemTally {
  rules: number
  cant_fire: number
  not_checked: number
  can_fire: number
}

interface RulesReport {
  /** Rows, one per stored copy. */
  total: number
  /** Rules, each counted once however many SIEM copies it has. */
  rule_total: number
  mapped: number
  counts: Record<RuleHealth, number>
  matched: number
  offset: number
  rules: RuleRow[]
  by_siem: Partial<Record<Siem, SiemTally>>
  blind_spots: { data_source: string; tool: string; rules: number }[]
  fires_window_days: number
  limits: string
}

const HEALTH: Record<RuleHealth, { label: string; tone: string; note: string; action: string }> = {
  fails_to_run: { label: 'Fails to run', tone: 'poor', note: 'the query errors before it can search', action: 'Fix with Vigil' },
  data_silent: { label: 'Data silent', tone: 'fair', note: 'runs, but the data it needs isn’t arriving', action: 'Check source' },
  missing_field: { label: 'Missing field', tone: 'high', note: 'references a field the index doesn’t have', action: 'Fix with Vigil' },
  never_fired: { label: 'Never fired', tone: 'ac', note: 'searches live data but has never matched', action: 'Replay attack' },
  fires: { label: 'Fires', tone: 'good', note: 'matched events in the window', action: 'View alerts' },
  disabled: { label: 'Disabled', tone: 'idle', note: 'turned off in the SIEM', action: 'Open rule' },
  no_fire_data: { label: 'No fire data', tone: 'none', note: 'the SIEM doesn’t report fire counts for these', action: 'Open rule' },
}
const ATTENTION: RuleHealth[] = ['fails_to_run', 'data_silent', 'missing_field', 'never_fired']
const MORE: View[] = ['disabled', 'no_fire_data', 'all']
const SIEMS: Siem[] = ['elastic', 'splunk', 'wazuh']
const SIEM_NAME: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk', wazuh: 'Wazuh', repo: 'Repo', overlay: 'Vigil' }

const n = (v: number) => v.toLocaleString()
/** The list's sticky header row, in px: the rows sit under it in the same scroll. */
const HEAD_ROW = 38
const plural = (v: number, one: string, many: string) => `${n(v)} ${v === 1 ? one : many}`

function viewCount(report: RulesReport, view: View): number {
  if (view === 'all') return Object.values(report.counts).reduce((a, b) => a + b, 0)
  if (view === 'attention') return ATTENTION.reduce((a, h) => a + report.counts[h], 0)
  return report.counts[view]
}

function prompt(r: RuleRow, days: number): string {
  const rule = `rule "${r.name}" (${SIEM_NAME[r.tool] ?? r.tool})`
  switch (r.health) {
    case 'fails_to_run': return `The ${rule} fails to run: ${r.why}. Find why and draft a fix.`
    case 'missing_field': return `The ${rule} references a field its index doesn't have: ${r.why}. Draft a fix.`
    case 'data_silent': return `The ${rule} is data silent: ${r.why}. Check whether that source is arriving, and why not.`
    case 'never_fired': return `The ${rule} has never fired. Replay an attack it should catch and tell me whether it alerts.`
    case 'fires': return `Show me the alerts the ${rule} raised in the last ${days} days.`
    default: return `Show me the ${rule}: what it detects, its query and its checks.`
  }
}

/** The shell heads the page (TITLES); under it, Import rules and the rule health. */
export default function DetectionsScreen({ goSettings, openChat }: ConsoleScreenProps) {
  return (
    <div className="x1 de">
      <div className="de-top">
        <button type="button" className="btn primary" onClick={() => goSettings('data')}>Import rules</button>
      </div>
      <RulesTab goSettings={goSettings} openChat={openChat} />
    </div>
  )
}

/** Rule health across every SIEM: the KPI strip, health by SIEM, blind spots and the inventory. */
function RulesTab({ goSettings, openChat }: Pick<ConsoleScreenProps, 'goSettings' | 'openChat'>) {
  const [view, setView] = useState<View>('attention')
  const [siem, setSiem] = useState<Siem | 'all'>('all')
  const [typed, setTyped] = useState('')
  const [q, setQ] = useState('')
  const [report, setReport] = useState<RulesReport | null>(null)
  const [rows, setRows] = useState<RuleRow[]>([])
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // One row opens at a time: its raw error, or (a Vigil copy) its exceptions.
  const [open, setOpen] = useState<{ id: number; what: 'raw' | 'exceptions' } | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // Bumped per view, so a page asked for under the last view is dropped.
  const generation = useRef(0)

  useEffect(() => {
    const t = window.setTimeout(() => setQ(typed.trim()), 250)
    return () => window.clearTimeout(t)
  }, [typed])

  const params = { health: view === 'all' ? undefined : view, tool: siem === 'all' ? undefined : siem, q: q || undefined }

  // Each view, SIEM and search asks the server: the listing is capped, so narrowing capped rows here would miss rules.
  useEffect(() => {
    let live = true
    generation.current += 1
    setLoading(true)
    setMore(false)
    detectionRulesApi.rules({ health: view === 'all' ? undefined : view, tool: siem === 'all' ? undefined : siem, q: q || undefined })
      .then((res) => {
        if (!live) return
        const data = res.data as RulesReport
        setReport(data)
        setRows(data.rules)
        setOpen(null)
        listRef.current?.scrollTo({ top: 0 })
        setError(null)
      })
      .catch((e) => { if (live) setError(errorText(e, 'Rule health did not load.')) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [view, siem, q])

  const matched = report?.matched ?? 0
  const hasMore = !loading && rows.length < matched
  // Only the rows on screen are in the DOM; each is measured, so a wrapped name or an open raw error or exception list keeps its height.
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 70,
    overscan: 8,
    scrollMargin: HEAD_ROW,
    getItemKey: (i) => rows[i].rule_id,
  })
  const shown = virtual.getVirtualItems()
  const last = shown.length ? shown[shown.length - 1].index : -1
  // The next page loads as the list's end nears, inside the list's own scroll.
  useEffect(() => {
    if (!hasMore || more || last < rows.length - 40) return
    const asked = generation.current
    setMore(true)
    detectionRulesApi.rules({ ...params, offset: rows.length })
      .then((res) => { if (asked === generation.current) setRows((prev) => [...prev, ...(res.data as RulesReport).rules]) })
      .catch((e) => { if (asked === generation.current) setError(errorText(e, 'More rules did not load.')) })
      .finally(() => { if (asked === generation.current) setMore(false) })
    // params follow view, siem and q, which reset rows and generation
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasMore, more, last, rows.length])

  if (error && !report) return <Failure>{error}</Failure>
  if (!report) return <span className="x1-muted">Reading the inventory…</span>
  if (report.total === 0) {
    return (
      <Empty title="The inventory is empty">
        Add a rule repository in <button type="button" className="x1-link" onClick={() => goSettings('data')}>Settings</button>, or enable the Elastic, Splunk or Wazuh integration. Vigil reads rules from those and tags them from what they declare.
      </Empty>
    )
  }

  const days = report.fires_window_days
  const siems = SIEMS.filter((s) => report.by_siem[s])
  const cantFire = siems.reduce((a, s) => a + (report.by_siem[s]?.cant_fire ?? 0), 0)
  const of = viewCount(report, view)
  const note = q
    ? `${plural(report.matched, 'rule matches', 'rules match')} “${q}”`
    : view === 'attention'
      ? `${n(of)} of ${n(viewCount(report, 'all'))} rules need attention, worst first`
      : view === 'all'
        ? `All ${n(of)} rules, worst first`
        : `${plural(of, 'rule', 'rules')} · ${HEALTH[view].note}`

  return (
    <>
      <div className="de-kpis">
        <div className="de-kpi">
          <span className="de-kpi-label">Rules in inventory</span>
          <b>{n(report.rule_total)}</b>
          <span className="de-kpi-sub">{report.total > report.rule_total ? `Stored as ${n(report.total)} copies` : 'One copy each'}</span>
        </div>
        <div className="de-kpi">
          <span className="de-kpi-label">Declare an ATT&amp;CK technique</span>
          <b>{n(report.mapped)}</b>
          <span className="de-kpi-sub">{Math.round((100 * report.mapped) / Math.max(report.rule_total, 1))}% of the inventory</span>
        </div>
        <div className="de-kpi">
          <span className="de-kpi-label">Rules that can’t fire</span>
          <b className={cantFire ? 'poor' : ''}>{n(cantFire)}</b>
          <span className="de-kpi-sub">{siems.map((s) => `${n(report.by_siem[s]?.cant_fire ?? 0)} ${SIEM_NAME[s]}`).join(' · ')}</span>
        </div>
      </div>

      <div className="de-pair">
        <section className="x1-card de-card de-siems">
          <div className="de-card-head">
            <div>
              <h2>Health by SIEM</h2>
              <p>Whether each enabled rule can actually fire where it is deployed</p>
            </div>
            <div className="de-legend">
              <span><i className="poor" />Can’t fire</span>
              <span><i className="idle" />Not checked</span>
              <span><i className="good" />Can fire</span>
            </div>
          </div>
          {siems.map((s) => {
            const t = report.by_siem[s] as SiemTally
            const checked = t.cant_fire + t.can_fire
            const pct = (v: number) => `${(100 * v) / Math.max(t.rules, 1)}%`
            return (
              <div key={s} className="de-siem">
                <div className="de-siem-head">
                  <b>{SIEM_NAME[s]} <span>· {plural(t.rules, 'rule', 'rules')}</span></b>
                  {checked ? (
                    <span className="x1-muted">
                      {t.cant_fire > 0 && <><b className="poor">{n(t.cant_fire)} can’t fire</b> · </>}
                      {t.not_checked > 0 && <>{n(t.not_checked)} not checked · </>}
                      {n(t.can_fire)} can fire
                    </span>
                  ) : <span className="x1-muted">None checked yet</span>}
                </div>
                {checked ? (
                  <div className="de-bar" aria-hidden="true">
                    {t.cant_fire > 0 && <i className="poor" style={{ width: pct(t.cant_fire) }} />}
                    {t.not_checked > 0 && <i className="idle" style={{ width: pct(t.not_checked) }} />}
                    {t.can_fire > 0 && <i className="good" style={{ flex: 1 }} />}
                  </div>
                ) : (
                  <>
                    <div className="de-bar unchecked" aria-hidden="true" />
                    <div className="de-siem-note">{plural(t.rules, `${SIEM_NAME[s]} rule has`, `${SIEM_NAME[s]} rules have`)} no Runs or Sees result yet, so none is known to fire or not.</div>
                  </>
                )}
              </div>
            )
          })}
        </section>

        <section className="x1-card de-card de-blind">
          <div className="de-card-head">
            <h2>Blind spots <span>· {n(report.blind_spots.length)}</span></h2>
            <button type="button" className="x1-link" onClick={() => goSettings('integrations')}>View sources</button>
          </div>
          <p className="de-card-sub">Data sources that aren’t arriving, and the rules each one starves</p>
          {report.blind_spots.length === 0 && <p className="x1-muted de-card-sub">Every enabled rule’s data is arriving.</p>}
          {siems.map((s) => {
            const spots = report.blind_spots.filter((b) => b.tool === s)
            if (!spots.length) return null
            return (
              <div key={s}>
                <h3 className="de-group">{SIEM_NAME[s]} · {spots.length}</h3>
                <ul className="de-spots">
                  {spots.map((b) => (
                    <li key={b.data_source}>
                      <code title={b.data_source}>{b.data_source}</code>
                      <span>{plural(b.rules, 'rule', 'rules')}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )
          })}
        </section>
      </div>

      <section className="x1-card de-card de-inv">
        <div className="de-inv-head">
          <div>
            <h2>Rule inventory</h2>
            <p>{note}</p>
          </div>
          <div className="de-inv-tools">
            <div className="de-seg" role="group" aria-label="SIEM">
              {(['all', ...siems] as const).map((s) => (
                <button key={s} type="button" aria-pressed={siem === s} className={siem === s ? 'on' : ''} onClick={() => setSiem(s)}>
                  {s === 'all' ? 'All' : SIEM_NAME[s]}
                </button>
              ))}
            </div>
            <label className="de-search">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
              <input type="search" aria-label="Search rules" placeholder="Search name, technique or reason" value={typed} onChange={(e) => setTyped(e.target.value)} />
            </label>
          </div>
        </div>

        <div className="de-chips-row">
          <div className="de-chips" role="group" aria-label="Health">
            <Chip on={view === 'attention'} onClick={() => setView('attention')} label="Needs attention" count={viewCount(report, 'attention')} />
            <span className="de-div" aria-hidden="true" />
            {ATTENTION.map((h) => (
              <Chip key={h} on={view === h} tone={HEALTH[h].tone} onClick={() => setView(h)} label={HEALTH[h].label} count={report.counts[h]} />
            ))}
            <span className="de-div" aria-hidden="true" />
            <Chip on={view === 'fires'} tone="good" onClick={() => setView('fires')} label="Fires" count={report.counts.fires} />
          </div>
          <select aria-label="Other views" className={`de-more${MORE.includes(view) ? ' on' : ''}`} value={MORE.includes(view) ? view : ''} onChange={(e) => setView((e.target.value || 'attention') as View)}>
            <option value="">More views</option>
            <option value="disabled">Disabled · {n(report.counts.disabled)}</option>
            <option value="no_fire_data">No fire data · {n(report.counts.no_fire_data)}</option>
            <option value="all">All rules · {n(viewCount(report, 'all'))}</option>
          </select>
        </div>

        {error && <div className="de-pad"><Failure>{error}</Failure></div>}

        {/* outside the 38px header row, whose height the virtual list counts on */}
        <p className="col-low-note de-pad de-hide-note">The health column is hidden while Ask Vigil is open; the chips above still filter by it.</p>
        <div ref={listRef} className={`de-table list-scroll${loading ? ' loading' : ''}`} data-testid="de-rules">
          <div className="de-tr de-th">
            <span>Rule</span><span className="col-low">Health ↓</span><span>Why</span><span className="de-right">Action</span>
          </div>
          <div className="de-rows" style={{ height: virtual.getTotalSize() }}>
          {shown.map((item) => {
            const r = rows[item.index]
            const h = HEALTH[r.health]
            const isOpen = open?.id === r.rule_id && open.what === 'raw'
            const excOpen = open?.id === r.rule_id && open.what === 'exceptions'
            const fired = r.fires > 0 ? `${plural(r.fires, 'fire', 'fires')} in ${days}d` : r.health === 'no_fire_data' ? 'No fire data' : r.health === 'fires' ? 'Alerted in the SIEM' : `No fires in ${days}d`
            return (
              <div key={item.key} data-index={item.index} ref={virtual.measureElement} className="de-tr de-row" style={{ transform: `translateY(${item.start - HEAD_ROW}px)` }}>
                <div className="de-rule">
                  <div className="de-name" title={r.name}>{r.name}</div>
                  <div className="de-meta">
                    <span className="de-where">{SIEM_NAME[r.tool] ?? r.tool}</span>
                    {r.techniques.length > 0 && <span className="x1-mono">{r.techniques.slice(0, 2).join(', ')}{r.techniques.length > 2 ? ` +${r.techniques.length - 2}` : ''}</span>}
                    <span>{!r.checks_run ? 'Not checked' : r.checks_passing ? plural(r.checks_passing, 'check passes', 'checks pass') : 'No check passes'}</span>
                    <span aria-hidden="true">·</span>
                    <span className={r.fires > 0 ? 'de-fired' : ''}>{fired}</span>
                  </div>
                </div>
                <div className="col-low"><span className={`de-health ${h.tone}`}><i />{h.label}</span></div>
                <div className="de-why">
                  <div className="de-why-text" title={r.why}>{r.why}</div>
                  <div className="de-toggles">
                    {r.raw && (
                      <button type="button" className="de-raw-toggle" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : { id: r.rule_id, what: 'raw' })}>
                        {isOpen ? 'Hide raw error' : 'Raw error'}
                        <svg viewBox="0 0 24 24" aria-hidden="true" className={isOpen ? 'up' : ''}><path d="m6 9 6 6 6-6" /></svg>
                      </button>
                    )}
                    {r.tool === 'overlay' && (
                      <button type="button" className="de-raw-toggle" aria-expanded={excOpen} onClick={() => setOpen(excOpen ? null : { id: r.rule_id, what: 'exceptions' })}>
                        {excOpen ? 'Hide exceptions' : 'Exceptions'}
                        <svg viewBox="0 0 24 24" aria-hidden="true" className={excOpen ? 'up' : ''}><path d="m6 9 6 6 6-6" /></svg>
                      </button>
                    )}
                  </div>
                  {isOpen && r.raw && <pre className="de-raw">{r.raw}</pre>}
                </div>
                <div className="de-right">
                  <button type="button" className="btn ghost de-act" onClick={() => openChat(prompt(r, days))}>{h.action}</button>
                </div>
                {excOpen && <RuleExceptions ruleId={r.rule_id} />}
              </div>
            )
          })}
          </div>
          {rows.length === 0 && !loading && (
            <div className="de-empty">{q ? `No rules match “${q}”.` : 'No rules in this view.'}</div>
          )}
          <div className="de-end" aria-hidden="true">{more ? 'Loading more rules…' : hasMore ? '' : rows.length ? 'End of the list' : ''}</div>
        </div>
        <div className="de-foot">
          <span aria-live="polite">Showing {n(rows.length)} of {n(report.matched)}{more ? ' · loading more…' : ''}</span>
        </div>
      </section>

      <p className="de-note">{report.limits}</p>
    </>
  )
}

function Chip({ on, tone, label, count, onClick }: { on: boolean; tone?: string; label: string; count: number; onClick: () => void }) {
  return (
    <button type="button" className={`de-chip${on ? ' on' : ''}`} aria-pressed={on} onClick={onClick}>
      {tone && <i className={tone} />}
      <span>{label}</span>
      <span className="de-chip-n">{n(count)}</span>
    </button>
  )
}
