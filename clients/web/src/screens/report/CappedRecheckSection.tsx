import { useEffect, useState } from 'react'
import { cappedRecheckApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'

type Report = Schema<'Day1Report'>
type Batch = Schema<'CappedBatch'>
type Group = Schema<'CappedGroup'>
type Direction = Schema<'CappedDirection'>
type DirectionName = Direction['direction']
type GroupName = Group['group']
type Cost = Schema<'CappedCost'>
type Ticket = Schema<'CappedTicket'>
type Projection = Schema<'Projection'>

function pct(value: number): string {
  const shown = value * 100
  const rounded = Math.round(shown)
  return Math.abs(shown - rounded) < 0.05 ? `${rounded}%` : `${shown.toFixed(1)}%`
}

function money(value: number): string {
  return `$${value.toFixed(2)}`
}

function wallText(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return rest ? `${minutes} min ${rest} s` : `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const min = minutes % 60
  return min ? `${hours} h ${min} min` : `${hours} h`
}

function directionWord(direction: DirectionName): string {
  switch (direction) {
    case 'not_real_not_real':
      return 'Closed as not real, re-decided not real'
    case 'not_real_real':
      return 'Closed as not real, re-decided real'
    case 'undetermined_real':
      return 'Closed undetermined, re-decided real'
    case 'undetermined_not_real':
      return 'Closed undetermined, re-decided not real'
    case 'not_real_undetermined':
      return 'Closed as not real, re-decided undetermined'
    case 'undetermined_undetermined':
      return 'Closed undetermined, re-decided undetermined'
    case 'could_not':
      return 'Could not re-check'
    default: {
      const never: never = direction
      return never
    }
  }
}

function definition(direction: DirectionName): string {
  switch (direction) {
    case 'could_not':
      return 'It stays in the count.'
    case 'not_real_not_real':
    case 'not_real_real':
    case 'undetermined_real':
    case 'undetermined_not_real':
    case 'not_real_undetermined':
    case 'undetermined_undetermined':
      return "This direction among the group's tickets."
    default: {
      const never: never = direction
      return never
    }
  }
}

function groupWord(group: GroupName): string {
  switch (group) {
    case 'top':
      return 'Top of the re-check list'
    case 'control':
      return 'Random control'
    default: {
      const never: never = group
      return never
    }
  }
}

function figure(row: Direction, n: number): string {
  const name = directionWord(row.direction)
  if (row.low == null || row.high == null) return `${name}: ${row.k} of ${n}. Measured. ${definition(row.direction)}`
  if (row.k === 0) {
    return `${name}: 0 of ${n}. The true rate could still be up to ${pct(row.high)}. Measured. ${definition(row.direction)}`
  }
  return `${name}: ${row.k} of ${n}, ${pct(row.k / n)} (${pct(row.low)}–${pct(row.high)}). Measured. ${definition(row.direction)}`
}

function costLine(cost: Cost): string {
  const runs = `${cost.runs} ${cost.runs === 1 ? 'run' : 'runs'}`
  if (cost.unread) {
    const wall = cost.wall_ms == null ? '' : `, ${wallText(cost.wall_ms)}`
    return `${runs}${wall}. Model calls and dollars could not be read.`
  }
  const parts = [runs]
  if (cost.calls != null) parts.push(`${cost.calls} model ${cost.calls === 1 ? 'call' : 'calls'}`)
  if (cost.wall_ms != null) parts.push(wallText(cost.wall_ms))
  const line = parts.join(', ')
  if (cost.priced_at_zero) {
    return cost.local_model ? `${line}. Priced at $0. Local model.` : `${line}. Priced at $0.`
  }
  if (cost.unpriced) return `${line}. An unpriced call is not $0.`
  if (cost.dollars != null) return `${line}. ${money(cost.dollars)}.`
  return `${line}.`
}

function statusOf(error: unknown): number | undefined {
  return (error as { response?: { status?: number } }).response?.status
}

function ProjectionView({ projection }: { projection: Projection }) {
  const reaches = projection.ceiling_reaches
  const worst = projection.worst_case
  const heading = projection.status === 'running' || projection.status === 'interrupted'
    ? 'Open batch'
    : projection.status.startsWith('stopped:')
      ? projection.status
      : 'Latest batch'
  return (
    <div>
      <p className="d1-sub">{heading}</p>
      <p>Status: {projection.status}</p>
      <p className="d1-facts">
        {projection.tickets_chosen} tickets chosen
        {projection.could_not_recheck > 0 ? `, ${projection.could_not_recheck} could not be re-checked` : ''}.
        {' '}The ceiling reaches {reaches.top} of the top and {reaches.control} of the control.
        {' '}{projection.run_count} {projection.run_count === 1 ? 'run' : 'runs'} started.
      </p>
      <p className="d1-facts">
        Worst case: {worst.calls} model calls, {money(worst.dollars)}, {wallText(worst.wall_ms)}.
        {' '}{projection.post_install_closures} closures at or after install were not drawn.
        {' '}Control draw: {projection.control_count}{projection.control_short ? ', short of 25' : ''}.
      </p>
      {projection.reserve && (
        <p className="d1-facts">
          {projection.reserve.remainder == null ? 'No reserve remainder.' : `Reserve remainder ${money(projection.reserve.remainder)}.`}
          {' '}{projection.reserve.note}
        </p>
      )}
    </div>
  )
}

function TicketLine({ ticket }: { ticket: Ticket }) {
  return (
    <div className="d1-row">
      <p>{ticket.ticket}</p>
      {ticket.title && <p>Title: {ticket.title}</p>}
      {(ticket.entities ?? []).length > 0 && <p>Entities: {(ticket.entities ?? []).join(', ')}</p>}
      {ticket.text_note && <p className="d1-facts">{ticket.text_note}</p>}
      <p className="d1-facts">
        {ticket.source} · {directionWord(ticket.direction)}
        {ticket.reason ? ` · ${ticket.reason}` : ''}
      </p>
    </div>
  )
}

function GroupBlock({ group }: { group: Group }) {
  const rows = group.rows ?? []
  const tickets = group.tickets ?? []
  return (
    <div>
      <p className="d1-sub">{groupWord(group.group)}</p>
      <p className="d1-facts">{group.n} {group.n === 1 ? 'ticket' : 'tickets'}</p>
      {group.n > 0 && (
        <div className="d1-rows">
          {rows.map((row) => (
            <div className="d1-row" key={row.direction}>
              <p>{figure(row, group.n)}</p>
            </div>
          ))}
        </div>
      )}
      {group.n > 0 && (
        <p className="d1-facts">
          Used knowledge from after the close: {group.after_close} of {group.n}. Approximate close: {group.approximate} of {group.n}.
        </p>
      )}
      {tickets.length > 0 && (
        <div className="d1-rows">
          {tickets.map((ticket, index) => (
            <TicketLine key={`${ticket.ticket}-${index}`} ticket={ticket} />
          ))}
        </div>
      )}
    </div>
  )
}

function BatchBlock({ title, batch, note }: { title: string; batch: Batch; note?: string }) {
  return (
    <div>
      <p className="d1-sub">{title}</p>
      {note && <p className="d1-facts">{note}</p>}
      {title !== batch.status && <p className="d1-facts">{batch.status}</p>}
      <p>{costLine(batch.cost)}</p>
      {(batch.groups ?? []).map((group) => (
        <GroupBlock key={group.group} group={group} />
      ))}
    </div>
  )
}

/** The capped re-check: a live projection, then the figures frozen on this report. */
export function CappedRecheckSection({ report }: { report: Report }) {
  const { hasPermission } = useAuth()
  const canStart = hasPermission('platform.write')
  const hidden = new Set(report.withheld ?? [])
  const modules = report.modules ?? []
  const section = report.history_capped_recheck
  const [projection, setProjection] = useState<Projection | null>(null)
  const [starting, setStarting] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [liveError, setLiveError] = useState<string | null>(null)

  useEffect(() => {
    let gone = false
    setLiveError(null)
    cappedRecheckApi.latest().then(
      ({ data }) => {
        if (!gone) setProjection(data)
      },
      (error: unknown) => {
        if (gone) return
        if (statusOf(error) === 404) {
          setProjection(null)
          return
        }
        setLiveError(errorText(error, 'The live batch could not be read.'))
      },
    )
    return () => {
      gone = true
    }
  }, [report.id])

  if (!modules.includes('history.capped_recheck') || hidden.has('history.capped_recheck') || !section) {
    return null
  }

  const frozen = new Set(
    [section.finished?.batch_id, section.stopped?.batch_id].filter((id): id is string => Boolean(id)),
  )
  const live = projection && !frozen.has(projection.batch_id) ? projection : null

  const start = async () => {
    setStarting(true)
    setRefusal(null)
    try {
      const { data } = await cappedRecheckApi.start()
      setProjection(data)
    } catch (error) {
      setRefusal(errorText(error, 'The capped re-check did not start.'))
    } finally {
      setStarting(false)
    }
  }

  return (
    <section className="d1-card" aria-label="Capped re-check">
      <h2>Capped re-check</h2>
      {section.not_checked && <p>{section.not_checked}</p>}
      {section.awaiting_list && <p>No Day 1 Report has a re-check list yet.</p>}
      {section.how && <p className="d1-fix">{section.how}</p>}
      {section.none_run && !live && <p>No capped re-check has run.</p>}
      {section.open_only && !live && (
        <p>A capped re-check is open. This report does not freeze it.</p>
      )}
      {canStart && (
        <div className="d1-actions">
          <button type="button" className="btn" onClick={() => void start()} disabled={starting}>
            {starting ? 'Starting…' : 'Start capped re-check'}
          </button>
        </div>
      )}
      {refusal && <p role="alert">{refusal}</p>}
      {liveError && <p role="alert">{liveError}</p>}
      {live && <ProjectionView projection={live} />}
      {section.finished && (
        <BatchBlock title="Latest finished" batch={section.finished} />
      )}
      {section.stopped && (
        <BatchBlock
          title={section.stopped.status}
          batch={section.stopped}
          note="Stopped later. Not included in the finished counts."
        />
      )}
    </section>
  )
}
