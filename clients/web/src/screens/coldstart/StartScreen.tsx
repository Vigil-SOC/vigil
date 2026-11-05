import { useEffect, useReducer, useRef, useState, type ChangeEvent, type Dispatch, type ReactNode } from 'react'
import {
  coldStartApi,
  type ColdStartDaemonState,
  type ColdStartGroup,
  type ColdStartRun,
  type ColdStartRunRequest,
  type ColdStartSource,
  type ColdStartSources,
  type ColdStartUploadResult,
  type ColdStartVerdict,
} from '../../services/api'
import { useAuth } from '../../contexts/AuthContext'
import { GO_LIVE_SOURCE_PERM } from '../../shell/landing'
import { extractApiError } from '../../shared/formKit'
import type { ConsoleScreenProps } from '../../shared/types'
import './coldstart.css'

const HISTORY_DAYS = [7, 14, 30, 60, 90]
const DEFAULT_HISTORY_DAYS = 60
const POLL_MS = 2000
const SOURCES_POLL_MS = 15_000
const ALERT_FILE_TYPES = '.json,.jsonl,.csv,.parquet'
const TICKET_FILE_TYPES = '.json,.jsonl,.csv'

type Nav = Pick<ConsoleScreenProps, 'go' | 'goSettings'>

type Prefill = { source: string; historyDays: number }

type Flow =
  | { step: 'connect'; prefill?: Prefill }
  | { step: 'building'; run: ColdStartRun }
  | { step: 'failed'; run: ColdStartRun }
  | { step: 'review'; run: ColdStartRun }
  | { step: 'go-live'; run: ColdStartRun }
  | { step: 'live'; run: ColdStartRun }

type FlowAction =
  | { type: 'run'; run: ColdStartRun }
  | { type: 'continue' }
  | { type: 'back' }
  | { type: 'start-over'; prefill?: Prefill }

const flowOf = (run: ColdStartRun): Flow => {
  switch (run.status) {
    case 'collecting':
    case 'judging':
      return { step: 'building', run }
    case 'failed':
      return { step: 'failed', run }
    case 'ready':
      return { step: 'review', run }
    case 'live':
      return { step: 'live', run }
  }
}

const reduceFlow = (flow: Flow, action: FlowAction): Flow => {
  switch (action.type) {
    case 'run':
      return flowOf(action.run)
    case 'continue':
      return flow.step === 'review' ? { step: 'go-live', run: flow.run } : flow
    case 'back':
      return flow.step === 'go-live' ? { step: 'review', run: flow.run } : flow
    case 'start-over':
      return { step: 'connect', prefill: action.prefill }
  }
}

const STEPS = ['Connect', 'Building', 'Review', 'Go live']
const STEP_AT: Record<Flow['step'], number> = { connect: 0, building: 1, failed: 1, review: 2, 'go-live': 3, live: 4 }

type Stage = keyof ColdStartGroup['stage1']

// Worst first: a kind with one escalation among many closes still reads as Escalate.
const STAGES: { stage: Stage; label: string; tone: string }[] = [
  { stage: 'escalate', label: 'Escalate', tone: 'poor' },
  { stage: 'abstain', label: 'Needs judgment', tone: 'fair' },
  { stage: 'dismiss_candidate', label: 'Would close', tone: 'good' },
]

const SOURCE_COPY: Record<ColdStartSource['kind'], { title: (source: ColdStartSource) => string; detail: string }> = {
  federation: { title: (source) => source.label, detail: 'Reads past alerts from this source.' },
  upload: { title: () => 'Upload alerts', detail: 'JSON, JSONL, CSV or Parquet.' },
}

const n = (value: number) => value.toLocaleString()
const plural = (count: number, noun: string) => `${n(count)} ${noun}${count === 1 ? '' : 's'}`
const isStatus = (error: unknown, status: number) => (error as { response?: { status?: number } }).response?.status === status

const sourceFor = (sources: ColdStartSources, id: string): ColdStartSource =>
  sources.sources.find((source) => source.id === id) ?? { id, label: id, kind: 'federation', configured: true }

const historyName = (source: ColdStartSource) =>
  ({ federation: source.label, upload: 'your file' })[source.kind]

const dropReasons = (dropped: Record<string, number>) =>
  Object.entries(dropped).map(([reason, count]) => `${reason.replace(/_/g, ' ')} ${n(count)}`).join(', ')

const keptAndDropped = (noun: string, kept: number, dropped: Record<string, number>) => {
  const total = Object.values(dropped).reduce((sum, count) => sum + count, 0)
  return `Kept ${plural(kept, noun)}.${total ? ` Dropped ${n(total)}: ${dropReasons(dropped)}.` : ''}`
}

const RUN_DAEMON = 'Run it with ./start.sh -d or the compose daemon profile.'

const DAEMON_GAP: Record<Exclude<ColdStartDaemonState, 'running'>, string> = {
  not_running: 'The SOC daemon is not running, so nothing is polled or triaged, and the baseline is unused until it starts.',
  unhealthy: 'The SOC daemon is not healthy, so new alerts may not be polled or triaged, and the baseline may go unused until it recovers.',
  unknown: 'Vigil could not read the state of the SOC daemon, so new alerts may not be polled or triaged, and the baseline may go unused.',
}

/** What still keeps new alerts from being triaged, which is the only time the baseline is read. */
const triageGaps = ({ daemon, model }: ColdStartSources): string[] => [
  ...(daemon.state === 'running' ? [] : [`${DAEMON_GAP[daemon.state]} ${RUN_DAEMON}`]),
  ...(daemon.auto_triage ? [] : ['Auto-triage is off (DAEMON_AUTO_TRIAGE), so new alerts are not triaged and the baseline is unused until it is on.']),
  ...(model ? [] : ['No model is assigned to triage, so new alerts are not triaged and the baseline is unused until one is.']),
]

// An upper bound rounds up, so the wording never understates the risk.
const missBoundWords = (bound: number) => `at most ${(Math.ceil(bound * 1000 - 1e-9) / 10).toFixed(1)}% real, 95% confidence`

const modelMethod = (model: ColdStartSources['model']) => {
  if (!model) return 'No model'
  return model.provider === 'ollama' ? 'Local model' : `${model.provider} model`
}

/** One button's request: its in-flight flag and the error to show beside it. */
function useRequest() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const send = async (work: () => Promise<void>, fallback: string) => {
    setBusy(true)
    setError(null)
    try {
      await work()
    } catch (e) {
      setError(extractApiError(e, fallback))
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, send }
}

type Files = { alerts?: File | null; tickets?: File | null }
type Added = { run: ColdStartRun; notice: string | null }

const uploaded = (noun: string, { data }: { data: ColdStartUploadResult }) => keptAndDropped(noun, data.kept, data.dropped)

/** Sends a run its files, then has it rebuild from them. A rejected file becomes a notice, because the run itself is still good. */
async function addFiles(run: ColdStartRun, { alerts, tickets }: Files): Promise<Added> {
  const notes: string[] = []
  if (alerts) {
    try {
      notes.push(uploaded('alert', await coldStartApi.uploadAlerts(run.id, alerts)))
    } catch (e) {
      return { run, notice: `The alerts file was not accepted. ${extractApiError(e, 'Try another file.')}` }
    }
  }
  let ticketsAdded = false
  if (tickets) {
    try {
      notes.push(uploaded('closed ticket', await coldStartApi.uploadTickets(run.id, tickets)))
      ticketsAdded = true
    } catch (e) {
      notes.push(`Closed tickets were not added. ${extractApiError(e, 'Try another file.')} You can add them on the review step.`)
    }
  }
  if (!alerts && !ticketsAdded) return { run, notice: notes.join(' ') || null }
  try {
    await coldStartApi.rebuild(run.id)
  } catch (e) {
    // A 409 means the run is still building, and that build reads the upload itself.
    if (!isStatus(e, 409)) throw e
  }
  const { data } = await coldStartApi.getRun(run.id)
  return { run: data, notice: notes.join(' ') || null }
}

type Load =
  | { at: 'loading' }
  | { at: 'failed'; message: string }
  | { at: 'ready'; sources: ColdStartSources; latest: ColdStartRun | null }

/** The daemon can start while this page is open, and what the page says about it must follow. */
function useFreshSources(first: ColdStartSources): ColdStartSources {
  const [sources, setSources] = useState(first)
  useEffect(() => {
    let live = true
    const id = window.setInterval(() => {
      coldStartApi.sources().then(({ data }) => live && setSources(data)).catch(() => undefined)
    }, SOURCES_POLL_MS)
    return () => {
      live = false
      window.clearInterval(id)
    }
  }, [])
  return sources
}

/** Day one: a baseline from the alert history the customer already has. Every figure comes from the API. */
export default function StartScreen({ go, goSettings }: ConsoleScreenProps) {
  const [load, setLoad] = useState<Load>({ at: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let live = true
    Promise.all([
      coldStartApi.sources(),
      coldStartApi.latestRun().then(({ data }) => data).catch((e) => {
        if (isStatus(e, 404)) return null
        throw e
      }),
    ])
      .then(([{ data: sources }, latest]) => live && setLoad({ at: 'ready', sources, latest }))
      .catch((e) => live && setLoad({ at: 'failed', message: extractApiError(e, 'The request failed.') }))
    return () => {
      live = false
    }
  }, [attempt])

  if (load.at === 'loading') {
    return <div className="cs"><div className="cs-card cs-muted" role="status">Checking what is connected.</div></div>
  }
  if (load.at === 'failed') {
    return (
      <div className="cs">
        <div className="cs-card cs-error" role="alert" style={{ display: 'grid', gap: 8, justifyItems: 'start' }}>
          <span>Could not read the setup state. {load.message}</span>
          <button type="button" className="btn ghost" onClick={() => { setLoad({ at: 'loading' }); setAttempt(attempt + 1) }}>Try again</button>
        </div>
      </div>
    )
  }
  return <ColdStartFlow sources={load.sources} latest={load.latest} go={go} goSettings={goSettings} />
}

function ColdStartFlow({ sources: firstSources, latest, go, goSettings }: Nav & { sources: ColdStartSources; latest: ColdStartRun | null }) {
  const sources = useFreshSources(firstSources)
  const [flow, dispatch] = useReducer(reduceFlow, latest, (run): Flow => (run ? flowOf(run) : { step: 'connect' }))
  const [notice, setNotice] = useState<string | null>(null)
  const request = useRequest()
  // kept so a retry after a failed run does not lose the file the person already picked
  const lastTickets = useRef<File | null>(null)

  const adopt = ({ run, notice: next }: Added) => {
    dispatch({ type: 'run', run })
    setNotice(next)
  }

  const start = (body: ColdStartRunRequest, files: Files) => {
    lastTickets.current = files.tickets ?? null
    setNotice(null)
    return request.send(async () => {
      try {
        const { data: run } = await coldStartApi.createRun(body)
        adopt(await addFiles(run, files))
      } catch (e) {
        if (!isStatus(e, 409)) throw e
        const { data: run } = await coldStartApi.latestRun()
        adopt({ run, notice: 'A build was already running, so Vigil picked it up.' })
      }
    }, 'Could not start the build.')
  }

  const at = STEP_AT[flow.step]
  return (
    <div className="cs">
      <ol className="cs-start-steps" aria-label="Setup steps">
        {STEPS.map((label, i) => (
          <li key={label} aria-current={i === at ? 'step' : undefined}>
            <div className={`cs-start-step${i === at ? ' on' : i < at ? ' past' : ''}`}>
              <span className="n">{i < at ? '✓' : i + 1}</span>
              <span className="l">{label}</span>
            </div>
          </li>
        ))}
      </ol>

      {notice && <div className="cs-card cs-note" role="status">{notice}</div>}

      {flow.step === 'connect' && (
        <Connect sources={sources} prefill={flow.prefill} busy={request.busy} error={request.error} onStart={start} goSettings={goSettings} />
      )}
      {flow.step === 'building' && <Building run={flow.run} source={sourceFor(sources, flow.run.source)} onAdded={adopt} dispatch={dispatch} />}
      {flow.step === 'failed' && (
        <Failed
          run={flow.run}
          source={sourceFor(sources, flow.run.source)}
          busy={request.busy}
          error={request.error}
          onRetry={() => start({ source: flow.run.source, history_days: flow.run.history_days, review_days: flow.run.review_days }, { tickets: lastTickets.current })}
          onStartOver={() => { setNotice(null); dispatch({ type: 'start-over', prefill: { source: flow.run.source, historyDays: flow.run.history_days } }) }}
        />
      )}
      {flow.step === 'review' && (
        <Review
          run={flow.run}
          model={sources.model}
          goSettings={goSettings}
          onAdded={adopt}
          onRefresh={() => { coldStartApi.getRun(flow.run.id).then(({ data }) => dispatch({ type: 'run', run: data })).catch(() => undefined) }}
          onContinue={() => { setNotice(null); dispatch({ type: 'continue' }) }}
          onStartOver={() => { setNotice(null); dispatch({ type: 'start-over' }) }}
        />
      )}
      {flow.step === 'go-live' && (
        <GoLive run={flow.run} sources={sources} goSettings={goSettings} onBack={() => dispatch({ type: 'back' })} onRun={(run) => dispatch({ type: 'run', run })} />
      )}
      {flow.step === 'live' && <Live run={flow.run} sources={sources} go={go} goSettings={goSettings} />}
    </div>
  )
}

function Connect({ sources, prefill, busy, error, onStart, goSettings }: Pick<Nav, 'goSettings'> & {
  sources: ColdStartSources
  prefill?: Prefill
  busy: boolean
  error: string | null
  onStart: (body: ColdStartRunRequest, files: Files) => void
}) {
  const [picked, setPicked] = useState<string | null>(prefill?.source ?? null)
  const [historyDays, setHistoryDays] = useState(prefill?.historyDays ?? DEFAULT_HISTORY_DAYS)
  const [alerts, setAlerts] = useState<File | null>(null)
  const [tickets, setTickets] = useState<File | null>(null)
  const offered = sources.sources
  const needsFile = offered.find((source) => source.id === picked)?.kind === 'upload'
  const canStart = picked !== null && (!needsFile || alerts !== null) && !busy

  return (
    <div className="cs-card" style={{ display: 'grid', gap: 14 }}>
      <h2>Where is your alert history?</h2>
      <p className="cs-muted" style={{ margin: 0 }}>Vigil builds a baseline from alerts you already have, so you do not wait weeks for one. Building it closes nothing and adds nothing to your queue.</p>

      {offered.length === 0 ? (
        <div className="cs-card cs-note">No alert sources are available on this install.</div>
      ) : (
        <div className="cs-sources">
          {offered.map((source) => {
            const copy = SOURCE_COPY[source.kind]
            if (source.kind === 'federation' && !source.configured) {
              return (
                <div key={source.id} className="cs-source">
                  <b>{copy.title(source)}</b>
                  <span>Not connected yet.</span>
                  <button type="button" className="cs-link" onClick={() => goSettings('integrations')}>Add credentials in Settings</button>
                </div>
              )
            }
            return (
              <button key={source.id} type="button" className="cs-source" aria-pressed={picked === source.id} onClick={() => setPicked(source.id)}>
                <b>{copy.title(source)}</b>
                <span>{copy.detail}</span>
              </button>
            )
          })}
        </div>
      )}

      {needsFile && (
        <label className="cs-field">
          <span>Alerts file (JSON, JSONL, CSV or Parquet)</span>
          <input type="file" accept={ALERT_FILE_TYPES} onChange={(e) => setAlerts(e.target.files?.[0] ?? null)} />
        </label>
      )}

      <label className="cs-field">
        <span>How much history</span>
        <select className="cs-range" value={historyDays} onChange={(e) => setHistoryDays(Number(e.target.value))}>
          {HISTORY_DAYS.map((days) => <option key={days} value={days}>{days} days</option>)}
        </select>
      </label>

      <label className="cs-field">
        <span>Closed tickets (optional)</span>
        <input type="file" accept={TICKET_FILE_TYPES} onChange={(e) => setTickets(e.target.files?.[0] ?? null)} />
        <span className="cs-muted" style={{ fontSize: 12.5 }}>Tickets make the baseline far stronger, and older ones count too. A ticket only counts if its alert is in the history above, so read as far back as your source keeps.</span>
      </label>

      <Readiness sources={sources} goSettings={goSettings} />

      {error && <div className="cs-card cs-error" role="alert">{error}</div>}
      <div className="cs-row">
        <button type="button" className="btn primary" disabled={!canStart} onClick={() => picked && onStart({ source: picked, history_days: historyDays }, { alerts: needsFile ? alerts : null, tickets })}>
          {busy ? 'Starting' : 'Build baseline'}
        </button>
      </div>
    </div>
  )
}

function Stat({ value, label, children }: { value: ReactNode; label: string; children?: ReactNode }) {
  return (
    <div className="cs-card cs-stat">
      <b>{value}</b>
      <span>{label}</span>
      {children}
    </div>
  )
}

const daemonWords = ({ state, auto_triage }: ColdStartSources['daemon']) => {
  if (state !== 'running') return `${DAEMON_GAP[state]} ${RUN_DAEMON}`
  return auto_triage ? 'Running' : 'Running, but auto-triage is off (DAEMON_AUTO_TRIAGE), so new alerts are not triaged'
}

function Readiness({ sources: { model, loglm, daemon }, goSettings }: Pick<Nav, 'goSettings'> & { sources: ColdStartSources }) {
  return (
    <dl className="cs-kv" aria-label="Readiness">
      <dt>Model</dt>
      <dd>
        <span className={`cs-dot${model ? '' : ' off'}`} />
        {model ? `${model.model} (${model.provider})` : 'No model: alerts it would judge wait for a person'}
        {!model && <> <button type="button" className="cs-link" onClick={() => goSettings('ai-config')}>Assign a model</button></>}
      </dd>
      <dt>LogLM</dt>
      <dd>
        <span className={`cs-dot${loglm.connected ? '' : ' off'}`} />
        {loglm.connected ? 'Connected' : 'Not connected: flow scoring is off, everything else works'}
      </dd>
      <dt>Daemon</dt>
      <dd>
        <span className={`cs-dot${daemon.state === 'running' && daemon.auto_triage ? '' : ' off'}`} />
        {daemonWords(daemon)}
      </dd>
    </dl>
  )
}

function Building({ run, source, onAdded, dispatch }: { run: ColdStartRun; source: ColdStartSource; onAdded: (added: Added) => void; dispatch: Dispatch<FlowAction> }) {
  const [pollError, setPollError] = useState<string | null>(null)
  const [alerts, setAlerts] = useState<File | null>(null)
  const request = useRequest()
  const { counts } = run
  const needsAlerts = run.status === 'collecting' && source.kind === 'upload' && counts.alerts_read === 0

  useEffect(() => {
    let live = true
    let timer = 0
    const tick = async () => {
      try {
        const { data } = await coldStartApi.getRun(run.id)
        if (!live) return
        setPollError(null)
        dispatch({ type: 'run', run: data })
      } catch (e) {
        if (live) setPollError(extractApiError(e, 'The request failed.'))
      }
      if (live) timer = window.setTimeout(tick, POLL_MS)
    }
    timer = window.setTimeout(tick, POLL_MS)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [run.id, dispatch])

  const droppedTickets = Object.values(counts.tickets_dropped).reduce((sum, count) => sum + count, 0)

  return (
    <div className="cs-card" style={{ display: 'grid', gap: 12 }}>
      <h2>{needsAlerts ? 'Vigil needs the alerts file' : `Building the baseline from ${historyName(source)}`}</h2>
      {run.progress.capped && <CappedNote />}
      {!needsAlerts && <ul className="cs-reasons" aria-live="polite">
        <li>Read {plural(counts.alerts_read, 'alert')}, covering {plural(counts.days_covered, 'day')}.</li>
        <li>Found {plural(counts.kinds, 'kind')} of alert.</li>
        {counts.tickets_unlinked > 0 && <li>{plural(counts.tickets_unlinked, 'kept ticket')} matched no alert in this window, so they do not count.</li>}
        {(counts.tickets_kept > 0 || droppedTickets > 0) && <li>{keptAndDropped('closed ticket', counts.tickets_kept, counts.tickets_dropped)}</li>}
        {run.status === 'judging' && <li>Model judging {n(counts.judged)} of {n(run.progress.total ?? 0)}.</li>}
      </ul>}

      {needsAlerts && (
        <div className="cs-row">
          <label className="cs-field">
            <span>Alerts file (JSON, JSONL, CSV or Parquet)</span>
            <input type="file" accept={ALERT_FILE_TYPES} onChange={(e) => setAlerts(e.target.files?.[0] ?? null)} />
          </label>
          <button type="button" className="btn primary" disabled={!alerts || request.busy} onClick={() => alerts && request.send(async () => onAdded(await addFiles(run, { alerts })), 'Could not upload the file.')}>
            {request.busy ? 'Uploading' : 'Upload alerts'}
          </button>
        </div>
      )}

      {pollError && <div className="cs-card cs-note" role="status">Could not read progress, still trying. {pollError}</div>}

      {!needsAlerts && run.resumable && (
        <div className="cs-card cs-note" style={{ display: 'grid', gap: 8, justifyItems: 'start' }}>
          <span>This build stopped before it finished. Resuming keeps what it already read and judged.</span>
          <button type="button" className="btn ghost" disabled={request.busy} onClick={() => request.send(async () => {
            await coldStartApi.judge(run.id)
          }, 'Could not resume the build.')}>Resume the build</button>
        </div>
      )}
      {request.error && <div className="cs-card cs-error" role="alert">{request.error}</div>}
    </div>
  )
}

function Failed({ run, source, busy, error, onRetry, onStartOver }: { run: ColdStartRun; source: ColdStartSource; busy: boolean; error: string | null; onRetry: () => void; onStartOver: () => void }) {
  return (
    <div className="cs-card" style={{ display: 'grid', gap: 10 }}>
      <h2>The build stopped</h2>
      <div className="cs-card cs-error" role="alert">{run.error ?? 'The run stopped without giving a reason.'}</div>
      {error && <div className="cs-card cs-error" role="alert">{error}</div>}
      <div className="cs-row">
        {source.kind === 'upload' ? (
          <button type="button" className="btn primary" onClick={onStartOver}>Choose the file again</button>
        ) : (
          <>
            <button type="button" className="btn primary" disabled={busy} onClick={onRetry}>{busy ? 'Starting' : 'Retry'}</button>
            <button type="button" className="btn ghost" onClick={onStartOver}>Pick another source</button>
          </>
        )}
      </div>
    </div>
  )
}

function CappedNote() {
  return <div className="cs-card cs-note" role="status">Vigil hit its reading limit, so this history is incomplete. The numbers below only cover what it read.</div>
}

const skippedText = (reason: string) =>
  reason === 'no model'
    ? 'No model was assigned when this ran, so alerts it would judge wait for a person.'
    : 'The model kept failing, so the alerts it did not judge wait for a person.'

/** Fewer than three failed calls in a row leave judging_skipped empty, and those alerts were not judged. */
const judgeGap = ({ judging_skipped, progress: { judge_failed: failed } }: ColdStartRun) => {
  if (judging_skipped) return skippedText(judging_skipped)
  if (!failed) return null
  return `The model failed on ${plural(failed, 'alert')}, so ${failed === 1 ? 'it waits' : 'they wait'} for a person.`
}

function Review({ run, model, goSettings, onAdded, onContinue, onStartOver, onRefresh }: Pick<Nav, 'goSettings'> & { onRefresh: () => void; run: ColdStartRun; model: ColdStartSources['model']; onAdded: (added: Added) => void; onContinue: () => void; onStartOver: () => void }) {
  const [groups, setGroups] = useState<ColdStartGroup[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [filter, setFilter] = useState<Stage | 'all'>('all')
  const retry = useRequest()
  const { counts } = run
  const gap = judgeGap(run)

  useEffect(() => {
    let live = true
    setLoadError(null)
    coldStartApi.groups(run.id)
      .then(({ data }) => live && setGroups(data.groups))
      .catch((e) => live && setLoadError(extractApiError(e, 'The request failed.')))
    return () => {
      live = false
    }
  }, [run.id, run.updated_at, attempt])

  const method = modelMethod(model)
  const judgedSomething = counts.judged > 0 && counts.average_model_seconds !== null
  const stats: { value: string; label: string; method: string | null }[] = [
    { value: n(counts.review_alerts), label: 'Alerts reviewed', method: null },
    { value: n(counts.dismiss_candidates), label: 'Would close', method: null },
    { value: n(counts.escalations), label: 'Escalated', method: null },
    { value: n(counts.judged), label: 'Judged by the model', method },
    { value: n(counts.unparsed), label: 'Unparsed', method },
    { value: judgedSomething ? (counts.average_model_seconds as number).toFixed(1) : 'None', label: 'Average model seconds', method },
  ]

  const shown = (groups ?? [])
    .filter((group) => filter === 'all' || group.stage1[filter] > 0)
    .sort((a, b) => b.count - a.count)

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div className="cs-grid3">
        {stats.map((stat) => (
          <Stat key={stat.label} value={stat.value} label={stat.label}>
            <span className={`cs-tag${stat.method ? ' ac' : ''}`}>{stat.method ?? 'No AI'}</span>
          </Stat>
        ))}
      </div>

      <div className="cs-card" style={{ display: 'grid', gap: 10 }}>
        <h2>What Vigil would have done</h2>
        {run.progress.capped && <CappedNote />}
        <p style={{ margin: 0, color: 'var(--tx0)' }}>Nothing has been closed, and every row below is only a recommendation.</p>
        <p className="cs-muted" style={{ margin: 0, fontSize: 12.5 }}>Not benign escalates that kind right away. Agree is recorded and changes nothing. A kind needs 59 clean closed tickets for a 5% bound.</p>
        {gap && (
          <p className="cs-muted" style={{ margin: 0, fontSize: 12.5 }}>
            {gap}
            {!model && <> <button type="button" className="cs-link" onClick={() => goSettings('ai-config')}>Assign a model</button>, then come back to judge them.</>}
          </p>
        )}
        {gap && model && (
          <div className="cs-row">
            <button type="button" className="btn ghost" disabled={retry.busy} onClick={() => retry.send(async () => onAdded({ run: (await coldStartApi.judge(run.id)).data, notice: null }), 'Could not retry judging.')}>
              {retry.busy ? 'Starting' : 'Retry judging'}
            </button>
            <span className="cs-muted" style={{ fontSize: 12.5 }}>Your reviews stay.</span>
          </div>
        )}
        {retry.error && <div className="cs-card cs-error" role="alert">{retry.error}</div>}

        <div className="cs-row" role="group" aria-label="Filter alert kinds">
          <button type="button" className="cs-chipbtn" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All <span className="n">{n(groups?.length ?? 0)}</span></button>
          {STAGES.map(({ stage, label }) => (
            <button key={stage} type="button" className="cs-chipbtn" aria-pressed={filter === stage} onClick={() => setFilter(stage)}>
              {label} <span className="n">{n((groups ?? []).filter((group) => group.stage1[stage] > 0).length)}</span>
            </button>
          ))}
        </div>

        {loadError ? (
          <div className="cs-card cs-error" role="alert" style={{ display: 'grid', gap: 8, justifyItems: 'start' }}>
            <span>Could not load the alert kinds. {loadError}</span>
            <button type="button" className="btn ghost" onClick={() => setAttempt(attempt + 1)}>Try again</button>
          </div>
        ) : groups === null ? (
          <div className="cs-muted" role="status">Loading the alert kinds.</div>
        ) : groups.length === 0 ? (
          <div className="cs-card cs-note">No alerts fell in the review window, so there is nothing to review yet. Pick more history or another source.</div>
        ) : shown.length === 0 ? (
          <div className="cs-muted">No alert kinds match this filter.</div>
        ) : (
          <div className="cs-table-wrap">
            <table className="cs-table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Source</th>
                  <th className="num">Count</th>
                  <th>Recommendation</th>
                  <th>Why</th>
                  <th>Ticket history</th>
                  <th>Model</th>
                  <th>Your call</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((group) => (
                  <GroupRow
                    key={group.key}
                    run={run}
                    group={group}
                    onRecorded={(next) => {
                      setGroups(next)
                      onRefresh()
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <AddTickets run={run} onAdded={onAdded} />

      <div className="cs-row">
        <button type="button" className="btn primary" disabled={counts.alerts_read === 0} onClick={onContinue}>Continue to go live</button>
        <button type="button" className="btn ghost" onClick={onStartOver}>Start over with another source</button>
      </div>
    </div>
  )
}

function GroupRow({ run, group, onRecorded }: { run: ColdStartRun; group: ColdStartGroup; onRecorded: (groups: ColdStartGroup[]) => void }) {
  const [open, setOpen] = useState(false)
  const request = useRequest()
  const stages = STAGES.filter(({ stage }) => group.stage1[stage] > 0)
  const verdicts = Object.entries(group.llm.verdicts).map(([verdict, count]) => `${verdict} ${n(count)}`).join(', ')
  const record = (verdict: ColdStartVerdict) =>
    request.send(async () => onRecorded((await coldStartApi.review(run.id, group.key, verdict)).data.groups), 'Could not record the review.')

  return (
    <>
      <tr className={open ? 'open' : undefined}>
        <td style={{ color: 'var(--tx0)' }}>{group.label}</td>
        <td>{group.source}</td>
        <td className="num cs-mono">{n(group.count)}</td>
        <td>
          <div style={{ display: 'grid', gap: 4, justifyItems: 'start' }}>
            {stages.map(({ stage, label, tone }) => (
              <span key={stage} className={`cs-tag ${tone}`}>{label}{stages.length > 1 ? ` ${n(group.stage1[stage])}` : ''}</span>
            ))}
          </div>
        </td>
        <td className="cs-muted">
          {group.reasons.length ? group.reasons.map((reason) => <div key={reason}>{reason}</div>) : group.top_rule}
        </td>
        <td>
          {group.tickets.benign + group.tickets.real > 0
            ? <div>{n(group.tickets.benign)} closed benign, {n(group.tickets.real)} real</div>
            : <div className="cs-muted">No ticket history</div>}
          {group.miss_bound !== null && <div className="cs-muted">{missBoundWords(group.miss_bound)}</div>}
        </td>
        <td>
          {group.llm.judged > 0
            ? <div>{plural(group.llm.judged, 'alert')} judged{verdicts ? `: ${verdicts}` : ''}</div>
            : <div className="cs-muted">{judgeGap(run) && group.stage1.abstain > 0 ? 'Not judged: waits for a person' : 'Not sent to the model'}</div>}
          <button type="button" className="cs-link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Hide details' : 'Show details'}</button>
        </td>
        <td>
          <div className="cs-row" role="group" aria-label={`Review ${group.label}`}>
            <button type="button" className="btn ghost" disabled={request.busy} onClick={() => record('agree')}>Agree</button>
            <button type="button" className="btn ghost" disabled={request.busy} onClick={() => record('not_benign')}>Not benign</button>
            {group.review.agree > 0 && <span className="cs-tag good">Agreed</span>}
            {group.review.not_benign > 0 && <span className="cs-tag poor">Marked not benign</span>}
          </div>
          {request.error && <div role="alert" style={{ color: 'var(--poor)', marginTop: 4 }}>{request.error}</div>}
        </td>
      </tr>
      {open && (
        <tr className="open">
          <td colSpan={8}>
            <dl className="cs-kv">
              <dt>Example alert</dt>
              <dd>{group.sample.title}</dd>
              <dt>Severity</dt>
              <dd>{group.sample.severity}</dd>
              <dt>Seen</dt>
              <dd>{new Date(group.sample.observed_at).toLocaleString()}</dd>
              {group.sample.entities.length > 0 && <><dt>Entities</dt><dd>{group.sample.entities.join(', ')}</dd></>}
              <dt>Rule</dt>
              <dd className="cs-mono">{group.top_rule}</dd>
              <dt>Model reasoning</dt>
              <dd>{group.llm.example_reasoning ?? 'The model gave no reasoning for this kind.'}</dd>
            </dl>
          </td>
        </tr>
      )}
    </>
  )
}

function AddTickets({ run, onAdded }: { run: ColdStartRun; onAdded: (added: Added) => void }) {
  const request = useRequest()
  const choose = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file) void request.send(async () => onAdded(await addFiles(run, { tickets: file })), 'Could not add the tickets.')
  }
  return (
    <div className="cs-card" style={{ display: 'grid', gap: 8 }}>
      <label className="cs-field">
        <span>Add closed tickets</span>
        <input type="file" accept={TICKET_FILE_TYPES} disabled={request.busy} onChange={choose} />
        <span className="cs-muted" style={{ fontSize: 12.5 }}>Tickets make the baseline far stronger. Vigil rebuilds it and keeps your reviews.</span>
      </label>
      {run.counts.tickets_kept > 0 && <span className="cs-muted" style={{ fontSize: 12.5 }}>{keptAndDropped('closed ticket', run.counts.tickets_kept, run.counts.tickets_dropped)}</span>}
      {request.error && <div className="cs-card cs-error" role="alert">{request.error}</div>}
    </div>
  )
}

function liveEffects(run: ColdStartRun, sources: ColdStartSources, goSettings: Nav['goSettings']): ReactNode[] {
  const source = sourceFor(sources, run.source)
  const { alerts_read, days_covered, tickets_kept } = run.counts
  const gaps = triageGaps(sources)
  const baseline = `The baseline from ${plural(alerts_read, 'alert')} over ${plural(days_covered, 'day')}${tickets_kept ? ` and ${plural(tickets_kept, 'closed ticket')}` : ''}`
  const connect = <button type="button" className="cs-link" onClick={() => goSettings('integrations')}>Connect a source in Settings</button>
  const sourceLine = {
    federation: `${source.label} is enabled from now.${sources.daemon.state === 'running' ? ' Vigil reads new alerts from it as they arrive.' : ''}`,
    upload: <>An uploaded file does not send new alerts. {connect} to read them.</>,
  }[source.kind]
  return [
    sourceLine,
    gaps.length ? `${baseline} is saved.` : `${baseline} is shown to people beside the verdict on new alerts. The model does not see it.`,
    ...gaps,
    'Vigil recommends and a person decides. Nothing closes on its own.',
  ]
}

function Effects({ items }: { items: ReactNode[] }) {
  return <ul className="cs-reasons">{items.map((item, i) => <li key={i}>{item}</li>)}</ul>
}

function GoLive({ run, sources, goSettings, onBack, onRun }: Pick<Nav, 'goSettings'> & { run: ColdStartRun; sources: ColdStartSources; onBack: () => void; onRun: (run: ColdStartRun) => void }) {
  const request = useRequest()
  const { hasPermission } = useAuth()
  const source = sourceFor(sources, run.source)
  const needsAnother = source.kind === 'federation' && !hasPermission(GO_LIVE_SOURCE_PERM)
  return (
    <div className="cs-card" style={{ display: 'grid', gap: 12, maxWidth: 720 }}>
      <h2>What happens when you go live</h2>
      <Effects items={liveEffects(run, sources, goSettings)} />
      {needsAnother && <div className="cs-card cs-note" role="status">Going live turns on {source.label}, which takes the integrations.write permission. A senior analyst, a manager or an admin can do it, and your reviews stay on this run.</div>}
      {request.error && <div className="cs-card cs-error" role="alert">{request.error}</div>}
      <div className="cs-row">
        <button type="button" className="btn primary" disabled={request.busy || needsAnother} onClick={() => request.send(async () => onRun((await coldStartApi.goLive(run.id)).data), 'Could not go live.')}>
          {request.busy ? 'Going live' : 'Go live'}
        </button>
        <button type="button" className="btn ghost" onClick={onBack}>Back to review</button>
      </div>
    </div>
  )
}

function Live({ run, sources, go, goSettings }: Nav & { run: ColdStartRun; sources: ColdStartSources }) {
  return (
    <div className="cs-card" style={{ display: 'grid', gap: 12, maxWidth: 720 }}>
      <h2>{triageGaps(sources).length ? 'Marked live, but new alerts may not be triaged' : 'The baseline is live'}</h2>
      <Effects items={liveEffects(run, sources, goSettings)} />
      <div className="cs-row">
        <button type="button" className="btn primary" onClick={() => go('overview')}>Open Overview</button>
        <button type="button" className="btn ghost" onClick={() => go('triage')}>Open Triage queue</button>
      </div>
    </div>
  )
}
