// What a finding is about, under its source evidence: the AI read of it, the
// ATT&CK techniques, the entities and indicators it names, and the recent
// alerts that look like it. Everything here is read from the finding itself.
import { useEffect, useRef, useState } from 'react'
import { findingsApi, type SimilarFinding } from '../services/api'

interface Enrichment {
  threat_summary?: string
  threat_type?: string
  risk_level?: string
  recommended_actions?: string[]
  indicators?: Record<string, string[] | undefined>
  related_techniques?: { technique_id: string; technique_name?: string }[]
}

interface FullFinding {
  finding_id: string
  entity_context?: Record<string, unknown> | null
  mitre_predictions?: Record<string, number> | null
  ai_enrichment?: Enrichment | null
}

const INDICATOR_LABEL: Record<string, string> = {
  malicious_ips: 'IPs',
  suspicious_domains: 'Domains',
  suspicious_users: 'Users',
  suspicious_processes: 'Processes',
}

function words(key: string): string {
  const s = key.replace(/[_.]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Flat label/value rows from an entity context, nested objects as "Parent · child". */
const PLURAL: Record<string, string> = { hostnames: 'hostname', src_ips: 'src_ip', usernames: 'username', dest_ips: 'dest_ip' }

function entityRows(ctx: Record<string, unknown> | null | undefined, prefix = ''): [string, string][] {
  if (!ctx) return []
  const rows: [string, string][] = []
  for (const [k, v] of Object.entries(ctx)) {
    // The raw record has its own panel, and the plural lists repeat the singular fields.
    if (k === 'source_evidence' || (PLURAL[k] && PLURAL[k] in ctx)) continue
    const label = prefix ? `${prefix} · ${words(k)}` : words(k)
    if (v === null || v === undefined || v === '') continue
    if (Array.isArray(v)) {
      const flat = v.filter((x) => typeof x === 'string' || typeof x === 'number')
      if (flat.length) rows.push([label, flat.join(', ')])
    } else if (typeof v === 'object') {
      rows.push(...entityRows(v as Record<string, unknown>, label))
    } else {
      rows.push([label, String(v)])
    }
  }
  return rows.slice(0, 16)
}

export default function FindingDetails({ findingId }: { findingId: string }) {
  const [finding, setFinding] = useState<FullFinding | null>(null)
  const [similar, setSimilar] = useState<SimilarFinding[] | null>(null)
  const [generating, setGenerating] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)
  // The alert on screen now: a summary started for another one must not touch this one's state.
  const current = useRef(findingId)

  useEffect(() => {
    let cancelled = false
    current.current = findingId
    setFinding(null)
    setSimilar(null)
    setGenerating(false)
    setGenError(null)
    // Through a resolved promise, so an API without these calls (an older server, a test double) only skips the section.
    Promise.resolve().then(() => findingsApi.getById(findingId)).then((r) => { if (!cancelled && r?.data) setFinding(r.data as FullFinding) }).catch(() => {})
    Promise.resolve().then(() => findingsApi.similar(findingId, 5)).then((r) => { if (!cancelled) setSimilar(r?.data?.similar ?? []) }).catch(() => { if (!cancelled) setSimilar([]) })
    return () => { cancelled = true }
  }, [findingId])

  const generate = async () => {
    setGenerating(true)
    setGenError(null)
    try {
      const r = await findingsApi.getEnrichment(findingId)
      const enrichment = (r.data as { enrichment?: Enrichment }).enrichment
      // The popup may have moved to another alert while this one was summarised.
      setFinding((f) => (f && f.finding_id === findingId ? { ...f, ai_enrichment: enrichment ?? f.ai_enrichment } : f))
    } catch {
      if (current.current === findingId) setGenError('The AI summary could not be generated right now.')
    } finally {
      if (current.current === findingId) setGenerating(false)
    }
  }

  if (!finding) return null
  const ai = finding.ai_enrichment?.threat_summary ? finding.ai_enrichment : null
  const techniques = Object.entries(finding.mitre_predictions ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 4)
  const names = new Map((ai?.related_techniques ?? []).map((t) => [t.technique_id, t.technique_name]))
  const entities = entityRows(finding.entity_context)
  const indicators = Object.entries(ai?.indicators ?? {}).filter(([, v]) => Array.isArray(v) && v.length > 0) as [string, string[]][]

  return (
    <div className="fd">
      <section className="fd-sec">
        <h4>AI summary</h4>
        {ai ? (
          <>
            <p>{ai.threat_summary}</p>
            <div className="fd-chips">
              {ai.threat_type && <span className="chip">{ai.threat_type}</span>}
              {ai.risk_level && <span className="chip">Risk: {ai.risk_level}</span>}
            </div>
            {ai.recommended_actions && ai.recommended_actions.length > 0 && (
              <ul className="fd-list">{ai.recommended_actions.slice(0, 3).map((a) => <li key={a}>{a}</li>)}</ul>
            )}
          </>
        ) : (
          <p className="fd-muted">
            <button type="button" className="btn ghost" onClick={generate} disabled={generating}>{generating ? 'Reading the finding…' : 'Summarise with AI'}</button>
            {genError && <span> {genError}</span>}
          </p>
        )}
      </section>

      {techniques.length > 0 && (
        <section className="fd-sec">
          <h4>ATT&amp;CK</h4>
          <div className="fd-chips">
            {techniques.map(([id, conf]) => (
              <a key={id} className="chip" href={`https://attack.mitre.org/techniques/${id.replace('.', '/')}/`} target="_blank" rel="noreferrer">
                {id}{names.get(id) ? ` · ${names.get(id)}` : ''} · {Math.round(conf * 100)}%
              </a>
            ))}
          </div>
        </section>
      )}

      {entities.length > 0 && (
        <section className="fd-sec">
          <h4>Entities</h4>
          <dl className="fd-kv">
            {entities.map(([k, v]) => (
              <div key={k}><dt>{k}</dt><dd title={v}>{v}</dd></div>
            ))}
          </dl>
        </section>
      )}

      {indicators.length > 0 && (
        <section className="fd-sec">
          <h4>Indicators</h4>
          <dl className="fd-kv">
            {indicators.map(([k, v]) => (
              <div key={k}><dt>{INDICATOR_LABEL[k] ?? words(k)}</dt><dd>{v.join(', ')}</dd></div>
            ))}
          </dl>
        </section>
      )}

      {similar && similar.length > 0 && (
        <section className="fd-sec">
          <h4>Similar alerts</h4>
          <ul className="fd-sim">
            {similar.map((s) => (
              <li key={s.finding_id}>
                <span className="fd-sim-t" title={s.finding_id}>{s.title || s.finding_id}</span>
                <span className="fd-muted">{s.reasons.join(' · ')}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
