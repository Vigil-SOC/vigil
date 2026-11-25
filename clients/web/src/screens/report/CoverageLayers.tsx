import type { Schema } from '../../services/apiTypes'
import { reportStamp } from './stamp'

type Report = Schema<'Day1Report'>
type Comparison = Report['coverage_comparison']

// The stored by-layer body (core/detections/inventory/coverage.py by_layer), as
// evidence.coverage carries it. Read defensively: it is untyped evidence.
type Run = {
  run_id?: string | null
  restricted?: boolean
  finished_at?: string
  hosts?: string[]
  steps_caught?: number
  steps_run?: number
}
type Cell = { state: string; marks?: string[]; reason?: string; evidence?: { newest_run?: Run } }
type LayerRow = { layer: string; kind: string; state?: string; reason?: string }
type Technique = { technique: string; name?: string; tactic?: string; layers: Record<string, Cell> }
type Measures = {
  techniques: number
  validated: Record<string, number>
  validated_by_one_layer: number
  present_never_validated: number
  not_observable: number
  loglm_lift?: number
}
type Body = {
  not_checked?: string
  window_days?: number
  layers?: LayerRow[]
  techniques?: Technique[]
  unlabelled?: string[]
  emulation?: { runs: number; error?: string; note?: string }
  measures?: Measures
}

const COMBINED = 'combined'
const LOGLM = 'loglm'
const PRODUCT: Record<string, string> = {
  elastic: 'Elastic',
  splunk: 'Splunk',
  wazuh: 'Wazuh',
  loglm: 'LogLM',
  crowdstrike: 'CrowdStrike',
  defender: 'Defender',
}
const STATE: Record<string, string> = {
  validated: 'Validated',
  partial: 'Partial',
  present: 'Present',
  no_detection: 'No detection',
  not_checked: 'Not checked',
}
const MARK: Record<string, string> = {
  tested_missed: 'tested, missed',
  shadow: 'shadow',
  not_observable: 'not observable',
}

export function layersBody(evidence: Report['evidence'] | undefined): Body | null {
  const body = evidence?.coverage
  return body && typeof body === 'object' ? (body as Body) : null
}

/** Names layers as stored, or numbered in the order of layers[] when hidden. Combined stays Combined. */
export function layerNamer(body: Body, hide: boolean): (key: string) => string {
  const order = (body.layers ?? []).map((row) => row.layer).filter((key) => key !== COMBINED)
  return (key) => {
    if (key === COMBINED) return 'Combined'
    if (!hide) return PRODUCT[key] ?? key
    const at = order.indexOf(key)
    return at >= 0 ? `Layer ${at + 1}` : 'Layer'
  }
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/** The measures as lines, for the section and the print summary. */
// Products other modules' check reasons name, beside the stored layer and source keys.
const NAMED = ['CrowdStrike', 'Defender', 'Wazuh', 'Elastic', 'Kibana', 'Splunk', 'Sentinel', 'LogLM']

/** Masks product and source names in free text, for the print summary with names hidden. */
export function nameMasker(body: Body | null): (text: string) => string {
  const keys = [...(body?.layers ?? []).map((row) => row.layer), ...(body?.unlabelled ?? [])]
  const words = [...NAMED, ...keys.filter((key) => key !== COMBINED && key.length > 2)]
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const pattern = new RegExp(`\\b(${escaped.join('|')})\\b`, 'gi')
  return (text) => text.replace(pattern, 'a source')
}

export function measureLines(body: Body, name: (key: string) => string): string[] {
  const m = body.measures
  if (!m) return []
  // In the order of layers[]; the stored map's key order is not kept.
  const order = (body.layers ?? []).map((row) => row.layer)
  const validated = order
    .filter((key) => key in (m.validated ?? {}))
    .map((key) => `${name(key)} ${m.validated[key]}`)
    .join(' · ')
  const lines = [
    `${plural(m.techniques, 'technique', 'techniques')} read${body.window_days != null ? ` over ${body.window_days} days` : ''}.`,
    `Validated by layer: ${validated || 'none'}.`,
    `Validated by one layer only: ${m.validated_by_one_layer}.`,
    `Present, never validated: ${m.present_never_validated}.`,
    `Not observable: ${m.not_observable}.`,
  ]
  if (m.loglm_lift != null) lines.push(`Validated only with ${name(LOGLM)}: ${m.loglm_lift}.`)
  return lines
}

export function comparisonLine(comparison: Comparison | undefined, comparedAt: string | null): string | null {
  if (!comparison) return null
  const against = comparedAt ? `Against ${reportStamp(comparedAt)}: ` : ''
  if (comparison === 'not_in_compared_run') return `${against}Not in the compared run.`
  const c = comparison.counts
  return `${against}${c.improved} improved, ${c.regressed} regressed, ${c.unchanged} unchanged${c.not_compared ? `, ${c.not_compared} not compared` : ''}.`
}

function cellWord(cell: Cell | undefined): string {
  if (!cell) return '—'
  const marks = (cell.marks ?? []).map((mark) => MARK[mark] ?? mark)
  return [STATE[cell.state] ?? cell.state, ...marks].join(', ')
}

function runLine(run: Run): string {
  const hosts = run.hosts?.length ? ` on ${run.hosts.join(', ')}` : ''
  const at = run.finished_at ? `, ${reportStamp(run.finished_at)}` : ''
  const id = run.restricted ? ' (a restricted run)' : ''
  return `${run.steps_caught ?? 0} of ${run.steps_run ?? 0} steps caught${hosts}${at}${id}`
}

export function CoverageLayers({
  body,
  comparison,
  comparedAt,
  preface,
  hideNames,
}: {
  body: Body
  comparison: Comparison | undefined
  comparedAt: string | null
  preface: string | null | undefined
  hideNames: boolean
}) {
  if (body.not_checked) {
    return (
      <>
        <p className="d1-sub">By layer</p>
        <p className="d1-quiet">{body.not_checked}</p>
      </>
    )
  }
  const name = layerNamer(body, hideNames)
  const layers = body.layers ?? []
  const techniques = body.techniques ?? []
  const unlabelled = body.unlabelled ?? []
  const compared = comparisonLine(comparison, comparedAt)
  const moves = comparison && comparison !== 'not_in_compared_run' ? (comparison.moves ?? []) : []
  // The validations: each technique's combined newest run, which names its hosts and steps.
  // The grid keeps techniques with something on a layer; the rest read No detection throughout.
  const shown = techniques.filter((row) =>
    Object.values(row.layers).some((cell) => cell.state !== 'no_detection' || (cell.marks ?? []).length > 0),
  )
  const blank = techniques.length - shown.length
  const proof = techniques.flatMap((row) => {
    const run = row.layers[COMBINED]?.evidence?.newest_run
    return run ? [{ row, run }] : []
  })
  return (
    <>
      <p className="d1-sub">By layer</p>
      {preface && <p className="d1-quiet">{preface}</p>}
      <div className="d1-rows">
        <div className="d1-row">
          {measureLines(body, name).map((line) => (
            <p key={line}>{line}</p>
          ))}
          {layers
            .filter((row) => row.state === 'not_checked')
            .map((row) => (
              <p className="d1-facts" key={row.layer}>
                {name(row.layer)}: not checked.{hideNames ? '' : ` ${row.reason ?? ''}`}
              </p>
            ))}
          {unlabelled.length > 0 && (
            <p className="d1-facts">
              {hideNames
                ? `${plural(unlabelled.length, 'source sends', 'sources send')} findings with no technique label.`
                : `Findings with no technique label: ${unlabelled.join(', ')}.`}
            </p>
          )}
          {body.emulation?.error && <p className="d1-facts">{body.emulation.error}</p>}
          {body.emulation?.note && <p className="d1-facts">{body.emulation.note}</p>}
        </div>
      </div>
      {compared && (
        <div className="d1-versus">
          <p className="d1-versus-kind">{compared}</p>
          {moves.map((move) => (
            <p className="d1-compare" key={`${move.technique}-${move.layer}`}>
              {move.technique}
              {move.name ? ` ${move.name}` : ''} · {name(move.layer)}: {STATE[move.before] ?? move.before} →{' '}
              {STATE[move.after] ?? move.after} ({move.kind})
            </p>
          ))}
        </div>
      )}
      {proof.length > 0 && (
        <>
          <p className="d1-sub">What the validations rest on</p>
          <div className="d1-rows">
            {proof.map(({ row, run }) => (
              <div className="d1-row" key={row.technique}>
                <p>
                  {row.technique}
                  {row.name ? ` ${row.name}` : ''}: {runLine(run)}.
                </p>
              </div>
            ))}
          </div>
        </>
      )}
      {shown.length > 0 && (
        <>
          <p className="d1-sub">Techniques by layer</p>
          <table className="d1-grid d1-layer-grid">
            <thead>
              <tr>
                <th scope="col">Technique</th>
                {layers.map((row) => (
                  <th key={row.layer} scope="col">
                    {name(row.layer)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.technique}>
                  <th scope="row">
                    {row.technique}
                    {row.name ? ` ${row.name}` : ''}
                  </th>
                  {layers.map((layer) => (
                    <td key={layer.layer} data-label={name(layer.layer)}>
                      {cellWord(row.layers[layer.layer])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {blank > 0 && (
        <p className="d1-facts">
          {shown.length > 0 ? 'The other ' : ''}
          {plural(blank, 'technique reads', 'techniques read')} No detection on every layer.
        </p>
      )}
    </>
  )
}
