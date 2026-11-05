import { useEffect, useState } from 'react'
import type { ConsoleScreenProps } from '../../shared/types'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi } from '../../services/api'
import { Empty, Failure, Stat, Tag, type TagTone } from '../shared/x1'
import './detections.css'

type RuleHealth = 'fails_to_run' | 'data_silent' | 'disabled' | 'never_fired' | 'fires'

interface RuleRow {
  rule_id: number
  name: string
  language: string
  tool: string
  techniques: string[]
  health: RuleHealth
  why: string
}

interface RulesReport {
  /** Rows, one per stored copy. */
  total: number
  /** Rules, each counted once however many SIEM copies it has. */
  rule_total: number
  mapped: number
  counts: Record<RuleHealth, number>
  rules: RuleRow[]
  limits: string
}

const HEALTH: Record<RuleHealth, { label: string; tone: TagTone | undefined }> = {
  fails_to_run: { label: 'Fails to run', tone: 'poor' },
  data_silent: { label: 'Data silent', tone: 'poor' },
  disabled: { label: 'Disabled', tone: 'fair' },
  never_fired: { label: 'Never fired', tone: 'fair' },
  fires: { label: 'Fires', tone: 'good' },
}

/** The shell heads the page (TITLES); this is Alex's Existing rules view under it. */
export default function DetectionsScreen({ goSettings }: ConsoleScreenProps) {
  return (
    <div className="x1">
      <RulesSection goSettings={goSettings} />
    </div>
  )
}

function RulesSection({ goSettings }: Pick<ConsoleScreenProps, 'goSettings'>) {
  const [loaded, setLoaded] = useState<{ report: RulesReport; filter: RuleHealth | 'all' } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<RuleHealth | 'all'>('all')

  // A chip asks for its own rows: the listing is capped, so filtering the capped rows here could miss them all.
  useEffect(() => {
    let live = true
    detectionRulesApi.rules(filter === 'all' ? undefined : filter)
      .then((res) => { if (live) { setLoaded({ report: res.data as RulesReport, filter }); setError(null) } })
      .catch((e) => { if (live) setError(errorText(e, 'Rule health did not load.')) })
    return () => { live = false }
  }, [filter])

  const report = loaded?.report
  if (error) return <Failure>{error}</Failure>
  if (!report) return <span className="x1-muted">Reading the inventory…</span>
  if (report.total === 0) {
    return (
      <Empty title="The inventory is empty">
        Add a rule repository in <button type="button" className="x1-link" onClick={() => goSettings('data')}>Settings</button>, or enable the Elastic, Splunk or Wazuh integration. Vigil reads rules from those and tags them from what they declare.
      </Empty>
    )
  }
  // until the chip's own rows arrive, the last ones stay, filtered
  const rows = loaded?.filter === filter ? report.rules : report.rules.filter((r) => filter === 'all' || r.health === filter)
  const of = filter === 'all' ? report.total : report.counts[filter]
  return (
    <div className="de-split">
      <div style={{ display: 'grid', gap: 10 }}>
        <Stat value={report.rule_total} label={report.total > report.rule_total ? `rules in the inventory, stored as ${report.total} copies` : 'rules in the inventory'} />
        <Stat value={report.mapped} label="declare an ATT&CK technique" />
        <div className="x1-card x1-saved">
          <h3 style={{ marginBottom: 4 }}>Health</h3>
          <button type="button" className={filter === 'all' ? 'on' : ''} onClick={() => setFilter('all')}>All · {report.total}</button>
          {(Object.keys(HEALTH) as RuleHealth[]).map((h) => (
            <button key={h} type="button" className={filter === h ? 'on' : ''} onClick={() => setFilter(h)}>{HEALTH[h].label} · {report.counts[h]}</button>
          ))}
        </div>
      </div>
      <div style={{ display: 'grid', gap: 10, minWidth: 0 }}>
        <div className="x1-table-wrap">
          <table className="x1-table" data-testid="de-rules">
            <thead><tr><th>Rule</th><th>Technique</th><th>Where</th><th>Health</th><th>Why</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.rule_id}>
                  <td style={{ color: 'var(--tx0)' }}>{r.name}</td>
                  <td className="x1-mono">{r.techniques.join(', ') || 'None declared'}</td>
                  <td>{r.tool} · {r.language}</td>
                  <td><Tag tone={HEALTH[r.health].tone}>{HEALTH[r.health].label}</Tag></td>
                  <td>{r.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {of > rows.length && <p className="de-note">Showing the worst {rows.length} of {of}. The counts cover them all.</p>}
        <p className="de-note">{report.limits}</p>
      </div>
    </div>
  )
}
