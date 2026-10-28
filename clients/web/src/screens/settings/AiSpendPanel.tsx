/* ============================================================
   Settings · AI models · Spend

   Total cost, calls, tokens, cache hit rate and the per-model table over a
   24h / 7d / 30d / All window. Spend is a Settings readout, not a case readout
   (PRD D11); the caps that bound it live in the Spending limit card and
   Settings › Autonomous investigation.
   ============================================================ */
import { useState } from 'react'
import { EmptyState, SettingsCard } from '../../shared/ui'
import { Cost, PROVENANCE_LABEL, fmtCost, type PricingSource } from '../../shared/cost'
import { useCostAnalytics, type CostData, type CostTimeRange } from './useSettings'

const RANGE_LABEL: Record<CostTimeRange, string> = { '24h': '24h', '7d': '7d', '30d': '30d', all: 'All' }
const RANGES = Object.keys(RANGE_LABEL) as CostTimeRange[]

// colour rides on top of the shared label; the word carries the meaning
const PRICING_COLOR: Record<PricingSource, string> = {
  exact: 'var(--ok)',
  zero: 'var(--med)',
  unknown: 'var(--crit)',
}

const fmtTokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n))
const fmtPct = (n: number) => `${(n * 100).toFixed(1)}%`

export default function AiSpendPanel() {
  const [range, setRange] = useState<CostTimeRange>('7d')
  const cost = useCostAnalytics(range)

  return (
    <SettingsCard
      wide
      title="Spend"
      desc="LLM spend and tokens across chat, enrichment, workflows and autonomous investigations."
      actions={
        <div className="range-tabs">
          {RANGES.map((k) => (
            <button key={k} className={k === range ? 'active' : ''} aria-pressed={k === range} onClick={() => setRange(k)}>{RANGE_LABEL[k]}</button>
          ))}
        </div>
      }
    >
      {cost.phase === 'loading' && <EmptyState loading compact icon="bars" title="Loading spend…" />}
      {cost.phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load spend" body={cost.error} primary={{ label: 'Retry', onClick: cost.reload, icon: 'refresh' }} />}
      {cost.phase === 'ready' && cost.data && (
        cost.data.totals.calls === 0 ? (
          <EmptyState compact icon="bars" title="No LLM traffic in this window" body="Spend and tokens appear after chat, enrichment, workflows, or autonomous investigations use a configured provider." />
        ) : (
          <SpendBody data={cost.data} />
        )
      )}
    </SettingsCard>
  )
}

// Extra text beside a dollar figure; the figure itself covers priced calls only.
const unpricedNote = (n: number | undefined) => (n ? `${n.toLocaleString()} unpriced` : undefined)

function SpendBody({ data }: { data: CostData }) {
  return (
    <>
      <div className="grid grid-cols-4 gap-3 mb-4">
        <Kpi label="Total cost" value={fmtCost(data.totals.cost_usd)} note={unpricedNote(data.totals.unpriced_calls)} accent />
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
              const unpriced = unpricedNote(m.unpriced_calls)
              return (
                <tr key={`${m.provider_type}-${m.model}`}>
                  <td className="font-mono text-xs">{m.model}</td>
                  <td className="muted">{m.provider_type}</td>
                  <td><span className="chip" style={{ color: PRICING_COLOR[source] }}>{PROVENANCE_LABEL[source]}</span></td>
                  <td>{m.calls.toLocaleString()}</td>
                  <td className="muted">{fmtTokens(m.input_tokens)}</td>
                  <td className="muted">{fmtTokens(m.output_tokens)}</td>
                  <td className="muted">{fmtPct(m.cache_hit_rate)}</td>
                  <td style={{ textAlign: 'right' }}>
                    <Cost usd={m.cost_usd} source={source} />
                    {unpriced && <span className="text-tx-faint"> · {unpriced}</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Kpi({ label, value, note, accent }: { label: string; value: string; note?: string; accent?: boolean }) {
  return (
    <div className="card card-sq p-3 flex flex-col gap-1">
      <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">{label}</span>
      <span className="text-[22px] font-semibold tracking-[-0.02em]" style={accent ? { color: 'var(--accent-2)' } : undefined}>{value}</span>
      {note && <span className="text-xs text-tx-faint">{note}</span>}
    </div>
  )
}
