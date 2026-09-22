import { useState, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import { Hbars, Pie } from '../../shared/charts'
import { Cost, PROVENANCE_LABEL, fmtCost, type PricingSource } from '../../shared/cost'
import type { ConsoleScreenProps } from '../../shared/types'
import { useCostAnalytics, type CostData, type CostTimeRange } from '../settings/useSettings'
import { usePendingApprovals } from '../decisions/useDecisions'
import { RUNS_PER_WORKFLOW, RUN_STATUSES, useRunOutcomes, type RunKindOutcomes, type RunStatus } from './useHealth'

const RANGE_LABEL: Record<CostTimeRange, string> = { '24h': '24h', '7d': '7d', '30d': '30d', all: 'All' }
const RANGES = Object.keys(RANGE_LABEL) as CostTimeRange[]

// colour rides on top of the shared label; the word carries the meaning
const PRICING_COLOR: Record<PricingSource, string> = {
  exact: 'var(--ok)',
  heuristic: 'var(--high)',
  zero: 'var(--med)',
  unknown: 'var(--crit)',
}

const STATUS_COLOR: Record<RunStatus, string> = {
  completed: 'var(--ok)',
  failed: 'var(--crit)',
  cancelled: 'var(--tx-faint)',
  running: 'var(--accent)',
  paused: 'var(--high)',
  other: 'var(--med)',
}

const fmtTokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n))
const fmtPct = (n: number) => `${(n * 100).toFixed(1)}%`

/** Spend, approvals waiting, and recent run outcomes — the facts the Grafana
 *  sibling shows, read from the JSON routes the console already uses. Probe
 *  scores and allocation drift are deliberately absent until they are real. */
export default function HealthScreen({ go }: ConsoleScreenProps) {
  const [range, setRange] = useState<CostTimeRange>('7d')
  const cost = useCostAnalytics(range)
  const approvals = usePendingApprovals()
  const runs = useRunOutcomes()

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Spend window</span>
        <div className="range-tabs">
          {RANGES.map((k) => (
            <button key={k} className={k === range ? 'active' : ''} aria-pressed={k === range} onClick={() => setRange(k)}>{RANGE_LABEL[k]}</button>
          ))}
        </div>
        <div className="flex-1" />
        <button
          className="btn ghost icon"
          title="Refresh"
          aria-label="Refresh"
          onClick={() => { cost.reload(); approvals.reload(); runs.reload() }}
        >
          <Icon name="refresh" />
        </button>
      </div>

      <div className="px-[22px] pt-5 pb-3 grid gap-3 grid-cols-[1.6fr_1fr]">
        <Card title="LLM spend and tokens" note={`window: ${RANGE_LABEL[range]}`}>
          {cost.phase === 'loading' && <EmptyState loading compact icon="bars" title="Loading spend…" />}
          {cost.phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load spend" body={cost.error} primary={{ label: 'Retry', onClick: cost.reload, icon: 'refresh' }} />}
          {cost.phase === 'ready' && cost.data && (
            cost.data.totals.calls === 0 ? (
              <EmptyState compact icon="bars" title="No LLM traffic in this window" body="Spend and tokens appear after chat, enrichment, workflows, or autonomous investigations use a configured provider." />
            ) : (
              <SpendBody data={cost.data} />
            )
          )}
        </Card>

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
    </>
  )
}

function SpendBody({ data }: { data: CostData }) {
  return (
    <>
      <div className="grid grid-cols-4 gap-3 mb-4">
        <Kpi label="Total cost" value={fmtCost(data.totals.cost_usd)} accent />
        <Kpi label="API calls" value={data.totals.calls.toLocaleString()} />
        <Kpi label="Tokens (in/out)" value={`${fmtTokens(data.totals.input_tokens)} / ${fmtTokens(data.totals.output_tokens)}`} />
        <Kpi label="Cache hit rate" value={fmtPct(data.totals.cache_hit_rate)} />
      </div>
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Model</th><th>Provider</th><th>Pricing</th><th>Calls</th>
              <th>Input</th><th>Output</th><th>Cache hit</th><th style={{ textAlign: 'right' }}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.by_model.map((m) => {
              const source: PricingSource = m.pricing_source in PRICING_COLOR ? m.pricing_source : 'unknown'
              return (
                <tr key={`${m.provider_type}-${m.model}`}>
                  <td className="font-mono text-xs">{m.model}</td>
                  <td className="muted">{m.provider_type}</td>
                  <td><span className="chip" style={{ color: PRICING_COLOR[source] }}>{PROVENANCE_LABEL[source]}</span></td>
                  <td>{m.calls.toLocaleString()}</td>
                  <td className="muted">{fmtTokens(m.input_tokens)}</td>
                  <td className="muted">{fmtTokens(m.output_tokens)}</td>
                  <td className="muted">{fmtPct(m.cache_hit_rate)}</td>
                  <td style={{ textAlign: 'right' }}><Cost usd={m.cost_usd} source={source} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
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

function Kpi({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="card card-sq p-3 flex flex-col gap-1">
      <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">{label}</span>
      <span className="text-[22px] font-semibold tracking-[-0.02em]" style={accent ? { color: 'var(--accent-2)' } : undefined}>{value}</span>
    </div>
  )
}
