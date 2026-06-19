/* ============================================================
   Finding detail popup — opened from a Findings row / the "View"
   eye. Fetches the full finding (findingsApi.getById) and shows
   the normalized fields plus description, MITRE predictions and
   extracted entities. A lightweight stand-in for the production
   FindingDetailDialog (no AI-enrichment-on-demand / VStrike yet —
   see REDESIGN_GAPS.md §3, §8).
   ============================================================ */
import { useEffect, useState } from 'react'
import { findingsApi } from '../../../services/api'
import { mapApiFinding, type ApiFinding } from '../../data/mappers'
import { techniqueName } from '../../data/mitre'
import { Popup } from '../../shared/ui'
import type { Phase } from '../cases/useCases'

interface RawFinding extends ApiFinding {
  description?: string
  cluster_id?: string | null
  ai_enrichment?: { model?: string } | null
}

export default function FindingPopup({ id, onClose }: { id: string | null; onClose: () => void }) {
  const [raw, setRaw] = useState<RawFinding | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setPhase('loading')
    setError(null)
    setRaw(null)
    findingsApi
      .getById(id)
      .then((res) => {
        if (cancelled) return
        setRaw(res.data as RawFinding)
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
            <span className="k">Status</span><span className="v"><span className={`status ${f.status}`}>{f.status}</span></span>
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
        </>
      )}
    </Popup>
  )
}
