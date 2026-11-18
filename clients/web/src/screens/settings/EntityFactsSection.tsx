import { useEffect, useState } from 'react'
import { entityFactsApi, type EntityFacts } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { utcDayClock } from '../../shared/utc'

type Limit = keyof EntityFacts['limits'] & string

const LIMITS: { field: Limit; title: string; hint: string; unit: string }[] = [
  { field: 'ttl_hours', title: 'Kept for', hint: 'A row is fetched again after this, and the cleanup sweep deletes it', unit: 'h' },
  { field: 'max_rows', title: 'Rows held at most', hint: 'At the cap, the oldest rows go first', unit: '' },
  { field: 'max_keys', title: 'Entities per read', hint: 'Users and hosts looked up for one finding', unit: '' },
  { field: 'calls_per_minute', title: 'Calls a minute', hint: 'Per source; past it, the rest of the read goes unanswered', unit: '' },
  { field: 'budget_seconds', title: 'Time per read', hint: 'Triage waits no longer than this, connecting included', unit: 's' },
  { field: 'failure_skip_seconds', title: 'Pause after a failure', hint: 'A source that failed is not asked again for this long', unit: 's' },
]

// The kinds a read records; the source's own error text is never kept.
const REASONS: Record<string, string> = {
  no_client: 'No MCP client',
  not_configured: 'Not set up',
  not_enabled: 'Turned off',
  timeout: 'Timed out',
  connect_failed: 'Could not connect',
  rate_limited: 'Over its call limit',
  no_tool: 'Missing its lookup tool',
  skipped: 'Paused after a failure',
}
const reason = (kind: string) => REASONS[kind] ?? kind.replace(/_/g, ' ')

// Times are naive UTC (core/time.py): read them as UTC.
const zoned = (iso: string) => (/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`)
const ms = (iso?: string | null) => (iso ? Date.parse(zoned(iso)) : NaN)
const utc = (iso?: string | null) => (iso ? utcDayClock(zoned(iso)) : null)
function span(from: number, to: number) {
  const m = Math.max(0, Math.round((to - from) / 60000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return h < 48 ? `${h}h${String(m % 60).padStart(2, '0')}m` : `${Math.floor(h / 24)}d`
}

/** Settings › Entity facts (#4743): what Vigil holds from Entra, Okta and Falcon about the
 * users and hosts in findings (GET /api/v1/entity-facts). Read only: limits are raised through
 * the entity_facts.limits system config, and rows leave by TTL or case close. */
export default function EntityFactsSection() {
  const [data, setData] = useState<EntityFacts | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    entityFactsApi.get()
      .then((res) => { if (live) setData(res.data) })
      .catch((e) => { if (live) setFailed(errorText(e, 'Could not load the entity facts. Reload to try again.')) })
    return () => { live = false }
  }, [])

  const now = ms(data?.now)
  const ago = (iso?: string | null) => (iso ? `${span(ms(iso), now)} ago` : '—')
  const window = data ? `last ${data.window_hours}h` : ''

  return (
    <SettingsCard
      wide
      title="What Vigil holds"
      desc={'Triage looks up a finding’s users and hosts in Entra, Okta and Falcon and keeps only the fields each source’s allowlist names. ' +
        'Rows on a case you can’t read are left out here, from the rows and from every count.'}
    >
      {failed && (
        <div className="settings-banner err">
          <Icon name="alert" size={14} />
          <span>{failed}</span>
        </div>
      )}
      {!data && !failed && <p className="text-sm text-tx-3">Loading…</p>}
      {data && (
        <div className="flex flex-col gap-5">
          <div className="table-wrap">
            <table className="tbl ef-table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th className="num">Rows</th>
                  <th>Oldest</th>
                  <th>Newest</th>
                  <th className="num">Hit rate</th>
                  <th>Last failure</th>
                </tr>
              </thead>
              <tbody>
                {data.sources.map((s) => {
                  const asked = s.served + s.fetched + s.unavailable
                  return (
                    <tr key={s.source}>
                      <td className="ef-src"><b>{s.label}</b></td>
                      <td className="num mono">{s.rows.toLocaleString()}</td>
                      <td className="mono">{ago(s.oldest_fetched_at)}</td>
                      <td className="mono">{ago(s.newest_fetched_at)}</td>
                      <td className="num mono" title={`${s.served} served, ${s.fetched} fetched, ${s.unavailable} unanswered`}>
                        {s.hit_rate == null ? '—' : `${Math.round(s.hit_rate * 100)}%`}
                        {asked > 0 && <span className="text-tx-3"> · {s.served}/{asked}</span>}
                      </td>
                      <td className="ef-failure">
                        {s.last_failure
                          ? <><span className="ef-fail">{reason(s.last_failure.reason)}</span><span className="mono text-tx-3" title={utc(s.last_failure.at) ?? undefined}>{ago(s.last_failure.at)}</span></>
                          : <span className="text-tx-3">None</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="lim-foot">
            Hit rate and last failure cover the {window}. Hit rate: lookups served from a held row, out of every lookup triage made. A failure shows its kind only; the source’s error text is not kept.
          </p>

          <div>
            <h4 className="ef-sub">Limits in force</h4>
            {LIMITS.map((l) => {
              const value = data.limits[l.field]
              const d = data.defaults[l.field]
              return (
                <div className="lim-row ef-lim" key={l.field}>
                  <span className="lim-row-title">{l.title}<span>{l.hint}</span></span>
                  <span className="lim-row-tag">{value === d ? 'Default' : `Changed · default ${d.toLocaleString()}${l.unit}`}</span>
                  <span className="mono ef-val">{value.toLocaleString()}{l.unit}</span>
                </div>
              )
            })}
            <p className="lim-foot">Raised through the <span className="mono">entity_facts.limits</span> system config; see docs/entity-facts.md.</p>
          </div>

          <div>
            <h4 className="ef-sub">Rows held</h4>
            <div className="table-wrap">
              <table className="tbl ef-table">
                <thead>
                  <tr>
                    <th>Entity</th><th>Source</th><th>Case</th><th>Fields</th><th>Fetched</th><th>Expires</th><th className="num">Served</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.length === 0 && (
                    <tr><td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>Nothing held.</td></tr>
                  )}
                  {data.rows.map((r) => {
                    const names = Object.keys(r.fields ?? {})
                    const left = ms(r.expires_at) - now
                    const label = data.sources.find((s) => s.source === r.source)?.label ?? r.source
                    return (
                      <tr key={`${r.entity_key}|${r.source}|${r.case_id ?? ''}`}>
                        <td className="mono ef-key">{r.entity_key}</td>
                        <td>{label}</td>
                        <td className="mono">{r.case_id ?? <span className="text-tx-3">Triage</span>}</td>
                        <td className="ef-fields">{names.slice(0, 3).join(', ')}{names.length > 3 && <span className="text-tx-3"> +{names.length - 3}</span>}</td>
                        <td className="mono">{ago(r.fetched_at)}</td>
                        <td className="mono">{left > 0 ? `in ${span(now, ms(r.expires_at))}` : <span className="text-tx-3">expired, next sweep</span>}</td>
                        <td className="num mono">{r.hit_count.toLocaleString()}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            {data.rows_total > data.rows.length && (
              <p className="lim-foot">The newest {data.rows.length.toLocaleString()} of {data.rows_total.toLocaleString()} rows.</p>
            )}
          </div>
        </div>
      )}
    </SettingsCard>
  )
}
