import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDayClock } from '../../shared/utc'
import { detectionEngineeringApi, detectionRulesApi, type ImportCheck, type RuleDetail } from '../../services/api'
import { proposalHref } from './proposalLinks'
import { IMPORT_STATE, who } from './ruleStage'

const SIEM: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk', wazuh: 'Wazuh' }

/** A repo rule's import check, read from its detail when the panel opens: Held from
 * coverage with its state and reason (absent fields per SIEM), Approve for a holder
 * of detections.write, and Propose for a Sigma rule that is not a correlation. */
export default function RuleImport({ ruleId, onChanged }: { ruleId: number; onChanged: () => void }) {
  const canWrite = useAuth().hasPermission('detections.write')
  const navigate = useNavigate()
  const [rule, setRule] = useState<RuleDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)
  const [proposing, setProposing] = useState(false)
  const [proposeError, setProposeError] = useState<string | null>(null)
  const load = () =>
    detectionRulesApi.rule(ruleId)
      .then((res) => { setRule(res.data); setError(null) })
      .catch((e) => setError(errorText(e, 'Its import check did not load.')))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [ruleId])

  if (!rule) {
    return (
      <div className="de-x">
        {error ? <span className="de-x-err" role="alert">{error}</span> : <span className="x1-muted">Reading its import check…</span>}
      </div>
    )
  }
  const check = rule.checks.import as ImportCheck | undefined
  const lint = rule.checks.lint
  const lintFails = lint?.result === 'fail'
  // What lint found, as the import check words it: "keyed to <kind> <value>, …".
  const keyed = ((lint?.findings as { kind?: string; value?: string }[] | undefined) ?? []).map((f) => `${f.kind} ${f.value}`).join(', ')
  const held = rule.held
  const canPropose = canWrite && rule.tool === 'repo' && rule.source === 'sigma' && !('correlation' in (rule.content ?? {}))
  const propose = () => {
    setProposing(true)
    setProposeError(null)
    detectionEngineeringApi.fromRule(ruleId)
      .then((res) => navigate(proposalHref(res.data.proposal_id)))
      .catch((e) => { setProposeError(errorText(e, 'Could not propose it.')); setProposing(false) })
  }
  return (
    <div className="de-x" data-testid="rule-import">
      <div className="de-x-head">
        <b>
          {held ? 'Held from coverage' : check ? 'Counts as coverage' : 'Import check'}
          {check && <span> · {IMPORT_STATE[check.result] ?? check.result}</span>}
        </b>
        {((canWrite && held && !asking) || canPropose) && (
          <span className="de-x-add de-x-acts">
            {canWrite && held && !asking && (
              <button type="button" className="x1-link" disabled={lintFails} title={lintFails ? 'Approval cannot pass a lint failure' : undefined} onClick={() => setAsking(true)}>Approve</button>
            )}
            {canPropose && (
              <button type="button" className="x1-link" disabled={proposing} onClick={propose}>{proposing ? 'Proposing…' : 'Propose'}</button>
            )}
          </span>
        )}
      </div>
      {asking && <Approve ruleId={ruleId} onDone={(done) => { setAsking(false); if (done) { load(); onChanged() } }} />}
      {check?.result === 'approved' ? (
        <p className="de-x-none">
          Approved by {who(check.by ?? '')}{check.at ? ` on ${utcDayClock(check.at)}` : ''}{check.reason ? `: ${check.reason}` : ''}.
          {check.was?.result && ` It was ${(IMPORT_STATE[check.was.result as ImportCheck['result']] ?? check.was.result).toLowerCase()}${check.was.reason ? `: ${check.was.reason}` : ''}.`}
        </p>
      ) : check?.reason ? (
        <p className="de-x-none">{check.reason}</p>
      ) : null}
      {check?.absent_fields && Object.keys(check.absent_fields).length > 0 && (
        <ul className="de-x-list" aria-label="Absent fields">
          {Object.entries(check.absent_fields).map(([siem, fields]) => (
            <li key={siem}>
              <span className="de-x-miss">Not in {SIEM[siem] ?? siem}’s fields: {fields.map((f, i) => <span key={f}>{i > 0 && ', '}<code>{f}</code></span>)}</span>
            </li>
          ))}
        </ul>
      )}
      {held && lintFails && canWrite && (
        <p className="de-x-miss">Approve is off: approval cannot pass a lint failure{keyed && !check?.reason?.includes('fails lint') ? ` (it is keyed to ${keyed})` : ''}. Fix the rule in its repo; the next version is checked again.</p>
      )}
      {proposeError && <span className="de-x-err" role="alert">{proposeError}</span>}
      <p className="de-x-none">
        {held
          ? `Held from coverage: it is listed but not counted on the coverage map until the import check finds its data in a SIEM Vigil reads${canWrite ? ', or a person approves it' : ''}. A new version is held again.`
          : !check
            ? 'Stored before the import check: it counts as coverage.'
            : 'It counts as coverage. A new version from its repo is held again until it is checked.'}
      </p>
    </div>
  )
}

function Approve({ ruleId, onDone }: { ruleId: number; onDone: (changed: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why.'); return }
    setBusy(true)
    detectionRulesApi.approveRule(ruleId, reason.trim())
      .then(() => onDone(true))
      // A 409 (lint fails, or no longer held) reads as the server says it.
      .catch((err) => { setError(errorText(err, 'Could not approve it.')); setBusy(false) })
  }
  return (
    <form className="de-x-form de-x-remove" onSubmit={submit}>
      <input className="de-x-in" aria-label="Reason" maxLength={2000} placeholder="Why it should count as coverage (required)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className="btn primary de-x-btn" disabled={busy}>{busy ? 'Saving…' : 'Approve'}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}
