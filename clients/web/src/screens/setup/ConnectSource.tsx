import { useState } from 'react'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { Icon } from '../../shared/icons'
import { extractApiError } from '../../shared/formKit'
import { SettingsCard } from '../../shared/ui'
import CheckMark from './CheckMark'
import CheckTable, { type CheckRow } from './CheckTable'
import ConnectFields from './ConnectFields'
import { fieldsOf, missingFields, type ConnectConfig } from './connectConfig'
import SourceCollection from './SourceCollection'

/** One server's answer to a save. `connected: null` means the MCP subsystem could not say. */
export interface ConnectResult {
  connected: boolean | null
  error?: string
  missing_credentials?: string[]
}

type Outcome = { kind: 'testing' } | { kind: 'done'; result: ConnectResult } | { kind: 'failed'; message: string }

const connectionRow = (name: string, o: Outcome): CheckRow => {
  const row = { id: 'connection', label: 'Connection' }
  if (o.kind === 'testing') return { ...row, phase: 'checking', detail: `Connecting to ${name}…` }
  if (o.kind === 'failed') return { ...row, phase: 'needs', detail: o.message }
  const { connected, error, missing_credentials: missing } = o.result
  if (connected === true) return { ...row, phase: 'passed', detail: `Connected to ${name}` }
  if (connected === null)
    return { ...row, phase: 'waiting', detail: `Saved, but the connection to ${name} could not be confirmed` }
  return {
    ...row,
    phase: 'needs',
    detail:
      error ||
      (missing?.length
        ? `Missing required credentials: ${missing.join(', ')}.`
        : `Couldn't connect to ${name}. Check the credentials and try again.`),
  }
}

/** The inline "Connect <name>" card: fields, Test connection, its result rows and the Connected banner. */
export default function ConnectSource({
  integration,
  existingConfig,
  secretsSet,
  sourceId,
  onTest,
  onAdvance,
  onAnother,
}: {
  integration: IntegrationMetadata
  existingConfig: ConnectConfig
  secretsSet: Record<string, boolean>
  /** federation source id, when the integration has a collector */
  sourceId?: string
  onTest: (config: ConnectConfig) => Promise<ConnectResult>
  onAdvance: () => void
  onAnother: () => void
}) {
  const [config, setConfig] = useState<ConnectConfig>(existingConfig)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [blocked, setBlocked] = useState<string | null>(null)
  // secrets this card has saved, so clearing the field and testing again still counts them
  const [stored, setStored] = useState(secretsSet)
  const [wasPassed, setWasPassed] = useState(false)
  const testing = outcome?.kind === 'testing'

  const test = async () => {
    const missing = missingFields(integration, config, stored)
    if (missing.length) {
      setBlocked(`Please fill in: ${missing.map((f) => f.label).join(', ')}`)
      return
    }
    setBlocked(null)
    setWasPassed(passed)
    setOutcome({ kind: 'testing' })
    try {
      const result = await onTest(config)
      setStored((s) => {
        const next = { ...s }
        for (const f of fieldsOf(integration)) if (f.type === 'password' && config[f.name]) next[f.name] = true
        return next
      })
      setOutcome({ kind: 'done', result })
    } catch (e) {
      setOutcome({ kind: 'failed', message: extractApiError(e, 'Could not save the connection') })
    }
  }

  const passed = outcome?.kind === 'done' && outcome.result.connected === true
  const rows = outcome ? [connectionRow(integration.name, outcome)] : []
  const banner = passed && (
    <div className="su-banner" role="status">
      <CheckMark phase="passed" />
      <span>
        Connected. Alerts from {integration.name} flow into triage as soon as you finish setup.
      </span>
      <button type="button" className="btn ghost" onClick={onAnother}>
        Connect another
      </button>
      <button type="button" className="btn primary" onClick={onAdvance}>
        Continue
      </button>
    </div>
  )

  return (
    <SettingsCard
      title={`Connect ${integration.name}`}
      desc="Vigil reads alerts only. Tools that change something are granted separately, and always ask first."
      actions={
        integration.docs_url && (
          <a className="btn ghost" href={integration.docs_url} target="_blank" rel="noreferrer">
            <Icon name="link" size={14} />
            Where do I find this?
          </a>
        )
      }
    >
      <div className="flex flex-col gap-3.5">
        <ConnectFields
          integration={integration}
          config={config}
          secretsSet={stored}
          onChange={(name, value) => setConfig((c) => ({ ...c, [name]: value }))}
        />
        <div className="su-actions">
          <button type="button" className="btn ghost test" disabled={testing} onClick={test}>
            <Icon name="bolt" size={14} />
            {testing ? 'Testing…' : outcome ? 'Test again' : 'Test connection'}
          </button>
          {blocked ? (
            <span className="su-note err" role="alert">
              {blocked}
            </span>
          ) : (
            <span className="su-note">Secrets are stored encrypted and never shown again.</span>
          )}
        </div>
        {sourceId && (passed || (testing && wasPassed)) ? (
          <SourceCollection key={sourceId} sourceId={sourceId} rows={rows} banner={banner} />
        ) : (
          <>
            {rows.length > 0 && <CheckTable label="Connection checks" rows={rows} />}
            {banner}
          </>
        )}
      </div>
    </SettingsCard>
  )
}
