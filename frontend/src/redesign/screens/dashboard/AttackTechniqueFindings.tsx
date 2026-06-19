/* ============================================================
   Inline expandable findings list for an ATT&CK technique row —
   the "Show findings" chevron expands the row to reveal this
   (a dropdown/accordion, matching the original prototype) rather
   than opening a modal. Fetches attackApi.getFindingsByTechnique.
   ============================================================ */
import { useEffect, useState } from 'react'
import { attackApi } from '../../../services/api'
import { mapApiFinding, type ApiFinding } from '../../data/mappers'
import type { Finding } from '../../data/data'
import type { Phase } from '../cases/useCases'

export default function AttackTechniqueFindings({ techniqueId }: { techniqueId: string }) {
  const [rows, setRows] = useState<Finding[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    attackApi
      .getFindingsByTechnique(techniqueId)
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.findings || []) as ApiFinding[]
        setRows(list.map(mapApiFinding))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load findings')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [techniqueId])

  if (phase === 'loading') return <div className="tech-findings muted">Loading findings…</div>
  if (phase === 'error') return <div className="tech-findings muted">Couldn’t load findings: {error}</div>
  if (rows.length === 0) return <div className="tech-findings muted">No findings for this technique.</div>

  return (
    <div className="tech-findings">
      <table className="tbl">
        <thead><tr><th>Finding ID</th><th>Severity</th><th>Source</th><th>Host</th><th>Time</th><th>Score</th></tr></thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.id}>
              <td><span className="id-cell">{f.id}</span></td>
              <td><span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span></td>
              <td className="muted">{f.src}</td>
              <td><span className="mono">{f.host}</span></td>
              <td className="muted">{f.time}</td>
              <td className="mono">{f.score.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
