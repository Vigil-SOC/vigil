import { useState } from 'react'
import type { DetectionMetrics } from '../../services/api'

const n = (v: number) => v.toLocaleString()
const pct = (v: number) => `${(100 * v).toFixed(v > 0 && v < 0.1 ? 1 : 0)}%`
const dayMonth = (iso: string) => new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
const span = (h: number) => (h < 48 ? `${Math.round(h)} h` : `${(h / 24).toFixed(1)} d`)
const FOUND: Record<'no_data' | 'silent' | 'never_firing', string> = { no_data: 'no data', silent: 'silent', never_firing: 'never firing' }

interface Row { name: string; value: string; tone?: string; line: string; target: string; unknown: string[] }

function rows(m: DetectionMetrics): Row[] {
  const g = m.gap_to_shadow
  const s = m.shadow_to_live
  const h = m.live_health
  const w = m.first_week
  const b = m.search_budget
  const found = w.silent + w.no_data + w.never_firing
  const later = (Object.keys(FOUND) as (keyof typeof FOUND)[])
    .filter((k) => w.first_found_day?.[k] != null)
    .map((k) => `${FOUND[k]} on day ${w.first_found_day[k]}`)
  const budgets = Object.entries(b.integrations ?? {}).map(([id, v]) => `${id} ${n(v.searches_last_hour)} of ${n(v.max_searches_per_hour)} searches this hour`)
  return [
    {
      name: 'Gap to rule in shadow',
      value: g.median_hours != null ? span(g.median_hours) : '—',
      tone: g.median_hours != null ? (g.median_hours <= g.target_hours ? 'good' : 'fair') : undefined,
      line: g.gaps === 0 ? 'No analyst-confirmed gaps in the last 90 days' : `Median of ${n(g.closed)} closed, of ${n(g.gaps)} gaps · ${n(g.within_target)} within a day`,
      target: `Target: under ${g.target_hours} h`,
      unknown: g.unknown,
    },
    {
      name: 'Shadow to live',
      value: s.median_hours != null ? span(s.median_hours) : '—',
      line: s.promoted === 0 ? `No promotions yet · ${n(s.in_shadow)} in shadow` : `${n(s.promoted)} promoted, ${n(s.promoted_by_vigil)} by Vigil · ${n(s.in_shadow)} in shadow`,
      target: `Exit: ${s.exit_criteria}`,
      unknown: s.unknown,
    },
    {
      name: 'Live rules passing',
      value: h.share != null ? pct(h.share) : '—',
      tone: h.share == null ? undefined : h.share >= h.target ? 'good' : 'poor',
      line: `${n(h.passing)} of ${n(h.live_rules)} live rules pass Runs and Sees`,
      target: `Target: ${pct(h.target)} · ${n(h.blind_spots)} blind spots`,
      unknown: h.unknown,
    },
    {
      name: 'First week',
      value: w.week_over || found ? n(found) : '—',
      line: !w.started_at
        ? 'No SIEM rule read yet'
        : `${w.week_over ? 'Found in week one' : 'Found so far in week one'} (${dayMonth(w.started_at)}–${w.ends_at ? dayMonth(w.ends_at) : ''}): ${n(w.silent)} silent, ${n(w.no_data)} no data, ${n(w.never_firing)} never firing${!found && later.length ? `. First found later: ${later.join(', ')}` : ''}`,
      target: 'Silent, no data, never firing',
      unknown: w.unknown,
    },
    {
      name: 'Search budget',
      value: n(b.throttle_errors),
      tone: b.throttle_errors ? 'fair' : 'good',
      line: `Throttle errors in 30 days${budgets.length ? ` · ${budgets.join(' · ')}` : ''}`,
      target: 'Target: zero',
      unknown: b.unknown,
    },
  ]
}

/** The rollout's five success metrics, a card under the stats strip: each a cell with
 * its number, a line in words and its target; "What these can't say" adds each one's
 * caveats, from the API's own unknown list. */
export default function RolloutMetrics({ data, error }: { data: DetectionMetrics | null; error: string | null }) {
  const [caveats, setCaveats] = useState(false)
  return (
    <section className="x1-card de-card de-rollout" aria-labelledby="de-rollout-title">
      <div className="de-card-head">
        <div>
          <h2 id="de-rollout-title">Rollout</h2>
          <p>How Vigil’s rule work is doing, from what Vigil stores</p>
        </div>
        {data && (
          <button type="button" className="x1-link de-rollout-toggle" aria-expanded={caveats} onClick={() => setCaveats((c) => !c)}>
            {caveats ? 'Hide what these can’t say' : 'What these can’t say'}
          </button>
        )}
      </div>
      {error && <p className="de-x-err" role="alert">{error}</p>}
      {!data && !error && <p className="x1-muted de-card-sub">Reading the rollout metrics…</p>}
      {data && (
        <ul className="de-rollout-cells">
          {rows(data).map((r) => (
            <li key={r.name}>
              <span className="de-rollout-name">{r.name}</span>
              <b className={r.tone ?? ''}>{r.value}</b>
              <span className="de-rollout-line">{r.line}</span>
              <span className="de-rollout-target">{r.target}</span>
              {caveats && r.unknown.length > 0 && <span className="de-rollout-cant">Can’t say: {r.unknown.join(' ')}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
