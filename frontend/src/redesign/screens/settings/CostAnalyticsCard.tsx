/* ============================================================
   Settings · General · Cost Analytics — compact, on-theme port of
   pages/CostAnalytics.tsx. KPI row + per-model spend breakdown over a
   selectable time range, fetched from /analytics/cost.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { useCostAnalytics, type CostModelRow, type CostTimeRange } from './useSettings'

const RANGES: [CostTimeRange, string][] = [
  ['24h', '24h'],
  ['7d', '7d'],
  ['30d', '30d'],
  ['all', 'All'],
]

const PRICING_LABEL: Record<CostModelRow['pricing_source'], { label: string; color: string }> = {
  exact: { label: 'exact', color: 'var(--ok)' },
  heuristic: { label: 'heuristic', color: 'var(--high)' },
  zero: { label: 'free', color: 'var(--med)' },
  unknown: { label: 'unknown', color: 'var(--crit)' },
}

const fmtTokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n))
const fmtCost = (n: number) => `$${n.toFixed(2)}`
const fmtPct = (n: number) => `${(n * 100).toFixed(1)}%`

export default function CostAnalyticsCard() {
  const [range, setRange] = useState<CostTimeRange>('7d')
  const { data, phase, error, reload } = useCostAnalytics(range)

  return (
    <SettingsCard
      wide
      title="Cost Analytics"
      desc="LLM spend and token usage across agents and models."
      actions={
        <>
          <div className="range-tabs">
            {RANGES.map(([k, label]) => (
              <button key={k} className={k === range ? 'active' : ''} onClick={() => setRange(k)}>
                {label}
              </button>
            ))}
          </div>
          <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
        </>
      }
    >
      {phase === 'loading' && <div className="text-sm text-tx-3 py-8 text-center">Loading cost analytics…</div>}
      {phase === 'error' && (
        <div className="py-8 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load cost analytics: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      )}
      {phase === 'ready' && data && (
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
                {data.by_model.length === 0 && (
                  <tr><td colSpan={8} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>No usage in this window.</td></tr>
                )}
                {data.by_model.map((m) => {
                  const pricing = PRICING_LABEL[m.pricing_source] || PRICING_LABEL.unknown
                  return (
                    <tr key={`${m.provider_type}-${m.model}`}>
                      <td className="font-mono text-xs">{m.model}</td>
                      <td className="muted">{m.provider_type}</td>
                      <td><span className="chip" style={{ color: pricing.color }}>{pricing.label}</span></td>
                      <td>{m.calls.toLocaleString()}</td>
                      <td className="muted">{fmtTokens(m.input_tokens)}</td>
                      <td className="muted">{fmtTokens(m.output_tokens)}</td>
                      <td className="muted">{fmtPct(m.cache_hit_rate)}</td>
                      <td style={{ textAlign: 'right' }}>{fmtCost(m.cost_usd)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </SettingsCard>
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
