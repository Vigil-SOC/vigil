import { useEffect, useState } from 'react'
import { attackApi } from '../../services/api'
import { mapApiFinding, formatFindingScore, type ApiFinding } from '../../data/mappers'
import type { Finding } from '../../data/data'
import type { Phase } from '../cases/useCases'
import SourceChip from '../../shared/SourceChip'
import type { AttackTechnique, LayerVerdict, MissedStep } from './useAttack'

function verdictLabel(verdict: LayerVerdict): string {
  switch (verdict) {
    case 'rule':
      return 'rule'
    case 'loglm':
      return 'LogLM'
    case 'both':
      return 'both'
    case 'missed':
      return 'missed'
    default: {
      const _exhaustive: never = verdict
      return _exhaustive
    }
  }
}

function stepHost(step: MissedStep): string {
  return step.hostname || step.host || step.computer_name || step.src_ip || '—'
}

export default function AttackTechniqueFindings({
  techniqueId,
  coverage,
}: {
  techniqueId: string
  coverage?: AttackTechnique
}) {
  const [rows, setRows] = useState<Finding[]>([])
  const [phase, setPhase] = useState<Phase>(coverage ? 'ready' : 'loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (coverage) {
      setPhase('ready')
      setError(null)
      setRows([])
      return
    }
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
  }, [techniqueId, coverage])

  if (coverage) {
    const missed = coverage.missed ?? []
    if (missed.length === 0) {
      return (
        <div className="tech-findings muted">
          {coverage.verdict ? `${verdictLabel(coverage.verdict)} — no missed steps.` : 'No missed steps for this technique.'}
        </div>
      )
    }
    return (
      <div className="tech-findings">
        <table className="tbl">
          <thead>
            <tr>
              <th>Step</th>
              <th>Host</th>
              <th>User</th>
              <th>Command</th>
              <th>Citations</th>
            </tr>
          </thead>
          <tbody>
            {missed.map((step) => (
              <tr key={step.id ?? String(step.index)}>
                <td><span className="id-cell">{step.id ?? `#${step.index}`}</span></td>
                <td><span className="mono">{stepHost(step)}</span></td>
                <td className="mono">{step.user || '—'}</td>
                <td className="mono">{step.command || '—'}</td>
                <td>
                  {step.citations.length === 0
                    ? <span className="muted">none</span>
                    : step.citations.map((cite) => (
                      <div key={cite.finding_id} className="coverage-cite">
                        <span className="id-cell">{cite.finding_id}</span>
                        {cite.rule_name ? <span> · {cite.rule_name}</span> : null}
                        {cite.description ? <span className="muted"> — {cite.description}</span> : null}
                      </div>
                    ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

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
              <td><SourceChip source={f.src} /></td>
              <td><span className="mono">{f.host}</span></td>
              <td className="muted">{f.time}</td>
              <td className="mono">{formatFindingScore(f.score)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
