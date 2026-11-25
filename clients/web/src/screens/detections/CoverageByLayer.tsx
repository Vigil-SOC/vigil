import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  detectionRulesApi, type CoverageByLayer as ByLayer, type CoverageLayer, type CoverageScope,
  type LayerCell, type LayerMark, type LayerRun, type LayerState, type LayerTechnique,
} from '../../services/api'
import { errorText } from '../../shared/errorText'
import Emulate from './Emulate'
import { ruleHref } from './proposalLinks'

// "By layer": each technique's state per detection layer and for the layers together, as
// GET /detection-rules/coverage?by_layer answers it. Nothing here recomputes a state.

const STATE: Record<LayerState, { label: string; glyph: string }> = {
  validated: { label: 'Validated', glyph: '✓' },
  partial: { label: 'Partial', glyph: '◐' },
  present: { label: 'Present, not validated', glyph: '○' },
  no_detection: { label: 'No detection', glyph: '✕' },
  not_checked: { label: 'Not checked', glyph: '?' },
}
const MARK: Record<LayerMark, { label: string; glyph: string }> = {
  tested_missed: { label: 'Tested, missed', glyph: '!' },
  shadow: { label: 'Caught in shadow', glyph: 's' },
  not_observable: { label: 'Not observable', glyph: '⊘' },
}
const NAMES: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk', wazuh: 'Wazuh', loglm: 'LogLM', combined: 'Combined' }
const layerName = (layer: string) => NAMES[layer] ?? layer
const formatDateTime = (iso: string) => new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

type Scope = CoverageScope | 'techniques'
type Pick = { technique: string; layer: string } | { layer: string; technique?: undefined }

/** Ids from a pasted list: split on commas, spaces or new lines; the server checks each. */
const parseIds = (text: string) => [...new Set(text.split(/[\s,;]+/).map((t) => t.trim().toUpperCase()).filter(Boolean))]

export default function CoverageByLayer({ tactics: picked }: { tactics: string[] | null }) {
  const [scope, setScope] = useState<Scope>('all')
  const [pasted, setPasted] = useState('')
  const [asked, setAsked] = useState<string[]>([])
  const [data, setData] = useState<ByLayer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Learned from the last intel answer; the scope control says so.
  const [intelOff, setIntelOff] = useState(false)
  const [sel, setSel] = useState<Pick | null>(null)
  const [tries, setTries] = useState(0)

  // Asked again on each scope or pasted list; an answer to an earlier ask is dropped.
  useEffect(() => {
    let live = true
    setBusy(true)
    setError(null)
    setSel(null)
    const call = scope === 'techniques' ? detectionRulesApi.coverageByLayer('all', asked) : detectionRulesApi.coverageByLayer(scope)
    call
      .then((res) => {
        if (!live) return
        const by = res.data.by_layer ?? null
        setData(by)
        if (by?.scope === 'intel') setIntelOff(by.intel?.connected === false)
      })
      .catch((e) => { if (live) { setData(null); setError(errorText(e, 'The map by layer did not load.')) } })
      .finally(() => { if (live) setBusy(false) })
    return () => { live = false }
  }, [scope, asked, tries])

  const applyList = () => {
    const ids = parseIds(pasted)
    if (!ids.length) return
    setAsked(ids)
    setScope('techniques')
  }
  const scopes: { id: CoverageScope; label: string }[] = [
    { id: 'all', label: 'All techniques' },
    { id: 'emulated', label: 'Emulated in the window' },
    { id: 'intel', label: intelOff ? 'Threat intelligence · not connected' : 'Threat intelligence' },
  ]
  const layers = data?.layers ?? []
  const shownTactics = (data?.tactics ?? []).filter((t) => !picked || picked.includes(t))
  const rows = (data?.techniques ?? []).filter((t) => shownTactics.includes(t.tactic))
  const pickedRow = sel ? layers.find((l) => l.layer === sel.layer) ?? null : null
  const pickedTech = sel?.technique ? rows.find((t) => t.technique === sel.technique) ?? null : null
  const m = data?.measures

  return (
    <div className="de-lay" data-testid="coverage-by-layer">
      <div className="de-lay-scope">
        <div className="de-cov-lens" role="group" aria-label="Techniques to answer for">
          {scopes.map((s) => (
            <button key={s.id} type="button" className={`de-cov-lens-b${scope === s.id ? ' on' : ''}`} aria-pressed={scope === s.id} onClick={() => setScope(s.id)}>{s.label}</button>
          ))}
        </div>
        <form className="de-lay-paste" onSubmit={(e) => { e.preventDefault(); applyList() }}>
          <input className="de-x-in" aria-label="Technique ids" placeholder="Paste ids: T1059, T1003.001" value={pasted} onChange={(e) => setPasted(e.target.value)} />
          <button type="submit" className={`de-cov-lens-b${scope === 'techniques' ? ' on' : ''}`} aria-pressed={scope === 'techniques'} disabled={!parseIds(pasted).length}>
            {scope === 'techniques' ? `Pasted list · ${asked.length}` : 'Answer for these'}
          </button>
        </form>
        <span className="de-note" role="status">{busy ? 'Reading every layer…' : ''}</span>
      </div>

      {error && <p className="de-lay-err" role="alert">{error} <button type="button" className="x1-link" onClick={() => setTries(tries + 1)}>Retry</button></p>}

      {data && data.intel?.connected === false && <p className="de-lay-msg">{data.note}</p>}

      {data && data.techniques && (
        <>
          {m && (
            <div className="de-lay-measures" role="group" aria-label="Measures">
              <span><b>{m.techniques}</b> techniques</span>
              <span><b>{m.validated.combined ?? 0}</b> validated by the layers together</span>
              {layers.filter((l) => l.kind !== 'combined').map((l) => (
                <span key={l.layer}><b>{m.validated[l.layer] ?? 0}</b> by {layerName(l.layer)}</span>
              ))}
              <span><b>{m.validated_by_one_layer}</b> by one layer only</span>
              <span><b>{m.present_never_validated}</b> present, never validated</span>
              <span><b>{m.not_observable}</b> not observable by any layer</span>
              {m.loglm_lift !== undefined && <span><b>{m.loglm_lift}</b> validated only with LogLM (lift)</span>}
            </div>
          )}
          {data.emulation?.error && <p className="de-lay-err">{data.emulation.error}</p>}
          {data.emulation?.note && <p className="de-lay-msg">{data.emulation.note}</p>}
          <div className="de-cov-legend">
            {(Object.keys(STATE) as LayerState[]).map((s) => (
              <span key={s}><span className={`de-lay-g ${s}`} aria-hidden="true">{STATE[s].glyph}</span>{STATE[s].label}</span>
            ))}
            {(Object.keys(MARK) as LayerMark[]).map((k) => (
              <span key={k}><span className="de-lay-mk" aria-hidden="true">{MARK[k].glyph}</span>{MARK[k].label}</span>
            ))}
          </div>
          {rows.length === 0
            ? <p className="de-note">No technique in this scope{picked ? ' under the tactics shown' : ''}.</p>
            : <Grid layers={layers} tactics={shownTactics} rows={rows} sel={sel} onSelect={setSel} />}
          {pickedTech && sel && <CellPanel tech={pickedTech} layer={sel.layer} />}
          {!sel?.technique && pickedRow && <RowPanel row={pickedRow} />}
          {(data.unlabelled?.length ?? 0) > 0 && (
            <p className="de-note">Sources with findings in the window but no technique label, so no row: {data.unlabelled!.join(', ')}.</p>
          )}
          <p className="de-note">States are the server’s, from the newest compose emulation run in the last {data.window_days ?? 30} days that scored the technique; older runs are evidence. Measures count the whole scope, whichever tactics are shown; counts only, no percentage.</p>
        </>
      )}
    </div>
  )
}

/** Layers down, techniques across under their tactics; the combined row last, as the server orders them. */
function Grid({ layers, tactics, rows, sel, onSelect }: { layers: CoverageLayer[]; tactics: string[]; rows: LayerTechnique[]; sel: Pick | null; onSelect: (p: Pick | null) => void }) {
  const byTactic = tactics.map((t) => ({ tactic: t, techs: rows.filter((r) => r.tactic === t) }))
  return (
    <div className="de-matrix-wrap de-lay-wrap">
      <table className="de-lay-grid">
        <thead>
          <tr>
            <th className="de-lay-corner" rowSpan={2}>Layer</th>
            {byTactic.map(({ tactic, techs }) => <th key={tactic} colSpan={techs.length} className="de-lay-tac" scope="colgroup">{tactic}</th>)}
          </tr>
          <tr>
            {byTactic.map(({ tactic, techs }) => (
              <Fragment key={tactic}>
                {techs.map((t, i) => <th key={t.technique} scope="col" className={`de-lay-tech${i === 0 ? ' first' : ''}`} title={t.name}>{t.technique}</th>)}
              </Fragment>
            ))}
          </tr>
        </thead>
        <tbody>
          {layers.map((l) => {
            const rowOn = sel?.layer === l.layer && !sel.technique
            return (
              <tr key={l.layer} className={l.kind === 'combined' ? 'combined' : undefined}>
                <th scope="row" className="de-lay-row">
                  <button type="button" className={`de-lay-rowb${rowOn ? ' on' : ''}`} aria-pressed={rowOn} onClick={() => onSelect(rowOn ? null : { layer: l.layer })}>
                    {layerName(l.layer)}{l.state === 'not_checked' && <span className="x1-muted"> · not checked</span>}
                  </button>
                </th>
                {byTactic.map(({ tactic, techs }) => (
                  <Fragment key={tactic}>
                    {techs.map((t, i) => {
                      const c = t.layers[l.layer]
                      const on = sel?.layer === l.layer && sel.technique === t.technique
                      return (
                        <td key={t.technique} className={i === 0 ? 'first' : undefined}>
                          {c && <LayerCellButton cell={c} on={on} label={`${t.technique} ${t.name}, ${layerName(l.layer)}`} onClick={() => onSelect(on ? null : { layer: l.layer, technique: t.technique })} />}
                        </td>
                      )
                    })}
                  </Fragment>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LayerCellButton({ cell, on, label, onClick }: { cell: LayerCell; on: boolean; label: string; onClick: () => void }) {
  const words = [STATE[cell.state].label, ...(cell.marks ?? []).map((k) => MARK[k].label.toLowerCase())].join(', ')
  return (
    <button type="button" className={`de-cell de-lay-cell ${cell.state}${on ? ' on' : ''}`} aria-pressed={on} aria-label={`${label}: ${words}`} title={cell.reason ? `${words}: ${cell.reason}` : words} onClick={onClick}>
      <span aria-hidden="true">{STATE[cell.state].glyph}{(cell.marks ?? []).map((k) => <span key={k} className="de-lay-mk">{MARK[k].glyph}</span>)}</span>
    </button>
  )
}

function RunLine({ run }: { run: LayerRun }) {
  return (
    <li>
      {formatDateTime(run.finished_at)} · {run.hosts.length ? run.hosts.join(', ') : 'no host named'} · {run.steps_caught} of {plural(run.steps_run, 'step')} caught ·{' '}
      {run.run_id
        ? <Link className="de-link" to={`/workflows?run=${encodeURIComponent(run.run_id)}`}>Open run</Link>
        : <span className="x1-muted" title="It ran on a Case you can’t read: counted, not linked">restricted</span>}
    </li>
  )
}

function CellPanel({ tech, layer }: { tech: LayerTechnique; layer: string }) {
  const cell = tech.layers[layer]
  if (!cell) return null
  const ev = cell.evidence ?? {}
  const older = ev.older_runs ?? []
  const restricted = [ev.newest_run, ...older].filter((r) => r?.restricted).length
  return (
    <div className="de-cov-detail" data-testid="coverage-layer-cell">
      <div className="de-cov-detail-head">
        <b>
          {tech.name} <span className="x1-mono x1-muted">{tech.technique}</span> · {layerName(layer)} ·{' '}
          <span className={`de-lay-g ${cell.state}`} aria-hidden="true">{STATE[cell.state].glyph}</span> {STATE[cell.state].label}
          {(cell.marks ?? []).map((k) => <span key={k} className="de-lay-mark"> · {MARK[k].glyph} {MARK[k].label}</span>)}
        </b>
        <Emulate key={`${tech.technique}-${layer}`} className="btn ghost" goal={`Emulate ${tech.technique} (${tech.name}) on a listed host and report which detection layers caught it. By layer, ${layerName(layer)} reads ${STATE[cell.state].label.toLowerCase()}.`} />
      </div>
      {cell.reason && <span className="x1-muted">Not checked: {cell.reason}</span>}
      {ev.rules && (ev.rules.length
        ? (
          <table className="de-cov-siems">
            <thead><tr><th>Rule</th><th>Runs</th><th>Sees</th><th>Caught</th></tr></thead>
            <tbody>
              {ev.rules.map((r) => (
                <tr key={r.rule_id}>
                  <td><Link className="de-link" to={ruleHref(r.rule_id)}>{r.name}</Link></td>
                  <td className={r.runs === 'fail' ? 'failing' : r.runs === 'pass' ? 'proven' : undefined}>{r.runs ?? '—'}</td>
                  <td className={r.sees === 'fail' ? 'failing' : r.sees === 'pass' ? 'proven' : undefined}>{r.sees ?? '—'}</td>
                  <td>{r.caught ? `${r.caught}${r.shadow ? ' (shadow)' : ''}${r.caught_at ? ` · ${formatDateTime(r.caught_at)}` : ''}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
        : <span className="x1-muted">No active rule in {layerName(layer)} declares it.</span>)}
      {ev.blind_spot && <span>Blind spot: <span className="x1-mono">{ev.blind_spot.join(', ')}</span></span>}
      {ev.labelled !== undefined && <span>{plural(ev.labelled, 'finding')} labelled with it in the window.</span>}
      {ev.loglm_flags !== undefined && <span>{plural(ev.loglm_flags, 'LogLM flag')} on it in the window.</span>}
      {ev.newest_run
        ? <div><span className="x1-muted">Newest run</span><ul className="de-lay-runs"><RunLine run={ev.newest_run} /></ul></div>
        : cell.state !== 'not_checked' && <span className="x1-muted">No emulation run in the window scored it.</span>}
      {older.length > 0 && <div><span className="x1-muted">Older runs · {older.length}</span><ul className="de-lay-runs">{older.map((r, i) => <RunLine key={r.run_id ?? `r${i}`} run={r} />)}</ul></div>}
      {restricted > 0 && <span className="x1-muted">{plural(restricted, 'run')} on a Case you can’t read: counted, not linked.</span>}
    </div>
  )
}

function RowPanel({ row }: { row: CoverageLayer }) {
  return (
    <div className="de-cov-detail" data-testid="coverage-layer-row">
      <b>{layerName(row.layer)}{row.state === 'not_checked' && ' · Not checked'}</b>
      <span>{row.reads}</span>
      {row.rules !== undefined && <span>{plural(row.rules, 'active rule')}.</span>}
      {row.observable && <span>{row.observable}</span>}
      {row.reason && <span className="x1-muted">Not checked: {row.reason}</span>}
    </div>
  )
}
