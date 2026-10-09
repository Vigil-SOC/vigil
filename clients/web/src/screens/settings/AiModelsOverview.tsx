/* ============================================================
   Settings · AI models, top half — where your data goes, one card per
   gateway provider, and which model each agent uses.

   Providers and keys come from Bifrost; assignments from Vigil's own
   ai_model_configs. Hosted-or-local is the setup wizard's rule
   (providerResidency), not a second one.
   ============================================================ */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, EmptyState, Select, SettingsCard, TextInput } from '../../shared/ui'
import { LevelBadge } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import { agentsApi, type AIModelInfo, type ComponentAssignment } from '../../services/api'
import type { BifrostKey } from '../../services/bifrostApi'
import { bifrostStaysOnSite, residencyCopy } from '../setup/providerResidency'
import { useToast } from '../../shell/toast'
import { useBifrostProviders } from './useBifrost'
import { useModelAssignment } from './useSettings'
import { COMPONENT_LABELS, CHAT_DEFAULT_KEY, FALLBACK_KEY } from '../../config/aiComponents'
import type { SectionProps } from './types'
import './aiModels.css'

const PROVIDER_LABELS: Record<string, string> = {
  openai: 'OpenAI',
  bedrock: 'AWS Bedrock',
  vertex: 'Google Vertex',
  azure: 'Azure OpenAI',
  xai: 'xAI',
  openrouter: 'OpenRouter',
  together_ai: 'Together AI',
}
const providerLabel = (name: string) =>
  PROVIDER_LABELS[name] || name.charAt(0).toUpperCase() + name.slice(1)

/** The gateway provider behind the chat_default assignment. Assignments name an
    llm_provider_configs row ("anthropic-default"); the gateway names the upstream. */
function defaultProviderName(
  assignment: ComponentAssignment | undefined,
  models: AIModelInfo[],
  names: string[],
): string | null {
  if (!assignment) return null
  const type = models.find((m) => m.provider_id === assignment.provider_id)?.provider_type
  const pid = assignment.provider_id
  return (
    names.find((n) => n === type) ||
    names.find((n) => n === pid || pid.startsWith(`${n}-`)) ||
    null
  )
}

/** Anchor for links that open Settings at the per-agent model table
 *  (Home's "Pick a model per agent" step sends ?tab=assignment, which AiConfigSection scrolls to). */
export const AGENT_MODEL_TABLE_ID = 'ai-model-for-each-agent'

export default function AiModelsOverview({ notify }: SectionProps) {
  const ma = useModelAssignment()
  const bf = useBifrostProviders()
  const names = bf.providers.map((p) => p.name)
  const defaultName = defaultProviderName(ma.assignments[CHAT_DEFAULT_KEY], ma.models, names)

  return (
    <>
      {bf.phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading providers…" />}
      {bf.phase === 'error' && (
        <EmptyState error compact icon="alert" title="Couldn’t reach the Bifrost gateway" body={bf.error} primary={{ label: 'Retry', onClick: bf.reload, icon: 'refresh' }} />
      )}
      {bf.phase === 'ready' && names.length === 0 && (
        <EmptyState compact icon="sparkle" title="No providers yet" body="Add a provider under Keys below, then pick the models Vigil uses." />
      )}
      {bf.phase === 'ready' && names.length > 0 && (
        <>
          <ResidencyBanner names={names} keys={bf.keys} />
          <div className="aim-cards">
            {names.map((name) => (
              <ProviderCard
                key={name}
                name={name}
                keyCount={(bf.keys[name] || []).length}
                stays={bifrostStaysOnSite(name, bf.keys[name] || [])}
                isDefault={name === defaultName}
                // null verdicts is "could not ask" — no badge, never Poor
                routable={bf.verdicts ? !!bf.verdicts.providers?.[name] : null}
              />
            ))}
          </div>
        </>
      )}
      <AgentModelTable ma={ma} notify={notify} />
    </>
  )
}

function ResidencyBanner({ names, keys }: { names: string[]; keys: Record<string, BifrostKey[]> }) {
  const stays = (n: string) => bifrostStaysOnSite(n, keys[n] || [])
  const hosted = names.filter((n) => !stays(n)).map(providerLabel)
  const local = names.filter(stays).map(providerLabel)
  return (
    <div className="aim-banner" role="status">
      <Icon name="info" size={17} />
      <span>
        <b>Where your data goes:</b>
        {hosted.length > 0 && ` hosted models (${hosted.join(', ')}): ${residencyCopy(false)}.`}
        {local.length > 0 && ` Local models (${local.join(', ')}): ${residencyCopy(true)}.`}
        {' '}Changing the default model asks you to confirm and is logged.
      </span>
    </div>
  )
}

function ProviderCard({ name, keyCount, stays, isDefault, routable }: {
  name: string
  keyCount: number
  stays: boolean
  isDefault: boolean
  routable: boolean | null
}) {
  const keys = keyCount > 0
    ? `${keyCount} ${keyCount === 1 ? 'key' : 'keys'} · via the gateway`
    : stays ? 'Local server · no key' : 'Not set up'
  return (
    <div className={`aim-card${isDefault ? ' default' : ''}`}>
      <div className="aim-card-top">
        <span className="aim-card-name">
          {providerLabel(name)}
          {isDefault && <small> · default</small>}
        </span>
        {routable !== null && <LevelBadge variant="pill" level={routable ? 'good' : 'poor'} />}
      </div>
      <span className="aim-card-keys">{keys}</span>
      <span className={`aim-card-where${stays ? ' local' : ''}`}>
        {stays ? 'Local' : 'Hosted'}: {residencyCopy(stays)}
      </span>
    </div>
  )
}

/* ---------------- Model for each agent ---------------- */
// '' is the model's own default effort: the key is left out of settings.
type Effort = '' | 'low' | 'medium' | 'high'
const EFFORT_OPTIONS: { value: string; label: string }[] = [
  { value: 'default', label: 'Model default' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]
// First choice in an editing row's provider Select; picking it clears the assignment.
const USE_DEFAULT = '__use_default__'
const RESET_KEY = 'ai-model-reset'
interface RowState { inherit: boolean; providerId: string; modelId: string; effort: Effort }

const savedEffort = (a: ComponentAssignment | undefined): Effort => (a?.settings?.effort as Effort) || ''

const rowFor = (c: string, a: ComponentAssignment | undefined): RowState =>
  a
    ? { inherit: false, providerId: a.provider_id, modelId: a.model_id, effort: savedEffort(a) }
    : { inherit: c !== CHAT_DEFAULT_KEY, providerId: '', modelId: '', effort: '' }

interface CustomAgentRow {
  id: string
  name: string
  model: string
  fallback_model: string
  saved_model: string
  saved_fallback_model: string
  thinking: boolean
}

function AgentModelTable({ ma, notify }: { ma: ReturnType<typeof useModelAssignment> } & SectionProps) {
  const { components, assignments, models, phase, error, reload, assign, clearAssign } = ma
  const { notifyUndoable, pending: fusing, settled } = useToast()
  const [rows, setRows] = useState<Record<string, RowState>>({})
  const [editing, setEditing] = useState<string | null>(null)
  const [pending, setPending] = useState<{ component: string; next: RowState } | null>(null)

  const modelsByProvider = useMemo(() => {
    const grouped: Record<string, AIModelInfo[]> = {}
    for (const m of models) (grouped[m.provider_id] ||= []).push(m)
    return grouped
  }, [models])
  const providerIds = useMemo(() => Object.keys(modelsByProvider).sort(), [modelsByProvider])

  useEffect(() => {
    if (phase !== 'ready') return
    setRows(Object.fromEntries(components.map((c) => [c, rowFor(c, assignments[c])])))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  // A fused reset commits in the toast provider; reload once it settles so the rows re-seed.
  const seenSettled = useRef(settled)
  useEffect(() => {
    if (seenSettled.current === settled) return
    seenSettled.current = settled
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled])

  // Components with a save in flight. A row's lock must not clear another row's.
  const [saving, setSaving] = useState<string[]>([])
  const lock = (component: string, on: boolean) =>
    setSaving((s) => (on ? [...s, component] : s.filter((x) => x !== component)))
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
      if (a && a.provider_id === next.providerId && a.model_id === next.modelId && savedEffort(a) === next.effort) return
      lock(component, true) // the fallback PUT would carry this row's old model
      try {
        // Other settings keys are carried over; the PUT replaces the whole object.
        const rest = { ...a?.settings }
        delete rest.effort
        // A fallback never crosses a provider or equals the new model; null clears it.
        if (rest[FALLBACK_KEY] && (a?.provider_id !== next.providerId || rest[FALLBACK_KEY] === next.modelId)) {
          rest[FALLBACK_KEY] = null
        }
        await assign(component, next.providerId, next.modelId, next.effort ? { ...rest, effort: next.effort } : rest)
      } finally {
        lock(component, false)
      }
      notify('ok', `${component} saved.`)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || `Failed to save ${component}.`)
    }
  }

  // Saves on select, no confirm: a fallback only acts once the model is unavailable.
  const setFallback = async (component: string, fallback: string) => {
    const a = assignments[component]
    if (!a) return
    lock(component, true)
    try {
      // The PUT replaces the whole settings object, so the effort rides along.
      await assign(component, a.provider_id, a.model_id, { ...a.settings, [FALLBACK_KEY]: fallback || null })
      notify('ok', `${component} fallback ${fallback ? 'saved' : 'cleared'}.`)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || `Failed to save ${component} fallback.`)
    } finally {
      lock(component, false)
    }
  }

  // Whether saving `next` would change what is stored.
  const changes = (component: string, next: RowState) => {
    const a = assignments[component]
    if (next.inherit) return a !== undefined
    return (
      !!next.providerId &&
      !!next.modelId &&
      (!a || a.provider_id !== next.providerId || a.model_id !== next.modelId || savedEffort(a) !== next.effort)
    )
  }

  const update = (component: string, patch: Partial<RowState>) => {
    const next = { ...(rows[component] ?? rowFor(component, undefined)), ...patch }
    setRows((prev) => ({ ...prev, [component]: next }))
    // chat_default is every unset component's fallback, so it asks first
    if (component === CHAT_DEFAULT_KEY && changes(component, next)) setPending({ component, next })
    else persist(component, next)
  }

  const resetting = fusing.includes(RESET_KEY)
  // Rows with their own assignment, Chat (Default) aside; these are what Reset clears.
  const resettable = components.filter((c) => c !== CHAT_DEFAULT_KEY && assignments[c])
  // The assignment a row shows: none while its reset is pending, so the row reads as Use default.
  const ownOf = (c: string) => (resetting && c !== CHAT_DEFAULT_KEY ? undefined : assignments[c])
  const inheritsOf = (c: string) => c !== CHAT_DEFAULT_KEY && !ownOf(c)

  const resetAll = () =>
    notifyUndoable({
      key: RESET_KEY,
      text: `${resettable.length} ${resettable.length === 1 ? 'agent' : 'agents'} back on the default model.`,
      commit: () => Promise.all(resettable.map((c) => clearAssign(c))),
      doneText: 'Agents reset to the default model.',
      failText: (e) => (e as { message?: string })?.message || 'Failed to reset the agents to the default model.',
    })

  const stopEditing = (c: string) => {
    setRows((prev) => ({ ...prev, [c]: rowFor(c, assignments[c]) }))
    setEditing(null)
  }

  // On window, not the cell: an open Select swallows Escape on document first, so one Escape
  // closes its menu and the next ends the edit.
  useEffect(() => {
    if (!editing) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && stopEditing(editing)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, assignments])

  const pickProvider = (c: string, v: string) =>
    v === USE_DEFAULT
      ? (update(c, { inherit: true, providerId: '', modelId: '', effort: '' }), setEditing(null))
      : update(c, { inherit: false, providerId: v, modelId: '' })

  const pickModel = (c: string, v: string) => {
    update(c, { modelId: v })
    setEditing(null)
  }

  const cancel = () => {
    if (pending) setRows((prev) => ({ ...prev, [pending.component]: rowFor(pending.component, assignments[pending.component]) }))
    setPending(null)
  }

  const confirm = () => {
    if (pending) persist(pending.component, pending.next)
    setPending(null)
  }

  // An inheriting row runs on chat_default's row, so it shows that fallback
  const fallbackOf = (c: string) =>
    (assignments[inheritsOf(c) ? CHAT_DEFAULT_KEY : c]?.settings?.[FALLBACK_KEY] as string | undefined) || ''
  // The row's provider models but the selected one; a stored fallback the provider
  // no longer lists stays in the list so it still shows
  const fallbackModels = (c: string) => {
    const a = assignments[inheritsOf(c) ? CHAT_DEFAULT_KEY : c]
    const list = (modelsByProvider[a?.provider_id ?? ''] || []).filter((m) => m.model_id !== a?.model_id)
    const stored = fallbackOf(c)
    if (stored && !list.some((m) => m.model_id === stored)) list.push({ model_id: stored } as AIModelInfo)
    return list
  }

  const custom = useCustomAgents(notify)

  return (
    <SettingsCard
      wide
      id={AGENT_MODEL_TABLE_ID}
      title="Model for each agent"
      desc="Pick a model per agent, and optionally how hard it thinks, or leave it on the default. Unassigned rows use Chat (Default). Workflow runs use the investigation assignment."
      actions={
        <button className="btn ghost" disabled={resettable.length === 0 || resetting} onClick={resetAll}>
          Reset to defaults
        </button>
      }
    >
      {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading AI config…" />}
      {phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load AI config" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />}
      {phase === 'ready' && (
        <>
          {providerIds.length === 0 && (
            <EmptyState compact icon="sparkle" title="No assignable models discovered" body="Add and test at least one active provider before assigning models to Vigil components." />
          )}
          <div className="table-wrap">
            <table className="tbl aim-table">
              <thead>
                <tr>
                  <th>Who uses it</th>
                  <th>Model</th>
                  <th>If it is unavailable</th>
                  <th>Thinking</th>
                  <th>Cost per case</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {components.map((c) => {
                  const meta = COMPONENT_LABELS[c] || { label: c, description: '' }
                  const row = rows[c] || rowFor(c, undefined)
                  const providerModels = row.providerId ? modelsByProvider[row.providerId] || [] : []
                  const inherits = inheritsOf(c)
                  const shown = ownOf(c) ?? assignments[CHAT_DEFAULT_KEY]
                  const shownModel = shown && models.find((m) => m.provider_id === shown.provider_id && m.model_id === shown.model_id)
                  return (
                    <tr key={c}>
                      <td style={{ minWidth: 200, maxWidth: 300 }}>
                        <div className="aim-who">{meta.label}</div>
                        <div className="aim-note">{meta.description}</div>
                      </td>
                      <td style={{ minWidth: 340 }}>
                        {editing === c ? (
                          <div className="aim-model">
                            <Select
                              value={row.inherit ? USE_DEFAULT : row.providerId}
                              placeholder="Select provider"
                              disabled={saving.includes(c)}
                              options={[
                                ...(c === CHAT_DEFAULT_KEY ? [] : [{ value: USE_DEFAULT, label: 'Use default' }]),
                                ...providerIds.map((pid) => ({ value: pid, label: pid })),
                              ]}
                              onSelect={(v) => pickProvider(c, v)}
                            />
                            <Select
                              value={row.modelId}
                              placeholder="Select model"
                              searchable
                              disabled={saving.includes(c) || row.inherit}
                              options={providerModels.map((m) => ({ value: m.model_id, label: m.display_name || m.model_id }))}
                              onSelect={(v) => pickModel(c, v)}
                            />
                          </div>
                        ) : shown ? (
                          <div className="aim-model-text">
                            <span className="aim-model-name">
                              {shownModel?.display_name || shown.model_id}
                              {inherits && <span className="aim-default-chip">default</span>}
                            </span>
                            <span className="aim-note">{shown.provider_id}</span>
                          </div>
                        ) : (
                          <span className="aim-muted">Not set</span>
                        )}
                      </td>
                      <td>
                        <div className="aim-fallback">
                        <Select
                          value={fallbackOf(c)}
                          placeholder="Stops"
                          disabled={!ownOf(c) || saving.includes(c)}
                          options={[
                            { value: '', label: 'Stops' },
                            ...fallbackModels(c).map((m) => ({ value: m.model_id, label: m.display_name || m.model_id })),
                          ]}
                          onSelect={(v) => setFallback(c, v)}
                        />
                        </div>
                      </td>
                      <td style={{ minWidth: 120 }}>
                        {inherits ? (
                          <span className="aim-muted">—</span>
                        ) : (
                          <Select
                            value={row.effort || 'default'}
                            options={EFFORT_OPTIONS}
                            disabled={saving.includes(c)}
                            onSelect={(v) => update(c, { effort: v === 'default' ? '' : (v as Effort) })}
                          />
                        )}
                      </td>
                      <td className="aim-nowrap"><NotMeasured /></td>
                      <td className="aim-nowrap">
                        <button
                          className="btn ghost"
                          disabled={resetting && c !== CHAT_DEFAULT_KEY}
                          onClick={() => (editing === c ? stopEditing(c) : setEditing(c))}
                        >
                          {editing === c ? 'Cancel' : 'Change'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
                {custom.rows.map((row) => (
                  <tr key={row.id}>
                    <td style={{ minWidth: 220, maxWidth: 300 }}>
                      <div className="aim-who">{row.name}</div>
                      <div className="aim-note">Custom agent · {row.id}</div>
                    </td>
                    <td>
                      <TextInput
                        value={row.model}
                        placeholder="Assignment model"
                        onChange={(e) => custom.edit(row.id, { model: e.target.value })}
                        onBlur={(e) => custom.save(row, 'model', e.target.value)}
                      />
                    </td>
                    <td>
                      <TextInput
                        value={row.fallback_model}
                        placeholder="Stops"
                        onChange={(e) => custom.edit(row.id, { fallback_model: e.target.value })}
                        onBlur={(e) => custom.save(row, 'fallback_model', e.target.value)}
                      />
                    </td>
                    <td>{row.thinking ? 'On' : 'Off'}</td>
                    <td className="aim-nowrap"><NotMeasured /></td>
                    <td />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {custom.phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading custom agents…" />}
          {custom.phase === 'error' && (
            <EmptyState error compact icon="alert" title="Couldn’t load custom agents" body={custom.error} primary={{ label: 'Retry', onClick: custom.load, icon: 'refresh' }} />
          )}
        </>
      )}
      <ConfirmDialog
        open={!!pending}
        danger={false}
        title="Change the default model?"
        body="Chat (Default) is the model every agent without its own assignment falls back to, including custom agents. The change is logged with your name."
        confirmLabel="Change default"
        onConfirm={confirm}
        onClose={cancel}
      />
    </SettingsCard>
  )
}

function useCustomAgents(notify: SectionProps['notify']) {
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [rows, setRows] = useState<CustomAgentRow[]>([])

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
          enable_thinking?: boolean | null
        }>
        setRows(
          agents.map((a) => ({
            id: a.id,
            name: a.name || a.id,
            model: a.model || '',
            fallback_model: a.fallback_model || '',
            saved_model: a.model || '',
            saved_fallback_model: a.fallback_model || '',
            thinking: !!a.enable_thinking,
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

  const edit = (id: string, patch: Partial<CustomAgentRow>) =>
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)))

  const save = async (row: CustomAgentRow, field: 'model' | 'fallback_model', value: string) => {
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

  return { phase, error, rows, load, edit, save }
}
