/* ============================================================
   Settings · AI Config — five sub-panels behind an internal tab bar.

   Providers, Models and Virtual Keys read and write the Bifrost gateway's own
   config store through the backend passthrough, so what this page shows is
   what actually routes. Model Assignment is Vigil's own concept (which model
   each component uses) and still resolves against llm_provider_configs;
   Operations are Vigil runtime knobs.
   ============================================================ */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  EmptyState,
  Field,
  NumberInput,
  Select,
  SettingsCard,
  TextInput,
  Toggle,
  ToggleRow,
} from '../../shared/ui'
import { agentsApi } from '../../services/api'
import AiProvidersPanel from './AiProvidersPanel'
import AiModelsPanel from './AiModelsPanel'
import AiBudgetsPanel from './AiBudgetsPanel'
import {
  AI_OPS_DEFAULTS,
  useAiOperations,
  useModelAssignment,
  type AIOperationsSettings,
} from './useSettings'
import type { AIModelInfo } from '../../services/api'
import type { SectionProps } from './types'
import { COMPONENT_LABELS, CHAT_DEFAULT_KEY } from '../../config/aiComponents'

type AiTab = 'providers' | 'catalogue' | 'assignment' | 'keys' | 'operations'
const TABS: [AiTab, string][] = [
  ['providers', 'Providers & Keys'],
  ['catalogue', 'Models'],
  ['assignment', 'Model Assignment'],
  ['keys', 'Virtual Keys'],
  ['operations', 'Operations'],
]

export default function AiConfigSection({ notify }: SectionProps) {
  const [tab, setTab] = useState<AiTab>('providers')
  return (
    <>
      <div className="tabs" style={{ gap: 4 }}>
        {TABS.map(([k, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'providers' && <AiProvidersPanel notify={notify} />}
      {tab === 'catalogue' && <AiModelsPanel />}
      {tab === 'assignment' && (
        <>
          <ModelAssignmentPanel notify={notify} />
          <CustomAgentModels notify={notify} />
        </>
      )}
      {tab === 'keys' && <AiBudgetsPanel notify={notify} />}
      {tab === 'operations' && <OperationsPanel notify={notify} />}
    </>
  )
}

/* ---------------- Model assignment ---------------- */
interface RowState { inherit: boolean; providerId: string; modelId: string }

function ModelAssignmentPanel({ notify }: SectionProps) {
  const { components, assignments, models, phase, error, reload, assign, clearAssign } = useModelAssignment()
  const [rows, setRows] = useState<Record<string, RowState>>({})

  const modelsByProvider = useMemo(() => {
    const grouped: Record<string, AIModelInfo[]> = {}
    for (const m of models) (grouped[m.provider_id] ||= []).push(m)
    return grouped
  }, [models])
  const providerIds = useMemo(() => Object.keys(modelsByProvider).sort(), [modelsByProvider])

  useEffect(() => {
    if (phase !== 'ready') return
    const next: Record<string, RowState> = {}
    for (const c of components) {
      const a = assignments[c]
      next[c] = a
        ? { inherit: false, providerId: a.provider_id, modelId: a.model_id }
        : { inherit: c !== CHAT_DEFAULT_KEY, providerId: '', modelId: '' }
    }
    setRows(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  const persist = async (component: string, next: RowState) => {
    try {
      if (next.inherit) {
        if (assignments[component] !== undefined) {
          await clearAssign(component)
          notify('ok', `${component} set to inherit.`)
        }
        return
      }
      if (!next.providerId || !next.modelId) return
      const a = assignments[component]
      if (a && a.provider_id === next.providerId && a.model_id === next.modelId) return
      await assign(component, next.providerId, next.modelId)
      notify('ok', `${component} saved.`)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || `Failed to save ${component}.`)
    }
  }

  const update = (component: string, patch: Partial<RowState>) => {
    setRows((prev) => {
      const next = { ...prev[component], ...patch }
      persist(component, next)
      return { ...prev, [component]: next }
    })
  }

  return (
    <SettingsCard
      wide
      title="Model Assignment"
      desc="Pick a provider + model for each system component. Unassigned rows fall back to the chat_default assignment. The model list is live-queried from each provider."
    >
      {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading AI config…" />}
      {phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load AI config" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />}
      {phase === 'ready' && (
        <>
          {providerIds.length === 0 && (
            <EmptyState compact icon="sparkle" title="No assignable models discovered" body="Add and test at least one active provider before assigning models to Vigil components." />
          )}
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr><th>Component</th><th>Provider</th><th>Model</th><th>Inherit</th></tr>
              </thead>
              <tbody>
                {components.map((c) => {
                  const meta = COMPONENT_LABELS[c] || { label: c, description: '' }
                  const row = rows[c] || { inherit: true, providerId: '', modelId: '' }
                  const isChatDefault = c === CHAT_DEFAULT_KEY
                  const providerModels = row.providerId ? modelsByProvider[row.providerId] || [] : []
                  return (
                    <tr key={c}>
                      <td style={{ verticalAlign: 'top', maxWidth: 280 }}>
                        <div className="font-medium">{meta.label}</div>
                        <div className="text-xs text-tx-3">{meta.description}</div>
                      </td>
                      <td style={{ minWidth: 150 }}>
                        <Select
                          value={row.providerId}
                          placeholder="Select provider"
                          options={providerIds.map((pid) => ({ value: pid, label: pid }))}
                          onSelect={(v) => update(c, { providerId: v, modelId: '' })}
                        />
                      </td>
                      <td style={{ minWidth: 200 }}>
                        <Select
                          value={row.modelId}
                          placeholder="Select model"
                          options={providerModels.map((m) => ({ value: m.model_id, label: m.display_name || m.model_id }))}
                          onSelect={(v) => update(c, { modelId: v })}
                        />
                      </td>
                      <td style={{ verticalAlign: 'top' }}>
                        <Toggle
                          checked={row.inherit}
                          disabled={isChatDefault}
                          onChange={(on) => update(c, { inherit: on, ...(on ? { providerId: '', modelId: '' } : {}) })}
                        />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </SettingsCard>
  )
}

const WORKFLOW_MODEL_NOTE =
  'Workflow runs use the investigation assignment in Settings › AI models.'

interface CustomAgentModelRow {
  id: string
  name: string
  model: string
  fallback_model: string
  saved_model: string
  saved_fallback_model: string
}

function CustomAgentModels({ notify }: SectionProps) {
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [rows, setRows] = useState<CustomAgentModelRow[]>([])

  const load = useCallback(() => {
    setPhase('loading')
    agentsApi
      .listCustom()
      .then((res) => {
        const agents = (res.data?.agents || []) as Array<{
          id: string
          name?: string
          model?: string | null
          fallback_model?: string | null
        }>
        setRows(
          agents.map((a) => ({
            id: a.id,
            name: a.name || a.id,
            model: a.model || '',
            fallback_model: a.fallback_model || '',
            saved_model: a.model || '',
            saved_fallback_model: a.fallback_model || '',
          })),
        )
        setPhase('ready')
      })
      .catch((e) => {
        setError((e as { message?: string })?.message || 'Couldn’t load custom agents.')
        setPhase('error')
      })
  }, [])

  useEffect(() => { load() }, [load])

  const edit = (id: string, patch: Partial<CustomAgentModelRow>) => {
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)))
  }

  const save = async (row: CustomAgentModelRow, field: 'model' | 'fallback_model', value: string) => {
    const next = value.trim()
    const stored = field === 'model' ? row.saved_model : row.saved_fallback_model
    if (next === stored.trim()) return
    const savedKey = field === 'model' ? 'saved_model' : 'saved_fallback_model'
    try {
      await agentsApi.updateCustom(row.id, { [field]: next || null })
      edit(row.id, { [field]: next, [savedKey]: next })
      notify('ok', `${row.name} saved.`)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || `Failed to save ${row.name}.`)
    }
  }

  return (
    <SettingsCard wide title="Custom agents" desc={WORKFLOW_MODEL_NOTE}>
      {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading custom agents…" />}
      {phase === 'error' && (
        <EmptyState error compact icon="alert" title="Couldn’t load custom agents" body={error} primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />
      )}
      {phase === 'ready' && rows.length === 0 && (
        <EmptyState compact icon="sparkle" title="No custom agents" body="Fork or create an agent to set a chat model for it." />
      )}
      {phase === 'ready' && rows.length > 0 && (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr><th>Agent</th><th>Model</th><th>Fallback model</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td style={{ verticalAlign: 'top' }}>
                    <div className="font-medium">{row.name}</div>
                    <div className="text-xs text-tx-3">{row.id}</div>
                  </td>
                  <td style={{ minWidth: 200 }}>
                    <TextInput
                      value={row.model}
                      placeholder="Assignment model"
                      onChange={(e) => edit(row.id, { model: e.target.value })}
                      onBlur={(e) => save(row, 'model', e.target.value)}
                    />
                  </td>
                  <td style={{ minWidth: 200 }}>
                    <TextInput
                      value={row.fallback_model}
                      placeholder="Optional"
                      onChange={(e) => edit(row.id, { fallback_model: e.target.value })}
                      onBlur={(e) => save(row, 'fallback_model', e.target.value)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SettingsCard>
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
