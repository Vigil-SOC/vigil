/* ============================================================
   Settings · Services — start local services on demand and pick
   which ones ./start.sh brings up automatically.

   Ollama is a host process, not a container: Docker on macOS has
   no Metal GPU passthrough, so a containerized Ollama would be
   CPU-only. It's therefore started but never stopped from here —
   the running instance is often the user's own (brew services /
   Ollama.app), and killing that would destroy unrelated state.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { SettingsCard, ToggleRow } from '../../shared/ui'
import { localServicesApi } from '../../../services/api'
import type { SectionProps } from './types'

interface ServiceRow {
  name: string
  kind: 'docker' | 'host'
  running: boolean
  ready: boolean
  installed: boolean
  status: string
  managed_by_vigil: boolean
  startable: boolean
  stoppable: boolean
  description: string
  detail?: string | null
}

function StatusPill({ s }: { s: ServiceRow }) {
  const tone = s.running ? 'ok' : s.installed ? 'idle' : 'warn'
  return <span className={`status ${tone === 'ok' ? 'open' : 'closed'}`}>{s.status}</span>
}

export default function ServicesSection({ notify }: SectionProps) {
  const [rows, setRows] = useState<ServiceRow[]>([])
  const [autostart, setAutostart] = useState<string[]>([])
  const [dockerOk, setDockerOk] = useState(true)
  const [dockerDetail, setDockerDetail] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const { data } = await localServicesApi.list()
      setRows(data.services || [])
      setAutostart(data.autostart || [])
      setDockerOk(!!data.docker_available)
      setDockerDetail(data.docker_detail || '')
    } catch (e) {
      notify('err', `Couldn't load services: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setLoading(false)
    }
  }, [notify])

  useEffect(() => { void load() }, [load])

  const act = async (name: string, action: 'start' | 'stop' | 'restart') => {
    setBusy(name)
    try {
      const { data } = await localServicesApi[action](name)
      notify(data.already_running ? 'info' : 'ok', data.message || `${name} ${action}ed`)
      // Ollama only becomes usable once Bifrost knows about it.
      if (data.bifrost_synced === false && data.bifrost_sync_error) {
        notify('info', `${name} is up, but the model catalog didn't sync: ${data.bifrost_sync_error}`)
      }
      await load()
    } catch (e) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      notify('err', msg || `Couldn't ${action} ${name}`)
    } finally {
      setBusy(null)
    }
  }

  const toggleAutostart = async (name: string, on: boolean) => {
    const next = on ? [...autostart, name] : autostart.filter((n) => n !== name)
    const previous = autostart
    setAutostart(next)  // optimistic
    try {
      const { data } = await localServicesApi.setAutostart(next)
      setAutostart(data.services)
    } catch (e) {
      setAutostart(previous)
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      notify('err', msg || "Couldn't save the autostart list")
    }
  }

  return (
    <>
      {!dockerOk && (
        <SettingsCard
          title="Docker isn't running"
          desc={dockerDetail || 'Container services can’t start until the Docker daemon is reachable. ./start.sh launches Docker Desktop for you.'}
        >
          <span />
        </SettingsCard>
      )}

      <SettingsCard
        title="Services"
        desc="Start local services on demand. Vigil starts Ollama natively rather than in Docker so it keeps GPU acceleration."
        actions={<button className="btn ghost icon" title="Refresh" onClick={() => void load()}><Icon name="refresh" /></button>}
        wide
      >
        {loading ? (
          <p className="muted">Loading services…</p>
        ) : (
          <div className="svc-list">
            {rows.map((s) => (
              <div key={s.name} className="svc-row" style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 0' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <strong>{s.name}</strong>
                    <StatusPill s={s} />
                    {s.kind === 'host' && <span className="tag">host</span>}
                    {s.running && !s.managed_by_vigil && s.kind === 'host' && (
                      <span className="muted" style={{ fontSize: 12 }}>started outside Vigil</span>
                    )}
                  </div>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {s.detail || s.description}
                  </span>
                </div>
                <button
                  className="btn primary"
                  disabled={busy === s.name || s.running || !s.startable || !s.installed}
                  onClick={() => void act(s.name, 'start')}
                >
                  {busy === s.name ? 'Starting…' : s.running ? 'Running' : 'Start'}
                </button>
                {s.stoppable && (
                  <button
                    className="btn ghost"
                    disabled={busy === s.name || !s.running}
                    onClick={() => void act(s.name, 'stop')}
                  >Stop</button>
                )}
              </div>
            ))}
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="Start automatically"
        desc="Services ./start.sh brings up on boot. Heavy ones (Splunk, Kafka, observability) are off by default so startup stays fast."
        wide
      >
        {rows.map((s) => (
          <ToggleRow
            key={s.name}
            label={s.name}
            hint={s.description}
            checked={autostart.includes(s.name)}
            onChange={(v) => void toggleAutostart(s.name, v)}
          />
        ))}
      </SettingsCard>
    </>
  )
}
