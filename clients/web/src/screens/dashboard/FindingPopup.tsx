/* The embedded VStrike NetworkContextPanel is deliberately not ported: it needs
   the VStrike provider, which isn't mounted under the console shell. */
import { useEffect, useRef, useState } from 'react'
import { format } from 'date-fns'
import { Link } from 'react-router-dom'
import { CopyButton } from '../../shared/CopyButton'
import { exclusionsApi, findingsApi } from '../../services/api'
import { findingIps } from '../../data/findingIps'
import { ipRoleLabels } from '../../data/ipRoles'
import { useToast } from '../../shell/toast'
import { mapApiFinding, formatFindingScore, rankTechniques, type ApiFinding } from '../../data/mappers'
import { techniqueName } from '../../data/mitre'
import { CauseSelect } from '../../shared/falsePositiveCause'
import { Popup } from '../../shared/ui'
import { stateWord } from '../../shared/findingState'
import { SEND_TO_TRIAGE_FAILED, sendFindingToTriage } from '../../shared/sendToTriage'
import SourceChip from '../../shared/SourceChip'
import type { Phase } from '../cases/useCases'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from './SourceEvidenceSection'

/** core.detections.inventory.catalog.rule_for_finding */
interface DetectionRuleRef {
  resolved: boolean
  reason?: string
  name?: string
  version?: number
  first_seen?: string
  checks?: { runs?: { result?: string } }
}

interface RawFinding extends ApiFinding {
  description?: string
  cluster_id?: string | null
  detection_rule?: DetectionRuleRef | null
}

// One line: the rule and version that fired the finding, or why it is unknown.
function RuleLine({ rule }: { rule: DetectionRuleRef }) {
  if (!rule.resolved) return <span style={{ color: 'var(--tx-2)' }}>Unknown: {rule.reason}</span>
  const seen = rule.first_seen ? new Date(rule.first_seen) : null
  const runs = rule.checks?.runs?.result
  return (
    <span>
      {rule.name} <span className="mono">v{rule.version}</span>
      <span style={{ color: 'var(--tx-2)' }}>
        {seen && !Number.isNaN(seen.getTime()) && <> · first seen {format(seen, 'MMM d, HH:mm')}</>}
        {' · '}Runs {runs ?? 'unchecked'}
      </span>
    </span>
  )
}

export default function FindingPopup({
  id,
  onClose,
  onChanged,
}: {
  id: string | null
  onClose: () => void
  onChanged?: () => void
}) {
  const [raw, setRaw] = useState<RawFinding | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [loadAttempt, setLoadAttempt] = useState(0)

  const [acting, setActing] = useState(false)
  const [cause, setCause] = useState('')
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
    triageFor.current = id
    setTriageNote(null)
    setTriageError(null)
    setExcludeIp(null)
    setExcludeReason('')
    setExcludeError(null)
    setJustExcluded(new Set())
    setDismissError(null)
    setCause('')
    findingsApi
      .getById(id)
      .then((res) => {
        if (cancelled) return
        const data = res.data as RawFinding
        setRaw(data)
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
    ;(dismissed ? findingsApi.clearNoise(target) : findingsApi.markNoise(target, cause || undefined))
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
  const preds = rankTechniques(raw?.mitre_predictions)
  const ec = raw?.entity_context || {}
  const sourceEvidence = parseSourceEvidence(ec.source_evidence)
  const ips = findingIps(ec)
  const ipRoles = ipRoleLabels(ec)
  const excludedSet = new Set((raw?.excluded_ips ?? []).map((ip) => ip.toLowerCase()))

  const title =
    phase === 'ready' && f ? (
      <span className="fp-title">
        <span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span>
        <span className="mono fp-id">{id}</span>
        {id && <CopyButton text={id} label="finding id" />}
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
          <div className="kv-grid fp-grid">
            <span className="k">Source</span><span className="v"><SourceChip source={f.src} /></span>
            {raw.detection_rule && (
              <><span className="k">Rule</span><span className="v"><RuleLine rule={raw.detection_rule} /></span></>
            )}
            <span className="k">Host</span><span className="v mono">{f.host}</span>
            <span className="k">User</span><span className="v mono">{f.user}</span>
            <span className="k">Time</span><span className="v">{f.time}</span>
            {typeof f.score === 'number' && (
              <><span className="k">Score</span><span className="v mono">{formatFindingScore(f.score)}</span></>
            )}
            <span className="k" style={{ alignSelf: 'center' }}>Status</span>
            <span className="v flex items-center gap-2 flex-wrap">
              {raw.case_id && !raw.case_restricted && stateWord(raw.terminal_state, raw.case_id).startsWith('Case ') ? (
                <Link className="underline" to={`/cases?case=${encodeURIComponent(raw.case_id)}`}>
                  {stateWord(raw.terminal_state, raw.case_id)}
                </Link>
              ) : (
                <span>{stateWord(raw.terminal_state, raw.case_id, raw.case_restricted)}</span>
              )}
              {!dismissed && <CauseSelect value={cause} onSelect={setCause} />}
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
                    {c !== null && (
                      <>
                        <span className="fp-pred-bar"><i style={{ width: `${Math.round(c * 100)}%` }} /></span>
                        <span className="mono fp-pred-pct">{Math.round(c * 100)}%</span>
                      </>
                    )}
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
                      {ipRoles.get(ip.toLowerCase()) && <span className="fp-ip-role mono">{ipRoles.get(ip.toLowerCase())}</span>}
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
                Excluding hides this address’s findings from every analyst’s queue. Vigil keeps ingesting and storing them,
                and removing the exclusion (Findings → Excluded IPs) brings them back unchanged.
              </p>
            </div>
          )}

          <SourceEvidenceSection evidence={sourceEvidence} />

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
