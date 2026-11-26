/* ============================================================
   Settings · AI models, top half — where your data goes, one card per
   gateway provider, and which model each agent uses.

   Providers and keys come from Bifrost; assignments from Vigil's own
   ai_model_configs. Where each provider's calls go is the server's class
   (core/llm/residency.py, on the routability read), labelled by shared/residency.
   ============================================================ */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, EmptyState, Field, Popup, Select, SettingsCard } from '../../shared/ui'
import { LevelBadge } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import {
  aiConfigApi,
  autonomyApi,
  llmProviderApi,
  type AIModelInfo,
  type ModelChangePreview,
  type ComponentAssignment,
  type LLMProvider,
  type StatementView,
} from '../../services/api'
import type { BifrostProvider, BifrostRoutability } from '../../services/bifrostApi'
import {
  RESIDENCY_LABEL,
  residencyLabel,
  residencyOf,
  type Residency,
  type ResidencyClass,
  type SelfHostedBy,
} from '../../shared/residency'
import { useAuth } from '../../contexts/AuthContext'
import { useToast } from '../../shell/toast'
import { useBifrostProviders } from './useBifrost'
import { ROUTABILITY_CHANGED } from './routabilityChanged'
import { useInvestigateWorkflows, useModelAssignment } from './useSettings'
import { dropWhy } from '../../shared/autonomy'
import { COMPONENT_LABELS, CHAT_DEFAULT_KEY, FALLBACK_KEY } from '../../config/aiComponents'
import { InvestigationReplay } from './InvestigationReplay'
import { PICK_AGENT_MODEL } from './modelPrompt'
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
  anthropic: 'Anthropic',
  ollama: 'Ollama',
  gemini: 'Gemini',
  mistral: 'Mistral',
  cohere: 'Cohere',
  groq: 'Groq',
  perplexity: 'Perplexity',
  cerebras: 'Cerebras',
  huggingface: 'Hugging Face',
  nebius: 'Nebius',
  parasail: 'Parasail',
  replicate: 'Replicate',
}
// stock providers have a display name; a custom one keeps the name it was typed with, as the Keys table shows it
const providerLabel = (name: string) => PROVIDER_LABELS[name] || name

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

/** Vigil's provider rows, for the Provider Statements each type holds. */
function useProviderRows() {
  const [rows, setRows] = useState<LLMProvider[] | null>(null)
  const reload = useCallback(() => {
    llmProviderApi
      .list()
      .then(({ data }) => setRows(data))
      .catch(() => setRows(null))
  }, [])
  useEffect(reload, [reload])
  return { rows, reload }
}

/** The row a statement save goes to: the gateway mirror first, else the type's first active row.
 *  The server writes it onto every active row of the type. */
function statementRow(rows: LLMProvider[] | null, name: string): LLMProvider | null {
  const own = (rows || []).filter((r) => r.provider_type === name)
  return (
    own.find((r) => r.provider_id === `bifrost-${name}` && r.is_active) ||
    own.find((r) => r.is_active) ||
    own[0] ||
    null
  )
}

function selfHostedOf(row: LLMProvider | null): SelfHostedBy | null {
  const s = row?.statements?.self_hosted
  return s && s.state === 'held' && s.value === 'yes' && s.by_name && s.at ? { by: s.by_name, at: s.at } : null
}

export default function AiModelsOverview({ notify }: SectionProps) {
  const ma = useModelAssignment()
  const bf = useBifrostProviders()
  const pr = useProviderRows()
  // A write in the Keys panel below (its own hook) can change a provider's class.
  const { reload } = bf
  const { reload: reloadRows } = pr
  useEffect(() => {
    const both = () => {
      reload()
      reloadRows()
    }
    window.addEventListener(ROUTABILITY_CHANGED, both)
    return () => window.removeEventListener(ROUTABILITY_CHANGED, both)
  }, [reload, reloadRows])
  const names = bf.shown.map((p) => p.name)
  const defaultName = defaultProviderName(ma.assignments[CHAT_DEFAULT_KEY], ma.models, names)
  // ?tab=statements&provider=<type> (the Day 1 Report's link) opens that provider's statements.
  const [params, setParams] = useSearchParams()
  const [stating, setStating] = useState<string | null>(null)
  const linked = params.get('tab') === 'statements' ? params.get('provider') : null
  useEffect(() => {
    if (linked && pr.rows && names.includes(linked)) setStating(linked)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linked, pr.rows, names.join()])
  const closeStating = () => {
    setStating(null)
    if (linked) {
      const next = new URLSearchParams(params)
      next.delete('tab')
      next.delete('provider')
      setParams(next, { replace: true })
    }
  }

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
          <ResidencyBanner
            providers={bf.shown}
            verdicts={bf.verdicts}
            selfHosted={(name) => selfHostedOf(statementRow(pr.rows, name))}
          />
          <div className="aim-cards">
            {bf.shown.map((provider) => (
              <ProviderCard
                key={provider.name}
                provider={provider}
                keyCount={(bf.keys[provider.name] || []).length}
                residency={residencyOf(bf.verdicts?.residency?.[provider.name])}
                selfHosted={selfHostedOf(statementRow(pr.rows, provider.name))}
                isDefault={provider.name === defaultName}
                // null verdicts is "could not ask" — no badge, never Poor
                routable={bf.verdicts ? !!bf.verdicts.providers?.[provider.name] : null}
                onStatements={pr.rows ? () => setStating(provider.name) : undefined}
              />
            ))}
          </div>
        </>
      )}
      {stating && (
        <StatementsDialog
          name={providerLabel(stating)}
          row={statementRow(pr.rows, stating)}
          onClose={closeStating}
          onSaved={() => {
            pr.reload()
            notify('ok', `Statements for ${providerLabel(stating)} saved.`)
            closeStating()
          }}
        />
      )}
      <AgentModelTable ma={ma} notify={notify} />
    </>
  )
}

// One sentence per class, each naming its providers; unknown names its reason.
const BANNER_GROUPS: { cls: ResidencyClass; text: (names: string) => string }[] = [
  { cls: 'hosted', text: (n) => ` Hosted models (${n}): data leaves your site.` },
  { cls: 'your_cloud', text: (n) => ` In your cloud account: ${n}.` },
  { cls: 'local_by_address', text: (n) => ` ${RESIDENCY_LABEL.local_by_address}: ${n}.` },
]

function ResidencyBanner({ providers, verdicts, selfHosted }: {
  providers: BifrostProvider[]
  verdicts: BifrostRoutability | null
  selfHosted: (name: string) => SelfHostedBy | null
}) {
  const of = (p: BifrostProvider) => residencyOf(verdicts?.residency?.[p.name])
  // a held self-hosted statement takes the provider out of its class, as on its card
  const stated = providers.filter((p) => of(p).class !== 'unknown' && selfHosted(p.name))
  const named = (cls: ResidencyClass) =>
    providers.filter((p) => of(p).class === cls && !stated.includes(p)).map((p) => providerLabel(p.name))
  // unknown providers grouped by the server's reason
  const unknown = new Map<string, string[]>()
  for (const p of providers.filter((p) => of(p).class === 'unknown')) {
    const why = residencyLabel(of(p)).replace(/^Not known: /, '')
    unknown.set(why, [...(unknown.get(why) || []), providerLabel(p.name)])
  }
  return (
    <div className="aim-banner" role="status">
      <Icon name="info" size={17} />
      <span>
        <b>Where your data goes:</b>
        {BANNER_GROUPS.map(({ cls, text }) => {
          const names = named(cls)
          return names.length > 0 && <Fragment key={cls}>{text(names.join(', '))}</Fragment>
        })}
        {stated.length > 0 && ` Self-hosted, as stated: ${stated.map((p) => providerLabel(p.name)).join(', ')}.`}
        {[...unknown].map(([why, names]) => (
          <Fragment key={why}>{` Not known for ${names.join(', ')}: ${why}.`}</Fragment>
        ))}
        {' '}Changing the default model asks you to confirm and is logged.
      </span>
    </div>
  )
}

/** host:port of a stored base URL, no scheme or path; null when absent or unparseable */
function hostOf(url: string | undefined): string | null {
  if (!url) return null
  try {
    return new URL(url).host || null
  } catch {
    return null
  }
}

function ProviderCard({ provider, keyCount, residency, selfHosted, isDefault, routable, onStatements }: {
  provider: BifrostProvider
  keyCount: number
  residency: Residency
  selfHosted: SelfHostedBy | null
  isDefault: boolean
  routable: boolean | null
  onStatements?: () => void
}) {
  const { name } = provider
  const url = provider.network_config?.base_url
  const host = hostOf(url)
  const keyLess = !!provider.custom_provider_config?.is_key_less
  const count = `${keyCount} ${keyCount === 1 ? 'key' : 'keys'}`
  let keys: string
  if (keyCount > 0) keys = `${count} · ${host ?? 'via the gateway'}`
  else if (keyLess && host) keys = `No key needed · ${host}`
  else keys = 'Not set up'
  return (
    <div className={`aim-card${isDefault ? ' default' : ''}`}>
      <div className="aim-card-top">
        <span className="aim-card-name">
          {providerLabel(name)}
          {isDefault && <small> · default</small>}
        </span>
        {routable !== null && (keyCount > 0 || keyLess) && <LevelBadge variant="pill" level={routable ? 'good' : 'poor'} />}
      </div>
      <span className="aim-card-keys" title={host ? url : undefined}>{keys}</span>
      <span className={`aim-card-where ${selfHosted ? 'self_hosted' : residency.class}`}>
        {residencyLabel(residency, selfHosted)}
      </span>
      {onStatements && (
        <button type="button" className="btn ghost aim-card-statements" onClick={onStatements}>
          <Icon name="edit" size={14} /> Statements
        </button>
      )}
    </div>
  )
}

/* ---------------- Provider Statements ---------------- */
// Each statement's list, "Not stated" first. The ids match core/llm/statements.py VALUES.
const STATEMENT_FIELDS: { name: string; label: string; hint: string; options: { value: string; label: string }[] }[] = [
  {
    name: 'self_hosted',
    label: 'Self-hosted',
    hint: 'The model runs on hardware you control. An address on your network is not this statement.',
    options: [
      { value: 'yes', label: 'Yes, on hardware we control' },
      { value: 'no', label: 'No' },
    ],
  },
  {
    name: 'baa',
    label: 'Business associate agreement',
    hint: 'A BAA signed with this provider (yours as covered entity or business associate).',
    options: [
      { value: 'signed', label: 'Signed' },
      { value: 'none', label: 'None' },
    ],
  },
  {
    name: 'hipaa_account',
    label: 'HIPAA-eligible account mode',
    hint: 'The account is in the provider’s HIPAA-eligible mode.',
    options: [
      { value: 'on', label: 'On' },
      { value: 'off', label: 'Off' },
    ],
  },
  {
    name: 'region',
    label: 'Processing and support region',
    hint: 'Where the provider processes and supports this account.',
    options: [
      { value: 'us', label: 'United States' },
      { value: 'eu_eea', label: 'EU/EEA' },
      { value: 'other', label: 'Elsewhere' },
    ],
  },
  {
    name: 'fedramp',
    label: 'FedRAMP authorization',
    hint: 'The provider’s FedRAMP authorization for this service.',
    options: [
      { value: 'high', label: 'High' },
      { value: 'moderate', label: 'Moderate' },
      { value: 'low', label: 'Low' },
      { value: 'none', label: 'None' },
    ],
  },
  {
    name: 'transfer_basis',
    label: 'EU transfer basis',
    hint: 'What covers transfers of EU personal data to this provider.',
    options: [
      { value: 'adequacy', label: 'Adequacy decision covering the processing country' },
      { value: 'dpf', label: 'EU–US Data Privacy Framework, provider on its list' },
      { value: 'scc', label: 'Standard contractual clauses' },
      { value: 'bcr', label: 'Binding corporate rules' },
      { value: 'art49', label: 'Art. 49 derogation' },
      { value: 'none', label: 'None' },
    ],
  },
]
const NOT_STATED = 'not_stated'

/** What a statement stands on now, in a sentence. */
function standingText(s: StatementView | undefined, field: (typeof STATEMENT_FIELDS)[number]): string | null {
  if (!s || s.state === 'absent') return s?.why ? `Not stated: ${s.why}.` : null
  const who = s.by_name && s.at ? `stated by ${s.by_name} on ${s.at.slice(0, 10)}` : 'stated'
  const Who = who.charAt(0).toUpperCase() + who.slice(1)
  if (s.state === 'held') return `${Who}.`
  if (s.state === 'cleared') {
    const was = field.options.find((o) => o.value === s.value)?.label ?? s.value
    return `Was “${was}”, ${who}, then cleared: ${s.why}. Pick it again to state it.`
  }
  // a flag from before statements has no author: the reason says so
  if (!s.by_name && s.why) return `${s.why.charAt(0).toUpperCase()}${s.why.slice(1)}.`
  return `${Who}; ${s.why}.`
}

function StatementsDialog({ name, row, onClose, onSaved }: {
  name: string
  row: LLMProvider | null
  onClose: () => void
  onSaved: () => void
}) {
  const { hasPermission } = useAuth()
  const canEdit = hasPermission('platform.write')
  const stored = row?.statements || {}
  // A standing statement shows its value; a cleared one starts at Not stated.
  const shownValue = (n: string) => {
    const s = stored[n]
    return (s?.state === 'held' || s?.state === 'pending') && s.value ? s.value : NOT_STATED
  }
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(STATEMENT_FIELDS.map((f) => [f.name, shownValue(f.name)])),
  )
  // Pending statements the person chose to re-confirm unchanged.
  const [confirm, setConfirm] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // What a save writes: only what this person changed or chose to re-confirm.
  const written = Object.fromEntries(
    STATEMENT_FIELDS.filter(
      ({ name: n }) => draft[n] !== shownValue(n) || (confirm.includes(n) && draft[n] !== NOT_STATED),
    ).map(({ name: n }) => [n, draft[n]]),
  )
  const submit = async () => {
    if (!row) return
    setSaving(true)
    setErr(null)
    try {
      await llmProviderApi.state(row.provider_id, written)
      onSaved()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      setErr(typeof detail === 'string' ? detail : 'The statements were not saved.')
    } finally {
      setSaving(false)
    }
  }
  return (
    <Popup open onClose={onClose} title={`Statements · ${name}`} width={520}>
      <p className="text-sm text-tx-2 mb-3">
        What you state about this provider, for the Day 1 Report’s check on where model processing runs. Each
        statement is tied to the provider’s address: a new address clears it, and a new key asks for it to be
        re-confirmed. Saving records your name and the date on the audit trail.
      </p>
      {!row && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> This provider has no row in Vigil yet. Add a key it can route with first.</div>}
      {err && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {err}</div>}
      <div className="aim-statements">
        {STATEMENT_FIELDS.map((f) => {
          const standing = standingText(stored[f.name], f)
          const pending = stored[f.name]?.state === 'pending' && draft[f.name] === shownValue(f.name)
          const confirming = confirm.includes(f.name)
          return (
            <div key={f.name} className="aim-statement">
              <Field label={f.label} hint={standing ? `${f.hint} ${standing}` : f.hint}>
                <Select
                  label={f.label}
                  value={draft[f.name]}
                  disabled={!canEdit || !row || saving}
                  options={[{ value: NOT_STATED, label: 'Not stated' }, ...f.options]}
                  onSelect={(v) => setDraft((d) => ({ ...d, [f.name]: v }))}
                />
              </Field>
              {pending && canEdit && (
                <button
                  type="button"
                  className="btn ghost"
                  aria-pressed={confirming}
                  disabled={saving}
                  onClick={() => setConfirm((c) => (confirming ? c.filter((n) => n !== f.name) : [...c, f.name]))}
                >
                  <Icon name={confirming ? 'check2' : 'refresh'} size={14} /> {confirming ? 'Re-confirmed on save' : 'Re-confirm'}
                </button>
              )}
            </div>
          )
        })}
      </div>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        {canEdit && (
          <button className="btn primary" disabled={saving || !row || Object.keys(written).length === 0} onClick={submit}>
            <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
          </button>
        )}
      </div>
    </Popup>
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

function errorDetail(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : (e as { message?: string })?.message || 'no reason given'
}

/** The Act workflows a model change would drop to Assist, each with the judgement's reason. */
function DropList({ preview, label }: { preview: ModelChangePreview; label: string }) {
  const { workflows } = useInvestigateWorkflows()
  const name = (id: string) => workflows?.find((w) => w.id === id)?.name ?? id
  const dropping = preview.workflows.filter((w) => w.outcome === 'drop')
  return (
    <>
      <p className="aim-replay-line">
        Saving {label} on {preview.model_id} moves investigation from {preview.investigation_model_before ?? 'an unresolved model'} to{' '}
        {preview.investigation_model_after ?? 'an unresolved model'}. {dropping.length === 1 ? 'This workflow' : `These ${dropping.length} workflows`} would
        drop from Act to Assist:
      </p>
      <ul className="aim-replay-cases">
        {dropping.map((w) => (
          <li key={w.workflow_id}>
            <span className="aim-replay-id">{name(w.workflow_id)}</span>
            {' · '}
            {dropWhy(w.reason)}
          </li>
        ))}
      </ul>
    </>
  )
}

function AgentModelTable({ ma, notify }: { ma: ReturnType<typeof useModelAssignment> } & SectionProps) {
  const { components, assignments, models, phase, error, reload, assign, clearAssign } = ma
  const { notifyUndoable, pending: fusing, settled } = useToast()
  const [rows, setRows] = useState<Record<string, RowState>>({})
  const [editing, setEditing] = useState<string | null>(null)
  const editingRef = useRef<string | null>(null)
  editingRef.current = editing
  const [pending, setPending] = useState<{ component: string; next: RowState } | null>(null)

  const modelsByProvider = useMemo(() => {
    const grouped: Record<string, AIModelInfo[]> = {}
    for (const m of models) (grouped[m.provider_id] ||= []).push(m)
    return grouped
  }, [models])
  const providerIds = useMemo(() => Object.keys(modelsByProvider).sort(), [modelsByProvider])
  // the group's provider_type is the gateway provider's name ("lan-vllm"); its id is a mirror row ("bifrost-lan-vllm")
  const providerName = (pid: string) => {
    const type = modelsByProvider[pid]?.[0]?.provider_type
    return providerLabel(type || pid.replace(/^bifrost-/, ''))
  }

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
  // Whether it saved; an investigation or Chat save also points the replay row back at the assigned model.
  const persist = async (component: string, next: RowState): Promise<boolean> => {
    if (component === 'investigation' || component === CHAT_DEFAULT_KEY) setReplayFor(null)
    try {
      if (next.inherit) {
        if (assignments[component] !== undefined) {
          await clearAssign(component)
          notify('ok', `${component} set to inherit.`)
        }
        return true
      }
      if (!next.providerId || !next.modelId) return false
      const a = assignments[component]
      if (a && a.provider_id === next.providerId && a.model_id === next.modelId && savedEffort(a) === next.effort) return true
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
      return true
    } catch (e) {
      notify('err', (e as { message?: string })?.message || `Failed to save ${component}.`)
      setRows((prev) => ({ ...prev, [component]: rowFor(component, assignments[component]) }))
      return false
    }
  }

  // A change that moves the investigation model asks the server's judgement first; one that drops
  // no Act workflow (or can't be previewed) saves as before, with no extra step.
  const [drops, setDrops] = useState<{ component: string; next: RowState; preview: ModelChangePreview } | null>(null)
  // The candidate the replay row runs while it is replayed first, unsaved; null is the assigned model.
  const [replayFor, setReplayFor] = useState<{ providerId: string; modelId: string } | null>(null)
  const [replayKey, setReplayKey] = useState(0)
  const dropsRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (drops) dropsRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [drops])

  const save = async (component: string, next: RowState) => {
    // a newer pick on the same row replaces the list it had open
    if (drops?.component === component) setDrops(null)
    const a = assignments[component]
    // Investigation going back to the default runs on Chat's model: previewed as that assignment.
    const chat = assignments[CHAT_DEFAULT_KEY]
    const to =
      component === 'investigation' && next.inherit
        ? a && chat && { providerId: chat.provider_id, modelId: chat.model_id }
        : !next.inherit && next.providerId && next.modelId && { providerId: next.providerId, modelId: next.modelId }
    const moves =
      (component === 'investigation' || component === CHAT_DEFAULT_KEY) &&
      !!to &&
      (!a || a.provider_id !== to.providerId || a.model_id !== to.modelId)
    if (!moves || !to) return void (await persist(component, next))
    let preview: ModelChangePreview
    lock(component, true)
    try {
      preview = (await autonomyApi.previewModelChange(component, to.providerId, to.modelId)).data
    } catch (e) {
      // 403 without ai_autonomy.read, 503 when it can't be judged: the save goes ahead, said plainly
      lock(component, false)
      if (await persist(component, next)) {
        notify('info', `The preview of which Act workflows this drops was unavailable (${errorDetail(e)}). The server still judged the change after saving.`)
      }
      return
    }
    lock(component, false)
    if (preview.moves && preview.workflows.some((w) => w.outcome === 'drop')) setDrops({ component, next, preview })
    else await persist(component, next)
  }

  const dropAnyway = () => {
    if (drops) persist(drops.component, drops.next)
    setDrops(null)
  }
  const dropCancel = () => {
    if (drops) setRows((prev) => ({ ...prev, [drops.component]: rowFor(drops.component, assignments[drops.component]) }))
    setDrops(null)
  }
  // Starts a replay of the candidate and leaves the assignment as it is.
  const replayFirst = async () => {
    if (!drops) return
    const { providerId, modelId } = drops.next
    dropCancel()
    try {
      await aiConfigApi.startInvestigationReplay({ provider_id: providerId, model_id: modelId })
      setReplayFor({ providerId, modelId })
      notify('ok', `Replaying ${modelId}. The assignment was not changed.`)
    } catch (e) {
      notify('err', `The replay of ${modelId} did not start: ${errorDetail(e)}. The assignment was not changed.`)
    }
    setReplayKey((k) => k + 1) // the replay row reads the new replay
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
    else save(component, next)
  }

  const resetting = fusing.includes(RESET_KEY)
  // Rows with their own assignment, Chat (Ask Vigil) aside; these are what Reset clears.
  const resettable = components.filter((c) => c !== CHAT_DEFAULT_KEY && assignments[c])
  // The assignment a row shows: none while its reset is pending, so the row reads as Use default.
  const ownOf = (c: string) => (resetting && c !== CHAT_DEFAULT_KEY ? undefined : assignments[c])
  const inheritsOf = (c: string) => c !== CHAT_DEFAULT_KEY && !ownOf(c)

  const resetAll = () => {
    // an open editor on a row about to be cleared would save into the reset
    if (editing && editing !== CHAT_DEFAULT_KEY) stopEditing(editing)
    notifyUndoable({
      key: RESET_KEY,
      text: `${resettable.length} ${resettable.length === 1 ? 'agent' : 'agents'} back on the default model.`,
      commit: () => Promise.all(resettable.map((c) => clearAssign(c))),
      doneText: 'Agents reset to the default model.',
      failText: (e) => (e as { message?: string })?.message || 'Failed to reset the agents to the default model.',
    })
  }

  const stopEditing = (c: string) => {
    setRows((prev) => ({ ...prev, [c]: rowFor(c, assignments[c]) }))
    setEditing(null)
  }

  // The Keys card asks for this after a key is saved and Chat still has no model.
  useEffect(() => {
    const onPick = () => {
      // the key write mirrored the provider; the catalogue from page load does not know it yet
      reload()
      document.getElementById(AGENT_MODEL_TABLE_ID)?.scrollIntoView?.({ block: 'start' })
      const cur = editingRef.current
      if (cur && cur !== CHAT_DEFAULT_KEY) {
        setRows((prev) => ({ ...prev, [cur]: rowFor(cur, assignments[cur]) }))
      }
      setEditing(CHAT_DEFAULT_KEY)
    }
    window.addEventListener(PICK_AGENT_MODEL, onPick)
    return () => window.removeEventListener(PICK_AGENT_MODEL, onPick)
  }, [assignments, reload])

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
    if (pending) save(pending.component, pending.next)
    setPending(null)
  }

  // The model this row is showing: its own, or Chat's while it inherits (including a pending reset).
  const replayOf = ownOf('investigation') ?? assignments[CHAT_DEFAULT_KEY]
  const replayIds =
    replayFor ??
    (replayOf?.provider_id && replayOf.model_id
      ? { providerId: replayOf.provider_id, modelId: replayOf.model_id }
      : { providerId: '', modelId: '' })

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

  return (
    <SettingsCard
      wide
      id={AGENT_MODEL_TABLE_ID}
      title="Model for each agent"
      desc="Pick a model per agent, and optionally how hard it thinks, or leave it on the default. Unassigned rows use Chat (Ask Vigil). Workflow runs use the investigation assignment."
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
          <div className="table-wrap aim-table-wrap">
            <table className="tbl aim-table">
              <thead>
                <tr>
                  <th>
                    Who uses it
                    <span className="col-low-note">Cost per case is hidden while Ask Vigil is open</span>
                  </th>
                  <th>Model</th>
                  <th>If it is unavailable</th>
                  <th>Thinking</th>
                  <th className="col-low">Cost per case</th>
                  <th className="col-act" aria-label="Actions" />
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
                    <Fragment key={c}>
                    <tr>
                      <td style={{ minWidth: 200, maxWidth: 300 }}>
                        <div className="aim-who">{meta.label}</div>
                        <div className="aim-note">{meta.description}</div>
                      </td>
                      <td style={{ minWidth: 260 }}>
                        {editing === c ? (
                          <div className="aim-model">
                            <Select
                              value={row.inherit ? USE_DEFAULT : row.providerId}
                              placeholder="Select provider"
                              disabled={saving.includes(c)}
                              options={[
                                ...(c === CHAT_DEFAULT_KEY ? [] : [{ value: USE_DEFAULT, label: 'Use default' }]),
                                ...providerIds.map((pid) => ({ value: pid, label: providerName(pid) })),
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
                      <td className="aim-nowrap col-low"><NotMeasured /></td>
                      <td className="aim-nowrap col-act">
                        <button
                          className="btn ghost"
                          disabled={resetting && c !== CHAT_DEFAULT_KEY && editing !== c}
                          onClick={() => {
                            if (editing) stopEditing(editing)
                            if (editing !== c) setEditing(c)
                          }}
                        >
                          {editing === c ? 'Cancel' : 'Change'}
                        </button>
                      </td>
                    </tr>
                    {c === 'investigation' && (
                      <tr className="aim-replay-row">
                        <td colSpan={6}>
                          {drops && (
                            <div className="aim-replay" ref={dropsRef}>
                              <DropList
                                preview={drops.preview}
                                label={COMPONENT_LABELS[drops.component]?.label ?? drops.component}
                              />
                              <div className="aim-replay-top">
                                <button type="button" className="btn primary" onClick={dropAnyway}>Change anyway</button>
                                <button type="button" className="btn ghost" onClick={() => void replayFirst()}>Replay first</button>
                                <button type="button" className="btn ghost" onClick={dropCancel}>Cancel</button>
                              </div>
                            </div>
                          )}
                          {replayFor && (
                            <div className="aim-replay">
                              <p className="aim-replay-line">
                                Replaying {replayFor.modelId}, not saved: the assignment is unchanged.{' '}
                                <button type="button" className="aim-replay-more" onClick={() => setReplayFor(null)}>
                                  Back to the assigned model
                                </button>
                              </p>
                            </div>
                          )}
                          <InvestigationReplay key={replayKey} providerId={replayIds.providerId} modelId={replayIds.modelId} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
      <ConfirmDialog
        open={!!pending}
        danger={false}
        title="Change the default model?"
        body="Chat (Ask Vigil) is the model every agent without its own assignment falls back to. The change is logged with your name."
        confirmLabel="Change default"
        onConfirm={confirm}
        onClose={cancel}
      />
    </SettingsCard>
  )
}
