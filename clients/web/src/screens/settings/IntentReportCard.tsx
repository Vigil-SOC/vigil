// INTENT.md is edited where it lives; this card shows the report and, for a
// person with ai_autonomy.write, accepts a declared directory's pending loosenings.
import { useEffect, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { configApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { errorText } from '../../shared/errorText'
import { ConfirmDialog, SettingsCard } from '../../shared/ui'
import type { SectionProps } from './types'

type IntentReport = Schema<'IntentReportResponse'>
type IntentRow = Schema<'IntentDiffRow'>

const isFlow = (row: IntentRow) => row.key.startsWith('workflows.')

/** A workflow entry as the console says it: "Act · floor 85 · bar 0.60", "Assist · bar 0.60", "Follows fleet". */
function showFlow(value: unknown): string {
  if (value === 'fleet') return 'Follows fleet'
  if (value == null || typeof value !== 'object') return '—'
  const entry = value as { tier?: string; floor?: number | null; bar?: number | null }
  const bar = typeof entry.bar === 'number' ? ` · bar ${entry.bar.toFixed(2)}` : ''
  if (entry.tier === 'act') return `Act · floor ${entry.floor ?? '?'}${bar}`
  return entry.tier === 'assist' ? `Assist${bar}` : JSON.stringify(value)
}

function show(value: unknown): string {
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map((item) => String(item)).join(', ')
  return JSON.stringify(value)
}

function Status({ row }: { row: IntentRow }) {
  const chips: [string, string, string][] = []
  if (row.pending) chips.push(['Pending', 'var(--fair)', 'A loosening waits for a person to accept it'])
  if (row.drift)
    chips.push(
      row.drift_reason
        ? [`Drift · ${row.drift_reason}`, 'var(--fair)', 'Narrower than declared; a raise to the declared value resets it']
        : ['Drift', 'var(--fair)', 'Narrower than declared, here or by an env lock'],
    )
  if (row.env_ignored)
    chips.push([`Env ${show(row.env_value)} ignored`, 'var(--tx2)', 'The declared value outranks the environment'])
  if (!chips.length) return <span className="text-tx-3">{row.managed ? 'Managed' : '—'}</span>
  return (
    <span className="flex flex-wrap gap-1">
      {chips.map(([text, color, title]) => (
        <span key={text} className="chip" style={{ color }} title={title}>
          {text}
        </span>
      ))}
    </span>
  )
}

export default function IntentReportCard({
  reloadKey = 0,
  notify,
  onAccepted,
  workflowNames = {},
}: {
  reloadKey?: number
  notify?: SectionProps['notify']
  onAccepted?: () => void
  /** Workflow id → name, for the workflows.<id> rows. */
  workflowNames?: Record<string, string>
}) {
  const [report, setReport] = useState<IntentReport | null>(null)
  const [failed, setFailed] = useState(false)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const { hasPermission } = useAuth()
  const canAccept = hasPermission('ai_autonomy.write')

  useEffect(() => {
    let cancelled = false
    configApi
      .getIntent()
      .then((res) => {
        if (cancelled) return
        setReport(res.data)
        setFailed(false)
      })
      .catch(() => {
        if (cancelled) return
        setReport(null)
        setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  if (failed) {
    return (
      <SettingsCard title="Declared intent" wide>
        <p className="text-sm text-tx-2">Could not load declared intent.</p>
      </SettingsCard>
    )
  }

  if (!report) {
    return (
      <SettingsCard title="Declared intent" wide>
        <p className="text-sm text-tx-3">Loading declared intent…</p>
      </SettingsCard>
    )
  }

  if (!report.readable) {
    return (
      <SettingsCard title="Declared intent" wide>
        <p className="text-sm text-tx-2">
          {report.path ? `Not read (${report.path}).` : 'Not read.'}
        </p>
      </SettingsCard>
    )
  }

  const managed = !!report.directory
  const pending = report.pending ?? []
  const pendingRows = report.rows?.filter((r) => r.pending) ?? []
  const skipped = report.skipped ?? []
  const value = (row: IntentRow, v: unknown) => (isFlow(row) ? showFlow(v) : show(v))
  const onDisk = report.revision_on_disk
  const accept = () => {
    if (!report.revision) return
    setBusy(true)
    configApi
      .acceptIntent({ revision: report.revision, hash: report.hash ?? null })
      .then((res) => {
        setReport(res.data)
        notify?.('ok', `Accepted. Revision ${report.revision} now applies in full.`)
        onAccepted?.()
      })
      .catch((e) => notify?.('err', errorText(e, 'The pending keys were not accepted.')))
      .finally(() => {
        setBusy(false)
        setAsking(false)
      })
  }

  return (
    <SettingsCard title="Declared intent" wide>
      {managed ? (
        <div className="text-xs text-tx-3 mb-3 flex flex-col gap-1 [overflow-wrap:anywhere]">
          <div>
            Managed by <span className="mono">{report.directory}</span> at revision{' '}
            <span className="mono">{report.revision ?? 'none'}</span>
            {onDisk && onDisk !== report.revision && (
              <>
                {' '}(on disk: <span className="mono">{onDisk}</span>)
              </>
            )}
            {' · '}
            <span style={{ color: report.in_sync ? 'var(--good)' : 'var(--fair)' }}>
              {report.in_sync ? 'in sync' : 'not in sync'}
            </span>
            . Change it there, not here: a local change may only narrow a managed key.
          </div>
          <div>{report.verification}</div>
          {report.state !== 'ok' && report.reason && <div style={{ color: 'var(--fair)' }}>{report.reason}</div>}
        </div>
      ) : (
        <p className="text-xs text-tx-3 mb-3">{report.path} is edited in git, not here.</p>
      )}
      {pending.length > 0 && (
        <div className="flex flex-wrap items-center gap-2.5 mb-3">
          <span className="text-sm text-tx-2">
            {pending.length === 1 ? 'A loosening waits' : `${pending.length} loosenings wait`} for a person:{' '}
            <span className="mono">{pending.join(', ')}</span>
          </span>
          {canAccept && (
            <button className="btn" onClick={() => setAsking(true)}>
              Accept
            </button>
          )}
        </div>
      )}
      {skipped.length > 0 && (
        <div className="text-xs text-tx-2 mb-3 [overflow-wrap:anywhere]">
          <span style={{ color: 'var(--fair)' }}>Not applied here:</span>
          <ul className="mt-1">
            {skipped.map((s) => (
              <li key={s.id}>
                <span className="mono">workflows.{s.id}</span> (line {s.line ?? '?'}): {s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Key</th>
              <th>Declared</th>
              <th>Effective</th>
              <th>Source</th>
              <th>Label</th>
              {managed && <th>Status</th>}
            </tr>
          </thead>
          <tbody>
            {report.rows?.map((row) => (
              <tr key={row.key}>
                <td>
                  {isFlow(row) && workflowNames[row.key.slice('workflows.'.length)] && (
                    <span className="block">{workflowNames[row.key.slice('workflows.'.length)]}</span>
                  )}
                  <span className="mono">{row.key}</span>
                  {row.managed && (
                    <span className="block text-xs text-tx-3 mono">
                      {row.file}:{row.line} @ {row.revision ?? 'unknown'}
                    </span>
                  )}
                </td>
                <td className="mono">{value(row, row.declared)}</td>
                <td className="mono">{value(row, row.effective)}</td>
                <td>{row.source}</td>
                <td>{row.label}</td>
                {managed && (
                  <td>
                    <Status row={row} />
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ConfirmDialog
        open={asking}
        title={`Accept revision ${report.revision ?? ''}?`}
        body={
          <>
            <p className="mb-2">These keys take their declared value and autonomy widens:</p>
            <ul className="mono text-xs mb-2">
              {pendingRows.map((r) => (
                <li key={r.key}>
                  {r.key}: {value(r, r.effective)} → {value(r, r.declared)}
                </li>
              ))}
            </ul>
            {pendingRows.some(isFlow) && (
              <p className="mb-2">A workflow put on Act stores the investigation model in force and its 30-day record.</p>
            )}
            <p>Your name and the revision go on the audit trail.</p>
          </>
        }
        confirmLabel="Accept"
        busy={busy}
        onConfirm={accept}
        onClose={() => setAsking(false)}
      />
    </SettingsCard>
  )
}
