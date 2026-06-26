/* ============================================================
   Settings · AI Config — four sub-panels behind an internal tab bar:
   Providers (CRUD + test + set-default), Model Assignment (per-
   component provider/model + inherit), Operations (cost/perf knobs),
   Budgets (Bifrost virtual-key config + live quota). Mirrors the
   legacy AI Config tab (LLMProvidersTab / ModelAssignmentTab /
   AIOperationsTab / BudgetsSection).
   ============================================================ */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  Field,
  NumberInput,
  Select,
  SettingsCard,
  TextInput,
  Toggle,
  ToggleRow,
} from '../../shared/ui'
import LlmProviderDialog from './LlmProviderDialog'
import {
  AI_OPS_DEFAULTS,
  useAiOperations,
  useBudgets,
  useLlmProviders,
  useModelAssignment,
  type AIOperationsSettings,
} from './useSettings'
import type { LLMProvider, AIModelInfo } from '../../../services/api'
import type { SectionProps } from './types'

type AiTab = 'providers' | 'models' | 'operations' | 'budgets'
const TABS: [AiTab, string][] = [
  ['providers', 'Providers'],
  ['models', 'Model Assignment'],
  ['operations', 'Operations'],
  ['budgets', 'Budgets'],
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
      {tab === 'providers' && <ProvidersPanel notify={notify} />}
      {tab === 'models' && <ModelAssignmentPanel notify={notify} />}
      {tab === 'operations' && <OperationsPanel notify={notify} />}
      {tab === 'budgets' && <BudgetsPanel notify={notify} />}
    </>
  )
}

/* ---------------- Providers ---------------- */
function ProvidersPanel({ notify }: SectionProps) {
  const { providers, phase, error, reload, test, remove, setDefault } = useLlmProviders()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<LLMProvider | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [confirmDel, setConfirmDel] = useState<LLMProvider | null>(null)
  const [deleting, setDeleting] = useState(false)

  const handleTest = async (id: string) => {
    setTestingId(id)
    try {
      const res = await test(id)
      if (res.success) notify('ok', `Connection OK for ${id}.`)
      else notify('err', `Test failed: ${res.error || 'unknown error'}`)
      reload()
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Test request failed.')
    } finally {
      setTestingId(null)
    }
  }

  const handleSetDefault = async (id: string) => {
    try {
      await setDefault(id)
      notify('ok', `Default set to ${id}.`)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to set default.')
    }
  }

  const handleDelete = async () => {
    if (!confirmDel) return
    setDeleting(true)
    try {
      await remove(confirmDel.provider_id)
      notify('ok', `Deleted ${confirmDel.name}.`)
      setConfirmDel(null)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Delete failed.')
    } finally {
      setDeleting(false)
    }
  }

  const hasOtherActiveOfType = (p: LLMProvider) =>
    providers.some(
      (q) => q.provider_id !== p.provider_id && q.provider_type === p.provider_type && q.is_active,
    )

  const requestDelete = (p: LLMProvider) => {
    // The API blocks deleting the only active provider of a type (it has no
    // sibling to promote to default), so surface that up front instead of a
    // confirm dialog that implies an auto-promotion that won't happen.
    if (p.is_default && !hasOtherActiveOfType(p)) {
      notify(
        'err',
        `"${p.name}" is the only active ${p.provider_type} provider and can't be deleted. ` +
          `Add or activate another ${p.provider_type} provider first.`,
      )
      return
    }
    setConfirmDel(p)
  }

  // Mirror the warning to what deletion actually does: default providers
  // trigger a promotion; either way the provider's model assignments and
  // stored API key are removed.
  const deleteBody = (p: LLMProvider) =>
    p.is_default
      ? `"${p.name}" is the primary ${p.provider_type} provider. Deleting it promotes the next active ${p.provider_type} provider to primary. Its model assignments and stored API key are also removed.`
      : `Delete "${p.name}"? Its model assignments and stored API key are also removed.`

  const statusChip = (p: LLMProvider) => {
    if (!p.is_active) return <span className="chip">Inactive</span>
    if (p.last_test_success === true) return <span className="status closed">Active</span>
    if (p.last_test_success === false) return <span className="chip" style={{ color: 'var(--crit)' }}>Error</span>
    return <span className="chip">Untested</span>
  }

  return (
    <SettingsCard
      wide
      title="LLM Providers"
      desc="Configure additional Anthropic, OpenAI, or Ollama providers. Traffic routes through the Bifrost gateway — Anthropic calls use the /anthropic passthrough so extended thinking and prompt caching round-trip unchanged."
      actions={
        <button className="btn primary" onClick={() => { setEditing(null); setDialogOpen(true) }}>
          <Icon name="plus" /> Add Provider
        </button>
      }
    >
      {phase === 'loading' && <div className="text-sm text-tx-3 py-8 text-center">Loading providers…</div>}
      {phase === 'error' && (
        <div className="py-8 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load providers: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      )}
      {phase === 'ready' && (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th><th>Type</th><th>Model</th><th>Status</th><th>Default</th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {providers.length === 0 && (
                <tr><td colSpan={6} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>No providers configured.</td></tr>
              )}
              {providers.map((p) => (
                <tr key={p.provider_id}>
                  <td>
                    <div className="flex flex-col">
                      <span>{p.name}</span>
                      <span className="text-xs text-tx-3">{p.provider_id}</span>
                    </div>
                  </td>
                  <td><span className="chip">{p.provider_type}</span></td>
                  <td className="font-mono text-xs">{p.default_model}</td>
                  <td>{statusChip(p)}</td>
                  <td>
                    <button
                      className="btn ghost icon"
                      title={p.is_default ? 'Default for this provider type' : 'Set as default'}
                      onClick={() => !p.is_default && handleSetDefault(p.provider_id)}
                      style={p.is_default ? { color: 'var(--accent-2)' } : undefined}
                    >
                      <Icon name={p.is_default ? 'sparkle' : 'plus'} size={15} />
                    </button>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <div className="inline-flex gap-1.5">
                      <button className="btn ghost icon" title="Test connection" disabled={testingId === p.provider_id} onClick={() => handleTest(p.provider_id)}>
                        <Icon name={testingId === p.provider_id ? 'refresh' : 'bolt'} size={15} />
                      </button>
                      <button className="btn ghost icon" title="Edit" onClick={() => { setEditing(p); setDialogOpen(true) }}>
                        <Icon name="edit" size={15} />
                      </button>
                      <button className="btn ghost icon" title="Delete" onClick={() => requestDelete(p)}>
                        <Icon name="trash" size={15} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {dialogOpen && (
        <LlmProviderDialog
          existing={editing}
          onClose={() => { setDialogOpen(false); setEditing(null) }}
          onSaved={() => { setDialogOpen(false); setEditing(null); notify('ok', 'Provider saved.'); reload() }}
        />
      )}
      <ConfirmDialog
        open={!!confirmDel}
        title="Delete provider?"
        body={confirmDel ? deleteBody(confirmDel) : ''}
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={handleDelete}
        onClose={() => setConfirmDel(null)}
      />
    </SettingsCard>
  )
}

/* ---------------- Model assignment ---------------- */
const COMPONENT_LABELS: Record<string, { label: string; description: string }> = {
  chat_default: { label: 'Chat (Default)', description: 'Fallback for interactive chat and every component below when unset.' },
  triage: { label: 'Triage Agent', description: 'Automated alert triage — cheaper/faster models work well here.' },
  investigation: { label: 'Investigation Agents', description: 'Investigator, Threat Hunter, Correlator, etc. — the heavy lifters.' },
  orchestrator_plan: { label: 'Orchestrator — Planning', description: 'Generates the investigation plan from the initial finding.' },
  orchestrator_review: { label: 'Orchestrator — Review', description: 'Reviews and approves sub-agent output at the end of an investigation.' },
  summarization: { label: 'Context Summarization', description: 'Compresses long conversations — a cheap model is usually fine.' },
  reporting: { label: 'Report Generation', description: 'Reporter agent output — clarity and structure matter more than depth.' },
}
const CHAT_DEFAULT_KEY = 'chat_default'

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
      {phase === 'loading' && <div className="text-sm text-tx-3 py-8 text-center">Loading AI config…</div>}
      {phase === 'error' && (
        <div className="py-8 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load AI config: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      )}
      {phase === 'ready' && (
        <>
          {providerIds.length === 0 && (
            <div className="settings-banner info mb-3">
              <Icon name="info" size={14} />
              <span>No models discovered — add at least one active provider under Providers first.</span>
            </div>
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

/* ---------------- Operations ---------------- */
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
      title="AI Operations (Cost & Performance)"
      desc="Runtime toggles for Anthropic prompt caching, conversation history windowing, tool-response truncation, and the daemon's default thinking budget. Persist in the DB and take effect across backend / daemon / llm-worker within ~60s."
      actions={
        <button className="btn ghost" onClick={() => { setSettings(AI_OPS_DEFAULTS); persist(AI_OPS_DEFAULTS) }}>
          <Icon name="refresh" /> Reset to defaults
        </button>
      }
    >
      <ToggleRow
        label="Anthropic prompt caching"
        hint="Tag system + tool blocks with cache_control. ~90% cheaper on cached input tokens. Leave on unless debugging cache behavior."
        checked={settings.prompt_cache_enabled}
        onChange={(v) => { const next = { ...settings, prompt_cache_enabled: v }; setSettings(next); persist(next) }}
      />
      <div className="settings-grid-2 mt-4" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
        {numField('history_window', 'History window (turns)', '20 turns ≈ 40 messages. 0 disables.', 0, 200)}
        {numField('tool_response_budget_default', 'Tool-result budget (tokens)', 'Default truncation budget for tool results.', 500, 60000)}
        {numField('thinking_budget', 'Daemon thinking budget (tokens)', 'Default extended-thinking budget for the daemon.', 500, 32000)}
      </div>
    </SettingsCard>
  )
}

/* ---------------- Budgets ---------------- */
function maskVk(vk: string): string {
  if (!vk || vk.length <= 8) return vk
  return `${vk.slice(0, 6)}…${vk.slice(-4)}`
}

const ENFORCEMENT_OPTIONS = [
  { value: 'warning', label: 'Warning only — log but allow' },
  { value: 'hard_stop', label: 'Hard stop — block on exceed' },
]

function BudgetsPanel({ notify }: SectionProps) {
  const { settings, quota, phase, reload, save } = useBudgets()
  const [draft, setDraft] = useState(settings)
  const [saving, setSaving] = useState(false)
  const [showVk, setShowVk] = useState(false)

  useEffect(() => { setDraft(settings) }, [settings])

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-8 text-center">Loading budget settings…</div>
  }

  const dirty =
    draft.default_vk !== settings.default_vk ||
    draft.budget_limit_usd !== settings.budget_limit_usd ||
    draft.enforcement_mode !== settings.enforcement_mode

  const handleSave = async () => {
    setSaving(true)
    try {
      await save({
        default_vk: draft.default_vk.trim(),
        budget_limit_usd: Number(draft.budget_limit_usd) || 0,
        enforcement_mode: draft.enforcement_mode,
      })
      notify('ok', 'Budget settings saved.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  const firstBudget = quota?.quota?.budgets?.[0]
  const spendPct =
    firstBudget && firstBudget.max_limit > 0
      ? Math.min(100, Math.round((firstBudget.current_usage / firstBudget.max_limit) * 100))
      : 0
  const barColor = spendPct >= 90 ? 'var(--crit)' : spendPct >= 75 ? 'var(--high)' : 'var(--accent)'

  return (
    <SettingsCard
      title="Budgets (Bifrost virtual key)"
      desc="Bifrost enforces a USD budget per virtual key, upstream of every LLM call. DEV_MODE=true or LLM_BUDGET_UNLIMITED=true bypasses enforcement."
      actions={
        <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
      }
    >
      {quota?.configured && quota.available && firstBudget && (
        <div className="mb-4">
          <div className="flex justify-between mb-1.5 text-sm">
            <span><strong>${firstBudget.current_usage.toFixed(2)}</strong> spent of <strong>${firstBudget.max_limit.toFixed(2)}</strong></span>
            <span className="text-xs text-tx-3">{firstBudget.reset_duration} cycle · resets {firstBudget.last_reset || '—'}</span>
          </div>
          <div className="h-1.5 rounded-full bg-[var(--bg-3)] overflow-hidden">
            <div className="h-full rounded-full" style={{ width: `${spendPct}%`, background: barColor }} />
          </div>
        </div>
      )}
      {quota?.configured && !quota.available && (
        <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {quota.message || "Bifrost is unreachable or the configured VK doesn't exist."}</div>
      )}
      {!quota?.configured && (
        <div className="settings-banner info mb-3"><Icon name="info" size={14} /> {quota?.message || 'No virtual key configured. Provision one in the Bifrost UI, then paste its ID below.'}</div>
      )}

      <div className="flex flex-col gap-3.5 max-w-[560px]">
        <Field label="Default virtual key (sk-bf-…)" hint="Read from x-bf-vk on every upstream LLM call. Empty = bootstrap mode (no enforcement).">
          <TextInput
            value={showVk ? draft.default_vk : maskVk(draft.default_vk)}
            onFocus={() => setShowVk(true)}
            onBlur={() => setShowVk(false)}
            onChange={(e) => setDraft({ ...draft, default_vk: e.target.value })}
          />
        </Field>
        <Field label="Monthly budget ceiling ($)" hint="Reference value for the dashboard. Keep in sync with the VK's Bifrost ceiling.">
          <NumberInput
            value={draft.budget_limit_usd}
            onChange={(e) => setDraft({ ...draft, budget_limit_usd: Number(e.target.value) })}
          />
        </Field>
        <Field label="Enforcement mode">
          <Select
            value={draft.enforcement_mode}
            options={ENFORCEMENT_OPTIONS}
            onSelect={(v) => setDraft({ ...draft, enforcement_mode: v as 'warning' | 'hard_stop' })}
          />
        </Field>
        <div>
          <button className="btn primary" disabled={!dirty || saving} onClick={handleSave}>
            <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </SettingsCard>
  )
}
