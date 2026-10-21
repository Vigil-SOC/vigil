import { useEffect, useState } from 'react'
import { fmtCost } from '../../shared/cost'
import { workflowApi } from '../../services/api'
import { toolShown, type CallRow, type RunFold } from './caseFold'
import { RunTag } from './RunTag'

// What a reader checks a record against: the query a call sent and what came back,
// read whole off the run's ledger, and the Checked tab's list of them by turn.

interface CallRecord {
  ref: string
  tool: string
  arguments: string
  result: string
  ok: boolean
  class?: string
  failure?: unknown
  duration_ms?: number
  ts?: string
}

const DASH = '—'

/** The result's body without the delimiter every tool result is wrapped in. */
function bodyOf(result: string): string {
  return /^<vigil:tool_result[^>]*>\n?([\s\S]*?)\n?<\/vigil:tool_result>$/.exec(result)?.[1] ?? result
}

/** "N row(s) from X" then a JSON array: the rows, when they are flat enough for a table. */
function tableOf(body: string): { head: string; columns: string[]; rows: Record<string, unknown>[] } | null {
  const split = body.indexOf('\n')
  if (split < 0) return null
  try {
    const parsed: unknown = JSON.parse(body.slice(split + 1))
    if (!Array.isArray(parsed) || parsed.length === 0) return null
    const rows = parsed.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object' && !Array.isArray(row))
    if (rows.length !== parsed.length) return null
    const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))]
    // A single nested record ("success, query, results: [...]") reads better as its results.
    if (rows.length === 1 && Array.isArray(rows[0]!.results)) {
      const inner = (rows[0]!.results as unknown[]).filter((row): row is Record<string, unknown> => !!row && typeof row === 'object' && !Array.isArray(row))
      // Its first line counted the wrapper; the count a reader wants is the results'.
      const head = body.slice(0, split).replace(/^\d+ rows?(\(s\))?/, `${inner.length} ${inner.length === 1 ? 'row' : 'rows'}`)
      if (inner.length > 0) return { head, columns: [...new Set(inner.flatMap((row) => Object.keys(row)))].slice(0, 14), rows: inner }
    }
    return { head: body.slice(0, split), columns: columns.slice(0, 14), rows }
  } catch {
    return null
  }
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

/** The arguments as a person reads them: the query on its own, the rest as name = value. */
function argumentsOf(text: string): { query: string | null; rest: [string, string][] } {
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>
    const key = ['spl_query', 'query', 'search', 'kql', 'sql', 'q'].find((one) => typeof parsed[one] === 'string')
    return {
      query: key ? (parsed[key] as string) : null,
      rest: Object.entries(parsed).filter(([name]) => name !== key).map(([name, value]) => [name, cell(value)]),
    }
  } catch {
    return { query: text, rest: [] }
  }
}

/** One call read whole; `label` is its tool in words, from the row that opened it. */
export function CallDetail({ runId, callRef, label }: { runId: string; callRef: string; label: string }) {
  const [call, setCall] = useState<CallRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setCall(null)
    setError(null)
    workflowApi
      .getCall(runId, callRef)
      .then((res) => !cancelled && setCall(res.data as CallRecord))
      .catch((e) => !cancelled && setError(e?.response?.data?.detail || 'Could not read this query'))
    return () => {
      cancelled = true
    }
  }, [runId, callRef])

  if (error) return <div className="call-detail muted">{error}</div>
  if (!call) return <div className="call-detail muted">Loading {callRef}…</div>
  const args = argumentsOf(call.arguments)
  const body = bodyOf(call.result)
  const table = call.ok ? tableOf(body) : null
  const failure = call.failure && typeof call.failure === 'object' ? cell((call.failure as { detail?: unknown }).detail) : cell(call.failure)
  return (
    <div className="call-detail">
      <div className="call-meta">
        <span className="mono">{call.ref}</span>
        <span>{label}</span>
        <span className={`call-class ${call.class ?? ''}`}>{call.ok ? call.class === 'visibility_gap' ? 'could not look' : 'answered' : 'failed'}</span>
        {typeof call.duration_ms === 'number' && <span>{call.duration_ms < 1000 ? `${call.duration_ms} ms` : `${(call.duration_ms / 1000).toFixed(1)} s`}</span>}
      </div>
      <div className="call-section">
        <span className="call-label">Query sent</span>
        {args.query && <pre className="call-query">{args.query}</pre>}
        {args.rest.length > 0 && (
          <dl className="call-args">
            {args.rest.map(([name, value]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd className="mono">{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
      <div className="call-section">
        <span className="call-label">{call.ok ? (table ? `What came back · ${table.head}` : 'What came back') : 'Why it failed'}</span>
        {!call.ok && <p className="call-failure">{failure || body}</p>}
        {table ? (
          <div className="call-table-wrap">
            <table className="call-table">
              <thead>
                <tr>{table.columns.map((column) => <th key={column}>{column}</th>)}</tr>
              </thead>
              <tbody>
                {table.rows.slice(0, 100).map((row, i) => (
                  <tr key={i}>{table.columns.map((column) => <td key={column} title={cell(row[column])}>{cell(row[column])}</td>)}</tr>
                ))}
              </tbody>
            </table>
            {table.rows.length > 100 && <p className="muted">Showing 100 of {table.rows.length} rows.</p>}
          </div>
        ) : (
          call.ok && <pre className="call-raw">{body}</pre>
        )}
      </div>
    </div>
  )
}

function latency(ms?: number): string {
  if (typeof ms !== 'number') return DASH
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

function outcome(call: CallRow): string {
  if (call.class === 'visibility_gap') return 'could not look'
  if (call.class === 'defect') return 'refused'
  if (typeof call.rows === 'number') return `${call.rows.toLocaleString()} ${call.rows === 1 ? 'row' : 'rows'}`
  if (call.failed) return 'failed'
  return `${call.result_length.toLocaleString()} chars`
}

/** A turn's model spend, to the digit that shows it: a tenth of a cent is not $0.00. */
function turnCost(usd: number): string {
  return fmtCost(usd, undefined, usd > 0 && usd < 0.01 ? 4 : 2)
}

interface Turn {
  key: string
  iteration: number | null
  thought: string
  cost: number | null
  calls: CallRow[]
}

/** Calls in run order, one group per turn, its words once above its queries. */
function turnsOf(calls: readonly CallRow[]): Turn[] {
  const turns: Turn[] = []
  for (const call of calls) {
    const thought = call.thought ?? call.question
    const last = turns[turns.length - 1]
    if (last && last.iteration === (call.iteration ?? null) && last.thought === thought) {
      last.calls.push(call)
      continue
    }
    turns.push({ key: `${call.iteration ?? 'x'}-${turns.length}`, iteration: call.iteration ?? null, thought, cost: call.cost_usd, calls: [call] })
  }
  return turns
}

export function CheckedList({ runId, calls, focusRef, tag }: { runId: string | null; calls: readonly CallRow[]; focusRef?: string | null; tag?: string }) {
  const [open, setOpen] = useState<string | null>(focusRef ?? null)
  useEffect(() => {
    if (focusRef) setOpen(focusRef)
  }, [focusRef])
  return (
    <div className="checked-turns">
      {turnsOf(calls).map((turn) => (
        <section key={turn.key} className="checked-turn">
          <header className="checked-turn-head">
            {tag && <RunTag label={tag} />}
            <span className="mono">{turn.iteration === null ? 'Turn' : `Turn ${turn.iteration}`}</span>
            {turn.cost !== null && <span className="mono">{turnCost(turn.cost)}</span>}
            <p title={turn.thought}>{turn.thought || DASH}</p>
          </header>
          <ul className="checked-calls">
            {turn.calls.map((call, i) => {
              const id = call.ref ?? `${turn.key}-${i}`
              const expandable = !!call.ref && !!runId
              const isOpen = open === id
              return (
                <li key={id} className={isOpen ? 'open' : undefined} data-call-ref={call.ref}>
                  <button
                    type="button"
                    className="checked-call"
                    disabled={!expandable}
                    aria-expanded={expandable ? isOpen : undefined}
                    onClick={() => setOpen(isOpen ? null : id)}
                  >
                    <span className="mono ref">{call.ref ?? DASH}</span>
                    <span className="tool">{toolShown(call)}</span>
                    <span className="mono query" title={call.query}>{call.query || DASH}</span>
                    <span className={`num ${call.class === 'served' || (!call.class && !call.failed) ? '' : 'poor'}`}>{outcome(call)}</span>
                    <span className="num">{latency(call.duration_ms)}</span>
                    <span className="chev" aria-hidden="true">{expandable ? (isOpen ? '▾' : '▸') : ''}</span>
                  </button>
                  {isOpen && runId && call.ref && <CallDetail runId={runId} callRef={call.ref} label={toolShown(call)} />}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </div>
  )
}

const FAILED_WORD: Record<string, string> = {
  timeout: 'timed out',
  unavailable: 'unreachable',
  backend_error: 'errored',
  visibility_gap: 'could not answer',
}

// The words a hunt files a failed call under: the call row already says it, and better.
const GENERIC_GAP = 'a query the hunt wanted could not be run' // compared case-blind: older runs wrote it lower-case

interface GapGroup {
  key: string
  source: string
  why: string
  calls: CallRow[]
  note: string | null
}

/** What the run could not see, one group per source and kind of failure: how many of
 *  its queries it lost, the run's own account of it once, and each lost query. */
export function blindSpotsOf(fold: RunFold | null): { groups: GapGroup[]; unconnected: string[] } {
  if (!fold) return { groups: [], unconnected: [] }
  const groups = new Map<string, GapGroup>()
  for (const call of fold.calls) {
    if (call.class !== 'visibility_gap' && !(call.failed && FAILED_WORD[call.failed])) continue
    const why = FAILED_WORD[call.failed ?? ''] ?? FAILED_WORD.visibility_gap
    const source = toolShown(call)
    const key = `${source}|${why}`
    const group = groups.get(key) ?? { key, source, why, calls: [], note: null }
    group.calls.push(call)
    groups.set(key, group)
  }
  const unconnected: string[] = []
  const notes: string[] = []
  if (fold.kind === 'hunt') {
    for (const row of fold.evidence) {
      if (!row.is_gap) continue
      const text = row.gap_detail || row.summary
      if (row.provenance === 'deployment_gap') {
        unconnected.push(text)
        continue
      }
      if (text && text.toLowerCase() !== GENERIC_GAP) notes.push(text)
    }
  }
  const list = [...groups.values()].sort((a, b) => b.calls.length - a.calls.length)
  // The run's own account of what it could not reach, kept once, on the source it lost most to.
  const note = [...new Set(notes)].join(' ')
  if (list[0] && note) list[0].note = note
  if (list.length === 0 && note) list.push({ key: 'note', source: 'Telemetry', why: 'could not answer', calls: [], note })
  return { groups: list, unconnected }
}

export function BlindSpots({ fold, runId, tag }: { fold: RunFold | null; runId: string | null; tag?: string }) {
  const [open, setOpen] = useState<string | null>(null)
  const [call, setCall] = useState<string | null>(null)
  const { groups, unconnected } = blindSpotsOf(fold)
  const total = fold?.calls.length ?? 0
  if (groups.length === 0 && unconnected.length === 0) return null
  return (
    <div className="blind-spots">
      {groups.map((group) => {
        const isOpen = open === group.key
        return (
          <div key={group.key} className={isOpen ? 'blind-group open' : 'blind-group'}>
            <button type="button" className="blind-head" aria-expanded={isOpen} disabled={group.calls.length === 0} onClick={() => setOpen(isOpen ? null : group.key)}>
              <span className="blind-dot" aria-hidden="true" />
              <span className="blind-title">
                <b>{group.source}</b> {group.why}
                {tag && <RunTag label={tag} />}
              </span>
              <span className="blind-count">
                {group.calls.length > 0 ? `${group.calls.length} of ${total} ${total === 1 ? 'query' : 'queries'} lost` : ''}
              </span>
              <span className="chev" aria-hidden="true">{group.calls.length > 0 ? (isOpen ? '▾' : '▸') : ''}</span>
            </button>
            {group.note && <p className="blind-note" title={group.note}>{group.note}</p>}
            {isOpen && (
              <ul className="blind-calls">
                {group.calls.map((lost, i) => {
                  const id = lost.ref ?? `${group.key}-${i}`
                  return (
                    <li key={id}>
                      <button type="button" className="checked-call" disabled={!lost.ref || !runId} onClick={() => setCall(call === id ? null : id)}>
                        <span className="mono ref">{lost.ref ?? DASH}</span>
                        <span className="tool">{toolShown(lost)}</span>
                        <span className="mono query" title={lost.query}>{lost.query || DASH}</span>
                        <span className="num poor">{group.why}</span>
                        <span className="num">{latency(lost.duration_ms)}</span>
                        <span className="chev" aria-hidden="true">{lost.ref && runId ? (call === id ? '▾' : '▸') : ''}</span>
                      </button>
                      {call === id && runId && lost.ref && <CallDetail runId={runId} callRef={lost.ref} label={toolShown(lost)} />}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )
      })}
      {unconnected.map((text, i) => (
        <div key={`u${i}`} className="blind-group">
          <div className="blind-head static">
            <span className="blind-dot muted" aria-hidden="true" />
            <span className="blind-title">
              <b>Not connected</b> {text}
              {tag && <RunTag label={tag} />}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}
