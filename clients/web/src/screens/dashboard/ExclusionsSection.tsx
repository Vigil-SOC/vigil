import { useState, type FormEvent } from 'react'
import { format } from 'date-fns'
import { exclusionsApi, type IpExclusion } from '../../services/api'
import { apiError, useExclusions } from './useExclusions'
import { Icon } from '../../shared/icons'
import { looksLikeIp } from '../../data/findingIps'
import { useToast } from '../../shell/toast'

const ORIGIN_LABEL: Record<IpExclusion['origin'], string> = {
  ad_hoc: 'Added here',
  finding: 'From finding',
  case: 'From case',
  run: 'From run',
}

function when(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d, HH:mm')
}

/**
 * The exclusion list, below the findings queue. Collapsed by default: it is the
 * record of what the queue is hiding, not something an analyst works from.
 */
export default function ExclusionsSection({
  refreshKey,
  onChanged,
  onShowExcluded,
}: {
  /** bump to re-read after an exclusion made elsewhere (the finding popup) */
  refreshKey: number
  /** the queue and KPIs need re-reading after any change here */
  onChanged: () => void
  /** switch the queue to the excluded findings */
  onShowExcluded: () => void
}) {
  const { notify } = useToast()
  const [open, setOpen] = useState(false)
  const { rows, hiddenTotal: hidden, phase, error, reload, history, setHistory } = useExclusions(refreshKey)
  const [ip, setIp] = useState('')
  const [reason, setReason] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const active = rows.filter((r) => r.active)
  const ipOk = looksLikeIp(ip)
  const canAdd = ipOk && reason.trim().length > 0 && busy === null

  const add = (e: FormEvent) => {
    e.preventDefault()
    if (!canAdd) return
    setBusy('add')
    setFormError(null)
    exclusionsApi
      .create({ ip: ip.trim(), reason: reason.trim(), origin: 'ad_hoc' })
      .then((res) => {
        notify('ok', `Excluded ${res.data.ip}. Its findings are hidden from the queue.`)
        setIp('')
        setReason('')
        reload()
        onChanged()
      })
      .catch((err) => setFormError(apiError(err, 'Could not add the exclusion')))
      .finally(() => setBusy(null))
  }

  const restore = (row: IpExclusion) => {
    setBusy(row.exclusion_id)
    exclusionsApi
      .remove(row.exclusion_id)
      .then(() => {
        const n = row.hidden_findings ?? 0
        notify('ok', `${row.ip} is back in the queue${n ? ` (${n} finding${n === 1 ? '' : 's'})` : ''}.`)
        reload()
        onChanged()
      })
      .catch((err) => notify('err', apiError(err, 'Could not remove the exclusion')))
      .finally(() => setBusy(null))
  }

  return (
    <section className="exclusions" aria-labelledby="exclusions-heading">
      <button
        className="exclusions-toggle"
        aria-expanded={open}
        aria-controls="exclusions-body"
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name={open ? 'chevD' : 'chevR'} size={14} />
        <span id="exclusions-heading" className="exclusions-title">Excluded IPs</span>
        <span className="exclusions-count">{phase === 'ready' ? active.length : '·'}</span>
        {phase === 'ready' && active.length > 0 && (
          <span className="exclusions-sub">
            hiding {hidden} finding{hidden === 1 ? '' : 's'} from the queue · still ingested and scored by LogLM
          </span>
        )}
      </button>

      {open && (
        <div id="exclusions-body" className="exclusions-body">
          <p className="exclusions-help">
            Exclusions apply org-wide. Findings that name an excluded address are hidden from every
            analyst&rsquo;s queue and KPIs. Nothing about the findings changes: removing an
            exclusion brings them back as they were.
          </p>

          <form className="exclusions-form" onSubmit={add}>
            <input
              className="field-input mono"
              aria-label="IP address to exclude"
              placeholder="IP address, e.g. 203.0.113.9"
              value={ip}
              onChange={(e) => setIp(e.target.value)}
              aria-invalid={ip.length > 0 && !ipOk}
            />
            <input
              className="field-input exclusions-reason"
              aria-label="Reason for excluding"
              placeholder="Why (e.g. known scanner, already blocked at the edge)"
              value={reason}
              maxLength={2000}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className="btn primary" type="submit" disabled={!canAdd}>
              <Icon name="plus" /> {busy === 'add' ? 'Adding…' : 'Exclude'}
            </button>
          </form>
          {ip.length > 0 && !ipOk && <div className="field-hint err">Enter one IPv4 or IPv6 address, not a range.</div>}
          {formError && <div className="field-hint err" role="alert">{formError}</div>}

          <div className="exclusions-bar">
            <label className="exclusions-history">
              <input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} />
              Show removed
            </label>
            <span className="flex-1" />
            {active.length > 0 && (
              <button className="btn ghost" onClick={onShowExcluded}>
                <Icon name="filter" /> Show excluded findings
              </button>
            )}
          </div>

          {phase === 'error' && (
            <div className="muted">
              Couldn’t load exclusions: {error}{' '}
              <button className="btn ghost" onClick={reload}>Retry</button>
            </div>
          )}
          {phase === 'ready' && rows.length === 0 && (
            <div className="muted exclusions-empty">No IPs are excluded. Add one above, or from a finding’s IP addresses.</div>
          )}
          {rows.length > 0 && (
            <div className="table-wrap">
              <table className="tbl exclusions-tbl">
                <thead>
                  <tr>
                    <th>IP address</th>
                    <th>Reason</th>
                    <th>Hidden</th>
                    <th>Excluded by</th>
                    <th>Origin</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.exclusion_id} className={r.active ? '' : 'exclusions-removed'}>
                      <td className="mono">{r.ip}</td>
                      <td className="exclusions-reason-cell">
                        {r.reason}
                        {!r.active && (
                          <div className="muted">
                            Removed {when(r.removed_at)} by {r.removed_by}
                            {r.removal_reason ? ` — ${r.removal_reason}` : ''}
                          </div>
                        )}
                      </td>
                      <td className="mono">{r.active ? r.hidden_findings ?? 0 : '—'}</td>
                      <td>
                        {r.created_by}
                        <div className="muted">{when(r.created_at)}</div>
                      </td>
                      <td>
                        {ORIGIN_LABEL[r.origin] ?? r.origin}
                        {r.origin_ref && <div className="mono muted">{r.origin_ref}</div>}
                      </td>
                      <td className="exclusions-actions">
                        {r.active ? (
                          <button
                            className="btn ghost"
                            disabled={busy !== null}
                            title="Stop excluding this address; its findings return to the queue"
                            onClick={() => restore(r)}
                          >
                            {busy === r.exclusion_id ? 'Restoring…' : 'Restore'}
                          </button>
                        ) : (
                          <span className="muted">removed</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
