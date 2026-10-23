/* The embedded VStrike NetworkContextPanel is deliberately not ported: it needs
   the VStrike provider, which isn't mounted under the console shell. */
import { useEffect, useRef, useState } from 'react'
import { exclusionsApi, findingsApi } from '../../services/api'
import { findingIps } from '../../data/findingIps'
import { useToast } from '../../shell/toast'
import { mapApiFinding, formatFindingScore, type ApiFinding } from '../../data/mappers'
import { techniqueName } from '../../data/mitre'
import { EmptyState, Popup } from '../../shared/ui'
import { stateWord } from '../../shared/findingState'
import { Icon } from '../../shared/icons'
import { SEND_TO_TRIAGE_FAILED, sendFindingToTriage } from '../../shared/sendToTriage'
import SourceChip from '../../shared/SourceChip'
import type { Phase } from '../cases/useCases'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from './SourceEvidenceSection'

interface RawFinding extends ApiFinding {
  description?: string
  cluster_id?: string | null
  ai_enrichment?: Enrichment | null
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
  raw_response?: string
  generated_at?: string
}

const ENRICHMENT_PROGRESS = [
  'Preparing the AI analysis…',
  'Reviewing the finding with your configured model…',
  'Still analysing — this can take a minute or two.',
  'Checking the AI gateway and automatically retrying if needed. Please keep this window open…',
  'Gateway recovery can take up to a minute. The analysis is still running…',
]

const RISK_COLOR: Record<string, string> = {
  critical: 'var(--crit)',
  high: 'var(--crit)',
  medium: 'var(--high)',
  low: 'var(--ok)',
}

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

      {e.raw_response && (
        <div className="modal-section">
          <h4>Raw model output</h4>
          <pre className="fp-raw-ai-output">{e.raw_response}</pre>
        </div>
      )}
    </>
  )
}

export default function FindingPopup({
  id,
  onClose,
  onChanged,
  onConfigureAi,
}: {
  id: string | null
  onClose: () => void
  onChanged?: () => void
  onConfigureAi?: () => void
}) {
  const [raw, setRaw] = useState<RawFinding | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [loadAttempt, setLoadAttempt] = useState(0)

  // on-demand: a getEnrichment call may invoke an LLM
  const [enrichment, setEnrichment] = useState<Enrichment | null>(null)
  const [enrichPhase, setEnrichPhase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [enrichError, setEnrichError] = useState<'not_configured' | 'budget' | 'failed' | null>(null)
  const [enrichmentProgressIndex, setEnrichmentProgressIndex] = useState(0)

  const [acting, setActing] = useState(false)
  const [dismissError, setDismissError] = useState<string | null>(null)
  const [triageNote, setTriageNote] = useState<string | null>(null)
  const [triageError, setTriageError] = useState<string | null>(null)
  const [triaging, setTriaging] = useState(false)
  // the finding the triage note belongs to; a reply for any other finding is dropped
  const triageFor = useRef<string | null>(null)

  // excluding an address from here, while looking at the finding that names it
  const { notify } = useToast()
  const [excludeIp, setExcludeIp] = useState<string | null>(null)
  const [excludeReason, setExcludeReason] = useState('')
  const [excluding, setExcluding] = useState(false)
  const [excludeError, setExcludeError] = useState<string | null>(null)
  const [justExcluded, setJustExcluded] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setPhase('loading')
    setError(null)
    setRaw(null)
    setEnrichment(null)
    setEnrichPhase('idle')
    setEnrichError(null)
    triageFor.current = id
    setTriageNote(null)
    setTriageError(null)
    setExcludeIp(null)
    setExcludeReason('')
    setExcludeError(null)
    setJustExcluded(new Set())
    setDismissError(null)
    findingsApi
      .getById(id)
      .then((res) => {
        if (cancelled) return
        const data = res.data as RawFinding
        setRaw(data)
        // The daemon writes triage keys into ai_enrichment too; only a
        // generated analysis carries generated_at.
        if (data.ai_enrichment?.generated_at) {
          setEnrichment(data.ai_enrichment)
          setEnrichPhase('ready')
        }
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        const code = (e as { code?: string })?.code
        setError(
          code === 'ECONNABORTED'
            ? 'The local Vigil API did not respond. It may be restarting.'
            : (e as { message?: string })?.message || 'Failed to load finding',
        )
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id, loadAttempt])

  useEffect(() => {
    if (enrichPhase !== 'loading') {
      setEnrichmentProgressIndex(0)
      return
    }

    const timers = [4_000, 15_000, 45_000, 75_000].map((delay, index) =>
      window.setTimeout(() => setEnrichmentProgressIndex(index + 1), delay),
    )
    return () => timers.forEach(window.clearTimeout)
  }, [enrichPhase])

  const loadEnrichment = (force = false) => {
    if (!id) return
    setEnrichPhase('loading')
    setEnrichError(null)
    setEnrichmentProgressIndex(0)
    findingsApi
      .getEnrichment(id, force)
      .then((res) => {
        setEnrichment((res.data?.enrichment || null) as Enrichment | null)
        setEnrichPhase('ready')
      })
      .catch((e) => {
        const response = (e as { response?: { status?: number; data?: { code?: string } } })?.response
        const status = response?.status
        const code = response?.data?.code
        setEnrichError(
          status === 402 || code === 'BUDGET_EXCEEDED'
            ? 'budget'
            : status === 503
              ? 'not_configured'
              : 'failed',
        )
        setEnrichPhase('error')
      })
  }

  const submitExclusion = () => {
    if (!id || !excludeIp || !excludeReason.trim()) return
    setExcluding(true)
    setExcludeError(null)
    exclusionsApi
      .create({ ip: excludeIp, reason: excludeReason.trim(), origin: 'finding', origin_ref: id })
      .then((res) => {
        setJustExcluded((prev) => new Set(prev).add(excludeIp.toLowerCase()).add(res.data.ip))
        notify('ok', `Excluded ${res.data.ip}. Findings naming it are hidden from the queue.`)
        setExcludeIp(null)
        setExcludeReason('')
        onChanged?.()
      })
      .catch((e) => {
        const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
        setExcludeError(typeof detail === 'string' ? detail : (e as { message?: string })?.message || 'Could not exclude this address')
      })
      .finally(() => setExcluding(false))
  }

  // Dismiss is the noise mark; the finding is re-read so the Status row shows the state it lands in.
  const dismissed = raw?.terminal_state === 'dismissed'
  const toggleDismiss = () => {
    if (!id || acting) return
    const target = id
    setActing(true)
    setDismissError(null)
    ;(dismissed ? findingsApi.clearNoise(target) : findingsApi.markNoise(target))
      .then(() => findingsApi.getById(target))
      .then((res) => {
        if (triageFor.current === target) setRaw(res.data as RawFinding)
        onChanged?.()
      })
      .catch(() => {
        if (triageFor.current === target) setDismissError(dismissed ? 'Couldn’t undo the dismissal' : 'Couldn’t dismiss this finding')
      })
      .finally(() => setActing(false))
  }

  const sendToTriage = () => {
    if (!id || triaging) return
    const target = id
    setTriaging(true)
    setTriageNote(null)
    setTriageError(null)
    sendFindingToTriage(target)
      .then(note => { if (triageFor.current === target) setTriageNote(note) })
      .catch(() => { if (triageFor.current === target) setTriageError(SEND_TO_TRIAGE_FAILED) })
      .finally(() => setTriaging(false))
  }

  const f = raw ? mapApiFinding(raw) : null
  const preds = Object.entries(raw?.mitre_predictions || {}).sort((a, b) => b[1] - a[1])
  const ec = raw?.entity_context || {}
  const sourceEvidence = parseSourceEvidence(ec.source_evidence)
  const ips = findingIps(ec)
  const excludedSet = new Set((raw?.excluded_ips ?? []).map((ip) => ip.toLowerCase()))

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
      {phase === 'loading' && (
        <div className="fp-ai-progress muted flex items-center gap-2" aria-live="polite">
          <span className="spin" aria-hidden="true" />
          <span>Loading finding details…</span>
        </div>
      )}
      {phase === 'error' && (
        <div className="muted">
          Couldn’t load finding: {error}{' '}
          <button className="btn ghost" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
            Retry
          </button>
        </div>
      )}
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
              <div className="fp-metric"><span className="fp-m-val">{formatFindingScore(f.score)}</span><span className="fp-m-lab">anomaly</span></div>
            </div>
          </div>

          <div className="kv-grid fp-grid">
            <span className="k">Source</span><span className="v"><SourceChip source={f.src} /></span>
            <span className="k">Host</span><span className="v mono">{f.host}</span>
            <span className="k">User</span><span className="v mono">{f.user}</span>
            <span className="k">Time</span><span className="v">{f.time}</span>
            <span className="k" style={{ alignSelf: 'center' }}>Status</span>
            <span className="v flex items-center gap-2 flex-wrap">
              <span>{stateWord(raw.terminal_state, raw.case_id)}</span>
              <button type="button" className="btn ghost" onClick={toggleDismiss} disabled={acting}>
                {dismissed ? 'Undo' : 'Dismiss'}
              </button>
            </span>
          </div>
          {dismissError && <div className="field-hint err" role="alert" style={{ marginTop: 6 }}>{dismissError}</div>}

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

          {!!(ec.hostnames?.length || ec.usernames?.length) && (
            <div className="modal-section">
              <h4>Entities</h4>
              <div className="fp-entities">
                {ec.hostnames?.length ? (
                  <div className="fp-ent-row"><span className="fp-ent-lab">Hosts</span><div className="fp-chips">{ec.hostnames.map((h) => <span className="chip mono" key={h}>{h}</span>)}</div></div>
                ) : null}
                {ec.usernames?.length ? (
                  <div className="fp-ent-row"><span className="fp-ent-lab">Users</span><div className="fp-chips">{ec.usernames.map((u) => <span className="chip mono" key={u}>{u}</span>)}</div></div>
                ) : null}
              </div>
            </div>
          )}

          {ips.length > 0 && (
            <div className="modal-section">
              <h4>IP addresses</h4>
              <ul className="fp-ips">
                {ips.map((ip) => {
                  const excluded = excludedSet.has(ip.toLowerCase()) || justExcluded.has(ip.toLowerCase())
                  return (
                    <li key={ip} className="fp-ip">
                      <span className="chip mono">{ip}</span>
                      {excluded ? (
                        <span className="tag excluded-tag" title="Findings naming this address are hidden from the queue">excluded</span>
                      ) : (
                        excludeIp !== ip && (
                          <button
                            className="btn ghost fp-ip-exclude"
                            onClick={() => { setExcludeIp(ip); setExcludeReason(''); setExcludeError(null) }}
                            title="Hide findings naming this address from every analyst's queue"
                          >
                            Exclude
                          </button>
                        )
                      )}
                      {excludeIp === ip && (
                        <form
                          className="fp-ip-form"
                          onSubmit={(e) => { e.preventDefault(); submitExclusion() }}
                        >
                          <input
                            className="field-input"
                            aria-label={`Reason for excluding ${ip}`}
                            placeholder="Why exclude it? (required)"
                            value={excludeReason}
                            maxLength={2000}
                            autoFocus
                            onChange={(e) => setExcludeReason(e.target.value)}
                          />
                          <button className="btn primary" type="submit" disabled={excluding || !excludeReason.trim()}>
                            {excluding ? 'Excluding…' : 'Exclude'}
                          </button>
                          <button className="btn ghost" type="button" onClick={() => setExcludeIp(null)} disabled={excluding}>
                            Cancel
                          </button>
                        </form>
                      )}
                    </li>
                  )
                })}
              </ul>
              {excludeError && <div className="field-hint err" role="alert">{excludeError}</div>}
              <p className="fp-ip-note muted">
                Excluding hides this address’s findings from every analyst’s queue. LogLM keeps scoring them,
                and removing the exclusion (Dashboard → Excluded IPs) brings them back unchanged.
              </p>
            </div>
          )}

          <SourceEvidenceSection evidence={sourceEvidence} />

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
            {enrichPhase === 'loading' && (
              <div className="fp-ai-progress muted flex items-center gap-2" style={{ marginTop: 10 }} aria-live="polite">
                <span className="spin" aria-hidden="true" />
                <span>{ENRICHMENT_PROGRESS[enrichmentProgressIndex]}</span>
              </div>
            )}
            {enrichPhase === 'error' && enrichError === 'not_configured' && (
              <EmptyState
                compact
                icon="sparkle"
                title="AI enrichment is not configured"
                body="Add an AI provider and assign a chat/enrichment model before generating finding analysis."
                primary={onConfigureAi ? { label: 'Open AI Config', onClick: onConfigureAi, icon: 'gear' } : undefined}
              />
            )}
            {enrichPhase === 'error' && enrichError === 'budget' && (
              <EmptyState
                compact
                icon="alert"
                title="Virtual-key budget spent"
                body="The virtual-key budget is spent. Raise its ceiling under AI models → Spending limit."
                primary={onConfigureAi ? { label: 'Open AI Config', onClick: onConfigureAi, icon: 'gear' } : undefined}
              />
            )}
            {enrichPhase === 'error' && enrichError === 'failed' && (
              <EmptyState
                error
                compact
                icon="alert"
                title="AI enrichment failed"
                body="The analysis request did not complete."
                primary={{ label: 'Retry', onClick: () => loadEnrichment(false), icon: 'refresh' }}
              />
            )}
            {enrichPhase === 'ready' && enrichment && <div style={{ marginTop: 10 }}><EnrichmentView e={enrichment} /></div>}
            {enrichPhase === 'ready' && !enrichment && <div className="muted" style={{ marginTop: 10 }}>No enrichment returned for this finding.</div>}
          </div>

          {/* actions */}
          <div className="fp-actions">
            <button className="btn ghost" onClick={sendToTriage} disabled={triaging}>Send to triage</button>
          </div>
          {triageNote && <div className="field-hint" style={{ marginTop: 10 }}>{triageNote}</div>}
          {triageError && <div className="field-hint err" role="alert" style={{ marginTop: 10 }}>{triageError}</div>}
        </>
      )}
    </Popup>
  )
}
