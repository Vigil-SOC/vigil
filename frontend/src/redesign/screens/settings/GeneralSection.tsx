/* ============================================================
   Settings · General — auto-sync / notifications / keyring toggles,
   destructive data cleanup controls, and always-on Mempalace health.
   Mirrors the legacy Settings.tsx "general" tab. (Cost Analytics
   embed from the legacy tab is deferred — see REDESIGN_GAPS.md.)
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, SettingsCard, ToggleRow } from '../../shared/ui'
import { casesApi, findingsApi, mcpApi, orchestratorApi } from '../../../services/api'
import { notificationService } from '../../../services/notifications'
import { useGeneralSettings, useMempalaceHealth } from './useSettings'
import CostAnalyticsCard from './CostAnalyticsCard'
import type { SectionProps } from './types'

type ClearAction = 'findings' | 'investigations' | 'cases' | 'workspace'

export default function GeneralSection({ notify }: SectionProps) {
  const { config, setConfig, phase, error, reload, save } = useGeneralSettings()
  const { health, loading: healthLoading, reload: reloadHealth } = useMempalaceHealth()

  const [saving, setSaving] = useState(false)
  const [clearAction, setClearAction] = useState<ClearAction | null>(null)
  const [clearing, setClearing] = useState(false)
  const [testing, setTesting] = useState(false)

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading general settings…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load general settings: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'General settings saved.')
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save general settings.')
    } finally {
      setSaving(false)
    }
  }

  const handleNotificationToggle = async (next: boolean) => {
    if (next) {
      const granted = await notificationService.requestPermission()
      if (!granted) {
        notify('err', 'Browser denied notification permission.')
        return
      }
    }
    // keep the shared service in sync so show()-gating takes effect immediately
    // (the findings poll that fires them starts on next load — see
    // useDesktopNotifications)
    notificationService.setEnabled(next)
    setConfig({ ...config, show_notifications: next })
  }

  const handleClear = async () => {
    if (!clearAction) return
    setClearing(true)
    try {
      if (clearAction === 'findings') {
        const res = await findingsApi.deleteAll()
        const data = res.data as { deleted?: number; deleted_count?: number }
        const count = data.deleted ?? data.deleted_count
        notify('ok', count != null ? `Cleared ${count} findings.` : 'All findings cleared.')
      } else if (clearAction === 'investigations') {
        const res = await orchestratorApi.purgeAll()
        const count = (res.data as { deleted?: number })?.deleted
        notify('ok', count != null ? `Cleared ${count} investigations.` : 'All investigations cleared.')
      } else if (clearAction === 'cases') {
        const res = await casesApi.deleteAll()
        const count = (res.data as { deleted?: number })?.deleted
        notify('ok', count != null ? `Cleared ${count} cases and case metrics.` : 'Cases and metrics cleared.')
      } else {
        const failed: string[] = []
        for (const [label, clear] of [
          ['investigations', () => orchestratorApi.purgeAll()],
          ['cases', () => casesApi.deleteAll()],
          ['findings', () => findingsApi.deleteAll()],
        ] as const) {
          try {
            await clear()
          } catch {
            failed.push(label)
          }
        }
        if (failed.length) {
          notify('err', `Workspace cleanup completed, but failed to clear ${failed.join(', ')}.`)
        } else {
          notify('ok', 'Workspace data cleared.')
        }
      }
      setClearAction(null)
      reloadHealth()
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to clear data.')
    } finally {
      setClearing(false)
    }
  }

  const handleTestMempalace = async () => {
    setTesting(true)
    try {
      await mcpApi.testServer('mempalace')
      notify('ok', 'Mempalace connection OK.')
      reloadHealth()
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Mempalace connection failed.')
    } finally {
      setTesting(false)
    }
  }

  const dot = health?.connected ? 'var(--ok)' : health ? 'var(--crit)' : 'var(--tx-faint)'

  return (
    <>
      <SettingsCard
        title="General"
        desc="Startup and notification preferences."
        actions={
          <button className="btn primary" onClick={handleSave} disabled={saving}>
            <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
          </button>
        }
      >
        <ToggleRow
          label="Auto-sync on start"
          hint="Pull from configured sources when the app launches."
          checked={config.auto_start_sync}
          onChange={(v) => setConfig({ ...config, auto_start_sync: v })}
        />
        <ToggleRow
          label="Desktop notifications"
          hint="Browser notifications for new findings and case updates."
          checked={config.show_notifications}
          onChange={handleNotificationToggle}
        />
        <ToggleRow
          label="Use OS Keyring"
          hint="Store secrets in the operating system keyring when available."
          checked={config.enable_keyring}
          onChange={(v) => setConfig({ ...config, enable_keyring: v })}
        />
      </SettingsCard>

      <SettingsCard
        title="Data Management"
        desc="Clear generated findings, investigations, cases, and metrics while preserving configuration."
      >
        <div className="flex gap-2 flex-wrap">
          <button className="btn danger" onClick={() => setClearAction('findings')} disabled={clearing}>
            <Icon name="trash" /> Clear Findings
          </button>
          <button className="btn danger" onClick={() => setClearAction('investigations')} disabled={clearing}>
            <Icon name="trash" /> Clear Investigations
          </button>
          <button className="btn danger" onClick={() => setClearAction('cases')} disabled={clearing}>
            <Icon name="trash" /> Clear Cases &amp; Metrics
          </button>
          <button className="btn danger" onClick={() => setClearAction('workspace')} disabled={clearing}>
            <Icon name="trash" /> Clear Workspace Data
          </button>
        </div>
      </SettingsCard>

      <CostAnalyticsCard />

      <SettingsCard
        title={
          <span className="inline-flex items-center gap-2">
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: dot }} />
            Mempalace Health
          </span>
        }
        desc="Persistent memory store used by every agent. Always on — not toggleable from Integrations."
        actions={
          <>
            <button className="btn ghost" onClick={reloadHealth} disabled={healthLoading}>
              <Icon name="refresh" /> Refresh
            </button>
            <button className="btn ghost" onClick={handleTestMempalace} disabled={testing}>
              {testing ? 'Testing…' : 'Test connection'}
            </button>
          </>
        }
      >
        {health ? (
          <div className="kv-grid" style={{ gridTemplateColumns: '180px 1fr' }}>
            <span className="k">Status</span>
            <span className="v">
              <span className={`status ${health.connected ? 'closed' : 'open'}`}>
                {health.connected ? 'Connected' : 'Disconnected'}
              </span>
              {health.error && <span className="text-crit ml-2">{health.error}</span>}
            </span>
            <span className="k">Palace path</span>
            <span className="v font-mono break-all">
              {health.palace_path}
              {!health.palace_exists && <span className="text-crit ml-1">(missing)</span>}
            </span>
            <span className="k">Size on disk</span>
            <span className="v">{health.size_human ?? '—'}</span>
            <span className="k">Last write</span>
            <span className="v">
              {health.last_modified_iso ? new Date(health.last_modified_iso).toLocaleString() : '—'}
            </span>
            <span className="k">Closed cases</span>
            <span className="v">{health.closed_cases_count ?? '—'}</span>
            <span className="k">Stored memories</span>
            <span className="v">
              {health.memories_count ?? '—'}
              {health.memories_count_source === 'unavailable' && (
                <span className="text-tx-3 ml-1">(chromadb unavailable)</span>
              )}
            </span>
          </div>
        ) : (
          <div className="text-sm text-tx-3">
            {healthLoading ? 'Loading mempalace health…' : 'Could not load mempalace health.'}
          </div>
        )}
      </SettingsCard>

      <ConfirmDialog
        open={clearAction != null}
        title={clearDialogTitle(clearAction)}
        body={clearDialogBody(clearAction)}
        confirmLabel="Yes, clear"
        busy={clearing}
        onConfirm={handleClear}
        onClose={() => setClearAction(null)}
      />
    </>
  )
}

function clearDialogTitle(action: ClearAction | null): string {
  switch (action) {
    case 'findings':
      return 'Clear findings?'
    case 'investigations':
      return 'Clear investigations?'
    case 'cases':
      return 'Clear cases and metrics?'
    case 'workspace':
      return 'Clear workspace data?'
    default:
      return 'Clear data?'
  }
}

function clearDialogBody(action: ClearAction | null): string {
  switch (action) {
    case 'findings':
      return 'This will permanently delete all findings from the database. Cases and settings are not deleted.'
    case 'investigations':
      return 'This will kill running investigations and permanently delete investigation records, logs, and working directories.'
    case 'cases':
      return 'This will permanently delete all cases and case-derived metrics. Findings and settings are not deleted.'
    case 'workspace':
      return 'This will permanently delete generated findings, investigations, cases, and case-derived metrics. Users, providers, integrations, credentials, and settings are preserved.'
    default:
      return 'This action cannot be undone.'
  }
}
