import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { LevelBadge, type Level } from '../../shared/LevelBadge'
import { SettingsCard } from '../../shared/ui'
import { configApi } from '../../services/api'
import { relativeTime } from './integrationHealth'
import { useIntegrationsConfig } from './useSettings'
import type { SectionProps } from './types'

type TestResult = { ok: boolean; message: string }

// The one editor for these fields is the Integrations screen; this card only reads them.
const CONFIGURE_TO = '/settings?section=integrations'

const SECRETS = [
  { field: 'mint_secret', label: 'Session secret', hint: 'Never sent to the browser' },
  { field: 'mcp_token', label: 'Agent access token', hint: 'Lets agents query LogLM' },
]

export default function LoglmCard({ notify }: SectionProps) {
  const { config, phase, reload } = useIntegrationsConfig()
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<TestResult | null>(null)

  const title = 'LogLM pipeline'
  const desc = 'Anomaly findings from LogLM flow into triage like any other source; agents can also query LogLM directly.'

  if (phase === 'loading') {
    return <SettingsCard title={title} desc={desc}><div className="text-sm text-tx-3 py-6 text-center">Loading…</div></SettingsCard>
  }
  if (phase === 'error') {
    return (
      <SettingsCard title={title} desc={desc}>
        <div className="py-6 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load the LogLM settings.</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      </SettingsCard>
    )
  }

  const url = String(config.integrations.loglm?.connectorUrl ?? '')
  const secretsSet = config.secrets_set.loglm ?? {}

  const test = async () => {
    setTesting(true)
    try {
      const res = await configApi.testIntegration('loglm')
      setResult({ ok: !!res.data.success, message: res.data.message ?? '' })
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setResult({ ok: false, message: detail || 'The test could not run.' })
      notify('err', detail || 'The connection test could not run.')
    } finally {
      setTesting(false)
    }
  }

  const last = config.last_test.loglm
  const stored: TestResult | null =
    last && last.success !== null
      ? { ok: last.success, message: last.success ? `Last test passed ${relativeTime(last.at)}.` : last.error || 'Last test failed.' }
      : null
  const shown = result ?? stored

  const status: { level: Level; word: string; text: string } = !url
    ? { level: null, word: 'Not set up', text: 'Add the connector URL in Integrations to connect LogLM.' }
    : shown
      ? { level: shown.ok ? 'good' : 'poor', word: '', text: shown.message }
      : { level: null, word: 'Not tested yet', text: 'Run a test to check the connector answers and accepts the session secret.' }

  return (
    <SettingsCard
      title={title}
      desc={desc}
      actions={<Link className="btn ghost" to={CONFIGURE_TO}><Icon name="gear" /> Configure</Link>}
    >
      <div className="data-loglm-fields">
        <div className="data-ro">
          <span className="data-ro-label">Connector URL</span>
          <div className={`data-ro-value font-mono${url ? '' : ' empty'}`} title={url || undefined}>{url || 'Not set'}</div>
          <span className="data-ro-hint">Must be reachable from your browser</span>
        </div>
        {SECRETS.map(({ field, label, hint }) => (
          <div key={field} className="data-ro">
            <span className="data-ro-label">{label}</span>
            <div className={`data-ro-value${secretsSet[field] ? '' : ' empty'}`}>
              {secretsSet[field] ? '•••••• saved' : 'Not set'}
            </div>
            <span className="data-ro-hint">{hint}</span>
          </div>
        ))}
      </div>

      <div className="data-loglm-status" role="status">
        {status.level ? <LevelBadge level={status.level} variant="pill" /> : <span className="level-pill idle">{status.word}</span>}
        <span className="data-loglm-msg">{status.text}</span>
        <button className="btn ghost" onClick={test} disabled={!url || testing}>
          <Icon name="bolt" /> {testing ? 'Testing…' : 'Test connection'}
        </button>
      </div>
    </SettingsCard>
  )
}
