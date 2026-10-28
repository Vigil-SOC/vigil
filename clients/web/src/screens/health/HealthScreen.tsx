import type { ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import { Hbars, Pie } from '../../shared/charts'
import type { ConsoleScreenProps } from '../../shared/types'
import { usePendingApprovals } from '../decisions/useDecisions'
import {
  PROBE_OUTCOMES, PROBE_TALLY_DAYS, RUNS_PER_WORKFLOW, RUN_STATUSES, useProbeScores, useRunOutcomes,
  type ProbeOutcome, type ProbeScores, type ProbeSummary, type RunKindOutcomes, type RunStatus,
} from './useHealth'

const STATUS_COLOR: Record<RunStatus, string> = {
  completed: 'var(--ok)',
  failed: 'var(--crit)',
  cancelled: 'var(--tx-faint)',
  running: 'var(--accent)',
  paused: 'var(--high)',
  other: 'var(--med)',
}

// a probe that missed or went silent is the problem this card exists to show
const PROBE_COLOR: Record<ProbeOutcome, string> = {
  hit: 'var(--ok)',
  miss: 'var(--crit)',
  silent: 'var(--high)',
}

const fmtSeconds = (s: number | null) => (s == null ? '—' : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${(s / 60).toFixed(1)}m` : `${(s / 3600).toFixed(1)}h`)

/** Approvals waiting, recent run outcomes, and known-answer probe scores —
 *  the facts the Grafana sibling shows, read from the JSON routes the console
 *  already uses. Allocation drift is deliberately absent until it is real. */
export default function HealthScreen({ go }: ConsoleScreenProps) {
  const approvals = usePendingApprovals()
  const runs = useRunOutcomes()
  const probes = useProbeScores()

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="flex-1" />
        <button
          className="btn ghost icon"
          title="Refresh"
          aria-label="Refresh"
          onClick={() => { approvals.reload(); runs.reload(); probes.reload() }}
        >
          <Icon name="refresh" />
        </button>
      </div>

      <div className="px-[22px] pt-5 pb-3">
        <Card title="Approvals waiting" note="runs parked on a person">
          {approvals.phase === 'loading' && <EmptyState loading compact icon="clock" title="Loading approvals…" />}
          {approvals.phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load approvals" body={approvals.error} primary={{ label: 'Retry', onClick: approvals.reload, icon: 'refresh' }} />}
          {/* the hook keeps the last good count through a failed poll; say so */}
          {approvals.phase === 'ready' && approvals.error && (
            <p className="text-xs text-high mb-2" role="status">Last refresh failed — showing the previous count.</p>
          )}
          {approvals.phase === 'ready' && (
            approvals.actions.length === 0 ? (
              <EmptyState compact icon="check" title="Nothing waiting" body="No workflow run is parked on an approval." />
            ) : (
              <div className="flex flex-col gap-3">
                <div className="flex items-baseline gap-2.5">
                  <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-high">{approvals.actions.length}</span>
                  <span className="text-xs text-tx-faint">pending</span>
                </div>
                <button className="btn primary self-start" onClick={() => go('decisions', { search: '?tab=approvals' })}>
                  <Icon name="brain" /> Open approvals
                </button>
              </div>
            )
          )}
        </Card>
      </div>

      <div className="px-[22px] pb-6">
        <Card title="Recent workflow runs by kind" note={`most recent ${RUNS_PER_WORKFLOW} runs per workflow, not lifetime totals`}>
          {runs.phase === 'loading' && <EmptyState loading compact icon="flow" title="Loading runs…" />}
          {runs.phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load runs" body={runs.error} primary={{ label: 'Retry', onClick: runs.reload, icon: 'refresh' }} />}
          {runs.phase === 'ready' && runs.unread.length > 0 && (
            <p className="text-xs text-high mb-3" role="status">
              Runs for {runs.unread.join(', ')} couldn’t be read and are missing from these counts.
            </p>
          )}
          {runs.phase === 'ready' && (
            runs.rows.every((r) => r.total === 0) ? (
              <EmptyState compact icon="flow" title="No runs yet" body="Outcomes by kind appear once a workflow has run." />
            ) : (
              <RunsBody rows={runs.rows.filter((r) => r.total > 0)} />
            )
          )}
        </Card>
      </div>

      <div className="px-[22px] pb-6">
        <Card title="Known-answer probes" note="synthetic findings the daemon triages daily and grades against a known answer">
          {probes.phase === 'loading' && <EmptyState loading compact icon="shield" title="Loading probes…" />}
          {probes.phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load probes" body={probes.error} primary={{ label: 'Retry', onClick: probes.reload, icon: 'refresh' }} />}
          {probes.phase === 'ready' && (
            probes.probes.length === 0 ? (
              <EmptyState compact icon="shield" title="No probes have run yet" body="The daemon’s hourly probe_sweep injects three probes a day and scores each an hour later; results appear here once it has run." />
            ) : (
              <ProbesBody data={probes} />
            )
          )}
        </Card>
      </div>
    </>
  )
}

function ProbesBody({ data }: { data: ProbeScores }) {
  return (
    <>
      <div className="grid grid-cols-3 gap-3 mb-4">
        {PROBE_OUTCOMES.map((o) => (
          <Kpi key={o} label={`${o} · last ${PROBE_TALLY_DAYS}d`} value={String(data.tally[o])} color={data.tally[o] > 0 ? PROBE_COLOR[o] : undefined} />
        ))}
      </div>
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr><th>Probe</th><th>Outcome</th><th>Verdict</th><th>Expected</th><th>Time to verdict</th></tr>
          </thead>
          <tbody>
            {data.probes.map((p) => <ProbeRow key={p.name} probe={p} />)}
          </tbody>
        </table>
      </div>
    </>
  )
}

function ProbeRow({ probe }: { probe: ProbeSummary }) {
  const { latest, expected } = probe
  const verdict = latest?.verdict
  return (
    <tr>
      <td className="font-mono text-xs">{probe.name}</td>
      <td>
        {probe.awaiting ? (
          <>
            <span className="chip" style={{ color: 'var(--tx-faint)' }}>awaiting score</span>
            {latest && <span className="muted text-xs ml-2">previous: <span style={{ color: PROBE_COLOR[latest.outcome] }}>{latest.outcome}</span></span>}
          </>
        ) : latest && (
          <span className="chip" style={{ color: PROBE_COLOR[latest.outcome] }}>{latest.outcome}</span>
        )}
      </td>
      <td>
        {!latest ? <span className="muted">—</span> : !verdict ? <span className="muted">no triage</span> : (
          <>
            <VerdictPart value={verdict.severity} allowed={expected.severity} /> · <VerdictPart value={verdict.recommended_action} allowed={expected.recommended_action} />
            {verdict.confidence != null && <span className="muted text-xs ml-2">conf {verdict.confidence}</span>}
          </>
        )}
      </td>
      <td className="muted">{expected.severity.join(' | ')} · {expected.recommended_action.join(' | ')}</td>
      <td className="muted">{latest ? fmtSeconds(latest.time_to_verdict_s) : '—'}</td>
    </tr>
  )
}

/** One half of the verdict, red when it falls outside the expected answer. */
function VerdictPart({ value, allowed }: { value?: string | null; allowed: string[] }) {
  if (!value) return <span className="muted">—</span>
  return <span style={{ color: allowed.includes(value) ? 'var(--ok)' : 'var(--crit)' }}>{value}</span>
}

function RunsBody({ rows }: { rows: RunKindOutcomes[] }) {
  const total = rows.reduce((acc, r) => acc + r.total, 0)
  const byStatus = RUN_STATUSES.map((s) => ({ status: s, count: rows.reduce((acc, r) => acc + r.byStatus[s], 0) })).filter((s) => s.count > 0)
  return (
    <div className="grid gap-6 grid-cols-[1fr_auto]">
      <Hbars
        items={rows.map((r) => ({
          label: `${r.runKind} · ${r.workflows} workflow${r.workflows === 1 ? '' : 's'}`,
          val: `${r.byStatus.completed} ok · ${r.byStatus.failed} failed · ${r.total} total`,
          // bar length is the kind's own completion rate; the text carries the counts
          pct: Math.round((r.byStatus.completed / r.total) * 100),
          cls: r.byStatus.failed > r.byStatus.completed ? 'c-crit' : 'c-ok',
        }))}
      />
      <div className="donut-wrap">
        {/* the legend carries the numbers; the pie is decoration for readers */}
        <div aria-hidden="true">
          <Pie segs={byStatus.map((s) => ({ v: s.count / total, color: STATUS_COLOR[s.status], label: s.status }))} size={140} />
        </div>
        <div className="legend">
          {byStatus.map((s) => (
            <div className="li" key={s.status}>
              <span className="sw" style={{ background: STATUS_COLOR[s.status] }} />
              <span className="capitalize">{s.status}</span>
              <span className="v">{s.count}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Card({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <section className="card card-sq">
      <div className="card-h">
        <h3 className="text-[14.5px]">{title}</h3>
        <span className="flex-1" />
        <span className="text-xs text-tx-3">{note}</span>
      </div>
      <div className="card-b">{children}</div>
    </section>
  )
}

function Kpi({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="card card-sq p-3 flex flex-col gap-1">
      <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">{label}</span>
      <span className="text-[22px] font-semibold tracking-[-0.02em]" style={color ? { color } : undefined}>{value}</span>
    </div>
  )
}
