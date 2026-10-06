/* The Watch a run page's two right-hand columns (PRD AW-W5): Explanations being tested,
   Limits used, Reviewer and Blind spots hit, each as of the step the player holds.
   Board: docs/design/console/boards/WorkflowRun.dc.html. Everything is a function of the
   run detail and, for a hunt, the lead's recorded digest at that step: no endpoint, no fold. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { workflowApi, type ReplayDigest } from '../../services/api'
import { Cost, fmtCost } from '../../shared/cost'
import { OpenCheckpoint, bearings, hypothesisColor, liveGap, provenanceTag } from './WorkflowsScreen'
import type { HuntEvidence, HuntStanding, HuntView, WfRunDetail } from './runRead'

export function Heading({ children }: { children: ReactNode }) {
  return <span className="text-[12px] font-semibold leading-[1.3] text-[var(--tx2)]">{children}</span>
}
const CARD = 'rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]'
export function NoData({ children }: { children: ReactNode }) {
  return <span className={`text-[12px] leading-[1.45] text-[var(--tx2)] px-3 py-2.5 ${CARD}`}>{children}</span>
}

const NOT_TRACKED = 'Not tracked for this kind.'
const NO_REVIEWER = 'No reviewer runs on this kind of run yet.'

/** The board's thresholds for a limit: green, then amber from 75%, red past 90%. */
const levelColor = (pct: number) => (pct > 90 ? 'var(--poor)' : pct >= 75 ? 'var(--fair)' : 'var(--good)')
const share = (n: number, of: number) => Math.max(0, Math.min(100, Math.round((100 * n) / of)))

function Bar({ pct, color, size }: { pct: number; color: string; size: number }) {
  return (
    <span className="block rounded-full bg-[var(--bg4)] overflow-hidden" style={{ height: size }}>
      <span className="block rounded-full" style={{ height: size, width: `${pct}%`, background: color }} />
    </span>
  )
}

interface LimitRow { label: string; value: ReactNode; pct?: number; color?: string }

function Limit({ label, value, pct, color }: LimitRow) {
  return (
    <div className={`flex flex-col gap-[5px] px-3 py-2.5 ${CARD}`}>
      <span className="flex justify-between gap-3 text-[12px]">
        <span className="font-[650] text-[var(--tx1)] shrink-0">{label}</span>
        <span className={`min-w-0 text-right break-words ${pct === undefined ? 'text-[var(--tx2)]' : 'text-[var(--tx0)]'}`}>{value}</span>
      </span>
      {pct !== undefined && <Bar pct={pct} color={color ?? levelColor(pct)} size={5} />}
    </div>
  )
}

/** One-line card for a reviewer or blind-spot note, in the board's plain text. */
function Note({ children }: { children: ReactNode }) {
  return <div className={`flex flex-col gap-1 text-[12px] leading-[1.45] text-[var(--tx0)] px-3 py-2.5 ${CARD}`}>{children}</div>
}

function Columns({ explanations, rows, reviewer, blind }: { explanations: ReactNode; rows: LimitRow[]; reviewer: ReactNode; blind: ReactNode }) {
  return (
    <>
      <div className="flex flex-col gap-2 min-w-0">
        <Heading>Explanations being tested</Heading>
        {explanations}
      </div>
      <div className="flex flex-col gap-2.5 min-w-0">
        <Heading>Limits used</Heading>
        {rows.map((r) => <Limit key={r.label} {...r} />)}
        <Heading>Reviewer</Heading>
        {reviewer}
        <Heading>Blind spots hit</Heading>
        {blind}
      </div>
    </>
  )
}

const untracked = (label: string): LimitRow => ({ label, value: NOT_TRACKED })

/** A list of blind spots: the first few, then a count, so one noisy run cannot decide the column's height. */
const SPOTS_SHOWN = 5
function Spots({ lines, empty, note }: { lines: { main: string; sub: string[] }[]; empty: string; note?: string }) {
  return (
    <>
      {lines.length === 0 ? <NoData>{empty}</NoData> : (
        <Note>
          {lines.slice(0, SPOTS_SHOWN).map((l) => (
            <span key={l.main + l.sub.join('|')} className="flex flex-col">
              <span className="line-clamp-2 break-words" title={l.main}>{l.main}</span>
              {l.sub.map((s) => <span key={s} className="text-[11px] text-[var(--tx2)] line-clamp-2 break-words" title={s}>{s}</span>)}
            </span>
          ))}
          {lines.length > SPOTS_SHOWN && <span className="text-[11px] text-[var(--tx2)]">+{lines.length - SPOTS_SHOWN} more</span>}
        </Note>
      )}
      {note && <NoData>{note}</NoData>}
    </>
  )
}

// ── hunts ────────────────────────────────────────────────────────────────

type Recorded = ReplayDigest | 'failed'

/** What the lead was shown before one decision, off the run's own record. A past decision's
 *  digest never changes, so each is asked for once and kept; the newest step reads the live
 *  projection instead and asks for nothing. */
function useRecorded(runId: string, decisionId: string | null): Recorded | undefined {
  const [held, setHeld] = useState<Record<string, Recorded>>({})
  const asked = useRef(new Set<string>())
  const mounted = useRef(true)
  // set on mount as well: StrictMode runs the cleanup once before the page is really gone
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (decisionId === null || asked.current.has(decisionId)) return
    asked.current.add(decisionId)
    const keep = (value: Recorded) => { if (mounted.current) setHeld((h) => ({ ...h, [decisionId]: value })) }
    Promise.resolve()
      .then(() => workflowApi.getReplay(runId, decisionId))
      .then((res) => {
        const one = res.data.decisions.find((d) => d.decision_id === decisionId)?.recorded
        keep(one && Array.isArray(one.hypotheses) ? one : 'failed')
      })
      // a failure is shown, but asked again when the step is next opened
      .catch(() => { asked.current.delete(decisionId); keep('failed') })
  }, [runId, decisionId])
  return decisionId === null ? undefined : held[decisionId]
}

interface Standing { hypothesis_id: string; statement: string; status: string; row?: HuntStanding }

function HuntExplanations({ hunt, iteration, last, recorded }: { hunt: HuntView; iteration: number; last: boolean; recorded: Recorded | undefined }) {
  const evidence = hunt.evidence ?? []
  const byId = new Map(hunt.hypotheses.map((h) => [h.hypothesis_id, h]))
  let standings: Standing[]
  if (last) standings = hunt.hypotheses.map((h) => ({ ...h, row: h }))
  else if (recorded === undefined) return <NoData>Loading what the lead was shown at this step…</NoData>
  else if (recorded === 'failed') return <NoData>Couldn’t read the explanations as of this step.</NoData>
  else standings = recorded.hypotheses.map((h) => ({ ...h, row: byId.get(h.hypothesis_id) }))
  if (standings.length === 0) return <NoData>No explanations on the board yet.</NoData>

  const tallies = bearings(evidence.filter((e) => e.iteration <= iteration))
  // the benign account is what the others are measured against, so it comes last
  const ordered = [...standings].sort((a, b) => Number(a.row?.provenance === 'base_rate') - Number(b.row?.provenance === 'base_rate'))
  // the evidence list is capped; the newest step reads the full totals the projection counted
  const partial = !last && evidence.length < hunt.evidence_count
  return (
    <>
      {ordered.map((h) => {
        const seen = tallies.get(h.hypothesis_id)
        const forN = last && h.row?.supports !== undefined ? h.row.supports : (seen?.supports ?? 0)
        const against = last && h.row?.weakens !== undefined ? h.row.weakens : (seen?.weakens ?? 0)
        const color = hypothesisColor(h.status)
        const tag = provenanceTag(h.row?.provenance)
        return (
          <div key={h.hypothesis_id} className="flex flex-col gap-2 px-3.5 py-3 rounded-[12px] bg-[var(--bg2)] border border-[var(--ln0)]">
            <span className="flex items-start justify-between gap-2.5">
              <span
                className={`min-w-0 text-[13px] font-[650] leading-[1.35] text-[var(--tx0)] line-clamp-2 break-words ${h.status === 'disproven' ? 'line-through' : ''}`}
                title={h.statement}
              >
                {h.statement}
              </span>
              <span className="text-[12px] font-bold whitespace-nowrap" style={{ color }}>{h.status}</span>
            </span>
            <Bar pct={forN + against === 0 ? 0 : Math.max(4, share(forN, forN + against))} color={color} size={6} />
            <span className="flex justify-between gap-2 text-[11px] text-[var(--tx2)]">
              <span className="whitespace-nowrap">{forN} for · {against} against</span>
              {tag && <span className="font-bold text-[var(--vio)] text-right" title={tag.title}>{tag.text}</span>}
            </span>
          </div>
        )
      })}
      {partial && <span className="text-[11px] text-[var(--tx2)]">Counted over the {evidence.length} most recent records.</span>}
    </>
  )
}

function scopeText(scope?: Record<string, unknown>): string | null {
  const tenant = typeof scope?.tenant === 'string' && scope.tenant ? `tenant ${scope.tenant}` : null
  const one = scope?.entity as { type?: unknown; value?: unknown } | undefined
  const entity = typeof one?.value === 'string' && one.value ? (typeof one.type === 'string' ? `${one.type} ${one.value}` : one.value) : null
  // approved scope extensions are appended here
  const more = Array.isArray(scope?.entities) ? scope.entities.filter((e): e is string => typeof e === 'string' && e !== '' && e !== entity) : []
  return [tenant, entity, ...more].filter(Boolean).join(' · ') || null
}

type ScopeState = 'within scope' | 'at limit' | 'extended'

/** No scope_extension checkpoint is within scope, a raised one is at its limit, an approved one extended it.
 *  The record says whether one was answered, not when, so an answer counts from the step after it was raised. */
function scopeState(hunt: HuntView, iteration: number, last: boolean): ScopeState {
  const raised = (hunt.report?.checkpoints ?? []).filter((c) => c.class === 'scope_extension' && (c.raised_iteration ?? 0) <= iteration)
  if (raised.some((c) => c.resolution?.answer === 'approve' && (last || (c.raised_iteration ?? 0) < iteration))) return 'extended'
  if (raised.length > 0 || (last && hunt.open_checkpoint?.checkpoint_class === 'scope_extension')) return 'at limit'
  return 'within scope'
}
const SCOPE_BAR: Record<ScopeState, { pct: number; color: string }> = {
  'within scope': { pct: 60, color: 'var(--good)' },
  'at limit': { pct: 100, color: 'var(--poor)' },
  extended: { pct: 100, color: 'var(--fair)' },
}

function huntRows(hunt: HuntView, iteration: number, last: boolean, recorded: Recorded | undefined): LimitRow[] {
  const grant = hunt.budgets
  const digest = recorded === undefined || recorded === 'failed' ? null : recorded.budget_remaining
  // the newest step reads what the run has spent; an earlier one, what the lead was shown it had left
  const spent = last ? hunt.cost_usd : grant && digest ? grant.max_cost_usd - digest.cost_usd : undefined
  const used = last ? hunt.iteration : grant && digest ? grant.max_iterations - digest.iterations : undefined
  const unread = recorded === undefined ? '…' : 'Not recorded for this step.'

  const budget: LimitRow = spent !== undefined && grant
    ? { label: 'Budget', value: <><Cost usd={spent} /> of {fmtCost(grant.max_cost_usd)}</>, pct: share(spent, grant.max_cost_usd) }
    : spent !== undefined ? { label: 'Budget', value: <><Cost usd={spent} /> spent</> }
    : digest ? { label: 'Budget', value: <><Cost usd={digest.cost_usd} /> left</> } : { label: 'Budget', value: unread }
  const steps: LimitRow = used !== undefined && grant
    ? { label: 'Steps', value: `${used} of ${grant.max_iterations}`, pct: share(used, grant.max_iterations) }
    : used !== undefined ? { label: 'Steps', value: `${used} so far` }
    : digest ? { label: 'Steps', value: `${digest.iterations} left` } : { label: 'Steps', value: unread }

  const state = scopeState(hunt, iteration, last)
  const where = scopeText(hunt.scope)
  const scope: LimitRow = { label: 'Scope', value: `${where ?? 'Not recorded'}${state === 'within scope' ? '' : ` (${state})`}`, ...SCOPE_BAR[state] }
  return [budget, steps, scope]
}

/** The newest verdict at or before the step, as the critic wrote it: model text, not a finding. */
function Reviewer({ hunt, iteration }: { hunt: HuntView; iteration: number }) {
  const seen = (hunt.reviews ?? []).filter((r) => r.iteration <= iteration)
  const latest = seen[seen.length - 1]
  if (!latest) return <NoData>Not asked yet.</NoData>
  const argued = hunt.hypotheses.find((h) => h.hypothesis_id === latest.hypothesis_id)?.statement ?? latest.hypothesis_id
  return (
    <Note>
      <span className="line-clamp-2 break-words" title={argued}>Reviewed: {argued}</span>
      <span className="text-[11px] uppercase tracking-[0.06em] text-[var(--tx2)]">model text</span>
      <span className="break-words" title={latest.strongest_benign_explanation}>Strongest innocent explanation: {latest.strongest_benign_explanation || '—'}</span>
      <span className="font-bold text-[var(--tx0)]">{latest.survives ? 'Stood' : 'Did not stand'}</span>
    </Note>
  )
}

function huntSpots(hunt: HuntView, iteration: number) {
  const found: HuntEvidence[] = hunt.evidence ?? []
  const detail = new Map(found.map((e) => [e.evidence_id, e.gap_detail]))
  const gaps = (hunt.report?.gaps ?? found.filter((e) => e.is_gap).map(liveGap)).filter((g) => g.iteration <= iteration)
  const lines = new Map<string, { main: string; sub: string[] }>()
  for (const g of gaps) {
    const sub = [g.query_intent ? g.summary : null, detail.get(g.evidence_id)].filter((s): s is string => !!s)
    const main = g.query_intent || g.summary
    lines.set(main + sub.join('|'), { main, sub })
  }
  return [...lines.values()]
}

/** Both right-hand columns for a hunt at one step. `last` is the newest step, which reads the live projection. */
export function HuntPanels({ runId, hunt, iteration, decisionId, last }: { runId: string; hunt: HuntView; iteration: number; decisionId: string; last: boolean }) {
  const recorded = useRecorded(runId, last ? null : decisionId)
  return (
    <Columns
      explanations={<><HuntExplanations hunt={hunt} iteration={iteration} last={last} recorded={recorded} /><OpenCheckpoint hunt={hunt} /></>}
      rows={huntRows(hunt, iteration, last, recorded)}
      reviewer={<Reviewer hunt={hunt} iteration={iteration} />}
      blind={<Spots lines={huntSpots(hunt, iteration)} empty="None so far." />}
    />
  )
}

// ── the other kinds ──────────────────────────────────────────────────────

const NO_EXPLANATIONS = <NoData>This kind of run tests no explanations.</NoData>
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const budgetRow = (spent: number | null, max: number | null): LimitRow =>
  spent === null && max === null ? untracked('Budget')
    : max === null ? { label: 'Budget', value: <><Cost usd={spent} /> spent</> }
    : { label: 'Budget', value: <>{spent === null ? '…' : <Cost usd={spent} />} of {fmtCost(max)}</>, ...(spent === null ? {} : { pct: share(spent, max) }) }

/** An investigate run at one step. `costs` are the replay's per-decision spend, so the budget follows the step. */
export function InvestigatePanels({ d, costs, at }: { d: WfRunDetail; costs: number[]; at: number }) {
  const view = (d.projection ?? {}) as { budgets?: { max_cost_usd?: unknown } | null; cost_usd?: unknown; gaps?: unknown }
  const last = at >= costs.length - 1
  // the newest step reads what the run has priced; an earlier one, what its decisions cost so far
  const spent = last && num(view.cost_usd) !== null ? num(view.cost_usd) : costs.slice(0, at + 1).reduce((sum, c) => sum + c, 0)
  const n = at + 1
  // a failed dispatch carries no iteration, so these are the run's whole list at every step
  const gaps = (Array.isArray(view.gaps) ? view.gaps : []) as { agent_id?: string; failure_reason?: string | null; query_intent?: string }[]
  const lines = gaps.map((g) => ({ main: g.query_intent || g.agent_id || 'A dispatch failed', sub: [g.failure_reason].filter((s): s is string => !!s) }))
  return (
    <Columns
      explanations={NO_EXPLANATIONS}
      rows={[budgetRow(spent, num(view.budgets?.max_cost_usd)), { label: 'Steps', value: `${n} decision${n === 1 ? '' : 's'} · no step limit on this kind` }, untracked('Scope')]}
      reviewer={<NoData>{NO_REVIEWER}</NoData>}
      blind={<Spots lines={lines} empty="None so far." note="Sources this deployment lacks are not recorded for investigations yet." />}
    />
  )
}

/** Root cause and compose have no steps to follow, so these read the run as it stands. */
export function OtherPanels({ d }: { d: WfRunDetail }) {
  const view = (d.projection ?? {}) as { run_kind?: unknown; cost_usd?: unknown; max_cost_usd?: unknown; notices?: unknown; recent_searches?: unknown }
  if (view.run_kind !== 'root_cause') {
    return (
      <Columns
        explanations={NO_EXPLANATIONS}
        rows={[untracked('Budget'), untracked('Steps'), untracked('Scope')]}
        reviewer={<NoData>{NO_REVIEWER}</NoData>}
        blind={<NoData>Not recorded for this kind of run yet.</NoData>}
      />
    )
  }
  const notices = (Array.isArray(view.notices) ? view.notices : []).filter((t): t is string => typeof t === 'string' && t !== '')
  const failed = (Array.isArray(view.recent_searches) ? view.recent_searches : []) as { tool?: string; args?: string; failed?: boolean }[]
  const lines = [
    ...notices.map((main) => ({ main, sub: [] as string[] })),
    ...failed.filter((s) => s.failed).map((s) => ({ main: `A search failed: ${s.tool ?? 'search'}`, sub: s.args ? [s.args] : [] })),
  ]
  return (
    <Columns
      explanations={NO_EXPLANATIONS}
      rows={[budgetRow(num(view.cost_usd), num(view.max_cost_usd)), untracked('Steps'), untracked('Scope')]}
      reviewer={<NoData>{NO_REVIEWER}</NoData>}
      blind={<Spots lines={lines} empty="None so far." />}
    />
  )
}
