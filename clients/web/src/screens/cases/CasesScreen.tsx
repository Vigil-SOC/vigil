import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { caseSearchApi, casesApi, workflowApi } from '../../services/api'
import { mapQueueCase } from '../../data/mappers'
import { CASE_EDIT_FIELDS, type CaseEditField, type CaseEditValues, type CaseRecord, type CaseRow } from '../../data/data'
import type { Schema } from '../../services/apiTypes'
import type { ChatSeed, ConsoleScreenProps } from '../../shared/types'
import {
  useCases,
  useCaseDetail,
  notifyCasesChanged,
  CASES_CHANGED,
  CASE_PAGE_LIMIT,
  INITIAL_CASE_FILTERS,
  isCaseStatus,
  type CaseFilters,
  type CaseStatus,
  type Phase,
} from './useCases'
import { ConfirmDialog, EmptyState, FilterButton, FilterGroup, Popup, Select, activateOnKey } from '../../shared/ui'
import { FilterChip } from '../../shared/FilterChip'
import { PageHead } from '../../shared/PageHead'
import { StatCard, StatCards } from '../../shared/StatCard'
import { StatePill, statePill, type PillTone } from '../../shared/StatePill'
import { SeverityMark } from '../../shared/SeverityMark'
import { LevelBadge, slaLevel } from '../../shared/LevelBadge'
import { InfoTip } from '../../shared/InfoTip'
import { useAuth } from '../../contexts/AuthContext'
import { useToast } from '../../shell/toast'
import { inputCls } from './CaseSections'
import { CasePage } from './CasePage'
import { caseConflicts, errorDetail, STALE_OWNER_NOTE } from './caseFormat'
import { ClosureFields, closurePayload, closureProblem, type ClosureCategory } from './ClosureFields'
import { LATER_RELEASE } from '../../shared/later'
import { holdCase } from '../../shell/heldCase'

const cap = (s: string) => s[0].toUpperCase() + s.slice(1)

const CASE_PRIO_OPTIONS: { value: CaseRow['prio']; label: string }[] = [
  { value: 'critical', label: 'Critical' },
  { value: 'high', label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low', label: 'Low' },
  { value: 'unknown', label: 'Unknown' },
]
const RATED_PRIO_OPTIONS = CASE_PRIO_OPTIONS.filter((o) => o.value !== 'unknown')

const STATE_OPTIONS = [
  { value: '', label: 'Open queue' },
  { value: 'new', label: 'New' },
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'assigned', label: 'Assigned' },
  { value: 'executing', label: 'Executing' },
  { value: 'waiting_approval', label: 'Waiting approval' },
  { value: 'closed', label: 'Closed' },
]

/** "4 alerts · step 9 · active 3h ago · age 2d": one line, the description is on hover. */
function metaLine(c: CaseRow): string {
  return [
    `${c.findings} alert${c.findings === 1 ? '' : 's'}`,
    c.iterations != null && `step ${c.iterations}`,
    c.lastActive !== '—' && `active ${c.lastActive} ago`,
    c.age !== '—' && `age ${c.age}`,
  ].filter(Boolean).join(' · ')
}

const TIME_LEFT_TIP = {
  source: 'The SLA policy attached to the case.',
  calculation: 'Resolution due minus now. Good while under 75% of the response or resolution window has passed, Fair from 75%, Poor from 90% or once past due.',
  limit: 'Paused while the case is on hold. Set per priority in Settings → SLA.',
}
// what the status cards count (strip in core/storage/case_repository.py, read by get_cases in cases_router.py)
const NEEDS_YOU_TIP = {
  source: 'Pending approvals and stopped hunts that wait on a person, matched to their cases.',
  calculation: 'Open cases holding at least one of them.',
  limit: 'Counts every open case; only the alerts-from filter narrows it.',
}
const WORKING_TIP = {
  source: 'Open cases, by their state.',
  calculation: 'Every open case minus the ones that need you.',
  limit: 'Counts every open case; only the alerts-from filter narrows it.',
}
const NEAR_SLA_TIP = {
  source: 'The latest SLA attached to each open case.',
  calculation: 'Open cases past a due time, or with 75% or more of the response or resolution window used.',
  limit: 'A paused SLA, or a case with no SLA, is not counted. Only the alerts-from filter narrows it.',
}
const CLOSED_TODAY_TIP = {
  source: 'Case closure records.',
  calculation: 'Closures since midnight UTC; "by an agent alone" is the share closed by an agent rather than from the console.',
  limit: 'Today only, so it has no filter: State › Closed lists every closed case.',
}
const STATE_WORDS = STATE_OPTIONS.filter((o) => o.value && o.value !== 'closed')
const TONE_DOT: Record<PillTone, string> = {
  needs: 'var(--poor)',
  live: 'var(--ac)',
  idle: 'var(--tx2)',
  closed: 'var(--tx2)',
}

// State and Assigned option values that are not a case state or an assignee
const NEEDS_YOU = ':needs-you'
const WORKING = ':working'
const MINE = ':mine'
const NEAR_SLA = ':near-sla'
const OTHER = ':other'
const UNASSIGNED = ':unassigned'
const OWNERS_FAILED = ':owners-failed'
// an owner's option value, so a username can't collide with the values above
const OWNER = 'owner:'

type CaseOwner = Schema<'CaseOwner'>
type OwnerPick = { assignee: string; unassigned: boolean }

/** The owners stored on cases this reader can list, reloaded when a case changes; `failed` if the read failed. */
function useCaseOwners(): { owners: CaseOwner[]; failed: boolean } {
  const [state, setState] = useState<{ owners: CaseOwner[]; failed: boolean }>({ owners: [], failed: false })
  useEffect(() => {
    let cancelled = false
    let latest = 0
    const load = () => {
      const call = ++latest
      // only the latest read lands, so a slow earlier one can't overwrite it
      const keep = () => !cancelled && call === latest
      casesApi
        .getOwners()
        .then((res) => { if (keep()) setState({ owners: res.data.owners, failed: false }) })
        .catch(() => { if (keep()) setState({ owners: [], failed: true }) })
    }
    load()
    window.addEventListener(CASES_CHANGED, load)
    return () => {
      cancelled = true
      window.removeEventListener(CASES_CHANGED, load)
    }
  }, [])
  return state
}

/** The option value for an owner filter: '' is everyone's. */
function ownerValue({ assignee, unassigned }: OwnerPick, me: string): string {
  if (!assignee.trim()) return unassigned ? UNASSIGNED : ''
  return assignee === me ? MINE : OWNER + assignee
}

/** The filter an owner option sets; null for the inert "couldn't load" note. */
function ownerPick(value: string, me: string): OwnerPick | null {
  if (value === OWNERS_FAILED) return null
  if (value === MINE) return { assignee: me, unassigned: false }
  if (value === UNASSIGNED) return { assignee: '', unassigned: true }
  return { assignee: value.startsWith(OWNER) ? value.slice(OWNER.length) : '', unassigned: false }
}

/** Everyone's, Assigned to me, Unassigned, then the readable owners, stale ones labelled. A selected owner
 *  the list lacks (a ?owner= link, or the list failed) is kept, so the filter never hides. */
function ownerOptions(
  { owners, failed }: { owners: CaseOwner[]; failed: boolean },
  me: string,
  selected: string,
  extra: { value: string; label: string; count?: number }[] = [],
) {
  const listed = owners.filter((o) => o.owner !== me).map((o) => ({
    value: OWNER + o.owner,
    label: o.owner_state === 'not_current' ? `${o.owner} · ${STALE_OWNER_NOTE}` : o.owner,
  }))
  if (selected.trim() && selected !== me && !owners.some((o) => o.owner === selected)) listed.unshift({ value: OWNER + selected, label: selected })
  return [
    { value: '', label: 'Everyone’s' },
    ...(me ? [{ value: MINE, label: 'Assigned to me' }] : []),
    ...extra,
    { value: UNASSIGNED, label: 'Unassigned' },
    ...(failed ? [{ value: OWNERS_FAILED, label: 'Couldn’t load owners' }] : []),
    ...listed,
  ]
}

/** An owner filter's name in a combined option. */
const ownerLabel = (pick: OwnerPick) => (pick.assignee.trim() || (pick.unassigned ? 'Unassigned' : ''))

const KIND_WORDS: Record<string, string> = {
  investigate: 'Investigation',
  hunt: 'Hunt',
  root_cause: 'Root cause',
  adjudicate: 'Adjudicate',
  compose: 'Compose',
}
const kindWord = (kind: string) => KIND_WORDS[kind] ?? cap(kind.replace(/_/g, ' '))

/** The workflow catalog: distinct run kinds (known ones first) and workflow id → name. Empty while it loads or if it fails. */
function useWorkflowCatalog(): { kinds: string[]; names: Record<string, string> } {
  const [catalog, setCatalog] = useState<{ kinds: string[]; names: Record<string, string> }>({ kinds: [], names: {} })
  useEffect(() => {
    let cancelled = false
    workflowApi
      .listAll()
      .then((res) => {
        if (cancelled) return
        const found = new Set<string>()
        const names: Record<string, string> = {}
        for (const w of res.data?.workflows ?? []) {
          if (w.run_kind) found.add(w.run_kind)
          if (w.id && w.name) names[w.id] = w.name
        }
        const known = Object.keys(KIND_WORDS)
        setCatalog({ kinds: [...known.filter((k) => found.has(k)), ...[...found].filter((k) => !known.includes(k)).sort()], names })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])
  return catalog
}

function originOf(state: unknown): { pathname: string; search: string } | null {
  const o = (state as { origin?: { pathname?: unknown; search?: unknown } } | null)?.origin
  return typeof o?.pathname === 'string' ? { pathname: o.pathname, search: typeof o.search === 'string' ? o.search : '' } : null
}

export default function CasesScreen({ setViewFull, viewFull, openCase, caseSeed, onCaseSeedConsumed }: ConsoleScreenProps) {
  // the full page is a ?case=<id> param, so it is deep-linkable; rows open the drawer instead
  const [searchParams, setSearchParams] = useSearchParams()
  const selected = searchParams.get('case')
  // ?state=closed, ?needs_you=1 and ?working=1 open the list on that chip, and ?closed_by and
  // ?alerts_from narrow it, so an Overview count can link to the cases its alerts are in; ?owner opens
  // it on one assignee's cases (?unassigned=1 on cases with none), as a notice about an account's open work links
  const [filters, setFilters] = useState<CaseFilters>(() => {
    const closedBy = searchParams.get('closed_by')
    const alertsFrom = searchParams.get('alerts_from')
    return {
      ...INITIAL_CASE_FILTERS,
      state: searchParams.get('state') === 'closed' || closedBy === 'agent' || closedBy === 'person' ? 'closed' : '',
      needsYou: searchParams.get('needs_you') === '1',
      working: searchParams.get('working') === '1',
      closedBy: closedBy === 'agent' || closedBy === 'person' ? closedBy : '',
      alertsFrom: alertsFrom === 'today' || alertsFrom === 'all' ? alertsFrom : '',
      assignee: searchParams.get('owner') ?? '',
      unassigned: searchParams.get('unassigned') === '1',
    }
  })
  const backToList = useCallback(() => setSearchParams({}), [setSearchParams])
  // Expand from the drawer leaves the page it was open on in the entry's state; a link or new tab has none
  const navigate = useNavigate()
  const { state } = useLocation()
  const origin = originOf(state)
  const leave = useCallback(() => (origin ? navigate(origin) : backToList()), [origin, navigate, backToList])
  const collapse = useCallback(
    () => navigate(origin ?? { pathname: '/cases', search: '' }, { state: holdCase(null, selected) }),
    [origin, navigate, selected],
  )

  useEffect(() => {
    // the list fills the view and scrolls inside its card; the full page is full-bleed too, which
    // is what pins the case composer to the bottom (#2457). Asked again whenever the shell clears
    // it, as a tour starting does, or the page grows past the viewport and the composer falls under it.
    if (viewFull !== true) setViewFull(true)
  }, [setViewFull, viewFull])

  return selected ? (
    <CasesDetail id={selected} openTab={searchParams.get('tab')} openRun={searchParams.get('run')} onBack={backToList} onClose={leave} onCollapse={collapse} pageKey="cases" seed={caseSeed} onSeedConsumed={onCaseSeedConsumed} />
  ) : (
    <CasesTable filters={filters} onFilters={setFilters} onSelect={openCase} />
  )
}

/* small state row spanning the whole grid */
function StateRow({ children }: { children: ReactNode }) {
  return (
    <div role="row" className="cg-state">
      <div role="cell">{children}</div>
    </div>
  )
}

function CasesTable({
  filters,
  onFilters,
  onSelect,
}: {
  filters: CaseFilters
  onFilters: (next: CaseFilters) => void
  onSelect: (id: string) => void
}) {
  const { rows, total, strip, phase, error, reload } = useCases(filters)
  const { kinds, names: workflowNames } = useWorkflowCatalog()
  const owners = useCaseOwners()
  const me = useAuth().user?.username ?? ''
  const [showAdvanced, setShowAdvanced] = useState(false)
  // Advanced search replaces the page until cleared. Results stay in API order.
  const [results, setResults] = useState<CaseRow[] | null>(null)
  const [newOpen, setNewOpen] = useState(false)

  // a case changed in the drawer over this list: its search results are stale
  useEffect(() => {
    const clear = () => setResults(null)
    window.addEventListener(CASES_CHANGED, clear)
    return () => window.removeEventListener(CASES_CHANGED, clear)
  }, [])

  const setFilters = (partial: Partial<CaseFilters>) =>
    onFilters({
      ...filters,
      ...partial,
      offset: 'offset' in partial ? partial.offset ?? 0 : 0,
    })

  // what the Filters panel holds; the chips own state, needs-you, kind and SLA
  const panelFilters =
    (filters.priority !== 'any' ? 1 : 0) +
    (filters.assignee.trim() || filters.unassigned ? 1 : 0) +
    (filters.workflow.trim() ? 1 : 0) +
    (filters.dataSource.trim() ? 1 : 0)
  const activeFilters =
    panelFilters +
    (filters.state ? 1 : 0) +
    (filters.needsYou ? 1 : 0) +
    (filters.working ? 1 : 0) +
    (filters.closedBy ? 1 : 0) +
    (filters.alertsFrom ? 1 : 0) +
    (filters.kind ? 1 : 0) +
    (filters.sla ? 1 : 0)
  const showingSearch = results !== null
  const display = showingSearch ? results : rows
  const openCount = Object.values(strip.by_state).reduce((sum, n) => sum + n, 0)
  const stateKeys = [
    ...STATE_WORDS.map((o) => o.value),
    ...Object.keys(strip.by_state).filter((k) => k !== 'closed' && !STATE_WORDS.some((o) => o.value === k)),
  ].filter((k) => (strip.by_state[k] ?? 0) > 0 || k === filters.state)
  const pageStart = total === 0 ? 0 : filters.offset + 1
  const pageEnd = Math.min(filters.offset + rows.length, total)
  const agentShare = strip.closed_today
    ? `${Math.round(strip.agent_closure_share * 100)}%`
    : '—'

  // the one reset, for the Filters panel's Clear all and the filter row's Clear; the search stays
  const clearAll = () =>
    setFilters({
      state: '',
      needsYou: false,
      working: false,
      closedBy: '',
      alertsFrom: '',
      kind: '',
      priority: 'any',
      sla: '',
      assignee: '',
      unassigned: false,
      workflow: '',
      dataSource: '',
    })

  // State: the needs-you and working flags read as their own options; each makes the call its chip did
  const allOpen: Partial<CaseFilters> = { state: '', needsYou: false, working: false, closedBy: '' }
  const stateValue = filters.needsYou ? NEEDS_YOU : filters.working ? WORKING : filters.state
  const pickState = (v: string) =>
    setFilters(
      v === NEEDS_YOU ? { ...allOpen, needsYou: true }
        : v === WORKING ? { ...allOpen, working: true }
        : { ...allOpen, state: v },
    )
  const stateOptions = [
    { value: '', label: 'All open', count: openCount },
    { value: NEEDS_YOU, label: 'Needs you', count: strip.needs_you, dot: TONE_DOT.needs },
    { value: WORKING, label: 'Still working', count: Math.max(0, openCount - strip.needs_you), dot: 'var(--ac)' },
    ...stateKeys.map((key) => ({
      value: key,
      label: STATE_WORDS.find((o) => o.value === key)?.label ?? cap(key.replace(/_/g, ' ')),
      count: strip.by_state[key] ?? 0,
      dot: TONE_DOT[statePill(key).tone],
    })),
    { value: 'closed', label: 'Closed' },
  ]

  // Assigned: the panel's owner can sit alongside Near SLA; then one option names both
  const owner = { assignee: filters.assignee, unassigned: filters.unassigned }
  const ownerSel = ownerValue(owner, me)
  const assignedValue = filters.sla === 'risk' ? (ownerSel ? OTHER : NEAR_SLA) : ownerSel
  const assignedOptions = [
    ...ownerOptions(owners, me, filters.assignee, [{ value: NEAR_SLA, label: 'Near SLA', count: strip.sla_at_risk }]),
    ...(filters.sla === 'risk' && ownerSel ? [{ value: OTHER, label: `${ownerSel === MINE ? 'Assigned to me' : ownerLabel(owner)} · Near SLA` }] : []),
  ]
  const noOwner = { assignee: '', unassigned: false }
  const pickAssigned = (v: string) => {
    if (v === OTHER) return
    if (v === NEAR_SLA) return setFilters({ sla: 'risk', ...noOwner })
    const pick = ownerPick(v, me)
    if (pick) setFilters({ ...pick, sla: '' })
  }
  const pickPanelOwner = (v: string) => {
    const pick = ownerPick(v, me)
    if (pick) setFilters(pick)
  }

  const kindName = (c: CaseRow) => (c.workflowId && workflowNames[c.workflowId]) || c.workflowId || '—'

  return (
    <div className="cases-list">
      <div className="cases-top">
        <PageHead
          title="Cases"
          level="h1"
          description="Every open case, who owns it and what it is waiting on."
          actions={
            <>
              <div className="search">
                <span><Icon name="search" /></span>
                <input
                  aria-label="Search cases"
                  placeholder="Search cases by title, ID, owner…"
                  value={filters.query}
                  onChange={(e) => setFilters({ query: e.target.value })}
                />
              </div>
              <FilterButton
                activeCount={panelFilters}
                onClearAll={clearAll}
              >
                <FilterGroup
                  label="Priority"
                  value={filters.priority}
                  onSelect={(priority) => setFilters({ priority })}
                  options={[{ value: 'any', label: 'Any' }, ...CASE_PRIO_OPTIONS]}
                />
                <div className="filter-grp">
                  <span className="filter-grp-label">Assignee</span>
                  <Select searchable value={ownerSel} options={ownerOptions(owners, me, filters.assignee)} onSelect={pickPanelOwner} />
                </div>
                <label className="filter-grp">
                  <span className="filter-grp-label">Workflow</span>
                  <input aria-label="Workflow filter" className={inputCls} value={filters.workflow} onChange={(e) => setFilters({ workflow: e.target.value })} />
                </label>
                <label className="filter-grp">
                  <span className="filter-grp-label">Data source</span>
                  <input aria-label="Data source filter" className={inputCls} value={filters.dataSource} onChange={(e) => setFilters({ dataSource: e.target.value })} />
                </label>
              </FilterButton>
              <button className="btn ghost" aria-pressed={showAdvanced} onClick={() => setShowAdvanced((v) => !v)}>
                Advanced
              </button>
              <button className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={reload}><Icon name="refresh" /></button>
              <button className="btn primary" onClick={() => setNewOpen(true)}><Icon name="plus" /> New case</button>
            </>
          }
        />
        <StatCards label="Case status">
          <StatCard
            label="Needs you"
            dot={TONE_DOT.needs}
            tone="var(--poor)"
            info={<InfoTip label="How needs you is counted" {...NEEDS_YOU_TIP} />}
            value={strip.needs_you.toLocaleString()}
            onClick={() => pickState(filters.needsYou ? '' : NEEDS_YOU)}
            pressed={filters.needsYou}
          />
          <StatCard
            label="Still working"
            info={<InfoTip label="How still working is counted" {...WORKING_TIP} />}
            value={Math.max(0, openCount - strip.needs_you).toLocaleString()}
            onClick={() => pickState(filters.working ? '' : WORKING)}
            pressed={filters.working}
          />
          <StatCard
            label="Near SLA"
            info={<InfoTip label="How near SLA is counted" {...NEAR_SLA_TIP} />}
            value={strip.sla_at_risk.toLocaleString()}
            onClick={() => setFilters({ sla: filters.sla === 'risk' ? '' : 'risk', ...noOwner })}
            pressed={filters.sla === 'risk'}
          />
          {/* display-only: the closed filter lists every closed case, not today's */}
          <StatCard
            label="Closed today"
            info={<InfoTip label="How closed today is counted" {...CLOSED_TODAY_TIP} />}
            value={strip.closed_today.toLocaleString()}
            sub={`${agentShare} by an agent alone`}
          />
        </StatCards>
        <div className="filter-row" role="group" aria-label="Filters">
          <div className="filter-row-picks">
            <Select name="State" value={stateValue} options={stateOptions} onSelect={pickState} />
            {kinds.length > 0 && (
              <Select
                name="Kind"
                value={filters.kind}
                options={[{ value: '', label: 'All kinds' }, ...kinds.map((kind) => ({ value: kind, label: kindWord(kind) }))]}
                onSelect={(kind) => setFilters({ kind })}
              />
            )}
            <Select name="Assigned" searchable={owners.owners.length > 8} value={assignedValue} options={assignedOptions} onSelect={pickAssigned} />
            {/* set by an Overview link; shown, and removable, so the list never looks wrong for no reason */}
            {filters.closedBy && (
              <FilterChip
                list
                active
                label={`Closed by ${filters.closedBy === 'agent' ? 'an agent' : 'a person'}`}
                count={<Icon name="close" size={11} />}
                title="Remove this filter"
                onClick={() => setFilters({ closedBy: '' })}
              />
            )}
            {filters.alertsFrom && (
              <FilterChip
                list
                active
                label={filters.alertsFrom === 'today' ? 'Holds alerts from today' : 'Holds alerts from any day'}
                count={<Icon name="close" size={11} />}
                title="Remove this filter"
                onClick={() => setFilters({ alertsFrom: '' })}
              />
            )}
          </div>
          {phase === 'ready' && (
            <span className="filter-row-summary">
              {activeFilters || filters.query
                ? `Showing ${total.toLocaleString()} ${filters.state === 'closed' ? `closed case${total === 1 ? '' : 's'}` : `of ${openCount.toLocaleString()}`}`
                : `${openCount.toLocaleString()} open case${openCount === 1 ? '' : 's'}`}
              {activeFilters > 0 && <> · <button type="button" onClick={clearAll}>Clear</button></>}
            </span>
          )}
        </div>
      </div>
      {showAdvanced && <AdvancedSearchPanel onResults={setResults} rows={rows} owners={owners} me={me} />}
      {results && (
        <div className="adv-results-bar">
          <span>Showing <b>{results.length}</b> search result{results.length === 1 ? '' : 's'}</span>
          <button className="btn ghost" onClick={() => setResults(null)}>Clear search</button>
        </div>
      )}
      <div className="cases-card list-scroll" data-testid="cases-scroll">
        <div className="cases-grid" role="table" aria-label="Cases">
          <div className="cg-row cg-head" role="row">
            <span role="columnheader"><span className="sr-only">Select</span></span>
            <span role="columnheader">Case</span>
            <span role="columnheader">What it is<span className="col-low-note cg-hide-note">Kind, owner and time left are hidden while Ask Vigil is open</span></span>
            <span role="columnheader" className="col-low">Kind</span>
            <span role="columnheader">Severity</span>
            <span role="columnheader">Status</span>
            <span role="columnheader" className="col-low">Owner</span>
            <span role="columnheader" className="cg-th-info col-low">Time left (SLA)<InfoTip label="How time left is calculated" {...TIME_LEFT_TIP} /></span>
            <span role="columnheader"><span className="sr-only">Comments</span></span>
          </div>
          <div role="rowgroup">
            {phase === 'loading' && <StateRow><EmptyState loading table compact icon="folder" title="Loading cases…" /></StateRow>}
            {phase === 'error' && (
              <StateRow>
                <EmptyState error table icon="alert" title="Couldn’t load cases" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
              </StateRow>
            )}
            {phase === 'ready' && display.length === 0 && (
              <StateRow>
                <EmptyState
                  table
                  icon={showingSearch || activeFilters || filters.query ? 'filter' : 'folder'}
                  title={showingSearch ? 'No cases match this search' : activeFilters || filters.query ? 'No cases match these filters' : 'No cases yet'}
                  body={showingSearch || activeFilters || filters.query ? 'Clear the search and filters to return to the open queue.' : 'Create a case manually or link findings from the dashboard to start an investigation record.'}
                  primary={showingSearch || activeFilters || filters.query ? { label: 'Clear filters', onClick: () => { setResults(null); onFilters({ ...INITIAL_CASE_FILTERS, limit: filters.limit }) }, icon: 'close' } : { label: 'New case', onClick: () => setNewOpen(true), icon: 'plus' }}
                />
              </StateRow>
            )}
            {phase === 'ready' &&
              display.map((c) => {
                return (
                  <div
                    key={c.id}
                    role="row"
                    className={`cg-row cg-case${c.needsYou ? ' needs' : ''}`}
                    tabIndex={0}
                    onClick={() => onSelect(c.id)}
                    onKeyDown={(e) => { if (e.target === e.currentTarget) activateOnKey(() => onSelect(c.id))(e) }}
                  >
                    <span role="cell" title={LATER_RELEASE} onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" className="cg-check" disabled aria-label={`Select case ${c.id}`} />
                    </span>
                    <span role="cell" className="cg-id" title={c.id}>{c.id}</span>
                    <span role="cell" className="cg-what" title={c.desc || c.title}>
                      <span className="cg-title">{c.title}</span>
                      <span className="cg-sub">{metaLine(c)}</span>
                    </span>
                    <span role="cell" className="cg-kind col-low" title={kindName(c)}>{kindName(c)}</span>
                    <span role="cell"><SeverityMark level={c.prio} /></span>
                    <span role="cell"><StatePill state={c.status} needs={c.needsYou} /></span>
                    <span role="cell" className="cg-owner col-low">
                      <span className="cg-avatar" aria-hidden="true">{c.owner}</span>
                      <span className="cg-owner-text">
                        <span className="cg-owner-name">
                          {c.ownerName || 'Unassigned'}
                          {me && c.ownerName === me && ' (you)'}
                        </span>
                        {c.ownerStale && <span className="owner-note">{STALE_OWNER_NOTE}</span>}
                      </span>
                    </span>
                    <span role="cell" className="cg-time col-low">
                      <span>{c.timeLeft}</span>
                      {!c.slaPaused && c.timeLeft !== '—' && <LevelBadge level={slaLevel(c.slaHealth)} variant="pill" />}
                    </span>
                    <span role="cell" className={`cg-comments${c.comments ? '' : ' none'}`}>
                      <Icon name="comment" size={13} />
                      {c.comments ?? 0}
                    </span>
                  </div>
                )
              })}
          </div>
        </div>
      </div>
      {!showingSearch && (
        <div className="cases-pager">
          <label>
            Rows per page
            <select
              aria-label="Rows per page"
              value={filters.limit}
              onChange={(e) => setFilters({ limit: Number(e.target.value) })}
            >
              {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span>
            {total === 0 ? '0 of 0' : `${pageStart}–${pageEnd} of ${total}`}
          </span>
          <span className="cases-pager-nav">
            <button aria-label="Previous page" disabled={filters.offset <= 0} onClick={() => setFilters({ offset: Math.max(0, filters.offset - filters.limit) })}><Icon name="chevL" size={14} /></button>
            <button aria-label="Next page" disabled={filters.offset + rows.length >= total} onClick={() => setFilters({ offset: filters.offset + filters.limit })}><Icon name="chevR" size={14} /></button>
          </span>
        </div>
      )}

      <NewCaseDialog open={newOpen} onClose={() => setNewOpen(false)} onCreated={reload} />
    </div>
  )
}

function DeleteCaseDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: CaseRow | null
  onClose: () => void
  onDeleted: (deleted: CaseRow) => void
}) {
  const { notify } = useToast()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (target) setError('')
  }, [target])

  const remove = async () => {
    if (!target || busy) return
    const deleting = target
    setBusy(true)
    setError('')
    try {
      await casesApi.delete(deleting.id)
      setBusy(false)
      notify('ok', `Deleted ${deleting.id}. Linked findings were preserved.`)
      onClose()
      onDeleted(deleting)
    } catch (cause) {
      const message = errorDetail(cause, `Failed to delete ${deleting.id}`)
      setBusy(false)
      setError(message)
      notify('err', message)
    }
  }

  return (
    <ConfirmDialog
      open={target !== null}
      title="Delete case?"
      body={
        <span className="flex flex-col gap-2">
          <span>
            Permanently delete case <span className="mono text-tx">{target?.id}</span>?
          </span>
          <span>
            The case and its case-owned records will be removed. Linked findings will remain.
            This cannot be undone.
          </span>
          {error && <span role="alert" style={{ color: 'var(--crit)' }}>{error}</span>}
        </span>
      }
      confirmLabel="Delete case"
      busy={busy}
      onConfirm={remove}
      onClose={() => {
        if (!busy) onClose()
      }}
    />
  )
}

function AdvancedSearchPanel({ onResults, rows, owners, me }: {
  onResults: (r: CaseRow[] | null) => void
  rows: CaseRow[]
  owners: { owners: CaseOwner[]; failed: boolean }
  me: string
}) {
  const [query, setQuery] = useState('')
  const [priority, setPriority] = useState('')
  const [status, setStatus] = useState('')
  const [assignee, setAssignee] = useState('')
  const [tags, setTags] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [sugOpen, setSugOpen] = useState(false)

  // typeahead suggestions drawn from loaded case titles/ids matching the query
  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return [] as { value: string; meta: string }[]
    const out: { value: string; meta: string }[] = []
    const seen = new Set<string>()
    for (const c of rows) {
      if (out.length >= 6) break
      const hay = `${c.title} ${c.id}`.toLowerCase()
      if (hay.includes(q) && !seen.has(c.title)) {
        seen.add(c.title)
        out.push({ value: c.title, meta: c.id })
      }
    }
    return out
  }, [rows, query])

  const run = async () => {
    const filters: Partial<Schema<'SearchRequest'>> = {}
    if (query.trim()) filters.query_text = query.trim()
    if (priority) filters.priority = [priority]
    if (status) filters.status = [status]
    const owner = ownerPick(assignee, me)
    if (owner?.assignee) filters.assignee = [owner.assignee]
    if (owner?.unassigned) filters.unassigned = true
    const tagList = tags.split(',').map((t) => t.trim()).filter(Boolean)
    if (tagList.length) filters.tags = tagList
    if (start) filters.created_after = start
    if (end) filters.created_before = `${end}T23:59:59`
    if (Object.keys(filters).length === 0) {
      onResults(null)
      return
    }
    setBusy(true)
    setErr('')
    try {
      const res = await caseSearchApi.search({ ...filters, unassigned: filters.unassigned ?? false, limit: 50, offset: 0 })
      onResults(res.data.results.map((c) => mapQueueCase(c)))
    } catch (e) {
      setErr(errorDetail(e, 'Search failed'))
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    setQuery(''); setPriority(''); setStatus(''); setAssignee(''); setTags(''); setStart(''); setEnd('')
    setErr('')
    onResults(null)
  }

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') run()
  }

  return (
    <div className="adv-search">
      <label className="af span-all"><span>Full-text query</span>
        <div className="sug-wrap">
          <input
            className={inputCls}
            placeholder="Search title, description, IOCs, or case ID…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSugOpen(true) }}
            onKeyDown={(e) => { if (e.key === 'Enter') setSugOpen(false); onKey(e) }}
            onFocus={() => setSugOpen(true)}
            onBlur={() => setTimeout(() => setSugOpen(false), 120)}
            autoComplete="off"
          />
          {sugOpen && suggestions.length > 0 && (
            <div className="drop-menu sug-menu" role="listbox">
              {suggestions.map((s) => (
                <button
                  key={s.meta}
                  type="button"
                  role="option"
                  onMouseDown={(e) => { e.preventDefault(); setQuery(s.value); setSugOpen(false) }}
                >
                  <span className="sug-val">{s.value}</span>
                  <span className="sug-meta mono">{s.meta}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </label>
      <label className="af"><span>Priority</span>
        <Select
          value={priority}
          onSelect={setPriority}
          placeholder="Any"
          options={[
            { value: '', label: 'Any' },
            ...CASE_PRIO_OPTIONS,
          ]}
        />
      </label>
      <label className="af"><span>Status</span>
        <Select
          value={status}
          onSelect={setStatus}
          placeholder="Any"
          options={[
            { value: '', label: 'Any' },
            { value: 'open', label: 'Open' },
            { value: 'investigating', label: 'Investigating' },
            { value: 'closed', label: 'Closed' },
          ]}
        />
      </label>
      <label className="af"><span>Assignee</span>
        <Select searchable value={assignee} options={ownerOptions(owners, me, ownerPick(assignee, me)?.assignee ?? '')} onSelect={(v) => v !== OWNERS_FAILED && setAssignee(v)} />
      </label>
      <label className="af"><span>Tags</span>
        <input className={inputCls} placeholder="comma-separated" value={tags} onChange={(e) => setTags(e.target.value)} onKeyDown={onKey} />
      </label>
      <label className="af"><span>From</span>
        <input className={inputCls} type="date" value={start} onChange={(e) => setStart(e.target.value)} />
      </label>
      <label className="af"><span>To</span>
        <input className={inputCls} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
      </label>
      <div className="af-actions">
        {err && <span className="muted" style={{ color: 'var(--crit)' }}>{err}</span>}
        <span style={{ flex: 1 }} />
        <button className="btn ghost" onClick={reset}>Clear</button>
        <button className="btn primary" onClick={run} disabled={busy}>{busy ? 'Searching…' : 'Search'}</button>
      </div>
    </div>
  )
}

function NewCaseDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState('medium')
  const [status, setStatus] = useState<CaseStatus>('open')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const submit = async () => {
    if (!title.trim()) {
      setErr('Title is required.')
      return
    }
    setBusy(true)
    setErr('')
    try {
      await casesApi.create({
        title: title.trim(),
        description: description.trim(),
        finding_ids: [],
        priority,
        status,
      })
      setTitle(''); setDescription(''); setPriority('medium'); setStatus('open')
      onCreated()
      onClose()
    } catch (e) {
      setErr(errorDetail(e, 'Failed to create case'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="New case" width={520}>
      <div className="flex flex-col gap-3.5">
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Title</span>
          <input className={inputCls} placeholder="Case title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Priority</span>
            <Select
              value={priority}
              onSelect={setPriority}
              options={RATED_PRIO_OPTIONS}
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Status</span>
            <Select
              value={status}
              onSelect={(v) => isCaseStatus(v) && setStatus(v)}
              options={[
                { value: 'open', label: 'Open' },
                { value: 'investigating', label: 'Investigating' },
                { value: 'closed', label: 'Closed' },
              ]}
            />
          </label>
        </div>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Description</span>
          <textarea className={inputCls} rows={4} placeholder="Optional description" value={description} onChange={(e) => setDescription(e.target.value)} style={{ resize: 'vertical' }} />
        </label>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Creating…' : 'Create case'}</button>
        </div>
      </div>
    </Popup>
  )
}

type CaseConflict = Schema<'CaseFieldConflict'>

const CASE_STATUS_OPTIONS = [
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'closed', label: 'Closed' },
]

/** A stored value outside the menu still shows as itself rather than as an empty picker. */
function withStored(options: { value: string; label: string }[], value: string) {
  return !value || options.some((o) => o.value === value) ? options : [{ value, label: value }, ...options]
}

const CONFLICT_NAME: Record<CaseEditField, string> = {
  title: 'The title',
  description: 'The description',
  status: 'The status',
  priority: 'The priority',
  assignee: 'The assignee',
}

function conflictValue(field: CaseEditField, value: string): string {
  if (field === 'status') return CASE_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? (value || 'blank')
  if (field === 'priority') return CASE_PRIO_OPTIONS.find((o) => o.value === value)?.label ?? (value || 'blank')
  if (field === 'assignee') return value || 'Unassigned'
  return value ? `“${value}”` : 'empty'
}

function EditCaseDialog({ open, c, onClose, onSaved, onStale }: {
  open: boolean
  c: CaseRecord | null
  onClose: () => void
  onSaved: () => void
  /** The case moved under this edit: re-read it behind the dialog. */
  onStale: () => void
}) {
  return (
    <Popup open={open} onClose={onClose} title="Edit case" width={520}>
      {c && <EditCaseForm c={c} onClose={onClose} onSaved={onSaved} onStale={onStale} />}
    </Popup>
  )
}

// Mounted once per opening. Its state starts from the case as read then and does
// not follow later reads: a run re-reads the case while it works, and following
// that would wipe what is typed here and hide the change it should be compared with.
function EditCaseForm({ c, onClose, onSaved, onStale }: {
  c: CaseRecord
  onClose: () => void
  onSaved: () => void
  onStale: () => void
}) {
  const [values, setValues] = useState<CaseEditValues>(c.stored)
  // What this edit last saw on the server, field by field. A save names these for
  // the fields it changes, so the server can refuse one that has moved since.
  const [seen, setSeen] = useState<CaseEditValues>(c.stored)
  const [conflicts, setConflicts] = useState<CaseConflict[]>([])
  const [category, setCategory] = useState<ClosureCategory>('resolved')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const set = (field: CaseEditField) => (value: string) => setValues((v) => ({ ...v, [field]: value }))

  // Only a Case crossing into closed is being closed. Re-saving one that is
  // already closed is an edit, and re-stamping it would move the closure's date
  // and re-derive its verdict for a typo fix.
  const closingFrom = (mine: CaseEditValues, stored: CaseEditValues) => mine.status === 'closed' && stored.status !== 'closed'
  const closing = closingFrom(values, seen)

  const save = async (mine: CaseEditValues, stored: CaseEditValues) => {
    if (!mine.title.trim()) { setErr('Title is required.'); return }
    const isClosing = closingFrom(mine, stored)
    const problem = isClosing ? closureProblem(category, reason) : null
    if (problem) { setErr(problem); return }
    // Only what this person changed: sending the rest would write back old values
    // over someone else's edit to a field this person never touched.
    const changed = CASE_EDIT_FIELDS.filter((field) => mine[field] !== stored[field])
    if (!changed.length) { onClose(); return }
    const update: Schema<'CaseUpdate'> = {
      expected: Object.fromEntries(changed.map((field) => [field, stored[field]])),
    }
    const landed = { ...stored }
    for (const field of changed) {
      if (field !== 'status') {
        // '' (not omitted) so clearing the field clears it
        update[field] = mine[field].trim()
        landed[field] = mine[field].trim()
      } else if (!isClosing && isCaseStatus(mine.status)) {
        // The status is left off when closing: the close endpoint sets it, and a
        // PATCH carrying it too would stamp a second, unstated closure over the
        // category chosen here.
        update.status = mine.status
        landed.status = mine.status
      }
    }
    setBusy(true)
    setErr('')
    try {
      await casesApi.update(c.id, update)
      setSeen(landed)
      // Second, so a close that fails leaves the edits saved and the Case open,
      // rather than closed under a title that never landed.
      if (isClosing) {
        await casesApi.closeCase(c.id, { ...closurePayload(category, reason), expected: { status: stored.status } })
      }
      onSaved()
      onClose()
    } catch (e) {
      const moved = caseConflicts(e)
      if (moved) {
        setConflicts(moved)
        onStale()
      } else {
        setErr(errorDetail(e, 'Failed to save changes'))
      }
    } finally {
      setBusy(false)
    }
  }

  // A conflict interrupts a save the person already asked for, so the last answer
  // carries it on, now comparing with the values just shown. A third edit is caught.
  const answer = (conflict: CaseConflict, take: 'current' | 'mine') => {
    const current = conflict.current ?? ''
    const nextSeen = { ...seen, [conflict.field]: current }
    const nextValues = take === 'current' ? { ...values, [conflict.field]: current } : values
    const left = conflicts.filter((other) => other.field !== conflict.field)
    setSeen(nextSeen)
    setValues(nextValues)
    setConflicts(left)
    if (!left.length) void save(nextValues, nextSeen)
  }

  return (
    <div className="flex flex-col gap-3.5">
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>Title</span>
        <input className={inputCls} value={values.title} onChange={(e) => set('title')(e.target.value)} autoFocus />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Priority</span>
          <Select value={values.priority} onSelect={set('priority')} options={withStored(CASE_PRIO_OPTIONS, values.priority)} />
        </label>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Status</span>
          <Select value={values.status} onSelect={set('status')} options={withStored(CASE_STATUS_OPTIONS, values.status)} />
        </label>
      </div>
      {closing && <ClosureFields category={category} reason={reason} onCategory={setCategory} onReason={setReason} />}
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>Assignee</span>
        <input className={inputCls} placeholder="name or email" value={values.assignee} onChange={(e) => set('assignee')(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>Description</span>
        <textarea className={inputCls} rows={4} value={values.description} onChange={(e) => set('description')(e.target.value)} style={{ resize: 'vertical' }} />
      </label>
      {conflicts.length > 0 && (
        <div role="alert" className="case-edit-conflicts">
          <p>This case changed while you were editing. Your edits are still here.</p>
          {conflicts.map((conflict) => (
            <div key={conflict.field} className="case-edit-conflict">
              <p>
                {CONFLICT_NAME[conflict.field]} changed from {conflictValue(conflict.field, conflict.expected ?? '')} to{' '}
                {conflictValue(conflict.field, conflict.current ?? '')}. Yours is {conflictValue(conflict.field, values[conflict.field])}.
              </p>
              <div className="flex flex-wrap gap-2">
                <button className="btn ghost" onClick={() => answer(conflict, 'current')} disabled={busy}>Use the current value</button>
                <button className="btn" onClick={() => answer(conflict, 'mine')} disabled={busy}>Save mine anyway</button>
              </div>
            </div>
          ))}
        </div>
      )}
      {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
      <div className="flex justify-end gap-2.5">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" onClick={() => void save(values, seen)} disabled={busy || conflicts.length > 0}>{busy ? 'Saving…' : 'Save changes'}</button>
      </div>
    </div>
  )
}

function MergeCaseDialog({ open, c, onClose, onMerged }: { open: boolean; c: CaseRow | null; onClose: () => void; onMerged: () => void }) {
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [rows, setRows] = useState<CaseRow[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [loadError, setLoadError] = useState('')
  const [loadKey, setLoadKey] = useState(0)
  const candidates = useMemo(() => rows.filter((r) => r.id !== c?.id), [rows, c])

  // the picker loads its own list: the drawer holds none
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setTarget('')
    setErr('')
    setPhase('loading')
    casesApi
      .getAll({ limit: CASE_PAGE_LIMIT })
      .then((res) => {
        if (cancelled) return
        setRows((res.data.cases ?? []).map((x) => mapQueueCase(x)))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setLoadError(errorDetail(e, 'Failed to load cases'))
        setPhase('error')
      })
    return () => { cancelled = true }
  }, [open, loadKey])

  const submit = async () => {
    if (!c) return
    if (!target) { setErr('Select a case to merge into.'); return }
    setBusy(true)
    setErr('')
    try {
      await casesApi.merge(target, c.id)
      onMerged()
      onClose()
    } catch (e) {
      setErr(errorDetail(e, 'Merge failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Merge case" width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5] m-0">
          Merge <span className="mono text-tx">{c?.id}</span> into another case. Its linked findings, IOCs and
          evidence move to the target case, and this case is closed.
        </p>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Merge into</span>
          <Select
            value={target}
            onSelect={setTarget}
            placeholder={phase === 'loading' ? 'Loading cases…' : 'Select target case…'}
            options={candidates.map((r) => ({ value: r.id, label: `${r.id} — ${r.title}` }))}
          />
        </label>
        {phase === 'error' && (
          <div role="alert" className="text-[13px]" style={{ color: 'var(--crit)' }}>
            Couldn’t load cases: {loadError}{' '}
            <button type="button" className="btn ghost" onClick={() => setLoadKey((k) => k + 1)}>Retry</button>
          </div>
        )}
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy || phase !== 'ready'}>{busy ? 'Merging…' : 'Merge case'}</button>
        </div>
      </div>
    </Popup>
  )
}

export function CasesDetail({
  id,
  openTab,
  openRun,
  onBack,
  onExpand,
  onClose,
  onCollapse,
  pageKey,
  seed,
  onSeedConsumed,
}: {
  id: string
  /** Full page only: the ?tab= and ?run= a link landed on. */
  openTab?: string | null
  openRun?: string | null
  /** Leave the case: back to the list, or close the drawer. */
  onBack: () => void
  /** Set only in the drawer. */
  onExpand?: () => void
  /** Full page only: × and Esc, back to the page the case was expanded from. Defaults to onBack. */
  onClose?: () => void
  /** Full page only: hand the case back to the side panel. */
  onCollapse?: () => void
  pageKey: string
  /** Command-bar text for this case's composer. */
  seed?: ChatSeed | null
  onSeedConsumed?: () => void
}) {
  const { row: c, created, combinedState, investigations, closure, linkedFindings, phase, error, reload: reloadDetail, refresh } =
    useCaseDetail(id)
  const { hasPermission } = useAuth()
  const canDelete = hasPermission('cases.delete')
  const canWrite = hasPermission('cases.write')
  const [action, setAction] = useState<'edit' | 'merge' | 'delete' | null>(null)
  // a list left mounted behind the drawer reloads on this
  const onChanged = () => { reloadDetail(); notifyCasesChanged() }

  return (
    <>
      <CasePage
        id={id}
        openTab={openTab}
        openRun={openRun}
        c={c}
        created={created}
        combinedState={combinedState}
        investigations={investigations}
        closure={closure}
        linkedFindings={linkedFindings}
        phase={phase}
        error={error}
        pageKey={pageKey}
        seed={seed}
        onSeedConsumed={onSeedConsumed}
        onBack={onBack}
        onExpand={onExpand}
        onClose={onClose}
        onCollapse={onCollapse}
        onEdit={() => setAction('edit')}
        onMerge={() => setAction('merge')}
        onDelete={() => setAction('delete')}
        canDelete={canDelete}
        canWrite={canWrite}
        onChanged={onChanged}
        onRefresh={refresh}
      />

      <EditCaseDialog
        open={action === 'edit'}
        c={c}
        onClose={() => setAction(null)}
        onSaved={onChanged}
        onStale={refresh}
      />
      <MergeCaseDialog
        open={action === 'merge'}
        c={c}
        onClose={() => setAction(null)}
        onMerged={() => { notifyCasesChanged(); onBack() }}
      />
      <DeleteCaseDialog
        target={action === 'delete' ? c : null}
        onClose={() => setAction(null)}
        onDeleted={() => {
          notifyCasesChanged()
          onBack()
        }}
      />
    </>
  )
}
