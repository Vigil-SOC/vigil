/* ============================================================
   Finding detail popup — opened from a Findings row / the "View"
   eye. Fetches the full finding (findingsApi.getById) and shows
   the normalized fields, description, MITRE predictions and
   extracted entities, plus on-demand AI enrichment
   (findingsApi.getEnrichment) and status / delete actions.
   Ports the production FindingDetailDialog's enrichment surface
   (REDESIGN_GAPS.md §8); the embedded VStrike NetworkContextPanel
   is intentionally not ported — it depends on the VStrike provider
   that isn't mounted under the SOC console shell.
   ============================================================ */
import { useEffect, useState } from 'react'
import { findingsApi } from '../../../services/api'
import { mapApiFinding, type ApiFinding } from '../../data/mappers'
import { techniqueName } from '../../data/mitre'
import { ConfirmDialog, Popup, Select } from '../../shared/ui'
import { Icon } from '../../shared/icons'
import type { Phase } from '../cases/useCases'

interface RawFinding extends ApiFinding {
  description?: string
  cluster_id?: string | null
  ai_enrichment?: { model?: string } | null
}

interface RelatedTechnique {
  technique_id: string
  technique_name: string
  relevance?: string
}
interface Enrichment {
  threat_summary?: string
  threat_type?: string
  risk_level?: string
  confidence_score?: number
  potential_impact?: string
  recommended_actions?: string[]
  investigation_questions?: string[]
  related_techniques?: RelatedTechnique[]
  timeline_context?: string
  business_context?: string
  indicators?: { malicious_ips?: string[]; suspicious_domains?: string[] }
  analysis_notes?: string
}

const RISK_COLOR: Record<string, string> = {
  critical: 'var(--crit)',
  high: 'var(--crit)',
  medium: 'var(--high)',
  low: 'var(--ok)',
}

const STATUS_OPTIONS = [
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'closed', label: 'Closed' },
]

function EnrichmentView({ e }: { e: Enrichment }) {
  return (
    <>
      {e.threat_summary && (
        <div className="modal-section">
          <h4><Icon name="info" size={14} /> Threat summary</h4>
          <p style={{ fontSize: 13, color: 'var(--tx-2)', margin: 0, lineHeight: 1.5 }}>{e.threat_summary}</p>
          {e.threat_type && <span className="tag" style={{ marginTop: 8, display: 'inline-block', color: 'var(--crit)' }}>{e.threat_type}</span>}
        </div>
      )}

      <div className="kv-grid fp-grid">
        {e.risk_level && (
          <>
            <span className="k">Risk level</span>
            <span className="v"><span className="tag" style={{ color: RISK_COLOR[e.risk_level.toLowerCase()] || 'var(--tx-2)' }}>{e.risk_level}</span></span>
          </>
        )}
        {typeof e.confidence_score === 'number' && (
          <>
            <span className="k">AI confidence</span>
            <span className="v mono">{Math.round(e.confidence_score * 100)}%</span>
          </>
        )}
      </div>

      {e.potential_impact && (
        <div className="modal-section">
          <h4><Icon name="alert" size={14} /> Potential impact</h4>
          <p style={{ fontSize: 13, color: 'var(--tx-2)', margin: 0, lineHeight: 1.5 }}>{e.potential_impact}</p>
        </div>
      )}

      {!!e.recommended_actions?.length && (
        <div className="modal-section">
          <h4><Icon name="check2" size={14} /> Recommended actions</h4>
          <ul className="fp-list">{e.recommended_actions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}

      {!!e.investigation_questions?.length && (
        <div className="modal-section">
          <h4>Investigation questions</h4>
          <ul className="fp-list">{e.investigation_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}

      {!!e.related_techniques?.length && (
        <div className="modal-section">
          <h4>Related MITRE techniques</h4>
          <div className="fp-preds">
            {e.related_techniques.map((t, i) => (
              <div className="fp-pred" key={i}>
                <span className="tag">{t.technique_id}</span>
                <span className="fp-pred-name">{t.technique_name}{t.relevance ? ` — ${t.relevance}` : ''}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {(e.timeline_context || e.business_context) && (
        <div className="modal-section">
          <h4>Additional context</h4>
          {e.timeline_context && <p style={{ fontSize: 13, color: 'var(--tx-2)', margin: '0 0 8px', lineHeight: 1.5 }}><strong className="text-tx">Timeline:</strong> {e.timeline_context}</p>}
          {e.business_context && <p style={{ fontSize: 13, color: 'var(--tx-2)', margin: 0, lineHeight: 1.5 }}><strong className="text-tx">Business:</strong> {e.business_context}</p>}
        </div>
      )}

      {(!!e.indicators?.malicious_ips?.length || !!e.indicators?.suspicious_domains?.length) && (
        <div className="modal-section">
          <h4>Indicators of compromise</h4>
          {!!e.indicators?.malicious_ips?.length && (
            <div className="fp-ent-row"><span className="fp-ent-lab">Malicious IPs</span><div className="fp-chips">{e.indicators.malicious_ips.map((ip) => <span className="chip mono" key={ip}>{ip}</span>)}</div></div>
          )}
          {!!e.indicators?.suspicious_domains?.length && (
            <div className="fp-ent-row"><span className="fp-ent-lab">Domains</span><div className="fp-chips">{e.indicators.suspicious_domains.map((d) => <span className="chip mono" key={d}>{d}</span>)}</div></div>
          )}
        </div>
      )}

      {e.analysis_notes && (
        <div className="modal-section">
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}><strong>Analyst notes:</strong> {e.analysis_notes}</p>
        </div>
      )}
    </>
  )
}

export default function FindingPopup({
  id,
  onClose,
  onChanged,
}: {
  id: string | null
  onClose: () => void
  /** called after a status change / delete so the list can refetch */
  onChanged?: () => void
}) {
  const [raw, setRaw] = useState<RawFinding | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  // AI enrichment — on-demand (a getEnrichment call may invoke an LLM, so we
  // don't fire it automatically on open). 'idle' until the user asks for it.
  const [enrichment, setEnrichment] = useState<Enrichment | null>(null)
  const [enrichPhase, setEnrichPhase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [enrichError, setEnrichError] = useState<'not_configured' | 'failed' | null>(null)

  const [status, setStatus] = useState('')
  const [acting, setActing] = useState(false)
  const [confirmDel, setConfirmDel] = useState(false)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setPhase('loading')
    setError(null)
    setRaw(null)
    setEnrichment(null)
    setEnrichPhase('idle')
    setEnrichError(null)
    findingsApi
      .getById(id)
      .then((res) => {
        if (cancelled) return
        const data = res.data as RawFinding
        setRaw(data)
        setStatus((data as { status?: string }).status || '')
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load finding')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id])

  const loadEnrichment = (force = false) => {
    if (!id) return
    setEnrichPhase('loading')
    setEnrichError(null)
    findingsApi
      .getEnrichment(id, force)
      .then((res) => {
        setEnrichment((res.data?.enrichment || null) as Enrichment | null)
        setEnrichPhase('ready')
      })
      .catch((e) => {
        const code = (e as { response?: { status?: number } })?.response?.status
        setEnrichError(code === 503 ? 'not_configured' : 'failed')
        setEnrichPhase('error')
      })
  }

  const changeStatus = (next: string) => {
    if (!id || next === status) return
    setStatus(next)
    setActing(true)
    findingsApi
      .update(id, { status: next })
      .then(() => onChanged?.())
      .catch(() => setStatus(status)) // revert on failure
      .finally(() => setActing(false))
  }

  const doDelete = () => {
    if (!id) return
    setActing(true)
    findingsApi
      .delete(id)
      .then(() => {
        setConfirmDel(false)
        onChanged?.()
        onClose()
      })
      .finally(() => setActing(false))
  }

  const f = raw ? mapApiFinding(raw) : null
  const preds = Object.entries(raw?.mitre_predictions || {}).sort((a, b) => b[1] - a[1])
  const ec = raw?.entity_context || {}

  const title =
    phase === 'ready' && f ? (
      <span className="fp-title">
        <span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span>
        <span className="mono fp-id">{id}</span>
      </span>
    ) : (
      id || 'Finding'
    )

  return (
    <Popup open={id !== null} onClose={onClose} title={title} width={640}>
      {phase === 'loading' && <div className="muted">Loading finding…</div>}
      {phase === 'error' && <div className="muted">Couldn’t load finding: {error}</div>}
      {phase === 'ready' && f && raw && (
        <>
          {/* hero — top technique + tactic on the left, key metrics on the right */}
          <div className="fp-hero">
            <div className="fp-hero-main">
              <div className="fp-tech"><span className="tag">{f.tech}</span> {techniqueName(f.tech)}</div>
              <div className="fp-tactic">{f.tactic}</div>
            </div>
            <div className="fp-metrics">
              <div className="fp-metric"><span className="fp-m-val">{f.conf}%</span><span className="fp-m-lab">confidence</span></div>
              <div className="fp-metric"><span className="fp-m-val">{f.score.toFixed(2)}</span><span className="fp-m-lab">anomaly</span></div>
            </div>
          </div>

          <div className="kv-grid fp-grid">
            <span className="k">Source</span><span className="v">{f.src}</span>
            <span className="k">Host</span><span className="v mono">{f.host}</span>
            <span className="k">User</span><span className="v mono">{f.user}</span>
            <span className="k">Time</span><span className="v">{f.time}</span>
            <span className="k">Status</span>
            <span className="v" style={{ maxWidth: 220 }}>
              <Select value={status} options={STATUS_OPTIONS} onSelect={changeStatus} />
            </span>
          </div>

          {raw.description && (
            <div className="modal-section">
              <h4>Description</h4>
              <p style={{ fontSize: 13, color: 'var(--tx-2)', margin: 0, lineHeight: 1.5 }}>{raw.description}</p>
            </div>
          )}

          {preds.length > 0 && (
            <div className="modal-section">
              <h4>MITRE predictions</h4>
              <div className="fp-preds">
                {preds.map(([tid, c]) => (
                  <div className="fp-pred" key={tid}>
                    <span className="tag">{tid}</span>
                    <span className="fp-pred-name">{techniqueName(tid)}</span>
                    <span className="fp-pred-bar"><i style={{ width: `${Math.round(c * 100)}%` }} /></span>
                    <span className="mono fp-pred-pct">{Math.round(c * 100)}%</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {(ec.hostnames?.length || ec.usernames?.length || ec.dest_ips?.length) && (
            <div className="modal-section">
              <h4>Entities</h4>
              <div className="fp-entities">
                {ec.hostnames?.length ? (
                  <div className="fp-ent-row"><span className="fp-ent-lab">Hosts</span><div className="fp-chips">{ec.hostnames.map((h) => <span className="chip mono" key={h}>{h}</span>)}</div></div>
                ) : null}
                {ec.usernames?.length ? (
                  <div className="fp-ent-row"><span className="fp-ent-lab">Users</span><div className="fp-chips">{ec.usernames.map((u) => <span className="chip mono" key={u}>{u}</span>)}</div></div>
                ) : null}
                {ec.dest_ips?.length ? (
                  <div className="fp-ent-row"><span className="fp-ent-lab">Dest IPs</span><div className="fp-chips">{ec.dest_ips.map((ip) => <span className="chip mono" key={ip}>{ip}</span>)}</div></div>
                ) : null}
              </div>
            </div>
          )}

          {/* AI enrichment — on-demand */}
          <div className="modal-section">
            <div className="flex items-center gap-2" style={{ justifyContent: 'space-between' }}>
              <h4 style={{ margin: 0 }}><Icon name="sparkle" size={14} /> AI analysis</h4>
              {enrichPhase === 'ready' && (
                <button className="btn ghost" onClick={() => loadEnrichment(true)} disabled={enrichPhase !== 'ready'}>
                  <Icon name="refresh" size={14} /> Regenerate
                </button>
              )}
            </div>

            {enrichPhase === 'idle' && (
              <button className="btn primary" style={{ marginTop: 10 }} onClick={() => loadEnrichment(false)}>
                <Icon name="sparkle" size={15} /> Generate AI analysis
              </button>
            )}
            {enrichPhase === 'loading' && <div className="muted" style={{ marginTop: 10 }}>Generating AI analysis…</div>}
            {enrichPhase === 'error' && (
              <div className="muted" style={{ marginTop: 10 }}>
                {enrichError === 'not_configured'
                  ? 'AI enrichment is not configured — add a Claude API key in Settings → AI.'
                  : 'AI enrichment failed. '}
                {enrichError !== 'not_configured' && (
                  <button className="btn ghost" onClick={() => loadEnrichment(false)}>Retry</button>
                )}
              </div>
            )}
            {enrichPhase === 'ready' && enrichment && <div style={{ marginTop: 10 }}><EnrichmentView e={enrichment} /></div>}
            {enrichPhase === 'ready' && !enrichment && <div className="muted" style={{ marginTop: 10 }}>No enrichment returned for this finding.</div>}
          </div>

          {/* actions */}
          <div className="fp-actions">
            <button className="btn danger" onClick={() => setConfirmDel(true)} disabled={acting}>
              <Icon name="trash" size={14} /> Delete finding
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmDel}
        title="Delete finding"
        body={`Permanently delete finding ${id}? This cannot be undone.`}
        confirmLabel="Delete"
        busy={acting}
        onConfirm={doDelete}
        onClose={() => setConfirmDel(false)}
      />
    </Popup>
  )
}
