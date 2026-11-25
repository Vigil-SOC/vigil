import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDay, utcDayClock } from '../../shared/utc'
import { Icon } from '../../shared/icons'
import { LevelBadge } from '../../shared/LevelBadge'
import {
  ConfirmDialog,
  Field,
  NumberInput,
  PasswordInput,
  Select,
  SettingsCard,
  TextInput,
  ToggleRow,
} from '../../shared/ui'
import {
  configApi,
  day1Api,
  federationApi,
  importsApi,
  ingestionApi,
  type ConnectedHistorySource,
  type HistoryImport,
  type IngestionJob,
} from '../../services/api'
import { stepUpDetailOf } from '../../services/stepUp'
import {
  useDarktrace,
  useIngestionJob,
  useKafka,
  useS3,
  notifyDemoModeChanged,
  type KafkaConfig,
} from './useSettings'
import type { SectionProps } from './types'

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export const DEMO_OFF = 'Demo data is off. Vigil now shows your real findings and cases.'

export function DemoDataClear({ notify }: SectionProps) {
  const [enabled, setEnabled] = useState(false)
  // DEMO_MODE in the server's environment wins over the setting saved here
  const [fromEnv, setFromEnv] = useState(false)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<string | null>(null)
  const [confirmOff, setConfirmOff] = useState(false)
  const [turnedOff, setTurnedOff] = useState(false)

  useEffect(() => {
    let live = true
    configApi
      .getDemoMode()
      .then((res) => {
        if (!live) return
        setEnabled(Boolean(res.data?.enabled))
        setFromEnv(res.data?.source === 'environment')
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  if (!enabled) return null

  const clear = async () => {
    setBusy(true)
    try {
      const res = await configApi.resetDemoData()
      const data = res.data as { findings_count?: number; cases_count?: number }
      const text = `Regenerated ${data.findings_count} findings and ${data.cases_count} cases.`
      setReport(text)
      notify('ok', text)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Could not clear demo data.')
    } finally {
      setBusy(false)
    }
  }

  const turnOff = async () => {
    setBusy(true)
    try {
      await configApi.setDemoMode(false)
      setTurnedOff(true)
      setReport(null)
      notifyDemoModeChanged()
      notify('ok', DEMO_OFF)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Could not turn off demo data.')
    } finally {
      setBusy(false)
      setConfirmOff(false)
    }
  }

  return (
    <SettingsCard
      title="Demo data"
      desc="Findings and cases on screen are generated sample data. Clearing them regenerates that set."
    >
      {!turnedOff && (
        <div className="flex flex-wrap gap-2.5">
          <button className="btn primary" onClick={clear} disabled={busy}>
            {busy ? 'Clearing…' : 'Clear demo data'}
          </button>
          {!fromEnv && (
            <button className="btn ghost" onClick={() => setConfirmOff(true)} disabled={busy}>
              Turn off demo data
            </button>
          )}
        </div>
      )}
      {fromEnv && (
        <p className="text-xs text-tx-3 mt-3">
          Demo mode is set by the server&apos;s environment (DEMO_MODE), so it can only be turned off there.
        </p>
      )}
      {(report || turnedOff) && (
        <div className="settings-banner ok mt-3" role="status">
          <Icon name="check2" size={13} />
          <span className="text-xs">{turnedOff ? DEMO_OFF : report}</span>
        </div>
      )}
      <ConfirmDialog
        open={confirmOff}
        danger={false}
        title="Turn off demo data"
        body="Vigil stops showing generated sample data and works on your real findings and cases."
        confirmLabel="Turn off"
        busy={busy}
        onConfirm={turnOff}
        onClose={() => setConfirmOff(false)}
      />
    </SettingsCard>
  )
}

export const SAMPLE_TRIAGE_NOTE = 'The daemon triages them like any other alert, within about five minutes.'

function errorDetail(e: unknown, fallback: string): string {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
}

export function SampleAlertsCard({ notify }: SectionProps) {
  const [loaded, setLoaded] = useState<number | null>(null)
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)

  useEffect(() => {
    let live = true
    ingestionApi
      .sampleAlerts()
      .then((res) => {
        if (!live) return
        setLoaded(res.data.loaded)
        setTotal(res.data.total)
      })
      // 409 in demo mode: the demo card covers that case
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  if (loaded === null) return null

  const load = async () => {
    setBusy(true)
    try {
      const res = await ingestionApi.loadSampleAlerts()
      setLoaded(res.data.loaded)
      setTotal(res.data.total)
      notify('ok', `${res.data.loaded} sample alerts stored. ${SAMPLE_TRIAGE_NOTE}`)
    } catch (e) {
      notify('err', errorDetail(e, 'Could not load sample alerts.'))
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    setBusy(true)
    try {
      const res = await ingestionApi.clearSampleAlerts()
      setLoaded(0)
      const cases = res.data.cases_deleted
      const kept = res.data.restricted_cases_kept ?? 0
      notify(
        'ok',
        `Removed ${res.data.findings_deleted} sample alerts` +
          (cases ? ` and ${cases} case${cases === 1 ? '' : 's'} made from them.` : '.') +
          (kept ? ` ${kept} restricted case${kept === 1 ? '' : 's'} kept.` : ''),
      )
    } catch (e) {
      notify('err', errorDetail(e, 'Could not clear sample alerts.'))
    } finally {
      setBusy(false)
      setConfirmClear(false)
    }
  }

  return (
    <SettingsCard
      title="Sample alerts"
      desc="A short set of example alerts, stored next to your real ones and marked vigil-sample, to watch triage work before a source is connected."
    >
      <div className="flex flex-wrap items-center gap-2.5">
        {loaded < total && (
          <button className="btn primary" onClick={load} disabled={busy}>
            {busy ? 'Loading…' : 'Load sample alerts'}
          </button>
        )}
        {loaded > 0 && (
          <button className="btn ghost" onClick={() => setConfirmClear(true)} disabled={busy}>
            Clear sample alerts
          </button>
        )}
        <span className="text-xs text-tx-3">
          {loaded ? `${loaded} of ${total} stored. ${SAMPLE_TRIAGE_NOTE}` : `${total} alerts across every severity.`}
        </span>
      </div>
      <ConfirmDialog
        open={confirmClear}
        title="Clear sample alerts"
        body="Deletes the sample alerts and any case that holds only sample alerts. Your real alerts and cases are not touched."
        confirmLabel="Clear"
        busy={busy}
        onConfirm={clear}
        onClose={() => setConfirmClear(false)}
      />
    </SettingsCard>
  )
}

export const ACCEPTED_UPLOAD_TYPES = '.parquet,.csv,.json,.jsonl,.ndjson'

export function UploadCard({ notify }: SectionProps) {
  const { job, attaching, upload } = useIngestionJob()
  return (
    <SettingsCard title="Upload files" desc="Import findings or cases from a file. Duplicates are skipped.">
      <FileDrop
        running={job?.status === 'running'}
        attaching={attaching}
        testId="manual-upload-input"
        onUpload={async (file) => {
          await upload(file)
          notify('ok', `Ingesting ${file.name} in the background.`)
        }}
        notify={notify}
      />
      {job && <IngestionJobStatus job={job} />}
    </SettingsCard>
  )
}

/** The drop zone both upload cards share; onUpload throws to keep the chosen file. */
function FileDrop({
  running,
  attaching,
  testId,
  onUpload,
  notify,
}: {
  running: boolean
  attaching: boolean
  testId: string
  onUpload: (file: File) => Promise<void>
  notify: SectionProps['notify']
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [dragging, setDragging] = useState(false)

  const doUpload = async () => {
    if (!file) return
    setSubmitting(true)
    try {
      await onUpload(file)
      setFile(null)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Upload failed.')
    } finally {
      setSubmitting(false)
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped && !running) setFile(dropped)
  }

  return (
    <div
      className={`data-drop${dragging ? ' over' : ''}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <Icon name="upload" size={20} />
      <span className="data-drop-title">Drop a file to import, or choose a file</span>
      <span className="data-drop-sub">
        {ACCEPTED_UPLOAD_TYPES.split(',').join(', ')} · large files keep importing in the background
      </span>
      <input
        ref={fileRef}
        type="file"
        accept={ACCEPTED_UPLOAD_TYPES}
        className="hidden"
        data-testid={testId}
        onChange={(e) => setFile(e.target.files?.[0] || null)}
        onClick={(e) => { (e.target as HTMLInputElement).value = '' }} // lets the same file be retried
      />
      <div className="flex items-center justify-center gap-2.5 flex-wrap">
        <button className={`btn ${file ? 'ghost' : 'primary'}`} onClick={() => fileRef.current?.click()} disabled={running}>
          <Icon name="paperclip" /> {file ? file.name : 'Choose File'}
        </button>
        {file && <span className="text-xs text-tx-3">{formatFileSize(file.size)}</span>}
        {file && (
          <button className="btn primary" disabled={submitting || running || attaching} onClick={doUpload}>
            <Icon name="upload" /> {submitting ? 'Uploading…' : 'Upload'}
          </button>
        )}
      </div>
    </div>
  )
}

const HISTORY_DESC =
  'Closed tickets become Verdicts and alerts become Sightings in agent memory, so agents recall how your team decided before. ' +
  'Nothing goes to triage: no findings or cases are created.'

const CONNECTED_SOURCES: { value: ConnectedHistorySource; label: string }[] = [
  { value: 'microsoft_defender', label: 'Microsoft Defender' },
  { value: 'azure_sentinel', label: 'Azure Sentinel' },
]

/** The Defender and Sentinel sources that are configured; null while loading. */
function useConnectedHistorySources() {
  const [configured, setConfigured] = useState<typeof CONNECTED_SOURCES | null>(null)
  useEffect(() => {
    federationApi
      .listSources()
      .then((res) => {
        const ready = new Set(res.data.sources.filter((s) => s.is_configured).map((s) => s.source_id))
        setConfigured(CONNECTED_SOURCES.filter((s) => ready.has(s.value)))
      })
      .catch(() => setConfigured([])) // no integrations.read, or no sources: nothing to offer
  }, [])
  return configured
}

/** Reads a configured Defender's or Sentinel's closed records; shown only when one is. */
function ConnectedHistoryImport({
  configured,
  lead,
  running,
  attaching,
  onImport,
}: {
  configured: typeof CONNECTED_SOURCES
  lead: boolean
  running: boolean
  attaching: boolean
  onImport: (source: ConnectedHistorySource, label: string, days: number) => Promise<void>
}) {
  const [picked, setSource] = useState<ConnectedHistorySource | ''>('')
  const [days, setDays] = useState('90')
  const [submitting, setSubmitting] = useState(false)

  const source = picked || configured[0]?.value
  if (!source) return null
  const n = Number(days)
  const valid = Number.isInteger(n) && n >= 1 && n <= 180
  const label = configured.find((s) => s.value === source)?.label ?? source

  const go = async () => {
    setSubmitting(true)
    try {
      await onImport(source, label, n)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={`data-connected${lead ? ' lead' : ''}`}>
      <span className="data-drop-title">From a connected source</span>
      <span className="data-drop-sub">
        {configured.length > 1
          ? 'Resolved Defender alerts and closed Sentinel incidents'
          : `${label}: ${source === 'azure_sentinel' ? 'closed incidents' : 'resolved alerts'}`}{' '}
        that carry a classification. Importing again replaces that source’s earlier import.
      </span>
      <div className="data-connected-row">
        {configured.length > 1 && (
          <div className="data-connected-source">
            <Field label="Source">
              <Select
                value={source}
                options={configured}
                onSelect={(v) => setSource(v as ConnectedHistorySource)}
                disabled={running}
              />
            </Field>
          </div>
        )}
        <div className="data-connected-days">
          <Field label="Last days" error={valid ? null : '1 to 180'}>
            <NumberInput value={days} min={1} max={180} onChange={(e) => setDays(e.target.value)} disabled={running} />
          </Field>
        </div>
        <button className="btn primary" disabled={!valid || running || attaching || submitting} onClick={go}>
          <Icon name="upload" /> {submitting ? 'Starting…' : 'Import'}
        </button>
      </div>
    </div>
  )
}

const IMPORT_STATUS: Record<HistoryImport['status'], { level: string; text: string }> = {
  running: { level: 'idle', text: 'Importing' },
  done: { level: 'good', text: 'Done' },
  failed: { level: 'poor', text: 'Failed' },
  incomplete: { level: 'fair', text: 'Didn’t complete' },
}

const LEFT_OUT_KIND: Record<string, string> = { tickets: 'Tickets', alerts: 'Alerts', rows: 'Rows' }

// the API's reason keys, in words; an unknown key reads with its underscores dropped
const LEFT_OUT_REASON: Record<string, string> = {
  duplicate: 'duplicate',
  no_closed_at: 'no close time',
  unmapped_disposition: 'unmapped disposition',
  bulk_close: 'bulk-closed',
  auto_closed: 'auto-closed',
  no_timestamp: 'no timestamp',
  no_entities: 'no entities',
  unreadable: 'unreadable',
}

const UNMAPPED_SHOWN = 20

const sourceLabel = (row: HistoryImport) =>
  CONNECTED_SOURCES.find((s) => s.value === row.source_system)?.label ?? (row.source_system || 'file')

const stopped = (row: HistoryImport) => row.status === 'failed' || row.status === 'incomplete'

// a connected pull can be repeated as it was; a file has to be uploaded again
const connectedPull = (row: HistoryImport) =>
  row.pull_days != null && CONNECTED_SOURCES.some((s) => s.value === row.source_system)

// opened by default: the rows someone has something to do about
const needsAttention = (row: HistoryImport) => stopped(row) || row.unmapped.length > 0 || !row.has_ticket_detail

// rerun: a stopped connected pull's Re-run; absent without findings.write or once its source is unconfigured
function ReportAgain({
  exists,
  canRun,
  busy,
  onRun,
}: {
  exists: boolean | null
  canRun: boolean
  busy: boolean
  onRun: () => void
}) {
  if (exists === null) return null
  if (!exists) return <p className="data-history-line">The Day 1 Report will include this history.</p>
  if (!canRun) {
    return <p className="data-history-line">Ask someone who can run the Day 1 Report to re-run it.</p>
  }
  return (
    <p className="data-history-line">
      <button type="button" className="data-link" disabled={busy} onClick={onRun}>
        {busy ? 'Running…' : 'Run the Day 1 Report again'}
      </button>
    </p>
  )
}

function ImportDetails({ row, canWrite, rerun }: { row: HistoryImport; canWrite: boolean; rerun?: ReactNode }) {
  const window = row.pull_days != null
    ? `Last ${row.pull_days} days${row.status === 'done' && row.pulled_at ? `, pulled ${utcDayClock(row.pulled_at)}` : ''}`
    : row.concluded_at ? `Through ${utcDay(row.concluded_at)}` : '—'
  const leftOut = Object.entries(row.left_out)
  const more = row.unmapped.length - UNMAPPED_SHOWN
  const droppedMore = row.dropped_columns.length - UNMAPPED_SHOWN
  const facts: [string, ReactNode][] = [
    ['Started', utcDayClock(row.started_at) ?? '—'],
    ['Finished', utcDayClock(row.finished_at) ?? (row.status === 'running' ? 'Still importing' : '—')],
    ['Window', window],
    ['Tickets kept', row.tickets_kept],
  ]
  return (
    <div className="data-history-detail">
      {stopped(row) && (
        <p className={`data-history-warn ${row.status}`}>
          {(row.error || 'The import stopped before it finished').replace(/([^.!?])$/, '$1.')}
          {!connectedPull(row) && ' Upload the file again to import it whole.'}
          {connectedPull(row) && canWrite && !rerun && ' Re-run it once its source is connected again.'}
          {rerun}
        </p>
      )}
      <dl className="data-history-facts">
        {facts.map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
        ))}
      </dl>
      {/* nothing recorded is not "nothing left out" while it runs, or for an import from before ticket detail */}
      {(leftOut.length > 0 || (row.status === 'done' && row.has_ticket_detail)) && (
      <div className="data-history-line">
        <b>Left out</b>{' '}
        {leftOut.length === 0
          ? 'Nothing.'
          : leftOut.map(([kind, reasons], i) => (
            <span key={kind}>
              {i > 0 && ' · '}
              {LEFT_OUT_KIND[kind] ?? kind}:{' '}
              {Object.entries(reasons)
                .map(([reason, n]) => `${n} ${LEFT_OUT_REASON[reason] ?? reason.replace(/_/g, ' ')}`)
                .join(', ')}
            </span>
          ))}
        {leftOut.length > 0 && '.'}
        {row.bulk_not_tested > 0 && ` ${row.bulk_not_tested} tickets name no closer, so they were not tested for bulk closes.`}
        {row.severity_unknown > 0 && ` ${row.severity_unknown} tickets have no severity.`}
      </div>
      )}
      {row.unmapped.length > 0 && (
        <div className="data-history-line">
          <b>Unmapped dispositions</b>{' '}
          {row.unmapped.slice(0, UNMAPPED_SHOWN).map((u, i) => (
            <span key={u.value}>
              {i > 0 && ', '}
              <code className="data-history-value">{u.value}</code> ({u.count})
            </span>
          ))}
          {more > 0 && ` and ${more} more`}. Rename them in the export to a listed value and import again.
        </div>
      )}
      {row.dropped_columns.length > 0 && (
        <div className="data-history-line">
          <b>Dropped columns</b>{' '}
          {row.dropped_columns.slice(0, UNMAPPED_SHOWN).map((column, i) => (
            <span key={column.name}>
              {i > 0 && ', '}
              <code className="data-history-value">{column.name}</code> ({column.count})
            </span>
          ))}
          {droppedMore > 0 && ` and ${droppedMore} more`}. Not stored.
        </div>
      )}
      {!row.has_ticket_detail && (
        <div className="data-history-line">
          Imported before this release; import again to add severity and bulk-closed tickets.
        </div>
      )}
    </div>
  )
}

function deleteImportBody(
  row: HistoryImport | null,
  ledgers: number | null,
  ledgerError: string | null,
  queued: number | null,
): string {
  const verdicts = row?.verdicts_written ?? 0
  const sightings = row?.sightings_written ?? 0
  const source = row ? sourceLabel(row) : ''
  const sentences = [
    `Deletes the ${verdicts} Verdicts and ${sightings} Sightings this ${source} import wrote to agent memory. Agents stop recalling them.`,
  ]
  if (ledgerError) sentences.push(ledgerError)
  else if (ledgers === null) sentences.push('Counting re-decision ledgers…')
  else if (ledgers === 0) sentences.push('No re-decision ledgers stay.')
  else if (ledgers === 1) sentences.push('1 re-decision ledger stays.')
  else sentences.push(`${ledgers} re-decision ledgers stay.`)
  if (queued === 0) sentences.push('No queued review leaves the queue.')
  else if (queued === 1) sentences.push('1 queued review leaves the queue.')
  else if (queued !== null) sentences.push(`${queued} queued reviews leave the queue.`)
  return sentences.join(' ')
}

function eraseLedgersBody(count: number): string {
  const ledgers = count === 1 ? '1 re-decision ledger' : `${count} re-decision ledgers`
  return `Erases ${ledgers}. This cannot be undone.`
}

export function HistoryImportCard({
  notify,
  title = 'Alert and ticket history',
  desc = HISTORY_DESC,
  leadConnected = false,
}: SectionProps & {
  title?: string
  desc?: string
  /** the setup wizard: a configured Defender or Sentinel pull comes before the file drop */
  leadConnected?: boolean
}) {
  const { job, attaching, upload, start } = useIngestionJob('history')
  const { hasPermission } = useAuth()
  const canWrite = hasPermission('findings.write')
  const canRunReport = hasPermission('platform.write')
  const [reportExists, setReportExists] = useState<boolean | null>(null)
  const [runningReport, setRunningReport] = useState(false)
  const configured = useConnectedHistorySources()
  const [imports, setImports] = useState<HistoryImport[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [source, setSource] = useState('')
  const [removing, setRemoving] = useState<HistoryImport | null>(null)
  const [ledgerCount, setLedgerCount] = useState<number | null>(null)
  const [queuedReviews, setQueuedReviews] = useState<number | null>(null)
  const [ledgerError, setLedgerError] = useState<string | null>(null)
  const [erasing, setErasing] = useState<{ importId: string; runIds: string[]; count: number } | null>(null)
  const [busy, setBusy] = useState(false)
  // ids whose details were toggled away from their default (open when they need attention)
  const [toggled, setToggled] = useState<Set<string>>(new Set())
  const running = job?.status === 'running'

  const load = useCallback(() => {
    importsApi
      .list()
      .then((res) => { setImports(res.data.imports); setLoadError(null) })
      .catch((e) => setLoadError(errorText(e, 'Could not load imports.')))
  }, [])
  // on open, and again when an import finishes, so it lists without a reload
  useEffect(load, [load, job?.job_id, running])
  // The list has no ledger count. Read it when the dialog opens, and leave a
  // sentence free for a later count of queued reviews.
  useEffect(() => {
    if (!removing) return
    let live = true
    setLedgerCount(null)
    setQueuedReviews(null)
    setLedgerError(null)
    importsApi.ledgers(removing.import_id).then(
      (res) => {
        if (!live) return
        setLedgerCount(res.data.count)
        setQueuedReviews(res.data.queued_reviews)
      },
      (e) => { if (live) setLedgerError(errorText(e, 'Could not count re-decision ledgers.')) },
    )
    return () => { live = false }
  }, [removing])
  useEffect(() => {
    let cancelled = false
    day1Api.status().then(
      (res) => { if (!cancelled) setReportExists(Boolean(res.data.latest_id)) },
      () => { if (!cancelled) setReportExists(null) },
    )
    return () => { cancelled = true }
  }, [imports])

  const runReport = async () => {
    setRunningReport(true)
    try {
      await day1Api.run()
      setReportExists(true)
      notify('ok', 'Day 1 Report is ready.')
    } catch (err) {
      if (!stepUpDetailOf(err)) notify('err', errorText(err, 'Could not run the Day 1 Report'))
    } finally {
      setRunningReport(false)
    }
  }

  const toggle = (id: string) =>
    setToggled((s) => {
      const next = new Set(s)
      if (!next.delete(id)) next.add(id)
      return next
    })

  const remove = async () => {
    if (!removing) return
    const name = sourceLabel(removing)
    setBusy(true)
    try {
      const res = await importsApi.remove(removing.import_id)
      const kept = res.data.ledgers_kept
      const keptText = kept === 0
        ? 'No re-decision ledgers were kept.'
        : kept === 1
          ? `1 re-decision ledger was kept. An admin with ${res.data.erase_permission} can erase it.`
          : `${kept} re-decision ledgers were kept. An admin with ${res.data.erase_permission} can erase them.`
      notify('ok', `Deleted the ${name} import. ${keptText}`)
      setRemoving(null)
      if (kept > 0 && hasPermission(res.data.erase_permission)) {
        setErasing({ importId: res.data.import_id, runIds: res.data.run_ids, count: kept })
      }
      load()
    } catch (e) {
      notify('err', errorText(e, 'Could not delete the import.'))
    } finally {
      setBusy(false)
    }
  }

  const erase = async () => {
    if (!erasing) return
    setBusy(true)
    try {
      const res = await importsApi.eraseLedgers(erasing.importId, erasing.runIds)
      const n = res.data.erased
      const spared = (res.data.cases ?? []).map((row) => row.case_id)
      const kept = spared.length === 0
        ? ''
        : spared.length === 1
          ? ` Left ${spared[0]}. That run was not erased.`
          : ` Left ${spared.join(', ')}. Those runs were not erased.`
      notify('ok', (n === 1 ? 'Erased 1 re-decision ledger.' : `Erased ${n} re-decision ledgers.`) + kept)
      setErasing(null)
    } catch (e) {
      if (!stepUpDetailOf(e)) notify('err', errorText(e, 'Could not erase the ledgers.'))
    } finally {
      setBusy(false)
    }
  }

  const importConnected = async (src: ConnectedHistorySource, label: string, days: number) => {
    try {
      await start(ingestionApi.importConnectedHistory(src, days))
      notify('ok', `Importing ${label} history into agent memory in the background.`)
    } catch (e) {
      // a 409 names the import that holds the lock
      notify('err', errorText(e, 'Could not start the import.'))
    }
  }

  const connected = configured && configured.length > 0 && (
    <ConnectedHistoryImport
      configured={configured}
      lead={leadConnected}
      running={running}
      attaching={attaching}
      onImport={importConnected}
    />
  )

  return (
    <SettingsCard title={title} desc={desc}>
      {canWrite && (
        <>
          {leadConnected && connected}
          {leadConnected && connected && <span className="data-drop-title data-history-file">From a file</span>}
          <div className="data-history-source">
            <Field label="Source system" hint="Optional. Names the import; left blank, Vigil reads it from the file.">
              <TextInput
                value={source}
                maxLength={64}
                placeholder="e.g. servicenow"
                onChange={(e) => setSource(e.target.value)}
                disabled={running}
              />
            </Field>
          </div>
          <FileDrop
            running={running}
            attaching={attaching}
            testId="history-upload-input"
            onUpload={async (file) => {
              await upload(file, source.trim() || undefined)
              notify('ok', `Importing ${file.name} into agent memory in the background.`)
            }}
            notify={notify}
          />
          {!leadConnected && connected}
          {job && <IngestionJobStatus job={job} wrap />}
        </>
      )}

      {loadError ? (
        <div className="data-history-empty">
          {loadError} <button className="data-link" onClick={load}>Retry</button>
        </div>
      ) : imports === null ? (
        <div className="data-history-empty">Loading…</div>
      ) : imports.length === 0 ? (
        <div className="data-history-empty">No history imported yet.</div>
      ) : (
        <div className="table-wrap data-history-list">
          <table className="tbl data-imports">
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th className="num">Rows read</th>
                <th className="num">Verdicts</th>
                <th className="num">Sightings</th>
                <th>Imported</th>
                {canWrite && <th className="col-act" aria-label="Action" />}
              </tr>
            </thead>
            <tbody>
              {imports.flatMap((row) => {
                const open = needsAttention(row) !== toggled.has(row.import_id)
                const status = IMPORT_STATUS[row.status]
                const name = sourceLabel(row)
                return [
                  <tr key={row.import_id} className={open ? 'open' : undefined}>
                    <td title={row.import_id}>
                      <button
                        className="data-history-toggle"
                        aria-expanded={open}
                        aria-label={`${open ? 'Hide' : 'Show'} details of the ${name} import`}
                        onClick={() => toggle(row.import_id)}
                      >
                        <Icon name={open ? 'chevD' : 'chevR'} size={13} />
                        <b>{name}</b>
                      </button>
                      {row.status === 'done' && (
                        <ReportAgain
                          exists={reportExists}
                          canRun={canRunReport}
                          busy={runningReport}
                          onRun={() => void runReport()}
                        />
                      )}
                    </td>
                    <td><span className={`level-pill ${status.level}`}>{status.text}</span></td>
                    <td className="num">{row.rows_read}</td>
                    <td className="num">{row.verdicts_written}</td>
                    <td className="num">{row.sightings_written}</td>
                    <td className="muted">{utcDayClock(row.imported_at)}</td>
                    {canWrite && (
                      <td className="data-act col-act">
                        <button
                          className="data-link"
                          aria-label={`Delete the ${name} import`}
                          onClick={() => {
                            setLedgerCount(null)
                            setQueuedReviews(null)
                            setLedgerError(null)
                            setRemoving(row)
                          }}
                        >
                          Delete
                        </button>
                      </td>
                    )}
                  </tr>,
                  <tr key={`${row.import_id}-detail`} className="data-expand" hidden={!open}>
                    <td colSpan={canWrite ? 7 : 6}>
                      <ImportDetails
                        row={row}
                        canWrite={canWrite}
                        rerun={canWrite && connectedPull(row) && configured?.some((c) => c.value === row.source_system) && (
                          <button
                            className="data-link data-history-rerun"
                            disabled={running || attaching}
                            aria-label={`Re-run the ${name} import`}
                            onClick={() => importConnected(row.source_system as ConnectedHistorySource, name, row.pull_days!)}
                          >
                            Re-run
                          </button>
                        )}
                      />
                    </td>
                  </tr>,
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      <ConfirmDialog
        open={removing !== null}
        title="Delete import"
        body={deleteImportBody(removing, ledgerCount, ledgerError, queuedReviews)}
        confirmLabel="Delete"
        busy={busy}
        confirmDisabled={ledgerCount === null || ledgerError !== null}
        onConfirm={remove}
        onClose={() => { if (!busy) setRemoving(null) }}
      />
      <ConfirmDialog
        open={erasing !== null}
        title="Erase re-decision ledgers"
        body={erasing ? eraseLedgersBody(erasing.count) : ''}
        confirmLabel="Erase"
        busy={busy}
        onConfirm={erase}
        onClose={() => { if (!busy) setErasing(null) }}
      />
    </SettingsCard>
  )
}

// wrap: a history job's message names what was left out and why, so it is shown whole
function IngestionJobStatus({ job, wrap = false }: { job: IngestionJob; wrap?: boolean }) {
  const running = job.status === 'running'
  const ok = job.status === 'succeeded'
  const pct = job.determinate && job.total > 0
    ? Math.min(100, Math.round((job.processed / job.total) * 100))
    : null
  const message = running
    ? pct !== null ? `${job.processed} of ${job.total} rows (${pct}%)` : `${job.processed} rows so far`
    : job.message
  return (
    <div className={`data-job${wrap ? ' wrap' : ''}`} role="status" aria-live="polite">
      <span className="font-mono data-job-name" title={job.filename}>{job.filename}</span>
      <span className="data-job-msg" title={message}>{message}</span>
      {running ? (
        <span className="level-pill idle"><span className="spin" aria-hidden="true" /> Importing</span>
      ) : ok ? (
        <span className="level-pill good"><Icon name="check2" size={11} /> Done</span>
      ) : (
        <span className="level-pill poor"><Icon name="alert" size={11} /> Failed</span>
      )}
    </div>
  )
}

const AUTH_OPTIONS = [
  { value: 'credentials', label: 'Manual credentials' },
  { value: 'profile', label: 'AWS profile (SSO)' },
]

function S3Form({ notify, s3 }: SectionProps & { s3: ReturnType<typeof useS3> }) {
  const { config, setConfig, phase, error, reload, save } = s3
  const [saving, setSaving] = useState(false)
  const [confirmSave, setConfirmSave] = useState(false)

  const [prefix, setPrefix] = useState('')
  const [files, setFiles] = useState<{ key: string; size: number; last_modified: string }[]>([])
  const [browsing, setBrowsing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [ingesting, setIngesting] = useState(false)
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [results, setResults] = useState<{ key: string; success: boolean; message: string }[]>([])

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
  if (phase === 'error') {
    return (
      <div className="py-6 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load S3 config: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const onSave = async () => {
    if (!config.bucket_name.trim()) { notify('err', 'Bucket name is required.'); return }
    setConfirmSave(true)
  }
  const doSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'S3 configuration saved.')
      setConfirmSave(false)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save S3 config.')
    } finally {
      setSaving(false)
    }
  }

  const browse = async () => {
    setBrowsing(true)
    setResults([])
    try {
      const res = await ingestionApi.listS3Files(prefix)
      setFiles(res.data.files || [])
      setLoaded(true)
      setSelected(new Set())
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to list S3 files.')
    } finally {
      setBrowsing(false)
    }
  }

  const toggleFile = (key: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  const toggleAll = () =>
    setSelected((prev) => (prev.size === files.length ? new Set() : new Set(files.map((f) => f.key))))

  const ingestSelected = async () => {
    const keys = Array.from(selected)
    if (!keys.length) return
    setIngesting(true)
    setProgress({ done: 0, total: keys.length })
    const out: { key: string; success: boolean; message: string }[] = []
    for (const key of keys) {
      try {
        const res = await ingestionApi.ingestS3File(key)
        out.push({ key, success: res.data.success, message: res.data.message })
      } catch (e) {
        out.push({ key, success: false, message: (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Ingestion failed' })
      }
      setProgress((p) => ({ ...p, done: p.done + 1 }))
    }
    setResults(out)
    setIngesting(false)
    setSelected(new Set())
    const ok = out.filter((r) => r.success).length
    notify(ok === keys.length ? 'ok' : 'err', `Ingested ${ok}/${keys.length} file(s).`)
  }

  return (
    <>
      <p className="text-xs text-tx-3 mb-3">AWS S3 bucket for browsing and ingesting supported files.</p>
      <div className="flex flex-col gap-3.5 max-w-[560px]">
        <Field label="Bucket Name" hint="Tip: paste a full s3:// URI to auto-populate the prefix.">
          <TextInput
            value={config.bucket_name}
            placeholder="my-bucket"
            onChange={(e) => {
              const val = e.target.value
              if (val.startsWith('s3://')) {
                const [bucket, ...rest] = val.slice(5).split('/')
                const path = rest.join('/')
                setConfig({ ...config, bucket_name: bucket, parquet_prefix: path ? (path.endsWith('/') ? path : path + '/') : config.parquet_prefix })
              } else {
                setConfig({ ...config, bucket_name: val })
              }
            }}
          />
        </Field>
        <Field label="Region">
          <TextInput value={config.region} onChange={(e) => setConfig({ ...config, region: e.target.value })} />
        </Field>
        <Field label="Authentication">
          <Select value={config.auth_method} options={AUTH_OPTIONS} onSelect={(v) => setConfig({ ...config, auth_method: v })} />
        </Field>
        {config.auth_method === 'profile' ? (
          <Field label="AWS Profile Name" hint="Name in ~/.aws/config. Run `aws sso login --profile <name>` first.">
            <TextInput value={config.aws_profile} placeholder="e.g. my-sso-profile" onChange={(e) => setConfig({ ...config, aws_profile: e.target.value })} />
          </Field>
        ) : (
          <>
            <Field label="Access Key ID">
              <TextInput value={config.access_key_id} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, access_key_id: e.target.value })} />
            </Field>
            <Field label="Secret Access Key">
              <PasswordInput value={config.secret_access_key} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, secret_access_key: e.target.value })} />
            </Field>
            <Field label="Session Token (optional)" hint="Required for temporary AWS STS credentials (keys starting with ASIA).">
              <PasswordInput value={config.session_token} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, session_token: e.target.value })} />
            </Field>
          </>
        )}
        <Field label="Default Path / Prefix" hint="S3 key prefix used as the default when browsing files.">
          <TextInput value={config.parquet_prefix} placeholder="e.g. lake/v1/embeddings/" onChange={(e) => setConfig({ ...config, parquet_prefix: e.target.value })} />
        </Field>
      </div>

      <SaveRow saving={saving} onSave={onSave} />

      {/* Browse & ingest */}
      <div className="mt-5 pt-5 border-t border-line-soft">
        <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2.5">Browse &amp; Ingest</h4>
        <div className="flex gap-2 items-end max-w-[560px]">
          <div className="flex-1">
            <Field label="Prefix">
              <TextInput value={prefix} placeholder={config.parquet_prefix || 'lake/v1/'} onChange={(e) => setPrefix(e.target.value)} />
            </Field>
          </div>
          <button className="btn ghost" onClick={browse} disabled={browsing}>
            <Icon name="search" /> {browsing ? 'Browsing…' : 'Browse'}
          </button>
        </div>

        {loaded && (
          <div className="mt-3">
            {files.length === 0 ? (
              <div className="text-sm text-tx-3 py-3">No files found under that prefix.</div>
            ) : (
              <>
                <div className="table-wrap">
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th style={{ width: 36 }}>
                          <input type="checkbox" checked={selected.size === files.length && files.length > 0} onChange={toggleAll} />
                        </th>
                        <th>Key</th><th>Size</th><th>Last Modified</th>
                      </tr>
                    </thead>
                    <tbody>
                      {files.map((f) => (
                        <tr key={f.key}>
                          <td><input type="checkbox" checked={selected.has(f.key)} onChange={() => toggleFile(f.key)} /></td>
                          <td className="font-mono text-xs">{f.key}</td>
                          <td className="muted">{formatFileSize(f.size)}</td>
                          <td className="muted">{f.last_modified ? new Date(f.last_modified).toLocaleString() : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center gap-3 mt-2.5">
                  <button className="btn primary" disabled={selected.size === 0 || ingesting} onClick={ingestSelected}>
                    <Icon name="upload" /> {ingesting ? `Ingesting ${progress.done}/${progress.total}…` : `Ingest selected (${selected.size})`}
                  </button>
                </div>
              </>
            )}
            {results.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-3">
                {results.map((r) => (
                  <div key={r.key} className={`settings-banner ${r.success ? 'ok' : 'err'}`}>
                    <Icon name={r.success ? 'check2' : 'alert'} size={13} />
                    <span className="text-xs"><span className="font-mono">{r.key}</span> — {r.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmSave}
        danger={false}
        title="Save S3 Configuration"
        body="Save these S3 settings? Credentials are stored in the encrypted secrets store."
        confirmLabel="Save"
        busy={saving}
        onConfirm={doSave}
        onClose={() => setConfirmSave(false)}
      />
    </>
  )
}

const OFFSET_RESETS = [
  { value: 'latest', label: 'latest' },
  { value: 'earliest', label: 'earliest' },
]
const SECURITY_PROTOCOLS = ['PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL'].map((p) => ({ value: p, label: p }))
const SASL_MECHANISMS = [
  { value: '', label: '(none)' },
  { value: 'PLAIN', label: 'PLAIN' },
  { value: 'SCRAM-SHA-256', label: 'SCRAM-SHA-256' },
  { value: 'SCRAM-SHA-512', label: 'SCRAM-SHA-512' },
]

function LoadError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div className="py-6 text-center flex flex-col items-center gap-2.5">
      <span className="text-sm text-tx-3">Couldn’t load {what}.</span>
      <button className="btn ghost" onClick={onRetry}>Retry</button>
    </div>
  )
}

function KafkaForm({ notify, kafka }: SectionProps & { kafka: ReturnType<typeof useKafka> }) {
  const { config, setConfig, stats, daemonReachable, phase, reload, save } = kafka
  const [saving, setSaving] = useState(false)
  const [topicInput, setTopicInput] = useState('')

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
  if (phase === 'error') return <LoadError what="the Kafka settings" onRetry={reload} />

  const set = (patch: Partial<KafkaConfig>) => setConfig({ ...config, ...patch })

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'Kafka settings saved.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to save Kafka settings.')
    } finally {
      setSaving(false)
    }
  }

  const addTopic = () => {
    const t = topicInput.trim()
    if (!t || config.topics.includes(t)) { setTopicInput(''); return }
    set({ topics: [...config.topics, t] })
    setTopicInput('')
  }

  const statusKind = stats?.connected ? 'ok' : config.enabled ? 'info' : 'info'
  const statusText = stats?.connected ? 'CONNECTED' : config.enabled ? 'ENABLED (not yet connected)' : 'DISABLED'

  return (
    <>
      <p className="text-xs text-tx-3 mb-3">Stream JSON-encoded finding objects from Kafka topics. SASL password and SSL CA path must be set via env vars (KAFKA_SASL_PASSWORD, KAFKA_SSL_CA_LOCATION).</p>
      {!daemonReachable && (
        <div className="settings-banner info mb-3"><Icon name="info" size={14} /> Daemon health endpoint unreachable — live stats unavailable. Changes apply once the daemon reads the updated config.</div>
      )}
      <div className={`settings-banner ${statusKind} mb-4`}>
        <Icon name="info" size={14} />
        <span>
          Consumer is <strong>{statusText}</strong>
          {stats && ` — ${stats.messages_consumed} consumed, ${stats.messages_enqueued} enqueued, ${stats.duplicates_skipped} dupes skipped`}
          {stats?.last_error && ` · last error: ${stats.last_error}`}
        </span>
      </div>

      <ToggleRow
        label="Enable Kafka consumer"
        hint="The daemon re-reads this flag every few seconds — no restart needed."
        checked={config.enabled}
        onChange={(v) => set({ enabled: v })}
      />

      <div className="settings-grid-2 mt-4">
        <Field label="Bootstrap servers" hint="Comma-separated host:port list">
          <TextInput value={config.bootstrap_servers} onChange={(e) => set({ bootstrap_servers: e.target.value })} />
        </Field>
        <Field label="Consumer group">
          <TextInput value={config.consumer_group} onChange={(e) => set({ consumer_group: e.target.value })} />
        </Field>
        <Field label="Auto offset reset">
          <Select value={config.auto_offset_reset} options={OFFSET_RESETS} onSelect={(v) => set({ auto_offset_reset: v })} />
        </Field>
        <Field label="Security protocol">
          <Select value={config.security_protocol} options={SECURITY_PROTOCOLS} onSelect={(v) => set({ security_protocol: v })} />
        </Field>
      </div>

      <div className="mt-4">
        <span className="text-[13px] text-tx-2">Topics</span>
        <div className="flex gap-2 items-center mt-2 max-w-[420px]">
          <TextInput
            value={topicInput}
            placeholder="Add topic"
            onChange={(e) => setTopicInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTopic() } }}
          />
          <button className="btn ghost" onClick={addTopic}><Icon name="plus" /> Add</button>
        </div>
        <div className="flex gap-2 flex-wrap mt-2">
          {config.topics.length === 0 ? (
            <span className="text-xs text-tx-3">No topics — the consumer won’t start until you add at least one.</span>
          ) : (
            config.topics.map((t) => (
              <span key={t} className="chip">
                {t}
                <button className="dd clear" aria-label={`Remove ${t}`} onClick={() => set({ topics: config.topics.filter((x) => x !== t) })}>
                  <Icon name="close" size={11} />
                </button>
              </span>
            ))
          )}
        </div>
      </div>

      <div className="settings-grid-2 mt-4">
        <Field label="SASL mechanism">
          <Select value={config.sasl_mechanism ?? ''} options={SASL_MECHANISMS} onSelect={(v) => set({ sasl_mechanism: v || null })} />
        </Field>
        <Field label="SASL username">
          <TextInput value={config.sasl_username ?? ''} onChange={(e) => set({ sasl_username: e.target.value || null })} />
        </Field>
        <Field label="Max poll records">
          <NumberInput value={config.max_poll_records} onChange={(e) => set({ max_poll_records: Number(e.target.value) })} />
        </Field>
        <Field label="Session timeout (ms)">
          <NumberInput value={config.session_timeout_ms} onChange={(e) => set({ session_timeout_ms: Number(e.target.value) })} />
        </Field>
      </div>
      <SaveRow saving={saving} onSave={handleSave} />
    </>
  )
}

function DarktraceForm({ notify, darktrace }: SectionProps & { darktrace: ReturnType<typeof useDarktrace> }) {
  const { config, setConfig, phase, reload, save } = darktrace
  const [saving, setSaving] = useState(false)

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
  if (phase === 'error') return <LoadError what="the Darktrace settings" onRetry={reload} />

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'Darktrace config saved.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to save Darktrace config.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <p className="text-xs text-tx-3 mb-3">Webhook receiver for Darktrace Model Breach, AI Analyst, and System Status alerts.</p>
      <ToggleRow
        label="Enable webhook receiver"
        checked={config.enabled}
        onChange={(v) => setConfig({ ...config, enabled: v })}
      />
      <div className="flex flex-col gap-3.5 max-w-[560px] mt-2">
        <Field label="Webhook secret" hint={config.configured ? 'A secret is stored. Leave blank to keep it.' : 'Shared secret Darktrace signs webhook payloads with.'}>
          <PasswordInput value={config.webhook_secret} onChange={(e) => setConfig({ ...config, webhook_secret: e.target.value })} />
        </Field>
        <Field label="Darktrace Console URL" hint="Used to build deep links back into the Darktrace console.">
          <TextInput value={config.url} placeholder="https://your-instance.darktrace.com" onChange={(e) => setConfig({ ...config, url: e.target.value })} />
        </Field>
        <Field label="Max body size (KB)" hint="Reject payloads larger than this.">
          <NumberInput value={config.max_body_kb} min={1} max={16384} onChange={(e) => setConfig({ ...config, max_body_kb: Math.max(1, Number(e.target.value) || 1024) })} />
        </Field>
      </div>
      <SaveRow saving={saving} onSave={handleSave} />
    </>
  )
}

function SaveRow({ saving, onSave }: { saving: boolean; onSave: () => void }) {
  return (
    <div className="mt-4">
      <button className="btn primary" onClick={onSave} disabled={saving}>
        <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )
}

type StreamKey = 's3' | 'kafka' | 'darktrace'
type StreamStatus = { level: 'good' | 'fair' | 'poor' | null; text: string }

const OFF: StreamStatus = { level: null, text: 'Off' }

/** One row of the Streams and buckets table; `status.level` null renders as plain text. */
interface StreamRow {
  key: StreamKey
  name: string
  settings: string
  status: StreamStatus
  action: string
  /** reloads this row's config; the action when the read failed */
  retry: () => void
  form: ReactNode
}

function StatusCell({ status }: { status: StreamStatus }) {
  if (status.level === 'good') return <LevelBadge level="good" variant="pill" />
  if (!status.level) return <span className="muted">{status.text}</span>
  return <span className={`level-pill ${status.level}`}>{status.text}</span>
}

export function StreamsCard({ notify }: SectionProps) {
  const s3 = useS3()
  const kafka = useKafka()
  const darktrace = useDarktrace()
  const [open, setOpen] = useState<StreamKey | null>(null)

  const s3c = s3.config
  const kc = kafka.config
  const dc = darktrace.config
  const kafkaSetUp = kc.enabled || kc.topics.length > 0
  // a failed read says nothing about what is set up, so it must not read as "Not set up"
  const failed = { s3: s3.phase === 'error', kafka: kafka.phase === 'error', darktrace: darktrace.phase === 'error' }
  const LOAD_FAILED = 'Couldn’t load'

  const s3Status: StreamStatus =
    s3.phase === 'loading' ? { level: null, text: 'Loading…' }
    : s3.phase === 'error' ? { level: 'poor', text: 'Unavailable' }
    : s3c.configured ? { level: 'good', text: 'Good' }
    : OFF
  const kafkaStatus: StreamStatus =
    kafka.phase === 'loading' ? { level: null, text: 'Loading…' }
    : kafka.phase === 'error' ? { level: 'poor', text: 'Unavailable' }
    : kafka.stats?.connected ? { level: 'good', text: 'Good' }
    : kc.enabled ? { level: 'fair', text: 'Not connected' }
    : OFF
  const darktraceStatus: StreamStatus =
    darktrace.phase === 'loading' ? { level: null, text: 'Loading…' }
    : darktrace.phase === 'error' ? { level: 'poor', text: 'Unavailable' }
    : dc.enabled && dc.configured ? { level: 'good', text: 'Good' }
    : dc.enabled ? { level: 'fair', text: 'No secret' }
    : OFF

  const rows: StreamRow[] = [
    {
      key: 's3',
      name: 'Amazon S3',
      settings: failed.s3 ? LOAD_FAILED : s3c.configured
        ? [s3c.bucket_name, s3c.region, s3c.auth_method === 'profile' ? 'AWS profile (SSO)' : 'Access keys'].filter(Boolean).join(' · ')
        : 'Not set up',
      status: s3Status,
      action: failed.s3 ? 'Retry' : s3c.configured ? 'Browse' : 'Set up',
      retry: s3.reload,
      form: <S3Form notify={notify} s3={s3} />,
    },
    {
      key: 'kafka',
      name: 'Kafka',
      settings: failed.kafka ? LOAD_FAILED : kafkaSetUp
        ? [kc.bootstrap_servers, kc.topics.length ? `topics ${kc.topics.join(', ')}` : 'no topics', kc.security_protocol].join(' · ')
        : 'Not set up',
      status: kafkaStatus,
      action: failed.kafka ? 'Retry' : kafkaSetUp ? 'Edit' : 'Set up',
      retry: kafka.reload,
      form: <KafkaForm notify={notify} kafka={kafka} />,
    },
    {
      key: 'darktrace',
      name: 'Darktrace webhook',
      settings: failed.darktrace ? LOAD_FAILED : dc.configured ? dc.url || 'Webhook secret saved' : 'Not set up',
      status: darktraceStatus,
      action: failed.darktrace ? 'Retry' : dc.configured ? 'Edit' : 'Set up',
      retry: darktrace.reload,
      form: <DarktraceForm notify={notify} darktrace={darktrace} />,
    },
  ]

  return (
    <SettingsCard title="Streams and buckets" desc="Read findings continuously from storage or a message bus.">
      <div className="table-wrap">
        <table className="tbl data-table">
          <thead>
            <tr>
              <th>
                Source
                <span className="col-low-note">Settings are hidden while Ask Vigil is open</span>
              </th>
              <th className="col-low">Settings</th>
              <th>Status</th>
              <th className="col-act" aria-label="Action" />
            </tr>
          </thead>
          <tbody>
            {rows.flatMap((r) => [
              <tr key={r.key}>
                <td><b>{r.name}</b></td>
                <td className="col-low" title={r.settings}>{r.settings}</td>
                <td><StatusCell status={r.status} /></td>
                <td className="data-act col-act">
                  <button
                    className="data-link"
                    aria-expanded={r.action === 'Retry' ? undefined : open === r.key}
                    aria-label={`${open === r.key ? 'Close' : r.action} ${r.name}`}
                    onClick={() => (r.action === 'Retry' ? r.retry() : setOpen(open === r.key ? null : r.key))}
                  >
                    {open === r.key ? 'Close' : r.action}
                  </button>
                </td>
              </tr>,
              // stays mounted while closed, so a browse or ingest in flight survives a collapse
              <tr key={`${r.key}-form`} className="data-expand" hidden={open !== r.key}>
                <td colSpan={4}>{r.form}</td>
              </tr>,
            ])}
          </tbody>
        </table>
      </div>
    </SettingsCard>
  )
}
