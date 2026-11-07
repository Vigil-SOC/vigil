import { useEffect, useState } from 'react'
import { errorText } from '../../shared/errorText'
import { format } from 'date-fns'
import { detectionRulesApi } from '../../services/api'

interface FiredFinding {
  finding_id: string
  title: string | null
  severity: string | null
  timestamp: string | null
}

interface Listing {
  total: number
  /** Findings that carry its name, but so does another listed rule: counted on neither. */
  unsure?: number
  days: number
  findings: FiredFinding[]
}

/** The finding's time as its popup shows it: local, "MMM d, HH:mm". */
function when(timestamp: string | null): string {
  if (!timestamp) return ''
  const at = new Date(timestamp)
  return Number.isNaN(at.getTime()) ? '' : format(at, 'MMM d, HH:mm')
}

/** The findings a rule copy fired in the fires window, as the list counts them; each opens. */
export default function RuleFindings({ ruleId, onOpen }: { ruleId: number; onOpen: (findingId: string) => void }) {
  const [data, setData] = useState<Listing | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    detectionRulesApi.ruleFindings(ruleId)
      .then((res) => { if (live) { setData(res.data as Listing); setError(null) } })
      .catch((e) => { if (live) setError(errorText(e, 'Findings did not load.')) })
    return () => { live = false }
  }, [ruleId])

  if (!data) return <div className="de-x">{error ? <span className="de-x-err" role="alert">{error}</span> : <span className="x1-muted">Reading findings…</span>}</div>

  return (
    <div className="de-x">
      <div className="de-x-head">
        <b>Findings <span>· {data.total.toLocaleString()} in {data.days}d</span></b>
        {data.total > data.findings.length && <span className="x1-muted">Newest {data.findings.length} shown</span>}
      </div>
      {data.findings.length === 0 && <p className="de-x-none">No finding in the last {data.days} days.</p>}
      {(data.unsure ?? 0) > 0 && <p className="de-x-none">{data.unsure} more {data.unsure === 1 ? 'finding carries' : 'findings carry'} this rule’s name, but so does another rule, so they count on neither.</p>}
      {data.findings.length > 0 && (
        <ul className="de-fd-list">
          {data.findings.map((f) => (
            <li key={f.finding_id}>
              <button type="button" className="de-fd" onClick={() => onOpen(f.finding_id)}>
                <span className={`de-fd-sev ${(f.severity ?? '').toLowerCase()}`}>{f.severity ?? '—'}</span>
                <span className="de-fd-title" title={f.title ?? f.finding_id}>{f.title || f.finding_id}</span>
                <span className="de-fd-when">{when(f.timestamp)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
