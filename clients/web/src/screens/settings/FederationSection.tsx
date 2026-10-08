import { useState } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { EmptyState, Select, TextInput, Toggle } from '../../shared/ui'
import type { FederationSourceView } from '../../services/api'
import { useFederation } from './useSettings'
import type { SectionProps } from './types'

const SEVERITY_OPTIONS = [
  { value: '', label: 'Any' },
  { value: 'low', label: 'Low and up' },
  { value: 'medium', label: 'Medium and up' },
  { value: 'high', label: 'High and up' },
  { value: 'critical', label: 'Critical only' },
]

// Source ids come from the backend's federation config, which can carry sources
// this list has never heard of — hence a lookup that can miss, falling back to
// the raw id at the call site.
const SOURCE_LABELS = new Map([
  ['splunk', 'Splunk'],
  ['crowdstrike', 'CrowdStrike Falcon'],
  ['azure_sentinel', 'Azure Sentinel'],
  ['aws_security_hub', 'AWS Security Hub'],
  ['microsoft_defender', 'Microsoft Defender'],
  ['elastic', 'Elastic Security'],
])

function formatDuration(sec: number): string {
  sec = Math.max(0, Math.floor(sec))
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`
  return `${Math.floor(sec / 86400)}d`
}

function formatRelative(iso: string | null): string {
  const t = iso ? new Date(iso).getTime() : NaN
  return Number.isNaN(t) ? 'never' : `${formatDuration((Date.now() - t) / 1000)} ago`
}

// Whole minutes read as "5 min"; anything else stays in seconds so a sub-minute
// interval is never rounded.
function formatEvery(sec: number): string {
  return sec % 60 === 0 ? `${sec / 60} min` : `${sec} s`
}

const UNIT_SECONDS = new Map([
  ...['s', 'sec', 'secs', 'second', 'seconds'].map((u) => [u, 1] as const),
  ...['m', 'min', 'mins', 'minute', 'minutes'].map((u) => [u, 60] as const),
  ...['h', 'hr', 'hrs', 'hour', 'hours'].map((u) => [u, 3600] as const),
])

/** "5", "5 min", "90s", "1h" to seconds; a bare number is minutes. null when unreadable. */
function parseEvery(text: string): number | null {
  const m = /^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$/i.exec(text)
  if (!m) return null
  const mult = UNIT_SECONDS.get(m[2].toLowerCase() || 'm')
  return mult ? Math.round(Number(m[1]) * mult) : null
}

const MIN_EVERY = 10
const MAX_EVERY = 86400

// A JSON dict a few keys deep; {} (or nothing) means the adapter has not saved a position yet.
function formatCursor(cursor: FederationSourceView['cursor']): string {
  if (!cursor || Object.keys(cursor).length === 0) return 'no cursor yet'
  const text = JSON.stringify(cursor)
  return text.length > 120 ? `${text.slice(0, 117)}…` : text
}

/** Edits as text so the user's own units survive; commits on blur or Enter. */
function EveryInput({
  seconds,
  onCommit,
  label,
}: {
  seconds: number
  onCommit: (seconds: number | null) => void
  label: string
}) {
  const [draft, setDraft] = useState<string | null>(null)
  const commit = () => {
    if (draft === null) return
    const next = parseEvery(draft)
    setDraft(null)
    if (next === null || next !== seconds) onCommit(next)
  }
  return (
    <TextInput
      className="!w-24"
      aria-label={label}
      value={draft ?? formatEvery(seconds)}
      onFocus={(e) => e.target.select()}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
    />
  )
}

function LastPoll({ s, name }: { s: FederationSourceView; name: string }) {
  const level = !s.enabled || s.quiet === undefined ? null : s.quiet ? 'poor' : 'good'
  const every = formatEvery(s.interval_seconds)
  return (
    <span className="inline-flex items-center gap-2">
      <LevelBadge variant="pill" level={level} />
      <span className={level ? undefined : 'muted'}>{formatRelative(s.last_poll_at)}</span>
      <InfoTip
        label={`How ${name} collection is measured`}
        source={`${name} poll results. Cursor: ${formatCursor(s.cursor)}.`}
        calculation={
          s.lag_seconds == null
            ? 'Lag is not measured yet: no poll has succeeded.'
            : `Lag is ${formatDuration(s.lag_seconds)} since the last successful poll. Good while that stays within the poll interval, Poor once it is exceeded.`
        }
        limit={`Expected every ${every}. A failed poll does not reset the lag. No level shows while the source is off.`}
      />
    </span>
  )
}

export default function FederationSection({ notify }: SectionProps) {
  const { sources, globalEnabled, phase, error, reload, setGlobal, patchSource, pollNow } =
    useFederation()

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading alert collection…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load alert collection sources: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const nameOf = (id: string) => SOURCE_LABELS.get(id) ?? id

  const onToggleGlobal = async () => {
    try {
      await setGlobal(!globalEnabled)
      notify('ok', `Alert collection turned ${!globalEnabled ? 'on' : 'off'}.`)
    } catch {
      notify('err', 'Failed to update alert collection.')
    }
  }

  const onPatch = async (id: string, patch: Parameters<typeof patchSource>[1]) => {
    try {
      await patchSource(id, patch)
    } catch {
      notify('err', `Failed to update ${nameOf(id)}.`)
    }
  }

  const onEvery = (id: string, seconds: number | null) => {
    if (seconds === null || seconds < MIN_EVERY || seconds > MAX_EVERY) {
      notify('err', 'Poll every must be between 10 seconds and 24 hours, for example 5 min or 90 s.')
      return
    }
    onPatch(id, { interval_seconds: seconds })
  }

  const onPollNow = async (id: string) => {
    try {
      await pollNow(id)
      notify('ok', `Triggered poll for ${nameOf(id)}.`)
    } catch {
      notify('err', `Failed to trigger poll for ${nameOf(id)}.`)
    }
  }

  return (
    <>
      <section className="card card-sq settings-card wide fed-master">
        <div className="card-b">
          <div className="toggle-row">
            <div className="toggle-row-text">
              <span className="toggle-row-label font-semibold">
                Alert collection is {globalEnabled ? 'on' : 'off'}
              </span>
              <span className="toggle-row-hint">
                Master switch. Turning it off stops every poll; each source keeps its settings.
                Collected alerts go to triage like any other.
              </span>
            </div>
            <Toggle checked={globalEnabled} onChange={onToggleGlobal} label="Alert collection" />
          </div>
        </div>
      </section>

      <section className="card card-sq settings-card wide">
        <div className="card-b">
          {/* no .table-wrap: its overflow would clip the ⓘ popover */}
          <div>
            <table className="tbl fed-table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>On</th>
                  <th>Poll every</th>
                  <th>Only alerts at or above</th>
                  <th>Last poll</th>
                  <th>Errors</th>
                  <th><span className="sr-only">Poll now</span></th>
                </tr>
              </thead>
              <tbody>
                {sources.length === 0 && (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState
                        table
                        compact
                        icon="graph"
                        title="No alert sources available"
                        body="Connect an integration such as Splunk, CrowdStrike, or Sentinel first. Sources appear here when the daemon starts."
                      />
                    </td>
                  </tr>
                )}
                {sources.map((s) => {
                  const name = nameOf(s.source_id)
                  return (
                    <tr key={s.source_id}>
                      <td>
                        <div className="flex flex-col">
                          <span className="font-semibold">{name}</span>
                          <span className="text-xs text-tx-3">
                            {s.is_configured ? s.source_id : 'Not configured: add the integration first'}
                          </span>
                        </div>
                      </td>
                      <td>
                        <Toggle
                          checked={s.enabled}
                          disabled={!s.is_configured}
                          label={`Collect from ${name}`}
                          onChange={(v) => onPatch(s.source_id, { enabled: v })}
                        />
                      </td>
                      <td>
                        <EveryInput
                          seconds={s.interval_seconds}
                          label={`Poll ${name} every`}
                          onCommit={(sec) => onEvery(s.source_id, sec)}
                        />
                      </td>
                      <td>
                        <div className="w-36">
                          <Select
                            value={s.min_severity || ''}
                            options={SEVERITY_OPTIONS}
                            onSelect={(v) => onPatch(s.source_id, { min_severity: v || null })}
                          />
                        </div>
                      </td>
                      <td><LastPoll s={s} name={name} /></td>
                      <td>
                        {(s.consecutive_errors || 0) > 0 ? (
                          <span style={{ color: 'var(--fair)' }} title={s.last_error || ''}>
                            {s.consecutive_errors}
                          </span>
                        ) : (
                          <span className="muted">{s.last_poll_at ? 0 : '—'}</span>
                        )}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <button
                          className="btn ghost"
                          disabled={!s.is_configured}
                          onClick={() => onPollNow(s.source_id)}
                          title={s.is_configured ? 'Trigger an immediate poll' : 'Configure the integration first'}
                        >
                          Poll now
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* mb-36: room under the last row for its ⓘ popover, so it opens without scrolling the pane */}
      <p className="text-xs text-tx-3 m-0 mb-36">
        Called “federation” in current Vigil. It pulls alerts from other tools on a schedule; it is
        not Vigil-to-Vigil sharing. Each source starts from now, with no historical backfill, and an
        alert pulled twice is counted once.
      </p>
    </>
  )
}
