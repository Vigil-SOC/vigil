/* ============================================================
   Settings · Developer (dev-only) — PostgreSQL connection + storage
   status and local Splunk Enterprise control. Mirrors the "dev" tab
   in Settings.tsx. Infrastructure/local-environment settings.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, PasswordInput, SettingsCard } from '../../shared/ui'
import { useSplunk, useStorage } from './useSettings'
import type { SectionProps } from './types'

export default function DeveloperSection({ notify }: SectionProps) {
  const { status, health, phase, error, reload, reconnect, savePostgres } = useStorage()
  const splunk = useSplunk()

  const [connStr, setConnStr] = useState('')
  const [saving, setSaving] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)

  const handleSave = async () => {
    if (!connStr.trim()) {
      notify('err', 'Connection string cannot be empty.')
      return
    }
    setSaving(true)
    try {
      await savePostgres(connStr)
      setConnStr('')
      notify('ok', 'PostgreSQL config saved.')
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save PostgreSQL config.')
    } finally {
      setSaving(false)
    }
  }

  const handleReconnect = async () => {
    setReconnecting(true)
    try {
      await reconnect()
      notify('ok', 'Reconnected to storage backend.')
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to reconnect.')
    } finally {
      setReconnecting(false)
    }
  }

  const splunkAction = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn()
      notify('ok', ok)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Splunk action failed.')
    }
  }

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading developer tools…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load storage status: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const isPg = status?.backend === 'postgresql'
  const sp = splunk.status

  return (
    <>
      <SettingsCard
        title="PostgreSQL"
        desc="Platform storage backend. Findings and cases persist here when connected; otherwise they fall back to JSON files."
        actions={
          <>
            <button className="btn ghost" onClick={reload}>
              <Icon name="refresh" /> Refresh
            </button>
            <button className="btn primary" onClick={handleReconnect} disabled={reconnecting}>
              <Icon name="refresh" /> {reconnecting ? 'Reconnecting…' : 'Reconnect'}
            </button>
          </>
        }
      >
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <span className={`status ${isPg ? 'closed' : 'open'}`}>
            {isPg ? 'PostgreSQL Active' : 'JSON Files'}
          </span>
          {health && (
            <span className="chip">
              {health.findings_count ?? 0} findings · {health.cases_count ?? 0} cases
            </span>
          )}
        </div>

        {status && !isPg && (
          <div className="settings-banner info mb-3">
            <Icon name="info" size={14} />
            <span>
              Start PostgreSQL to enable database storage:{' '}
              <code className="font-mono">./scripts/start_database.sh</code>
            </span>
          </div>
        )}

        <div className="max-w-[520px] flex flex-col gap-3">
          <Field
            label="Connection String"
            hint="Write-only — the stored value is never returned. Leave blank to keep the current one."
          >
            <PasswordInput
              value={connStr}
              placeholder="postgresql://user:pass@localhost:5432/db"
              onChange={(e) => setConnStr(e.target.value)}
            />
          </Field>
          <div>
            <button className="btn primary" onClick={handleSave} disabled={saving}>
              <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </SettingsCard>

      <SettingsCard
        title={
          <span className="inline-flex items-center gap-2">
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: sp?.running ? 'var(--ok)' : 'var(--tx-faint)',
              }}
            />
            Local Splunk Enterprise
          </span>
        }
        desc="Start, stop, and restart a local Splunk Enterprise instance for development."
        actions={
          <button className="btn ghost" onClick={splunk.reload} disabled={splunk.busy}>
            <Icon name="refresh" /> Refresh Status
          </button>
        }
      >
        {sp && (
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <span className="text-sm text-tx-2">
              Status: <strong>{sp.running ? 'Running' : 'Stopped'}</strong>
            </span>
            {sp.running && sp.web_url && (
              <button className="btn ghost" onClick={() => window.open(sp.web_url, '_blank')}>
                <Icon name="link" /> Open Splunk UI
              </button>
            )}
          </div>
        )}

        {sp?.running && (
          <div className="settings-banner info mb-3">
            <Icon name="info" size={14} />
            <span className="text-xs">
              <strong>Web UI:</strong> {sp.web_url} · <strong>HEC:</strong> {sp.hec_url} ·{' '}
              <strong>User:</strong> {sp.username} · <strong>Pass:</strong>{' '}
              {sp.note?.split(': ')[1] || 'changeme123'}
            </span>
          </div>
        )}

        <div className="flex gap-2 flex-wrap">
          {!sp?.running ? (
            <button
              className="btn primary"
              disabled={splunk.busy}
              onClick={() => splunkAction(splunk.start, 'Starting Splunk…')}
            >
              <Icon name="play" /> Start Splunk
            </button>
          ) : (
            <>
              <button
                className="btn danger"
                disabled={splunk.busy}
                onClick={() => splunkAction(splunk.stop, 'Stopping Splunk…')}
              >
                <Icon name="pause" /> Stop
              </button>
              <button
                className="btn ghost"
                disabled={splunk.busy}
                onClick={() => splunkAction(splunk.restart, 'Restarting Splunk…')}
              >
                <Icon name="refresh" /> Restart
              </button>
            </>
          )}
        </div>
      </SettingsCard>
    </>
  )
}
