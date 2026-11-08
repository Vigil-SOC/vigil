import { useEffect, useState } from 'react'
import {
  detectionRulesApi, type CoverageCell, type CoverageSiemName, type CoverageState, type DetectionCoverage,
} from '../../services/api'
import { Empty, Failure } from '../shared/x1'

// The heat map is Alex Margaris's from the 1.x line (ProgramTab's HeatMap), here
// with main's meaning of proven (an active SIEM copy passes Runs and Sees) and a
// split by SIEM.

const STATES: Record<CoverageState, string> = {
  proven: 'Proven',
  unproven: 'Rule, not proven',
  no_rule: 'No active rule',
}
const SIEMS: { id: CoverageSiemName; label: string }[] = [
  { id: 'elastic', label: 'Elastic' },
  { id: 'splunk', label: 'Splunk' },
  { id: 'wazuh', label: 'Wazuh' },
]
type Lens = 'all' | CoverageSiemName

/** A cell's state through the lens: every SIEM, or one SIEM's copies only. */
function stateIn(cell: CoverageCell, lens: Lens): CoverageState {
  if (lens === 'all') return cell.state
  return cell.siems[lens]?.state ?? 'no_rule'
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`
}

export default function CoverageMap({ goSettings }: { goSettings: (section: 'data') => void }) {
  const [coverage, setCoverage] = useState<DetectionCoverage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lens, setLens] = useState<Lens>('all')
  const [selected, setSelected] = useState<string | null>(null)

  const load = () => {
    setError(null)
    detectionRulesApi.coverage()
      .then((res) => setCoverage(res.data))
      .catch(() => setError('The coverage map did not load.'))
  }
  useEffect(load, [])

  if (error) {
    return (
      <Failure>
        {error} <button type="button" className="x1-link" onClick={load}>Retry</button>
      </Failure>
    )
  }
  if (!coverage) return <span className="x1-muted">Reading the coverage map…</span>
  if (coverage.techniques.length === 0) {
    return (
      <Empty title="No technique to show yet">
        The map reads the rules in the inventory and the alerts mapped to a technique, and both are empty. Add a rule source in{' '}
        <button type="button" className="x1-link" onClick={() => goSettings('data')}>Settings</button>.
      </Empty>
    )
  }
  const present = SIEMS.filter((s) => coverage.techniques.some((t) => t.siems[s.id]))
  const counts = (Object.keys(STATES) as CoverageState[]).map((s) => [s, coverage.techniques.filter((t) => stateIn(t, lens) === s).length] as const)
  const cell = coverage.techniques.find((t) => t.technique === selected) ?? null
  return (
    <div className="x1-card de-cov" data-testid="coverage-map">
      <div className="de-cov-head">
        <div className="de-cov-lens" role="group" aria-label="Coverage in">
          <button type="button" className={`de-cov-lens-b${lens === 'all' ? ' on' : ''}`} aria-pressed={lens === 'all'} onClick={() => setLens('all')}>Every SIEM</button>
          {present.map((s) => (
            <button key={s.id} type="button" className={`de-cov-lens-b${lens === s.id ? ' on' : ''}`} aria-pressed={lens === s.id} onClick={() => setLens(s.id)}>{s.label}</button>
          ))}
        </div>
        <div className="de-cov-legend">
          {counts.map(([s, count]) => (
            <span key={s}><span className={`de-swatch ${s}`} />{STATES[s]} · {count}</span>
          ))}
        </div>
      </div>
      <div className="de-matrix-wrap">
        <div className="de-matrix">
          {coverage.tactics.map((tactic) => (
            <div key={tactic} className="de-col">
              <h4>{tactic}</h4>
              {coverage.techniques.filter((t) => t.tactic === tactic).map((t) => (
                <button key={t.technique} type="button" className={`de-cell ${stateIn(t, lens)}${selected === t.technique ? ' on' : ''}`} aria-pressed={selected === t.technique} onClick={() => setSelected(selected === t.technique ? null : t.technique)}>
                  <b>{t.name}</b>
                  <span className="x1-mono">{t.technique}{t.rules ? ` · ${plural(t.rules, 'rule')}` : ''}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
      {cell && <CellDetail cell={cell} />}
      <p className="de-note">
        Proven: an active copy in a SIEM passes Runs and Sees, as the Rules page counts it. A rule counts once however many SIEM copies it has. {plural(coverage.mapped, 'rule')} declare a technique, as on the Rules page; a disabled one is no coverage, so its technique shows as no active rule.
      </p>
    </div>
  )
}

function CellDetail({ cell }: { cell: CoverageCell }) {
  return (
    <div className="de-cov-detail" data-testid="coverage-cell">
      <b>{cell.name} <span className="x1-mono x1-muted">{cell.technique}</span> · {STATES[cell.state]}</b>
      <span className="x1-muted">
        {plural(cell.rules, 'active rule')} declare it{cell.disabled > 0 ? `, and ${plural(cell.disabled, 'disabled rule')}` : ''}. Seen on {plural(cell.seen, 'stored alert')}.
        {cell.replayed > 0 && ` ${plural(cell.replayed, 'rule')} passed a replay on stored events.`}
      </span>
      <table className="de-cov-siems">
        <thead><tr><th>SIEM</th><th>Copies</th><th>Proven</th><th>Failing Runs or Sees</th></tr></thead>
        <tbody>
          {SIEMS.map((s) => {
            const split = cell.siems[s.id]
            return (
              <tr key={s.id}>
                <td>{s.label}</td>
                <td>{split?.copies ?? 0}</td>
                <td className={split?.proven ? 'proven' : undefined}>{split?.proven ?? 0}</td>
                <td className={split?.failing ? 'failing' : undefined}>{split?.failing ?? 0}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
