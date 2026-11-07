import { useEffect, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useLocation, useNavigate } from 'react-router-dom'
import type { ConsoleScreenProps } from '../../shared/types'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi } from '../../services/api'
import { Empty, Failure } from '../shared/x1'
import RuleExceptions from './RuleExceptions'
import ProofCardExport from './ProofCardExport'
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
type Flag = 'noisy' | 'quiet' | 'accurate' | 'silent' | 'drift'
type FlagView = Flag | 'caught'
type Sort = 'worst' | 'most_fires' | 'fewest_fires' | 'name'
const SORTS: { key: Sort; label: string }[] = [
  { key: 'worst', label: 'Worst first' },
  { key: 'most_fires', label: 'Most fires' },
  { key: 'fewest_fires', label: 'Fewest fires' },
  { key: 'name', label: 'Name A–Z' },
]
type Siem = 'elastic' | 'splunk' | 'wazuh'

interface RuleRow {
  rule_id: number
  name: string
  language: string
  tool: string
  techniques: string[]
  /** The data source its Sees check reads: an index pattern, decoder or index/sourcetype. */
  where: string | null
  health: RuleHealth
  /** Failing checks past Runs and Sees, worst first. */
  flags: Flag[]
  /** Its Caught check passed: it alerted on a real or replayed attack. */
  caught: boolean
  /** Its worst problem across every check: a health state, a flag, or caught. */
  worst: RuleHealth | Flag | 'caught'
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
  /** Rules with each flag failing, and with Caught passing. */
  flag_counts: Record<FlagView, number>
  matched: number
  /** With a search: how many it matches in each view outside this one. */
  matched_elsewhere: Partial<Record<RuleHealth, number>>
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
/** The checks past Runs and Sees: each a chip, a filter, and the row's label when it's the worst. */
const FLAG: Record<FlagView, { label: string; tone: string; note: string; action: string }> = {
  noisy: { label: 'Went noisy', tone: 'high', note: 'far more alerts today than its usual', action: 'Tune with Vigil' },
  quiet: { label: 'Too loud', tone: 'high', note: 'over its alerts-per-day limit', action: 'Tune with Vigil' },
  accurate: { label: 'False positives', tone: 'fair', note: 'most of its judged alerts were not real', action: 'Tune with Vigil' },
  silent: { label: 'Went silent', tone: 'ac', note: 'fired regularly, then stopped', action: 'Check source' },
  drift: { label: 'Drifted', tone: 'ac', note: 'changed in the SIEM, not in its repo', action: 'Compare with repo' },
  caught: { label: 'Caught an attack', tone: 'good', note: 'alerted on a real or replayed attack', action: 'View proof' },
}
const FLAGS: FlagView[] = ['noisy', 'quiet', 'accurate', 'silent', 'drift', 'caught']
const isFlag = (k: string): k is FlagView => k in FLAG
const labelOf = (k: RuleRow['worst']) => (isFlag(k) ? FLAG[k] : HEALTH[k])
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
  switch (r.worst) {
    case 'noisy': return `The ${rule} went noisy: far more alerts than its usual. Find what changed and suggest tuning.`
    case 'quiet': return `The ${rule} is over its alerts-per-day limit. Show what it alerts on and suggest tuning.`
    case 'accurate': return `Most of the ${rule}'s judged alerts were false positives. Find the pattern and suggest an exception or a fix.`
    case 'silent': return `The ${rule} used to fire regularly and has gone silent. Check whether its data still arrives, and why it stopped.`
    case 'drift': return `The ${rule} was changed in the SIEM but not in its repo. Show the difference and which copy should win.`
    case 'caught': return `Show me the proof that the ${rule} caught an attack: the case, the alert and its checks.`
  }
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
  const [sort, setSort] = useState<Sort>('worst')
  // A flag chip filters on top of All rules; a health chip clears it.
  const [flag, setFlag] = useState<FlagView | null>(null)
  const pickView = (v: View) => { setFlag(null); setView(v) }
  const pickFlag = (f: FlagView) => { setView('all'); setFlag(flag === f ? null : f) }
  const [report, setReport] = useState<RulesReport | null>(null)
  const [rows, setRows] = useState<RuleRow[]>([])
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // One row opens at a time: its raw error, or (a Vigil copy) its exceptions.
  const [open, setOpen] = useState<{ id: number; what: 'raw' | 'exceptions' } | null>(null)
  const [exporting, setExporting] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  // Bumped per view, so a page asked for under the last view is dropped.
  const generation = useRef(0)
  // ?rules=1,2,3: the rules a rule-health alert named, listed in every health state.
  const location = useLocation()
  const navigate = useNavigate()
  const picked = new URLSearchParams(location.search).get('rules') ?? ''
  const pickedCount = picked ? new Set(picked.split(',')).size : 0
  useEffect(() => { if (picked) setView('all') }, [picked])

  useEffect(() => {
    const t = window.setTimeout(() => setQ(typed.trim()), 250)
    return () => window.clearTimeout(t)
  }, [typed])

  const params = { health: view === 'all' ? undefined : view, tool: siem === 'all' ? undefined : siem, q: q || undefined, sort: sort === 'worst' ? undefined : sort, flag: flag ?? undefined, rules: picked || undefined }

  // Each view, SIEM and search asks the server: the listing is capped, so narrowing capped rows here would miss rules.
  useEffect(() => {
    let live = true
    generation.current += 1
    setLoading(true)
    setMore(false)
    detectionRulesApi.rules(params)
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
    // params follows view, siem, q, sort, flag and picked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, siem, q, sort, flag, picked])

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
  const viewName = flag ? FLAG[flag].label : view === 'attention' ? 'Needs attention' : view === 'all' ? 'All rules' : HEALTH[view].label
  // A search that misses here may hit in a view the chips leave out (a healthy rule
  // under Needs attention): say how many and where, one click from all of them.
  const elsewhere = Object.entries(report.matched_elsewhere ?? {}) as [RuleHealth, number][]
  const elsewhereN = elsewhere.reduce((a, [, c]) => a + c, 0)
  const elsewhereText = elsewhere.map(([h, c]) => `${n(c)} in ${HEALTH[h].label}`).join(', ')
  const showAll = () => { setView('all'); setOpen(null) }
  // The view's rule copies for a proof card: the pages listed so far, then the rest.
  const viewIds = async () => {
    const ids = rows.map((r) => r.rule_id)
    while (ids.length < matched) {
      const page = (await detectionRulesApi.rules({ ...params, offset: ids.length })).data as RulesReport
      if (!page.rules.length) break
      ids.push(...page.rules.map((r) => r.rule_id))
    }
    return ids
  }
  const exportName = [viewName, siem === 'all' ? null : SIEM_NAME[siem], q ? `“${q}”` : null].filter(Boolean).join(' · ')
  const order = sort === 'worst' ? 'worst first' : sort === 'name' ? 'by name' : sort === 'most_fires' ? 'most fires first' : 'fewest fires first'
  const note = flag && !q
    ? `${plural(report.matched, 'rule', 'rules')} · ${FLAG[flag].note}, ${order}`
    : q
    ? `${plural(report.matched, 'rule matches', 'rules match')} “${q}” in ${viewName}${elsewhereN ? ` · ${n(elsewhereN)} more in other views` : ''}`
    : picked
      ? `${plural(report.matched, 'rule', 'rules')} in ${viewName}, ${order}`
    : view === 'attention'
      ? `${n(of)} of ${n(viewCount(report, 'all'))} rules need attention, ${order}`
      : view === 'all'
        ? `All ${n(of)} rules, ${order}`
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
              <input type="search" aria-label="Search rules" placeholder="Search name, index, technique or reason" value={typed} onChange={(e) => setTyped(e.target.value)} />
            </label>
            <select aria-label="Sort rules" className="de-sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              {SORTS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
            </select>
            <button type="button" className="btn ghost de-export" onClick={() => setExporting(true)}>Export proof card</button>
          </div>
        </div>

        {picked && (
          <div className="de-picked de-pad" role="status">
            Showing the {plural(pickedCount, 'rule', 'rules')} a rule-health alert named
            {!q && siem === 'all' && view === 'all' && report.matched < pickedCount
              ? ` · ${n(pickedCount - report.matched)} no longer listed (removed or not a SIEM rule)`
              : ''}
            .{' '}
            <button type="button" className="de-link" onClick={() => navigate('/detections')}>Show all rules</button>
          </div>
        )}

        <div className="de-chips-row">
          <div className="de-chips" role="group" aria-label="Health">
            <Chip on={!flag && view === 'attention'} onClick={() => pickView('attention')} label="Needs attention" count={viewCount(report, 'attention')} />
            <span className="de-div" aria-hidden="true" />
            {ATTENTION.map((h) => (
              <Chip key={h} on={!flag && view === h} tone={HEALTH[h].tone} onClick={() => pickView(h)} label={HEALTH[h].label} count={report.counts[h]} />
            ))}
            <span className="de-div" aria-hidden="true" />
            <Chip on={!flag && view === 'fires'} tone="good" onClick={() => pickView('fires')} label="Fires" count={report.counts.fires} />
          </div>
          <div className="de-chips" role="group" aria-label="Other checks">
            {FLAGS.map((f) => (
              <Chip key={f} on={flag === f} tone={FLAG[f].tone} onClick={() => pickFlag(f)} label={FLAG[f].label} count={report.flag_counts?.[f] ?? 0} />
            ))}
          </div>
          <select aria-label="Other views" className={`de-more${!flag && MORE.includes(view) ? ' on' : ''}`} value={!flag && MORE.includes(view) ? view : ''} onChange={(e) => pickView((e.target.value || 'attention') as View)}>
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
            <span>Rule</span><span>Where</span><span>Technique</span><span className="col-low">Health{sort === 'worst' ? ' ↓' : ''}</span><span className="de-right">Fires · {days}d{sort === 'most_fires' ? ' ↓' : sort === 'fewest_fires' ? ' ↑' : ''}</span><span className="de-right">Action</span>
          </div>
          <div className="de-rows" style={{ height: virtual.getTotalSize() }}>
          {shown.map((item) => {
            const r = rows[item.index]
            const h = labelOf(r.worst ?? r.health)
            const others = [...(r.flags ?? []).filter((f) => f !== r.worst), ...(r.caught && r.worst !== 'caught' ? ['caught' as const] : [])]
            const isOpen = open?.id === r.rule_id && open.what === 'raw'
            const excOpen = open?.id === r.rule_id && open.what === 'exceptions'
            const fired = r.fires > 0 ? n(r.fires) : r.health === 'no_fire_data' ? 'No data' : 'None'
            return (
              <div key={item.key} data-index={item.index} ref={virtual.measureElement} className="de-tr de-row" style={{ transform: `translateY(${item.start - HEAD_ROW}px)` }}>
                <div className="de-rule">
                  <div className="de-name" title={r.name}>{r.name}</div>
                  <div className="de-why-text" title={r.why}>{r.why}</div>
                  {others.length > 0 && (
                    <div className="de-flags">
                      {others.map((f) => <span key={f} className={`de-flag ${FLAG[f].tone}`} title={FLAG[f].note}>{FLAG[f].label}</span>)}
                    </div>
                  )}
                  <div className="de-meta">
                    <span>{!r.checks_run ? 'Not checked' : r.checks_passing ? plural(r.checks_passing, 'check passes', 'checks pass') : 'No check passes'}</span>
                  </div>
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
                <div className="de-where-cell">
                  <span className="de-where">{SIEM_NAME[r.tool] ?? r.tool}</span>
                  {r.where && <span className="de-source" title={r.where}>{r.where}</span>}
                </div>
                <div className="de-tech" title={r.techniques.length ? r.techniques.join(', ') : 'No technique declared'}>
                  {r.techniques.length ? <>{r.techniques[0]}{r.techniques.length > 1 && <span className="de-tech-more"> +{r.techniques.length - 1}</span>}</> : <span className="de-none">—</span>}
                </div>
                <div className="col-low"><span className={`de-health ${h.tone}`}><i />{h.label}</span></div>
                <div className={`de-right de-fires${r.fires > 0 ? ' de-fired' : ''}`}>{fired}</div>
                <div className="de-right">
                  <button type="button" className="btn ghost de-act" onClick={() => openChat(prompt(r, days))}>{h.action}</button>
                </div>
                {excOpen && <RuleExceptions ruleId={r.rule_id} />}
              </div>
            )
          })}
          </div>
          {rows.length === 0 && !loading && (
            <div className="de-empty">
              {!q ? 'No rules in this view.' : elsewhereN ? (
                <>
                  No rules in {viewName} match “{q}”. {plural(elsewhereN, 'rule matches', 'rules match')} in other views ({elsewhereText}).{' '}
                  <button type="button" className="de-link" onClick={showAll}>Show all rules</button>
                </>
              ) : `No rules match “${q}”.`}
            </div>
          )}
          <div className="de-end" aria-hidden="true">{more ? 'Loading more rules…' : hasMore ? '' : rows.length ? 'End of the list' : ''}</div>
        </div>
        <div className="de-foot">
          <span aria-live="polite">Showing {n(rows.length)} of {n(report.matched)}{more ? ' · loading more…' : ''}</span>
          {elsewhereN > 0 && rows.length > 0 && (
            <span>
              {plural(elsewhereN, 'more rule matches', 'more rules match')} in other views ({elsewhereText}).{' '}
              <button type="button" className="de-link" onClick={showAll}>Show all rules</button>
            </span>
          )}
        </div>
      </section>

      <p className="de-note">{report.limits}</p>
      <ProofCardExport open={exporting} onClose={() => setExporting(false)} viewName={exportName} viewCount={matched} viewIds={viewIds} />
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
