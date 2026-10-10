import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import AgentCards from './AgentCards'
import AlertRail, { SevMark } from './AlertRail'
import { clock } from './clock'
import FlowDiagram from './FlowDiagram'
import { LevelBadge } from '../../shared/LevelBadge'
import { useSourceBadge } from '../../shared/useSourceBadge'
import { EmptyState, Popup } from '../../shared/ui'
import type { ConsoleScreenProps } from '../../shared/types'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from '../dashboard/SourceEvidenceSection'
import { jiraReadiness, type JiraReadiness } from '../../shell/commandBarModel'
import api, {
  configApi,
  findingsApi,
  overviewApi,
  type OverviewFeedItem,
  type OverviewPayload,
} from '../../services/api'

const NOISE_INFO = 'The mark is stored and does not change scoring.'

const POLL_MS = 10_000

type Phase = 'loading' | 'error' | 'ready'
// The single read of an alert that is not in the feed.
type AlertRead = { id: string; status: 'loading' | 'ready' | 'error' | 'missing'; item?: OverviewFeedItem }

function errorText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: { detail?: unknown; error?: unknown } } })?.response?.data
  if (typeof data?.detail === 'string' && data.detail.trim()) return data.detail
  if (typeof data?.error === 'string' && data.error.trim()) return data.error
  const message = (error as { message?: string })?.message
  return message && message.trim() ? message : fallback
}

function legendTitle(data: OverviewPayload): string {
  const pct = (n: number) => Math.round(n * 100)
  return `Health: Good ${pct(data.good_at)}% and up, Fair ${pct(data.fair_at)} to ${pct(data.good_at)}%, Poor under ${pct(data.fair_at)}%.`
}

/** The popup header: severity and "id · time" over the description. */
function PopupTitle({ item }: { item: OverviewFeedItem }) {
  return (
    <span className="ov-pop-t">
      <span className="ov-pop-m">
        <SevMark severity={item.severity} />
        <span>{item.finding_id} · {clock(item.created_at)}</span>
      </span>
      <span className="ov-pop-d">{item.description ?? item.finding_id}</span>
    </span>
  )
}

function EvidenceBody({ item }: { item: OverviewFeedItem }) {
  const evidence = item.source_evidence
  if (!evidence) return <p>No source evidence on this finding.</p>
  if (evidence.payload_included !== false) {
    const parsed = parseSourceEvidence(evidence)
    if (parsed) return <SourceEvidenceSection evidence={parsed} />
  }
  const kind = typeof evidence.telemetry_kind === 'string' ? evidence.telemetry_kind : 'Evidence'
  const status = typeof evidence.status === 'string' ? evidence.status : 'unknown'
  const schema = typeof evidence.schema_id === 'string' ? evidence.schema_id : ''
  return (
    <p>
      {kind} · {status}
      {schema ? ` · ${schema}` : ''}
      {evidence.payload_included === false ? ' · records omitted from this list' : ''}
    </p>
  )
}

export default function OverviewScreen({ go, openCase, setViewFull, setWallMode, chatOpen }: ConsoleScreenProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const alertId = searchParams.get('alert') || null // an empty value is no alert
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<OverviewPayload | null>(null)
  const [wall, setWall] = useState(false)
  const [paused, setPaused] = useState(false)
  const badgeOf = useSourceBadge()
  const [read, setRead] = useState<AlertRead | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [marked, setMarked] = useState(false)
  const [launchNote, setLaunchNote] = useState<string | null>(null)
  const [ticketNote, setTicketNote] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [jira, setJira] = useState<JiraReadiness>({ gap: 'Jira configuration could not be read', projectKey: '' })
  // The feed row when there is one; else the single read; else the last item shown, so a poll that
  // drops the alert (marked as noise) does not blank the popup.
  const shown = useRef<OverviewFeedItem | null>(null)
  if (!alertId) shown.current = null
  const open =
    (alertId ? data?.feed.find((row) => row.finding_id === alertId) : null) ??
    (read?.id === alertId ? read?.item : null) ??
    (shown.current?.finding_id === alertId ? shown.current : null) ??
    null
  if (open) shown.current = open
  const openId = useRef<string | null>(null)
  openId.current = open?.finding_id ?? null
  const hasOpen = open !== null
  const readStatus = read?.id === alertId ? read.status : 'loading'
  const feedSettled = phase !== 'loading'

  const setAlert = (id: string | null) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (id) next.set('alert', id)
        else next.delete('alert')
        return next
      },
      { replace: id === null },
    )

  const load = useCallback(() => {
    overviewApi
      .get()
      .then((res) => {
        setData(res.data)
        setPhase('ready')
        setError(null)
      })
      .catch(() => {
        setPhase('error')
        setError('Couldn’t load overview')
      })
  }, [])

  // Pausing clears the interval; resuming loads at once and starts a fresh one.
  useEffect(() => {
    if (paused) return
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load, paused])

  // The rail scrolls on its own, so the screen takes the full-height view.
  useEffect(() => {
    setViewFull(true)
    return () => setViewFull(false)
  }, [setViewFull])

  useEffect(() => {
    let live = true
    configApi
      .getIntegrations()
      .then((res) => {
        if (live) setJira(jiraReadiness(res.data))
      })
      .catch(() => {
        if (live) setJira({ gap: 'Jira configuration could not be read', projectKey: '' })
      })
    return () => {
      live = false
    }
  }, [])

  // An alert that is not in the feed (older than the cap, or noise-marked) comes from its own read.
  useEffect(() => {
    if (!alertId) {
      setRead(null)
      return
    }
    if (hasOpen || !feedSettled) return
    let live = true
    setRead({ id: alertId, status: 'loading' })
    overviewApi
      .alert(alertId)
      .then((res) => {
        if (live) setRead({ id: alertId, status: 'ready', item: res.data })
      })
      .catch((error) => {
        if (live) setRead({ id: alertId, status: error?.response?.status === 404 ? 'missing' : 'error' })
      })
    return () => {
      live = false
    }
  }, [alertId, hasOpen, feedSettled, attempt])

  useEffect(() => {
    setMarked(open?.noise_marked ?? false)
    setLaunchNote(null)
    setTicketNote(null)
    setActionError(null)
  }, [open?.finding_id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => setWallMode?.(false), [setWallMode])

  const toggleWall = () => {
    const next = !wall
    setWall(next)
    setWallMode?.(next)
  }

  // Escape leaves full screen. The popup's own Escape handler runs first and stops propagation,
  // so with an alert open one keypress closes only the popup.
  useEffect(() => {
    if (!wall) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      setWall(false)
      setWallMode?.(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [wall, setWallMode])

  const sourceLabel = open ? badgeOf(open.data_source).label : ''

  const openCaseFromPopup = (id: string) => {
    setAlert(null) // one overlay at a time
    openCase(id)
  }

  const stillOpen = (findingId: string) => openId.current === findingId

  const markNoise = async () => {
    if (!open) return
    const findingId = open.finding_id
    const wasMarked = marked
    setActionError(null)
    try {
      if (wasMarked) await findingsApi.clearNoise(findingId)
      else await findingsApi.markNoise(findingId)
      if (stillOpen(findingId)) setMarked(!wasMarked)
      load()
    } catch (error) {
      if (stillOpen(findingId)) {
        setActionError(errorText(error, wasMarked ? 'Couldn’t clear the noise mark' : 'Couldn’t mark this finding'))
      }
    }
  }

  const launch = async () => {
    if (!open) return
    const findingId = open.finding_id
    setActionError(null)
    try {
      const res = await findingsApi.launchIntake(findingId)
      if (!stillOpen(findingId)) return
      setLaunchNote(res.data.already_queued ? 'Already waiting in the Triage queue.' : 'Waiting in the Triage queue')
    } catch (error) {
      if (stillOpen(findingId)) setActionError(errorText(error, 'Couldn’t send this finding to triage'))
    }
  }

  const createTicket = async () => {
    if (!open?.case_id) return
    const findingId = open.finding_id
    const caseId = open.case_id
    setActionError(null)
    setTicketNote(null)
    try {
      const res = await api.post<{ success?: boolean; error?: string; issue_key?: string }>(
        `/cases/${encodeURIComponent(caseId)}/export/jira`,
        { project_key: jira.projectKey },
      )
      if (!stillOpen(findingId)) return
      if (res.data?.success === false) {
        setActionError(res.data.error || 'Jira export failed')
        return
      }
      setTicketNote(res.data.issue_key ? `Created ${res.data.issue_key}` : 'Ticket created')
    } catch (error) {
      if (stillOpen(findingId)) setActionError(errorText(error, 'Jira export failed'))
    }
  }

  return (
    <div className={`ov-screen${wall ? ' wall' : ''}`}>
      <div className="ov-main">
      {(!wall || phase !== 'ready') && (
        <div className="ov-head">
          <div>
            <h1>Overview</h1>
            <p>
              {data?.empty
                ? 'Where your data comes from, what Vigil does with it, and what comes out. Nothing is connected yet, so each part below shows where to connect.'
                : `Where your data comes from, what Vigil does with it, and what came out. ${data ? `Today, UTC ${data.day}.` : ''}`.trim()}
            </p>
          </div>
          <div className="ov-head-r">
            {data && !data.empty && (
              <div className="ov-legend" title={legendTitle(data)}>
                <LevelBadge level="good" variant="pill" />
                <LevelBadge level="fair" variant="pill" />
                <LevelBadge level="poor" variant="pill" />
              </div>
            )}
            <button type="button" className="ov-btn" aria-pressed={wall} onClick={toggleWall}>
              <Icon name="fit" size={13} />
              {wall ? 'Exit full screen' : 'Full screen'}
            </button>
            <button type="button" className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={load}>
              <Icon name="refresh" />
            </button>
          </div>
        </div>
      )}

      {phase === 'loading' && <EmptyState loading icon="graph" title="Loading overview…" />}
      {phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load overview" body={error} primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />
      )}
      {phase === 'ready' && data && (
        <>
          <FlowDiagram data={data} wall={wall} onToggleWall={toggleWall} />
          {!wall && <AgentCards data={data} go={go} />}
        </>
      )}
      </div>

      {phase === 'ready' && data && !chatOpen && (
        <AlertRail data={data} paused={paused} onTogglePause={() => setPaused((p) => !p)} onOpen={setAlert} openCase={openCase} />
      )}

      <Popup open={alertId !== null} onClose={() => setAlert(null)} title={open ? <PopupTitle item={open} /> : (alertId ?? 'Alert')} width={860}>
        {!open && readStatus === 'missing' && <p>Alert {alertId} not found.</p>}
        {!open && readStatus === 'error' && (
          <>
            <p role="alert">Couldn’t load alert {alertId}.</p>
            <div><button type="button" className="btn ghost" onClick={() => setAttempt((n) => n + 1)}>Retry</button></div>
          </>
        )}
        {!open && readStatus === 'loading' && <p>Loading alert…</p>}
        {open && (
          <>
            <dl className="ov-facts">
              <div><dt>Source</dt><dd>{sourceLabel}</dd></div>
              <div><dt>Status</dt><dd>{open.status}</dd></div>
              <div><dt>Triage</dt><dd>{open.terminal_label}</dd></div>
              <div>
                <dt>Case</dt>
                <dd>
                  {open.case_id ? (
                    <button type="button" className="ov-case-link" onClick={() => openCaseFromPopup(open.case_id!)}>
                      Case {open.case_id}
                    </button>
                  ) : (
                    'Not in a case yet'
                  )}
                </dd>
              </div>
            </dl>
            <EvidenceBody item={open} />
            <div className="flex items-center gap-2 flex-wrap">
              <button type="button" className="btn ghost" onClick={markNoise}>
                {marked ? 'Clear noise' : 'Mark as noise'}
              </button>
              <button type="button" className="btn ghost icon" aria-label={NOISE_INFO} title={NOISE_INFO}>
                <Icon name="info" size={14} />
              </button>
              <button type="button" className="btn ghost" onClick={launch}>Send to triage</button>
              {open.case_id && (
                <button type="button" className="btn ghost" onClick={() => openCaseFromPopup(open.case_id!)}>
                  Open case
                </button>
              )}
              {open.case_id && (
                <button type="button" className="btn ghost" onClick={createTicket}>Create ticket</button>
              )}
              <span title="Coming in a later release">
                <button type="button" className="btn ghost" disabled>ServiceNow</button>
              </span>
            </div>
            {launchNote && (
              <p>
                <Link to="/triage">{launchNote}</Link>
              </p>
            )}
            {ticketNote && <p>{ticketNote}</p>}
            {actionError && <p role="alert">{actionError}</p>}
            {open.source_link && (
              <div className="ov-pop-f">
                <a href={open.source_link}>Open in {sourceLabel} ↗</a>
              </div>
            )}
          </>
        )}
      </Popup>
    </div>
  )
}
