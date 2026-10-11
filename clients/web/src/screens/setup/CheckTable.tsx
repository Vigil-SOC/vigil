import CheckMark, { type CheckPhase } from './CheckMark'

export interface CheckRow {
  id: string
  label: string
  phase: CheckPhase
  detail: string
}

/** components/row-stack.md: columns `18px 150px minmax(0,1fr)`, an optional summary line under the stack. */
export default function CheckTable({
  rows,
  label,
  summary,
}: {
  rows: CheckRow[]
  label: string
  summary?: { tone: 'fair' | 'good'; text: string }
}) {
  return (
    <div className="su-checks">
      <div role="list" aria-label={label} aria-live="polite" className="su-check-rows">
        {rows.map((row) => (
          <div key={row.id} role="listitem" className="su-check-row">
            <CheckMark phase={row.phase} />
            <span className="su-check-name">{row.label}</span>
            <span
              className={`su-check-detail${row.phase === 'needs' ? ' needs' : ''}`}
              title={row.detail}
            >
              {row.detail}
            </span>
          </div>
        ))}
      </div>
      {summary && (
        <p role="status" className={`su-check-summary ${summary.tone}`}>
          {summary.text}
        </p>
      )}
    </div>
  )
}
