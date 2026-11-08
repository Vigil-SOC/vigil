import { useEffect, useState } from 'react'
import {
  detectionRulesApi, type CoverageCell, type CoverageSiemName, type CoverageState, type DetectionCoverage,
} from '../../services/api'
import { Empty, Failure } from '../shared/x1'

// The heat map is Alex Margaris's from the 1.x line (ProgramTab's HeatMap), here
// with main's meaning of proven (an active SIEM copy passes Runs and Sees) and a
// split by SIEM. It opens on a summary per tactic; the whole grid is "Full map".

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

const ORDER = Object.keys(STATES) as CoverageState[]

function countStates(cells: CoverageCell[], lens: Lens): Record<CoverageState, number> {
  const out: Record<CoverageState, number> = { proven: 0, unproven: 0, no_rule: 0 }
  for (const c of cells) out[stateIn(c, lens)] += 1
  return out
}

// The tactics a viewer chose to see, kept in this browser only.
const PICKED_KEY = 'vigil.coverageTactics'
function readPicked(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(PICKED_KEY) ?? '[]')
    return Array.isArray(raw) ? raw.filter((t): t is string => typeof t === 'string') : []
  } catch {
    return []
  }
}
function writePicked(picked: string[]) {
  try {
    localStorage.setItem(PICKED_KEY, JSON.stringify(picked))
  } catch {
    // private window or blocked storage: the choice lasts this visit only
  }
}

/** A technique's parent id: T1059 for T1059.001, itself for a parent. */
const parentOf = (technique: string) => technique.split('.')[0]

export default function CoverageMap({ goSettings }: { goSettings: (section: 'data') => void }) {
  const [coverage, setCoverage] = useState<DetectionCoverage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lens, setLens] = useState<Lens>('all')
  const [selected, setSelected] = useState<string | null>(null)
  const [full, setFull] = useState(false)
  // Tactics to show; none picked means all of them.
  const [picked, setPicked] = useState<string[]>(readPicked)
  const pick = (next: string[]) => { setPicked(next); writePicked(next) }

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
  const known = picked.filter((t) => coverage.tactics.includes(t))
  const tactics = known.length ? coverage.tactics.filter((t) => known.includes(t)) : coverage.tactics
  const total = countStates(coverage.techniques.filter((t) => tactics.includes(t.tactic)), lens)
  const toggleTactic = (tactic: string) => pick(known.includes(tactic) ? known.filter((t) => t !== tactic) : [...known, tactic])
  const select = (technique: string) => setSelected(selected === technique ? null : technique)
  const cell = coverage.techniques.find((t) => t.technique === selected) ?? null
  return (
    <div className="x1-card de-cov" data-testid="coverage-map">
      <div className="de-cov-head">
        <div className="de-cov-lens" role="group" aria-label="Coverage in">
          <button type="button" className={`de-cov-lens-b${lens === 'all' ? ' on' : ''}`} aria-pressed={lens === 'all'} onClick={() => setLens('all')}>Every SIEM</button>
          {present.map((s) => (
            <button key={s.id} type="button" className={`de-cov-lens-b${lens === s.id ? ' on' : ''}`} aria-pressed={lens === s.id} onClick={() => setLens(s.id)}>{s.label}</button>
          ))}
          <span className="de-cov-sep" aria-hidden="true" />
          <button type="button" className={`de-cov-lens-b${full ? ' on' : ''}`} aria-pressed={full} onClick={() => setFull(!full)}>Full map</button>
        </div>
        <div className="de-cov-legend">
          {ORDER.map((s) => (
            <span key={s}><span className={`de-swatch ${s}`} />{STATES[s]} · {total[s]}</span>
          ))}
        </div>
      </div>
      <div className="de-cov-tactics" role="group" aria-label="Tactics shown">
        <span className="x1-muted">Tactics</span>
        <button type="button" className={`de-cov-tac-b${known.length === 0 ? ' on' : ''}`} aria-pressed={known.length === 0} onClick={() => pick([])}>All</button>
        {coverage.tactics.map((tactic) => (
          <button key={tactic} type="button" className={`de-cov-tac-b${known.includes(tactic) ? ' on' : ''}`} aria-pressed={known.includes(tactic)} onClick={() => toggleTactic(tactic)}>{tactic}</button>
        ))}
      </div>
      {!full && <TacticSummary coverage={coverage} tactics={tactics} lens={lens} selected={selected} onSelect={select} />}
      {full && <div className="de-matrix-wrap">
        <div className="de-matrix">
          {tactics.map((tactic) => (
            <div key={tactic} className="de-col">
              <h4>{tactic}</h4>
              {coverage.techniques.filter((t) => t.tactic === tactic).map((t) => (
                <Cell key={t.technique} cell={t} lens={lens} on={selected === t.technique} onSelect={select} />
              ))}
            </div>
          ))}
        </div>
      </div>}
      {full && cell && <CellDetail cell={cell} />}
      <p className="de-note">
        Proven: an active copy in a SIEM passes Runs and Sees, as the Rules page counts it. A rule counts once however many SIEM copies it has. {plural(coverage.mapped, 'rule')} declare a technique, as on the Rules page; a disabled one is no coverage, so its technique shows as no active rule.
      </p>
    </div>
  )
}

function Cell({ cell, lens, on, onSelect, extra }: { cell: CoverageCell; lens: Lens; on: boolean; onSelect: (technique: string) => void; extra?: string }) {
  return (
    <button type="button" className={`de-cell ${stateIn(cell, lens)}${on ? ' on' : ''}`} aria-pressed={on} onClick={() => onSelect(cell.technique)}>
      <b>{cell.name}</b>
      <span className="x1-mono">{cell.technique}{cell.rules ? ` · ${plural(cell.rules, 'rule')}` : ''}{extra ? ` · ${extra}` : ''}</span>
    </button>
  )
}

/** One row per tactic: a bar and counts of its techniques' states; open one for its techniques. */
function TacticSummary({ coverage, tactics, lens, selected, onSelect }: { coverage: DetectionCoverage; tactics: string[]; lens: Lens; selected: string | null; onSelect: (technique: string) => void }) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <div className="de-tac" data-testid="coverage-tactics">
      {tactics.map((tactic) => {
        const cells = coverage.techniques.filter((t) => t.tactic === tactic)
        const n = countStates(cells, lens)
        const isOpen = open === tactic
        return (
          <div key={tactic} className={`de-tac-row${isOpen ? ' open' : ''}`}>
            <button type="button" className="de-tac-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : tactic)}>
              <span className="de-tac-name"><span aria-hidden="true">{isOpen ? '▾' : '▸'}</span> {tactic}</span>
              <span className="de-tac-bar" aria-hidden="true">
                {ORDER.map((s) => n[s] > 0 && <span key={s} className={`de-tac-seg ${s}`} style={{ flexGrow: n[s] }} />)}
              </span>
              <span className="de-tac-counts">
                <span className="proven">{n.proven} proven</span> · <span className="unproven">{n.unproven} rule, not proven</span> · <span className="no_rule">{n.no_rule} no active rule</span>
              </span>
            </button>
            {isOpen && <TacticTechniques cells={cells} lens={lens} selected={selected} onSelect={onSelect} />}
          </div>
        )
      })}
    </div>
  )
}

/** A tactic's techniques, each parent with its sub-techniques folded under it. */
function TacticTechniques({ cells, lens, selected, onSelect }: { cells: CoverageCell[]; lens: Lens; selected: string | null; onSelect: (technique: string) => void }) {
  const [unfolded, setUnfolded] = useState<Set<string>>(() => new Set())
  const groups = new Map<string, { parent: CoverageCell | null; subs: CoverageCell[] }>()
  for (const c of cells) {
    const key = parentOf(c.technique)
    const group = groups.get(key) ?? { parent: null, subs: [] }
    if (c.technique === key) group.parent = c
    else group.subs.push(c)
    groups.set(key, group)
  }
  const toggle = (key: string) => setUnfolded((u) => {
    const next = new Set(u)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  const cell = cells.find((t) => t.technique === selected) ?? null
  return (
    <div className="de-tac-body">
      <div className="de-tac-groups">
        {[...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, { parent, subs }]) => {
          const isOpen = unfolded.has(key)
          const subsLabel = subs.length ? plural(subs.length, 'sub-technique') : undefined
          return (
            <div key={key} className="de-tac-group">
              {parent
                ? <Cell cell={parent} lens={lens} on={selected === key} onSelect={onSelect} extra={subsLabel} />
                : <span className="de-tac-orphan x1-mono">{key} · {subsLabel}</span>}
              {subs.length > 0 && (
                <button type="button" className="x1-link de-tac-fold" aria-expanded={isOpen} onClick={() => toggle(key)}>
                  {isOpen ? 'Fold' : `Show ${subsLabel}`}
                </button>
              )}
              {isOpen && <div className="de-tac-subs">{subs.map((t) => <Cell key={t.technique} cell={t} lens={lens} on={selected === t.technique} onSelect={onSelect} />)}</div>}
            </div>
          )
        })}
      </div>
      {cell && <CellDetail cell={cell} />}
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
