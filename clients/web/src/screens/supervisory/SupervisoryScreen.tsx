import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { FilterChip } from '../../shared/FilterChip'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { MeterBar } from '../../shared/MeterBar'
import type { ConsoleScreenProps } from '../../shared/types'
import { EmptyState } from '../../shared/ui'
import { utcClock } from '../../shared/utc'
import { FailuresPanel, HistoryPanel, LearnsPanel, SettingsPanel } from './SidePanels'
import { HeldCard, SuggestionCard } from './SuggestionCard'
import type { FunctionEvidence, HistoryEntry, Suggestion, SupervisorStatus } from './supervisoryTypes'
import { useSupervisory } from './useSupervisory'
import { FN_LABEL, ago } from './words'
import './supervisory.css'

/** How long an applied card stays, offering Undo, before it leaves the list. */
const UNDO_WINDOW_MS = 10_000

const FILTERS = ['all', 'open', 'held', 'widens'] as const
type Filter = (typeof FILTERS)[number]

const STATE_WORD: Record<SupervisorStatus['state'], [string, 'good' | 'fair' | null]> = {
  running: ['Running', 'good'],
  on_hold: ['On hold', 'fair'],
  off: ['Off', null],
  unknown: ['Not on schedule', 'fair'],
}

/** What each part of the scheduled check is called on the page. */
const PART_WORD: Record<string, string> = {
  review_threshold: 'review threshold check',
  investigation_limits: 'investigation limits check',
  run_judge: 'model judge check',
  medic_hold: 'Medic read',
  follow_up: 'follow-up',
}

/** "Failed: Medic read and model judge check" */
function failedWords(failed: string[]): string {
  const words = failed.map((f) => PART_WORD[f] ?? f)
  const list = words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
  return `Failed: ${list}`
}

function StatusCard({ status, openMedic, refreshFailed, retry }: {
  status: SupervisorStatus
  openMedic: () => void
  /** The last read failed; the page still shows the one before it. */
  refreshFailed: boolean
  retry: () => void
}) {
  const [word, level] = STATE_WORD[status.state]
  const failed = status.failed ?? []
  return (
    <section className="spv-card spv-status" aria-label="Supervisory model status">
      <div className="spv-status-head">
        {level ? <LevelBadge level={level} variant="pill" label={word} /> : <span className="spv-off">{word}</span>}
        <InfoTip
          label="How status is worked out"
          source="The supervisory model runs as a scheduled job, about once an hour."
          calculation="Running while checks happen on schedule; On hold while a related Medic incident is open, when it suggests nothing; Off when it is not switched on; Not on schedule when no check has run yet, or the last one is more than two intervals old. The last check line names any part of that check that failed; the rest still ran."
          limit="It reads Medic's incidents; it runs no health checks of its own."
          align="start"
        />
      </div>
      {status.last_check_at ? (
        <p>
          Last check {utcClock(status.last_check_at)} ({ago(status.last_check_at)})
          {status.next_check_at && <> · next {utcClock(status.next_check_at)}</>}
          {failed.length > 0 && <span className="spv-status-warn">{failedWords(failed)}</span>}
          {refreshFailed && <RefreshFailed retry={retry} />}
        </p>
      ) : (
        <p>
          No check yet
          {refreshFailed && <RefreshFailed retry={retry} />}
        </p>
      )}
      {status.state === 'off'
        ? <p>Its scheduled checks aren't running on this install, so it makes no suggestions yet.</p>
        : <p>Reading {status.history_days} days of history · {status.functions_watched} functions watched</p>}
      <p>
        Medic:{' '}
        <button type="button" className="spv-link" onClick={openMedic}>
          {status.medic_open_related === 0
            ? 'no related incident open ›'
            : `${status.medic_open_related} related incident${status.medic_open_related === 1 ? '' : 's'} open ›`}
        </button>
      </p>
    </section>
  )
}

function RefreshFailed({ retry }: { retry: () => void }) {
  return (
    <span className="spv-status-warn" role="alert">
      Couldn't refresh ·{' '}
      <button type="button" className="spv-link" onClick={retry}>Retry</button>
    </span>
  )
}

function FunctionTile({ f }: { f: FunctionEvidence }) {
  const ready = f.state === 'ready'
  return (
    <div className="spv-card spv-tile">
      <div className="spv-row">
        <span className="spv-strong">{FN_LABEL[f.fn]}</span>
        <span className={`spv-tag ${ready ? 'good' : 'fair'}`}>{ready ? 'Ready' : 'Waiting'}</span>
      </div>
      <MeterBar pct={(f.labelled / f.floor) * 100} level={ready ? 'good' : 'fair'} label={`${FN_LABEL[f.fn]}: ${f.labelled} of ${f.floor} needed`} />
      <span className="spv-note">
        {ready ? (
          <><span className="spv-mono spv-num">{f.labelled}</span> {f.unit}</>
        ) : (
          <><span className="spv-mono spv-num">{f.labelled}/{f.floor}</span> {f.unit} · needs {f.floor - f.labelled} more</>
        )}
      </span>
    </div>
  )
}

export default function SupervisoryScreen({ go, openCase }: ConsoleScreenProps) {
  const { hasPermission } = useAuth()
  const canWrite = hasPermission('ai_autonomy.write')
  const { data, error, reload, dismiss, apply, settle, undo } = useSupervisory()
  // suggestion id → its apply, while the card still offers Undo
  const [applied, setApplied] = useState<Record<string, HistoryEntry>>({})
  const timers = useRef(new Map<string, number>())
  useEffect(() => {
    const pending = timers.current
    return () => pending.forEach((t) => window.clearTimeout(t))
  }, [])
  const endWindow = (id: string) => {
    window.clearTimeout(timers.current.get(id))
    timers.current.delete(id)
    setApplied((cur) => {
      const rest = { ...cur }
      delete rest[id]
      return rest
    })
  }
  const startWindow = (id: string) => {
    window.clearTimeout(timers.current.get(id))
    timers.current.set(id, window.setTimeout(() => { endWindow(id); settle(id) }, UNDO_WINDOW_MS))
  }
  const applyIt = async (s: Suggestion) => {
    const entry = await apply(s)
    setApplied((cur) => ({ ...cur, [s.id]: entry }))
    startWindow(s.id)
  }
  /** From the card or from History. An undone suggestion is not offered again, so its card leaves. */
  const undoIt = async (entry: HistoryEntry) => {
    const id = Object.keys(applied).find((k) => applied[k].id === entry.id)
    // the window must not close while the undo is in flight; a failed undo gets a fresh one
    if (id) window.clearTimeout(timers.current.get(id))
    try {
      await undo(entry)
    } catch (e) {
      // restart the window only while the card still offers Undo; another undo may have closed it
      if (id && timers.current.has(id)) startWindow(id)
      throw e
    }
    if (id) {
      endWindow(id)
      settle(id)
    }
  }
  const [params, setParams] = useSearchParams()
  const [expanded, setExpanded] = useState<string | null>(null)
  const raw = params.get('show')
  const filter: Filter = FILTERS.find((f) => f === raw) ?? 'all'
  const setFilter = (f: Filter) => setParams(f === 'all' ? {} : { show: f }, { replace: true })

  if (!data) {
    return (
      <div className="spv-page">
        {error
          ? <EmptyState error icon="alert" title="Couldn't load the supervisory model" body="Vigil could not read its suggestions. Try again in a moment." primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
          : <EmptyState loading icon="clock" title="Loading the supervisory model…" />}
      </div>
    )
  }

  const { functions, suggestions, held } = data
  const status = data.status
  const openMedic = (incident?: string) => go('medic', incident ? { search: `?tab=incidents&incident=${encodeURIComponent(incident)}` } : undefined)

  const widens = suggestions.filter((s) => s.widens_autonomy)
  const visible = filter === 'held' ? [] : filter === 'widens' ? widens : suggestions
  const showHeld = filter === 'all' || filter === 'held'
  const empty = visible.length === 0 && (!showHeld || held.length === 0)

  return (
    <div className="spv-page">
      <div className="spv-top">
        <StatusCard status={status} openMedic={() => openMedic()} refreshFailed={error !== null} retry={reload} />
        <section className="spv-tiles" aria-label="Evidence per function">
          {functions.map((f) => <FunctionTile key={f.fn} f={f} />)}
        </section>
      </div>

      <div className="spv-main">
        <section className="spv-list-col" aria-label="Suggestions">
          <div className="spv-bar">
            <div className="spv-chips" role="group" aria-label="Filter suggestions">
              <FilterChip list label="All" count={suggestions.length + held.length} active={filter === 'all'} onClick={() => setFilter('all')} />
              <FilterChip list label="Open" count={suggestions.length} active={filter === 'open'} onClick={() => setFilter('open')} />
              <FilterChip list label="Held" count={held.length} active={filter === 'held'} onClick={() => setFilter('held')} />
              <FilterChip list label="Widens autonomy" count={widens.length} active={filter === 'widens'} onClick={() => setFilter('widens')} />
            </div>
            <span className="spv-quiet">Newest first</span>
          </div>

          <div className="spv-list" tabIndex={0} aria-label="Suggestion list, scrolls on its own">
            {visible.map((s) => (
              <SuggestionCard
                key={s.id}
                s={s}
                expanded={expanded === s.id}
                onToggle={() => setExpanded(expanded === s.id ? null : s.id)}
                canWrite={canWrite}
                applied={applied[s.id] ?? null}
                onApply={() => applyIt(s)}
                onUndo={() => undoIt(applied[s.id])}
                onDismiss={(reason) => dismiss(s.id, reason)}
                openCase={openCase}
              />
            ))}
            {showHeld && held.map((h) => <HeldCard key={h.id} h={h} onOpenIncident={() => openMedic(h.incident.id)} />)}
            {empty && (
              <div className="spv-empty">
                {filter === 'all' && status.state === 'off'
                  ? `No suggestions yet. They start once its scheduled checks run and a function has at least ${functions[0]?.floor ?? 20} labelled examples.`
                  : 'Nothing here. Suggestions appear once a function has enough evidence.'}
              </div>
            )}
          </div>
        </section>

        <aside className="spv-aside">
          <FailuresPanel failures={data.failures} />
          <HistoryPanel items={data.history} canUndo={canWrite} onUndo={undoIt} />
          <SettingsPanel items={data.settings} />
          <LearnsPanel days={data.learns_from.days} items={data.learns_from.items} />
        </aside>
      </div>
    </div>
  )
}
