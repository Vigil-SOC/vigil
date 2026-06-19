/* ============================================================
   Settings · Federation — pull findings from external SIEM/EDR
   sources on a cadence and feed the auto-investigator. Mirrors
   FederationTab.tsx. Auto-refreshes every 10s via useFederation.
   ============================================================ */
import { Icon } from '../../shared/icons'
import { Select, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import { useFederation } from './useSettings'
import type { SectionProps } from './types'

const SEVERITY_OPTIONS = [
  { value: '', label: 'Any' },
  { value: 'low', label: 'Low+' },
  { value: 'medium', label: 'Medium+' },
  { value: 'high', label: 'High+' },
  { value: 'critical', label: 'Critical only' },
]

const SOURCE_LABELS: Record<string, string> = {
  splunk: 'Splunk',
  crowdstrike: 'CrowdStrike Falcon',
  azure_sentinel: 'Azure Sentinel',
  aws_security_hub: 'AWS Security Hub',
  microsoft_defender: 'Microsoft Defender',
  elastic: 'Elastic Security',
}

function formatRelative(iso: string | null): string {
  if (!iso) return 'never'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return 'never'
  const sec = Math.floor((Date.now() - t) / 1000)
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

export default function FederationSection({ notify }: SectionProps) {
  const {
    sources,
    globalEnabled,
    phase,
    error,
    reload,
    setGlobal,
    patchSource,
    editSourceLocal,
    pollNow,
  } = useFederation()

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading federated monitoring…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load federation sources: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const enabledCount = sources.filter((s) => s.enabled).length
  const errorCount = sources.filter((s) => (s.consecutive_errors || 0) > 0).length

  const onToggleGlobal = async () => {
    try {
      await setGlobal(!globalEnabled)
      notify('ok', `Federated monitoring ${!globalEnabled ? 'enabled' : 'disabled'}.`)
    } catch {
      notify('err', 'Failed to update global setting.')
    }
  }

  const onPatch = async (id: string, patch: Parameters<typeof patchSource>[1]) => {
    try {
      await patchSource(id, patch)
    } catch {
      notify('err', `Failed to update ${id}.`)
    }
  }

  const onPollNow = async (id: string) => {
    try {
      await pollNow(id)
      notify('ok', `Triggered poll for ${id}.`)
    } catch {
      notify('err', `Failed to trigger poll for ${id}.`)
    }
  }

  return (
    <SettingsCard
      wide
      title="Federated Monitoring"
      desc="Pull findings from external SIEM/EDR sources on a configurable cadence and feed them into the auto-investigator. First run on enable starts from now — no historical backfill."
      actions={
        <>
          <span className="chip">{enabledCount}/{sources.length} enabled</span>
          {errorCount > 0 && (
            <span className="chip" style={{ color: 'var(--high)' }}>{errorCount} with errors</span>
          )}
          <button className="btn ghost" onClick={reload}>
            <Icon name="refresh" /> Refresh
          </button>
        </>
      }
    >
      <div className="toggle-row" style={{ paddingTop: 0 }}>
        <div className="toggle-row-text">
          <span className="toggle-row-label">
            Federated monitoring is {globalEnabled ? 'ON' : 'OFF'}
          </span>
          <span className="toggle-row-hint">
            Master switch — disables all polling. Per-source rows can still be configured while off.
          </span>
        </div>
        <Toggle checked={globalEnabled} onChange={onToggleGlobal} />
      </div>

      <div className="table-wrap mt-3">
        <table className="tbl">
          <thead>
            <tr>
              <th>Source</th>
              <th>Enabled</th>
              <th>Interval (s)</th>
              <th>Min severity</th>
              <th>Last success</th>
              <th>Errors</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sources.length === 0 && (
              <tr>
                <td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                  No federation sources available. Configure an integration (Splunk, CrowdStrike,
                  Sentinel, etc.) under Integrations first — adapters auto-seed when the daemon starts.
                </td>
              </tr>
            )}
            {sources.map((s) => (
              <tr key={s.source_id}>
                <td>
                  <div className="flex flex-col">
                    <span>{SOURCE_LABELS[s.source_id] || s.source_id}</span>
                    <span className="text-xs text-tx-3">
                      {s.source_id}
                      {!s.is_configured && ' · not configured'}
                    </span>
                  </div>
                </td>
                <td>
                  <Toggle
                    checked={s.enabled}
                    disabled={!s.is_configured}
                    onChange={(v) => onPatch(s.source_id, { enabled: v })}
                  />
                </td>
                <td>
                  <TextInput
                    type="number"
                    className="!w-24"
                    value={s.interval_seconds}
                    min={10}
                    max={86400}
                    onChange={(e) =>
                      editSourceLocal(s.source_id, { interval_seconds: Number(e.target.value) })
                    }
                    onBlur={() => onPatch(s.source_id, { interval_seconds: s.interval_seconds })}
                  />
                </td>
                <td>
                  <div className="w-28">
                    <Select
                      value={s.min_severity || ''}
                      options={SEVERITY_OPTIONS}
                      onSelect={(v) => onPatch(s.source_id, { min_severity: v || null })}
                    />
                  </div>
                </td>
                <td className="muted">{formatRelative(s.last_success_at)}</td>
                <td>
                  {(s.consecutive_errors || 0) > 0 ? (
                    <span className="chip" style={{ color: 'var(--high)' }} title={s.last_error || ''}>
                      {s.consecutive_errors}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td style={{ textAlign: 'right' }}>
                  <button
                    className="btn ghost"
                    disabled={!s.is_configured}
                    onClick={() => onPollNow(s.source_id)}
                    title={s.is_configured ? 'Trigger an immediate poll' : 'Configure the integration first'}
                  >
                    <Icon name="play" /> Poll now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </SettingsCard>
  )
}
