// What a finding is about, under its source evidence: the ATT&CK techniques and
// the entities it names. Everything here is read from the finding itself.
import { useEffect, useState } from 'react'
import { findingsApi } from '../services/api'
import { NO_CONFIDENCE } from '../data/data'
import { rankTechniques } from '../data/mappers'
import { techniqueName } from '../data/mitre'

interface FullFinding {
  finding_id: string
  entity_context?: Record<string, unknown> | null
  mitre_predictions?: Record<string, unknown> | null
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

  useEffect(() => {
    let cancelled = false
    setFinding(null)
    // Through a resolved promise, so an API without this call (an older server, a test double) only skips the panel.
    Promise.resolve()
      .then(() => findingsApi.getById(findingId))
      .then((r) => {
        if (cancelled || !r?.data) return
        setFinding(r.data as FullFinding)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [findingId])

  if (!finding) return null
  const techniques = rankTechniques(finding.mitre_predictions).slice(0, 4)
  const entities = entityRows(finding.entity_context)
  if (techniques.length === 0 && entities.length === 0) return null

  return (
    <div className="fd">
      {techniques.length > 0 && (
        <section className="fd-sec">
          <h4>ATT&amp;CK</h4>
          <div className="fd-chips">
            {techniques.map(([id, conf]) => {
              const name = techniqueName(id)
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
    </div>
  )
}

export default FindingDetails
