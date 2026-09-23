// Read-only. INTENT.md is edited in git; this card only shows the report.
import { useEffect, useState } from 'react'
import { configApi } from '../../services/api'
import { SettingsCard } from '../../shared/ui'

interface IntentRow {
  key: string
  declared: unknown
  effective: unknown
  source: string
  label: string
}

interface IntentReport {
  path: string
  readable: boolean
  rows: IntentRow[]
}

function show(value: unknown): string {
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map((item) => String(item)).join(', ')
  return JSON.stringify(value)
}

export default function IntentReportCard({ reloadKey = 0 }: { reloadKey?: number }) {
  const [report, setReport] = useState<IntentReport | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    configApi
      .getIntent()
      .then((res) => {
        if (cancelled) return
        setReport(res.data as IntentReport)
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

  return (
    <SettingsCard title="Declared intent" wide>
      <p className="text-xs text-tx-3 mb-3">{report.path} is edited in git, not here.</p>
      <table className="tbl">
        <thead>
          <tr>
            <th>Key</th>
            <th>Declared</th>
            <th>Effective</th>
            <th>Source</th>
            <th>Label</th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => (
            <tr key={row.key}>
              <td className="mono">{row.key}</td>
              <td className="mono">{show(row.declared)}</td>
              <td className="mono">{show(row.effective)}</td>
              <td>{row.source}</td>
              <td>{row.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </SettingsCard>
  )
}
