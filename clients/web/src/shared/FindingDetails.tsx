// What a finding is about, under its source evidence: the AI read of it, the
// ATT&CK techniques, and the entities and indicators it names. Everything here
// is read from the finding itself.
import { useEffect, useState } from 'react'
import { findingsApi } from '../services/api'
import { NO_CONFIDENCE } from '../data/data'
import { rankTechniques } from '../data/mappers'
import { techniqueName } from '../data/mitre'
import { useFindingEnrichment, type EnrichError, type Enrichment } from './useFindingEnrichment'

interface FullFinding {
  finding_id: string
  entity_context?: Record<string, unknown> | null
  mitre_predictions?: Record<string, unknown> | null
  ai_enrichment?: Enrichment | null
}

const INDICATOR_LABEL: Record<string, string> = {
  malicious_ips: 'IPs',
  suspicious_domains: 'Domains',
  suspicious_users: 'Users',
  suspicious_processes: 'Processes',
}

const ENRICH_ERROR: Record<EnrichError, string> = {
  not_configured: 'No AI model is set up for finding analysis.',
  budget: 'The AI spending limit is used up.',
  forbidden: 'Your role can’t generate AI analysis.',
  failed: 'The AI summary could not be generated right now.',
}

const words = (key: string): string => {
  const s = key.replace(/[_.]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Flat label/value rows from an entity context, nested objects as "Parent · child". */
const PLURAL: Record<string, string> = { hostnames: 'hostname', src_ips: 'src_ip', usernames: 'username', dest_ips: 'dest_ip' }

const entityRows = (ctx: Record<string, unknown> | null | undefined, prefix = ''): [string, string][] => {
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

const FindingDetails = ({ findingId }: { findingId: string }) => {
  const [finding, setFinding] = useState<FullFinding | null>(null)
  const { enrichment, phase, error, seed, load } = useFindingEnrichment(findingId)

  useEffect(() => {
    let cancelled = false
    setFinding(null)
    // Through a resolved promise, so an API without this call (an older server, a test double) only skips the panel.
    Promise.resolve()
      .then(() => findingsApi.getById(findingId))
      .then((r) => {
        if (cancelled || !r?.data) return
        const data = r.data as FullFinding
        setFinding(data)
        seed(data.ai_enrichment)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [findingId, seed])

  if (!finding) return null
  const ai = phase === 'ready' ? enrichment : null
  const techniques = rankTechniques(finding.mitre_predictions).slice(0, 4)
  const names = new Map((ai?.related_techniques ?? []).map((t) => [t.technique_id, t.technique_name]))
  const entities = entityRows(finding.entity_context)
  const indicators = Object.entries(ai?.indicators ?? {}).filter(([, v]) => Array.isArray(v) && v.length > 0) as [string, string[]][]

  return (
    <div className="fd">
      <section className="fd-sec">
        <h4>AI summary</h4>
        {ai ? (
          <>
            <p>{ai.threat_summary || 'The analysis came back without a summary.'}</p>
            <div className="fd-chips">
              {ai.threat_type && <span className="chip">{ai.threat_type}</span>}
              {ai.risk_level && <span className="chip">Risk: {ai.risk_level}</span>}
            </div>
            {ai.recommended_actions && ai.recommended_actions.length > 0 && (
              <ul className="fd-list">{ai.recommended_actions.slice(0, 3).map((a, i) => <li key={i}>{a}</li>)}</ul>
            )}
          </>
        ) : (
          <p className="fd-muted">
            <button type="button" className="btn ghost" onClick={() => load()} disabled={phase === 'loading'}>{phase === 'loading' ? 'Reading the finding…' : 'Summarise with AI'}</button>
            {phase === 'error' && error && <span> {ENRICH_ERROR[error]}</span>}
          </p>
        )}
      </section>

      {techniques.length > 0 && (
        <section className="fd-sec">
          <h4>ATT&amp;CK</h4>
          <div className="fd-chips">
            {techniques.map(([id, conf]) => {
              const name = names.get(id) || techniqueName(id)
              return (
                <a key={id} className="chip" href={`https://attack.mitre.org/techniques/${id.replace('.', '/')}/`} target="_blank" rel="noreferrer">
                  {id}{name && name !== id ? ` · ${name}` : ''} · {conf === null ? NO_CONFIDENCE : `${Math.round(conf * 100)}%`}
                </a>
              )
            })}
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
    </div>
  )
}

export default FindingDetails
