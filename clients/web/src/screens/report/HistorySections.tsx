import { Link } from 'react-router-dom'
import type { Schema } from '../../services/apiTypes'
import { CappedRecheckSection } from './CappedRecheckSection'
import { reportStamp } from './stamp'

type Before = Schema<'HistoryBefore'>
type Recheck = Schema<'HistoryRecheck'>
type Tuning = Schema<'HistoryTuning'>
type Figure = Schema<'HistoryFigure'>
type TimeRow = Schema<'HistoryTime'>
type PerDay = Schema<'HistoryPerDay'>
type Bulk = Schema<'HistoryBulk'>
type Source = Schema<'HistorySource'>
type Severity = Schema<'HistorySeverity'>
type ImportRead = Schema<'HistoryImportRead'>
type WindowRow = Schema<'HistoryWindow'>
type RecheckEntry = Schema<'HistoryRecheckEntry'>
type TuningEntry = Schema<'HistoryTuningEntry'>
type Report = Schema<'Day1Report'>
type Kind = Figure['kind']
type Group = TuningEntry['group']

const REASON: Record<string, string> = {
  bulk_closed: 'Bulk-closed',
  closed_fast: 'Closed fast',
  thin_notes: 'Thin notes',
  later_true_positive: 'Same host, user or IP on a later true positive',
  undetermined: 'Closed undetermined',
}

function kindWord(kind: Kind): string {
  switch (kind) {
    case 'measured':
      return 'Measured'
    case 'estimated':
      return 'Estimated'
    default: {
      const never: never = kind
      return never
    }
  }
}

function groupWord(group: Group): string {
  switch (group) {
    case 'rule':
      return 'Rule'
    case 'title':
      return 'Title'
    case 'entity':
      return 'Entity'
    default: {
      const never: never = group
      return never
    }
  }
}

function pct(value: number): string {
  const shown = value * 100
  const rounded = Math.round(shown)
  return Math.abs(shown - rounded) < 0.05 ? `${rounded}%` : `${shown.toFixed(1)}%`
}

function share(figure: Figure): string {
  if (figure.low == null || figure.high == null) return `${figure.k} of ${figure.n}`
  const point = figure.n > 0 ? pct(figure.k / figure.n) : '0%'
  return `${figure.k} of ${figure.n}, ${point} (${pct(figure.low)}–${pct(figure.high)})`
}

function windowOf(start: string, end: string): string {
  return `${reportStamp(start)} to ${reportStamp(end)}`
}

function FigureLine({ figure }: { figure: Figure }) {
  return (
    <p>
      {figure.label}: {share(figure)}. {kindWord(figure.kind)}. {figure.definition}. Window{' '}
      {windowOf(figure.window_start, figure.window_end)}.
    </p>
  )
}

function TimeLine({ row }: { row: TimeRow }) {
  const bands = (row.bands ?? []).map((band) => `${band.count} ${band.label}`).join(', ')
  const headline = row.median ? `Median ${row.median}, p90 ${row.p90 ?? '—'}` : bands || 'No timed closures'
  return (
    <p>
      Time to close: {headline}. {row.count} timed
      {row.opened_missing > 0 ? `, ${row.opened_missing} left out with no open time` : ''}. {kindWord(row.kind)}. {row.definition}. Window {windowOf(row.window_start, row.window_end)}.
    </p>
  )
}

function spanText(days: number): string {
  if (days >= 1) return `${days} days`
  const hours = days * 24
  if (hours >= 1) return `${hours.toFixed(1)} hours`
  return `${Math.max(1, Math.round(hours * 60))} min`
}

function PerDayLine({ row }: { row: PerDay }) {
  const rate = row.per_day == null
    ? `${row.count} closures, all at one time`
    : `${row.per_day} per day, ${row.count} closures over ${spanText(row.span_days)}`
  return (
    <p>
      Closures per day: {rate}. {kindWord(row.kind)}. {row.definition}. Window {windowOf(row.window_start, row.window_end)}.
    </p>
  )
}

function BulkLine({ bulk }: { bulk: Bulk }) {
  return (
    <>
      <FigureLine figure={bulk} />
      {(bulk.not_tested ?? []).map((line) => (
        <p className="d1-facts" key={line}>{line}</p>
      ))}
    </>
  )
}

function Slice({ row }: { row: Pick<Source, 'dispositions' | 'time_to_close' | 'bulk' | 'per_day' | 'closures'> }) {
  return (
    <>
      <p className="d1-facts">{row.closures} closures</p>
      {(row.dispositions ?? []).map((figure) => (
        <FigureLine key={figure.label} figure={figure} />
      ))}
      {(row.time_to_close ?? []).map((time) => (
        <TimeLine key={time.definition} row={time} />
      ))}
      <BulkLine bulk={row.bulk} />
      <PerDayLine row={row.per_day} />
    </>
  )
}

function Imports({ rows }: { rows: ImportRead[] }) {
  if (rows.length === 0) return null
  return (
    <>
      <p className="d1-sub">Imports read</p>
      <div className="d1-rows">
        {rows.map((row) => (
          <div className="d1-row" key={row.id}>
            <p>
              {row.source} · {row.included ? 'Included' : 'Not included'} · imported {reportStamp(row.imported_at)} · {row.has_ticket_detail ? 'ticket detail' : 'no ticket detail'}
            </p>
            <p className="d1-facts">{row.id}</p>
            {(row.left_out ?? []).map((line) => (
              <p className="d1-facts" key={line}>{line}</p>
            ))}
            {row.unmapped_count > 0 && (
              <p className="d1-facts">
                Unmapped dispositions: {(row.unmapped ?? []).join(', ') || row.unmapped_note}. {row.unmapped_count} rows.
              </p>
            )}
            {row.bulk_not_tested != null && row.bulk_not_tested > 0 && (
              <p className="d1-facts">{row.bulk_not_tested} not tested for bulk closes, as the import recorded it.</p>
            )}
          </div>
        ))}
      </div>
    </>
  )
}

function Windows({ rows }: { rows: WindowRow[] }) {
  if (rows.length === 0) return null
  return (
    <>
      {rows.map((row) => (
        <p className="d1-facts" key={row.source}>
          {row.source}: {windowOf(row.start, row.end)}
          {row.outside > 0 ? ` · ${row.outside} outside the window` : ''}
        </p>
      ))}
    </>
  )
}

function Lead({ notChecked, how, notes }: { notChecked?: string | null; how?: string | null; notes?: string[] }) {
  return (
    <>
      {notChecked && <p>{notChecked}</p>}
      {how && <p className="d1-fix">{how}</p>}
      {(notes ?? []).map((note) => (
        <p className="d1-facts" key={note}>{note}</p>
      ))}
    </>
  )
}

function Before({ section }: { section: Before }) {
  return (
    <section className="d1-card" aria-label='Your "before"'>
      <h2>Your &ldquo;before&rdquo;</h2>
      <Lead notChecked={section.not_checked} how={section.how} notes={section.notes} />
      <Windows rows={section.windows ?? []} />
      <Imports rows={section.imports ?? []} />
      {(section.sources ?? []).map((source) => (
        <div key={source.source}>
          <p className="d1-sub">{source.source}</p>
          <div className="d1-rows">
            <div className="d1-row">
              <Slice row={source} />
              {source.outside > 0 && <p className="d1-facts">{source.outside} outside the window</p>}
            </div>
          </div>
          {(source.by_severity ?? []).map((severity: Severity) => (
            <div key={severity.severity}>
              <p className="d1-sub">{source.source} · {severity.severity}</p>
              <div className="d1-rows">
                <div className="d1-row">
                  <Slice row={severity} />
                </div>
              </div>
            </div>
          ))}
        </div>
      ))}
      {!section.not_checked && (section.sources ?? []).length === 0 && (
        <p className="d1-quiet">Nothing to report in this section.</p>
      )}
    </section>
  )
}

function Recheck({ section }: { section: Recheck }) {
  const entries = section.entries ?? []
  return (
    <section className="d1-card" aria-label="Re-check list">
      <h2>Re-check list</h2>
      <Lead notChecked={section.not_checked} how={section.how} notes={section.notes} />
      <Windows rows={section.windows ?? []} />
      {section.total != null && (
        <p className="d1-facts">{entries.length} of {section.total} shown. Each is a candidate for review.</p>
      )}
      {entries.length > 0 && (
        <div className="d1-rows">
          {entries.map((entry: RecheckEntry, index) => (
            <div className="d1-row" key={index}>
              <p>{entry.ticket}</p>
              {entry.title && <p>Title: {entry.title}</p>}
              {entry.text_note && <p className="d1-facts">{entry.text_note}</p>}
              <p className="d1-facts">
                {entry.source} · {reportStamp(entry.closed_at)} · {entry.disposition} · {entry.severity}
              </p>
              <p className="d1-facts">
                {(entry.reasons ?? []).map((reason) => REASON[reason] ?? reason).join(' · ') || 'Candidate for review'}
              </p>
              {entry.source === 'microsoft_defender' && (
                <p className="d1-facts">not checked: Defender gives a category, not notes</p>
              )}
            </div>
          ))}
        </div>
      )}
      <Imports rows={section.imports ?? []} />
    </section>
  )
}

function TuningRow({ row }: { row: TuningEntry }) {
  const name = row.group === 'rule' ? row.rule_id : row.label
  const range = row.low != null && row.high != null ? ` (${pct(row.low)}–${pct(row.high)})` : ''
  return (
    <div className="d1-row">
      <p>
        {groupWord(row.group)} {name || ''}
      </p>
      {row.ticket && <p className="d1-facts">{row.ticket}</p>}
      {row.text_note && <p className="d1-facts">{row.text_note}</p>}
      <p>
        {row.true_positives} of {row.judged} true positives{range}. {kindWord(row.kind)}. {row.definition}.
      </p>
      <p className="d1-facts">
        {row.source} · window {windowOf(row.window_start, row.window_end)} · {row.where}
      </p>
      {row.exclude_link && <Link to={row.exclude_link}>Exclude this address</Link>}
      {row.exclude_note && <p>{row.exclude_note}</p>}
    </div>
  )
}

function Tuning({ section }: { section: Tuning }) {
  const candidates = section.candidates ?? []
  return (
    <section className="d1-card" aria-label="Tuning candidates">
      <h2>Tuning candidates</h2>
      <Lead notChecked={section.not_checked} how={section.how} notes={section.notes} />
      <Windows rows={section.windows ?? []} />
      {candidates.length > 0 && (
        <div className="d1-rows">
          {candidates.map((row, index) => (
            <TuningRow key={index} row={row} />
          ))}
        </div>
      )}
      {!section.not_checked && candidates.length === 0 && (
        <p className="d1-quiet">Nothing to report in this section.</p>
      )}
      <Imports rows={section.imports ?? []} />
    </section>
  )
}

/** The three history sections. A withheld module is the page's Not included line. */
export function HistorySections({ report }: { report: Report }) {
  const hidden = new Set(report.withheld ?? [])
  const modules = report.modules ?? []
  return (
    <>
      {modules.includes('history.before') && !hidden.has('history.before') && report.history_before && (
        <Before section={report.history_before} />
      )}
      {modules.includes('history.recheck') && !hidden.has('history.recheck') && report.history_recheck && (
        <Recheck section={report.history_recheck} />
      )}
      <CappedRecheckSection report={report} />
      {modules.includes('history.tuning') && !hidden.has('history.tuning') && report.history_tuning && (
        <Tuning section={report.history_tuning} />
      )}
    </>
  )
}
