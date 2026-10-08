/* ============================================================
   Settings · AI Config — the overview (providers, data residency, model for
   each agent) above four sub-panels behind an internal tab bar.

   Providers, Models and Virtual Keys read and write the Bifrost gateway's own
   config store through the backend passthrough, so what this page shows is
   what actually routes. Which model each component uses is Vigil's own concept
   and lives in the overview; Operations are Vigil runtime knobs.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { Field, NumberInput, SettingsCard, ToggleRow } from '../../shared/ui'
import AiProvidersPanel from './AiProvidersPanel'
import AiModelsPanel from './AiModelsPanel'
import AiBudgetsPanel from './AiBudgetsPanel'
import AiModelsOverview, { AGENT_MODEL_TABLE_ID } from './AiModelsOverview'
import {
  AI_OPS_DEFAULTS,
  useAiOperations,
  type AIOperationsSettings,
} from './useSettings'
import type { SectionProps } from './types'

type AiTab = 'providers' | 'catalogue' | 'keys' | 'operations'
const TABS: [AiTab, string][] = [
  ['providers', 'Providers & Keys'],
  ['catalogue', 'Models'],
  ['keys', 'Virtual Keys'],
  ['operations', 'Operations'],
]

function tabFromQuery(value: string | null): AiTab {
  return TABS.find(([k]) => k === value)?.[0] ?? 'providers'
}

export default function AiConfigSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const query = searchParams.get('tab')
  const requested = tabFromQuery(query)
  const [tab, setTab] = useState<AiTab>(requested)

  useEffect(() => {
    setTab(requested)
  }, [requested])

  // The old Model Assignment tab is now the "Model for each agent" table in
  // the overview; ?tab=assignment (Home's per-agent step) scrolls to it.
  useEffect(() => {
    if (query === 'assignment') document.getElementById(AGENT_MODEL_TABLE_ID)?.scrollIntoView?.({ block: 'start' })
  }, [query])
  return (
    <>
      <AiModelsOverview notify={notify} />
      <div className="tabs" style={{ gap: 4 }}>
        {TABS.map(([k, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'providers' && <AiProvidersPanel notify={notify} />}
      {tab === 'catalogue' && <AiModelsPanel />}
      {tab === 'keys' && <AiBudgetsPanel notify={notify} />}
      {tab === 'operations' && <OperationsPanel notify={notify} />}
    </>
  )
}

function OperationsPanel({ notify }: SectionProps) {
  const { settings, setSettings, phase, save } = useAiOperations()
  const lastSaved = useRef<AIOperationsSettings>(AI_OPS_DEFAULTS)

  useEffect(() => {
    if (phase === 'ready') lastSaved.current = settings
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-8 text-center">Loading AI operations…</div>
  }

  const persist = async (next: AIOperationsSettings) => {
    try {
      await save(next)
      lastSaved.current = next
      notify('ok', 'AI operations settings saved.')
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save AI operations config.')
    }
  }

  const numField = (key: keyof AIOperationsSettings, label: string, hint: string, min: number, max: number) => (
    <Field label={label} hint={hint}>
      <NumberInput
        value={settings[key] as number}
        min={min}
        max={max}
        onChange={(e) =>
          setSettings({ ...settings, [key]: Math.max(min, Math.min(max, Number(e.target.value) || 0)) })
        }
        onBlur={() => {
          if (settings[key] !== lastSaved.current[key]) persist(settings)
        }}
      />
    </Field>
  )

  return (
    <SettingsCard
      title="Local Ollama enrichment recovery"
      desc="Retry a local Ollama enrichment request when it loses the Bifrost connection. These settings persist in the database and take effect without a service restart. Cloud providers are never retried here."
      actions={
        <button className="btn ghost" onClick={() => { setSettings(AI_OPS_DEFAULTS); persist(AI_OPS_DEFAULTS) }}>
          <Icon name="refresh" /> Reset to defaults
        </button>
      }
    >
      <ToggleRow
        label="Automatically retry local AI enrichment"
        hint="When a local Ollama enrichment request loses the Bifrost connection, retry it in the background."
        checked={settings.local_ollama_recovery_enabled}
        onChange={(v) => { const next = { ...settings, local_ollama_recovery_enabled: v }; setSettings(next); persist(next) }}
      />
      <ToggleRow
        label="Restart the local AI gateway when unavailable"
        hint="If Bifrost is unhealthy, restart the local gateway before retrying. Disable this to retry only when the gateway is already healthy."
        checked={settings.local_ollama_recovery_restart_gateway}
        disabled={!settings.local_ollama_recovery_enabled}
        onChange={(v) => { const next = { ...settings, local_ollama_recovery_restart_gateway: v }; setSettings(next); persist(next) }}
      />
      <div className="settings-grid-2 mt-4" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 2fr)' }}>
        {numField('local_ollama_recovery_retry_limit', 'Retry attempts', 'Retries after the first failed request. 0 disables retries.', 0, 3)}
      </div>
    </SettingsCard>
  )
}
