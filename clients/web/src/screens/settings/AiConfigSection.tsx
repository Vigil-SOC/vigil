/* ============================================================
   Settings · AI models — the overview (providers, data residency, model for
   each agent), then Keys, Spending limit and Advanced as cards on the page,
   with the Bifrost model catalogue behind a second tab.

   Keys and the spending limit read and write the Bifrost gateway's own config
   store through the backend passthrough, so what this page shows is what
   actually routes. Which model each component uses is Vigil's own concept and
   lives in the overview; Advanced holds Vigil runtime knobs.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { NumberInput, SettingsCard, ToggleRow } from '../../shared/ui'
import AiProvidersPanel from './AiProvidersPanel'
import AiModelsPanel from './AiModelsPanel'
import AiBudgetsPanel from './AiBudgetsPanel'
import AiModelsOverview from './AiModelsOverview'
import {
  AI_OPS_DEFAULTS,
  useAiOperations,
  type AIOperationsSettings,
} from './useSettings'
import type { SectionProps } from './types'

type AiTab = 'keys' | 'catalogue'
const TABS: [AiTab, string][] = [
  ['keys', 'Keys & limits'],
  ['catalogue', 'Models'],
]

export default function AiConfigSection({ notify }: SectionProps) {
  const [tab, setTab] = useState<AiTab>('keys')
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
      {tab === 'keys' && (
        <>
          <AiProvidersPanel notify={notify} />
          <AiBudgetsPanel notify={notify} />
          <AdvancedPanel notify={notify} />
        </>
      )}
      {tab === 'catalogue' && <AiModelsPanel />}
    </>
  )
}

function AdvancedPanel({ notify }: SectionProps) {
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

  const apply = (next: AIOperationsSettings) => { setSettings(next); persist(next) }

  return (
    <SettingsCard
      wide
      title="Advanced"
      desc="Performance settings. They apply without a restart."
      actions={
        <button className="btn ghost" onClick={() => apply(AI_OPS_DEFAULTS)}>
          <Icon name="refresh" /> Reset to defaults
        </button>
      }
    >
      <ToggleRow
        label="Retry a local model that stops responding"
        hint="When a local Ollama request loses the Bifrost connection, retry it in the background. Cloud providers are never retried here."
        checked={settings.local_ollama_recovery_enabled}
        onChange={(v) => apply({ ...settings, local_ollama_recovery_enabled: v })}
      />
      {settings.local_ollama_recovery_enabled && (
        <>
          <ToggleRow
            label="Restart the local gateway first"
            hint="If Bifrost is unhealthy, restart the local gateway before retrying. Off retries only when the gateway is already healthy."
            checked={settings.local_ollama_recovery_restart_gateway}
            onChange={(v) => apply({ ...settings, local_ollama_recovery_restart_gateway: v })}
          />
          <div className="toggle-row">
            <div className="toggle-row-text">
              <span className="toggle-row-label">Retry attempts</span>
              <span className="toggle-row-hint">Retries after the first failed request. 0 disables retries.</span>
            </div>
            <div style={{ width: 96 }}>
              <NumberInput
                aria-label="Retry attempts"
                value={settings.local_ollama_recovery_retry_limit}
                min={0}
                max={3}
                onChange={(e) =>
                  setSettings({ ...settings, local_ollama_recovery_retry_limit: Math.max(0, Math.min(3, Number(e.target.value) || 0)) })
                }
                onBlur={() => {
                  if (settings.local_ollama_recovery_retry_limit !== lastSaved.current.local_ollama_recovery_retry_limit) persist(settings)
                }}
              />
            </div>
          </div>
        </>
      )}
    </SettingsCard>
  )
}
