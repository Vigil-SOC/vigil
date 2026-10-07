import { useEffect, useState } from 'react'
import { getAllIntegrations } from '../../config/integrations'
import { configApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import CheckMark from './CheckMark'
import ConnectFields from './ConnectFields'
import { missingFields, type ConnectConfig } from './connectConfig'
import { errorText } from './errorText'

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, ConnectConfig>
  secrets_set: Record<string, Record<string, boolean>>
}

type Phase = 'loading' | 'ready' | 'error'

const readIntegrations = async (): Promise<IntegrationsConfig> => {
  const { data } = await configApi.getIntegrations()
  // the endpoint answers 200 with an `error` key when it cannot read the config
  if (!data || data.error) throw new Error('integrations unreadable')
  return {
    enabled_integrations: data.enabled_integrations || [],
    integrations: data.integrations || {},
    secrets_set: data.secrets_set || {},
  }
}

/** The one notification route: Slack, connected inline with the same fields as Settings › Integrations. */
export default function SlackRoute() {
  const slack = getAllIntegrations().find((i) => i.id === 'slack')
  const [cfg, setCfg] = useState<IntegrationsConfig | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState<ConnectConfig>({})
  const [saving, setSaving] = useState(false)
  const [blocked, setBlocked] = useState<string | null>(null)

  const load = () => {
    setPhase('loading')
    readIntegrations()
      .then((next) => {
        setCfg(next)
        setForm(next.integrations.slack ?? {})
        setPhase('ready')
      })
      .catch(() => setPhase('error'))
  }
  useEffect(load, [])

  // the bot token is what the orchestrator posts with, so it is what "connected" means
  const connected = cfg?.secrets_set.slack?.bot_token === true

  const connect = async () => {
    if (!slack || !cfg) return
    const missing = missingFields(slack, form, cfg.secrets_set.slack ?? {})
    if (missing.length) {
      setBlocked(`Please fill in: ${missing.map((f) => f.label).join(', ')}`)
      return
    }
    setBlocked(null)
    setSaving(true)
    try {
      // merge into the config as it is now, so another integration's save is not undone
      const cur = await readIntegrations()
      const enabled = cur.enabled_integrations.includes('slack')
        ? cur.enabled_integrations
        : [...cur.enabled_integrations, 'slack']
      await configApi.setIntegrations({
        enabled_integrations: enabled,
        integrations: { ...cur.integrations, slack: form },
      })
      load()
      setOpen(false)
    } catch (err) {
      setBlocked(errorText(err, 'Could not save the Slack connection.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3.5">
      <div className="su-route">
        <span className="su-route-ico">
          <Icon name="chat" size={16} />
        </span>
        <span className="su-route-text">
          <span className="su-route-t">Slack</span>
          <span className="su-route-s">Post urgent notifications to a channel</span>
        </span>
        {phase === 'loading' && <span className="su-note">Checking…</span>}
        {phase === 'error' && (
          <span className="su-note err" role="alert">
            Could not read the Slack connection.{' '}
            <button type="button" className="su-more" onClick={load}>
              Retry
            </button>
          </span>
        )}
        {phase === 'ready' &&
          (connected ? (
            <span className="su-route-state">
              <CheckMark phase="passed" />
              Connected
            </span>
          ) : (
            <button
              type="button"
              className="btn"
              aria-expanded={open}
              disabled={!slack}
              onClick={() => setOpen((o) => !o)}
            >
              Connect Slack
            </button>
          ))}
      </div>
      {phase === 'ready' && !connected && open && slack && cfg && (
        <div className="flex flex-col gap-3.5">
          <ConnectFields
            integration={slack}
            config={form}
            secretsSet={cfg.secrets_set.slack ?? {}}
            onChange={(name, value) => setForm((f) => ({ ...f, [name]: value }))}
          />
          <div className="su-actions">
            <button type="button" className="btn primary" disabled={saving} onClick={connect}>
              {saving ? 'Saving…' : 'Save Slack'}
            </button>
            {blocked ? (
              <span className="su-note err" role="alert">
                {blocked}
              </span>
            ) : (
              <span className="su-note">Secrets are stored encrypted and never shown again.</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
