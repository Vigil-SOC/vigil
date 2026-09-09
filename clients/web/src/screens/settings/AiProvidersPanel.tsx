/* ============================================================
   Settings · AI Config · Providers & Keys

   Bifrost holds one provider per upstream and any number of keys under it,
   each with its own weight, model allow-list and health. That is the shape
   here: providers expand to their keys, and a key is the unit you edit.

   There is no separate "test connection" — Bifrost validates a credential
   upstream when it accepts the write and reports the verdict as the key's
   `status`, so saving is testing.
   ============================================================ */
import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  EmptyState,
  Field,
  NumberInput,
  PasswordInput,
  Popup,
  SettingsCard,
  TextInput,
  Toggle,
} from '../../shared/ui'
import { useBifrostProviders, useProviderModels, bifrostError } from './useBifrost'
import {
  bifrostApi,
  isMasked,
  secretText,
  secretEnvRef,
  COMMON_PROVIDERS,
  type KeyVerdict,
  type BifrostKey,
  type BifrostKeyWrite,
} from '../../services/bifrostApi'
import type { SectionProps } from './types'

// Without a verdict, a `list_models_failed` cannot be told from a refusal.
function KeyStatusChip({ status, description, verdict }: {
  status?: string
  description?: string
  verdict?: KeyVerdict
}) {
  if (verdict?.health === 'healthy' || (!verdict && status === 'success')) {
    return <span className="status closed">Healthy</span>
  }
  if (verdict?.health === 'unverifiable') {
    return (
      <span className="chip" title={description}>
        Unverifiable
      </span>
    )
  }
  if (verdict?.health === 'rejected') {
    return (
      <span className="chip" style={{ color: 'var(--crit)' }} title={description || status}>
        {status === 'list_models_failed' ? 'Rejected' : status}
      </span>
    )
  }
  return (
    <span className="chip" title={verdict ? description : status}>
      Unverified
    </span>
  )
}

/** Re-reads the verdict: the save has just invalidated the hook's copy. */
async function keyRejected(keyId: string | undefined): Promise<boolean> {
  if (!keyId) return false
  try {
    const { data } = await bifrostApi.routability()
    return data.keys?.[keyId]?.health === 'rejected'
  } catch {
    return false
  }
}

export default function AiProvidersPanel({ notify }: SectionProps) {
  const {
    providers,
    keys,
    verdicts,
    phase,
    error,
    reload,
    saveKey,
    removeKey,
    addProvider,
    removeProvider,
  } = useBifrostProviders()
  const [expanded, setExpanded] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ provider: string; key: BifrostKey | null } | null>(null)
  const [addingProvider, setAddingProvider] = useState(false)
  const [newProvider, setNewProvider] = useState('')
  const [confirmDel, setConfirmDel] = useState<
    { kind: 'key'; provider: string; key: BifrostKey } | { kind: 'provider'; provider: string } | null
  >(null)
  const [busy, setBusy] = useState(false)

  const handleAddProvider = async () => {
    const name = newProvider.trim().toLowerCase()
    if (!name) return
    setBusy(true)
    try {
      await addProvider(name)
      notify('ok', `Provider ${name} added. Add a key to make it routable.`)
      setAddingProvider(false)
      setNewProvider('')
      setExpanded(name)
    } catch (e) {
      notify('err', bifrostError(e, 'Bifrost rejected that provider name.'))
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!confirmDel) return
    setBusy(true)
    try {
      if (confirmDel.kind === 'key') {
        await removeKey(confirmDel.provider, confirmDel.key.id)
        notify('ok', `Deleted key ${confirmDel.key.name}.`)
      } else {
        await removeProvider(confirmDel.provider)
        notify('ok', `Deleted provider ${confirmDel.provider}.`)
      }
      setConfirmDel(null)
    } catch (e) {
      notify('err', bifrostError(e, 'Delete failed.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard
      wide
      title="Providers & Keys"
      desc="Configuration lives in the Bifrost gateway, which is the only route out to a model. A provider can hold several keys — Bifrost load-balances across them by weight and fails over when one is rejected."
      actions={
        <button className="btn primary" onClick={() => setAddingProvider(true)}>
          <Icon name="plus" /> Add Provider
        </button>
      }
    >
      {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading gateway config…" />}
      {phase === 'error' && (
        <EmptyState
          error
          compact
          icon="alert"
          title="Couldn’t reach the Bifrost gateway"
          body={error}
          primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }}
        />
      )}
      {phase === 'ready' && providers.length === 0 && (
        <EmptyState
          compact
          icon="sparkle"
          title="No providers configured"
          body="Add a provider and give it a key before using AI analysis, workflow generation, or agent chat."
          primary={{ label: 'Add provider', onClick: () => setAddingProvider(true), icon: 'plus' }}
        />
      )}
      {phase === 'ready' &&
        providers.map((p) => {
          const pk = keys[p.name] || []
          const open = expanded === p.name
          return (
            <div key={p.name} className="mb-2.5" style={{ border: '1px solid var(--line)', borderRadius: 6 }}>
              <div className="flex items-center gap-2.5 px-3 py-2.5">
                <button
                  className="btn ghost icon"
                  title={open ? 'Collapse' : 'Expand'}
                  onClick={() => setExpanded(open ? null : p.name)}
                >
                  <Icon name={open ? 'x2' : 'plus'} size={14} />
                </button>
                <span className="font-medium">{p.name}</span>
                <span className="chip">{pk.length === 1 ? '1 key' : `${pk.length} keys`}</span>
                {p.provider_status && p.provider_status !== 'active' && (
                  <span className="chip" style={{ color: 'var(--high)' }}>{p.provider_status}</span>
                )}
                {pk.length === 0 && (
                  <span className="text-xs text-tx-3">No key — this provider cannot route</span>
                )}
                <span className="grow" />
                <button className="btn ghost" onClick={() => setEditing({ provider: p.name, key: null })}>
                  <Icon name="plus" size={14} /> Add key
                </button>
                <button
                  className="btn ghost icon"
                  title="Delete provider"
                  onClick={() => setConfirmDel({ kind: 'provider', provider: p.name })}
                >
                  <Icon name="trash" size={15} />
                </button>
              </div>
              {open && (
                <div className="table-wrap" style={{ borderTop: '1px solid var(--line)' }}>
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>Key</th><th>Credential</th><th>Weight</th><th>Models</th><th>Health</th>
                        <th style={{ textAlign: 'right' }}>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pk.length === 0 && (
                        <tr>
                          <td colSpan={6} className="text-sm text-tx-3">
                            No keys yet.
                          </td>
                        </tr>
                      )}
                      {pk.map((k) => (
                        <tr key={k.id}>
                          <td>
                            <div className="flex flex-col">
                              <span>{k.name}</span>
                              {!k.enabled && <span className="text-xs text-tx-3">Disabled</span>}
                            </div>
                          </td>
                          <td className="font-mono text-xs">
                            {secretText(k.value) || (k.ollama_key_config ? secretText(k.ollama_key_config.url) : '—')}
                          </td>
                          <td>{k.weight}</td>
                          <td className="text-xs">
                            {k.models?.includes('*')
                              ? 'All'
                              : `${k.models?.length || 0} allowed`}
                          </td>
                          <td><KeyStatusChip status={k.status} description={k.description} verdict={verdicts?.keys[k.id]} /></td>
                          <td style={{ textAlign: 'right' }}>
                            <div className="inline-flex gap-1.5">
                              <button
                                className="btn ghost icon"
                                title="Edit"
                                onClick={() => setEditing({ provider: p.name, key: k })}
                              >
                                <Icon name="edit" size={15} />
                              </button>
                              <button
                                className="btn ghost icon"
                                title="Delete key"
                                onClick={() => setConfirmDel({ kind: 'key', provider: p.name, key: k })}
                              >
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
            </div>
          )
        })}

      <Popup open={addingProvider} onClose={() => setAddingProvider(false)} title="Add provider" width={440}>
        <Field
          label="Provider name"
          hint="Bifrost's own identifier for the upstream. It validates the name and reports back if it doesn't know it."
        >
          <input
            className="field-input"
            list="bf-providers"
            value={newProvider}
            onChange={(e) => setNewProvider(e.target.value)}
            placeholder="anthropic"
          />
          <datalist id="bf-providers">
            {COMMON_PROVIDERS.filter((n) => !providers.some((p) => p.name === n)).map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
        </Field>
        <div className="flex justify-end gap-2.5 mt-5">
          <button className="btn ghost" onClick={() => setAddingProvider(false)}>Cancel</button>
          <button className="btn primary" disabled={!newProvider.trim() || busy} onClick={handleAddProvider}>
            <Icon name="check2" /> {busy ? 'Adding…' : 'Add'}
          </button>
        </div>
      </Popup>

      {editing && (
        <KeyDialog
          provider={editing.provider}
          existing={editing.key}
          onClose={() => setEditing(null)}
          onSave={async (data) => {
            const saved = await saveKey(editing.provider, editing.key?.id || null, data)
            setEditing(null)
            setExpanded(editing.provider)
            const rejected = await keyRejected(saved?.id)
            notify(
              rejected ? 'err' : 'ok',
              rejected
                ? `Key stored, but Bifrost reports "${saved?.status}" — check the credential.`
                : 'Key saved.',
            )
          }}
        />
      )}

      <ConfirmDialog
        open={!!confirmDel}
        title={confirmDel?.kind === 'provider' ? 'Delete provider?' : 'Delete key?'}
        body={
          confirmDel?.kind === 'provider'
            ? `Delete "${confirmDel.provider}" and all of its keys? Any component assigned to one of its models will stop resolving.`
            : confirmDel
              ? `Delete key "${confirmDel.key.name}"? Its stored credential is removed too. ${
                  (keys[confirmDel.provider] || []).length === 1
                    ? `It is the only key on ${confirmDel.provider}, which will leave that provider unable to route.`
                    : ''
                }`
              : ''
        }
        confirmLabel="Delete"
        busy={busy}
        onConfirm={handleDelete}
        onClose={() => setConfirmDel(null)}
      />
    </SettingsCard>
  )
}

/* ---------------- Key editor ---------------- */
export function KeyDialog({
  provider,
  existing,
  onClose,
  onSave,
}: {
  provider: string
  existing: BifrostKey | null
  onClose: () => void
  onSave: (data: BifrostKeyWrite) => Promise<void>
}) {
  // Vertex takes either a bare API key or a service-account JSON scoped by
  // project/region — so it gets a mode switch and its own fields.
  const isVertex = provider === 'vertex'
  const isOllama = provider === 'ollama'
  const storedUrl = secretText(existing?.ollama_key_config?.url)
  const storedToken = secretText(existing?.value)
  const storedUrlEnv = secretEnvRef(existing?.ollama_key_config?.url)
  const storedProject = secretText(existing?.vertex_key_config?.project_id)
  const storedRegion = secretText(existing?.vertex_key_config?.region)
  const [vertexAuth, setVertexAuth] = useState<'service_account' | 'api_key'>(
    storedProject ? 'service_account' : 'api_key',
  )
  // A key Bifrost holds no token for reads back as an empty `value`, which is
  // the unauthenticated case — the common one, so it leads.
  const [ollamaAuth, setOllamaAuth] = useState<'none' | 'api_key'>(
    storedToken ? 'api_key' : 'none',
  )

  const [name, setName] = useState(existing?.name || `${provider}-key`)
  const [secret, setSecret] = useState('')
  // Bifrost masks every stored field on read and stores a mask echoed back,
  // so a masked field starts blank behind its own mask.
  const [url, setUrl] = useState(
    existing ? (isMasked(storedUrl) ? '' : storedUrl) : 'env.OLLAMA_URL',
  )
  const [projectId, setProjectId] = useState(isMasked(storedProject) ? '' : storedProject)
  const [region, setRegion] = useState(isMasked(storedRegion) ? '' : storedRegion)
  const [weight, setWeight] = useState(existing?.weight ?? 1)
  const [enabled, setEnabled] = useState(existing?.enabled ?? true)
  const [allowAll, setAllowAll] = useState(existing ? existing.models?.includes('*') !== false : true)
  const [chosen, setChosen] = useState<string[]>((existing?.models || []).filter((m) => m !== '*'))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    setSaving(true)
    setErr(null)
    try {
      const base: BifrostKeyWrite = {
        name: name.trim(),
        weight: Number(weight) || 1,
        enabled,
        models: allowAll ? ['*'] : chosen,
      }
      if (isOllama) {
        // Always sent, even empty: Bifrost blanks an endpoint the write omits.
        base.ollama_key_config = { url: url.trim() || storedUrlEnv }
        // An empty `value` is No auth; an omitted one keeps the stored token.
        if (ollamaAuth === 'none') base.value = ''
        else if (secret.trim()) base.value = secret.trim()
      } else if (isVertex && vertexAuth === 'service_account') {
        // Every field is omitted when left blank; the backend substitutes its
        // stored copy, and for the credential mirrors it into `value` too.
        base.vertex_key_config = {
          ...(projectId.trim() ? { project_id: projectId.trim() } : {}),
          ...(region.trim() ? { region: region.trim() } : {}),
          ...(secret.trim() ? { auth_credentials: secret.trim() } : {}),
        }
      } else if (secret.trim()) {
        // API-key vertex takes the plain-value path like any other provider.
        // Omitted when left blank: the backend substitutes the stored
        // credential, since Bifrost has no models-only update.
        base.value = secret.trim()
      }
      await onSave(base)
    } catch (e) {
      setErr(bifrostError(e, 'Save failed.'))
    } finally {
      setSaving(false)
    }
  }

  // A create needs a credential; a service-account vertex create also needs
  // project + region.
  const missingCredential = !existing && !(isOllama ? url.trim() : secret.trim())
  const missingOllamaToken =
    isOllama && ollamaAuth === 'api_key' && !existing && !secret.trim()
  const missingVertexScope =
    isVertex && vertexAuth === 'service_account' && !existing && (!projectId.trim() || !region.trim())

  return (
    <Popup open onClose={onClose} title={existing ? `Edit key · ${provider}` : `Add key · ${provider}`}>
      {err && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {err}</div>}
      <div className="flex flex-col gap-3.5">
        <Field label="Key name" hint="Must be unique across the gateway.">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        {isOllama ? (
          <>
            <Field
              label="Authentication"
              hint="Ollama's own server takes no credential, which is the single-machine case. Pick API key only when the endpoint below sits behind something that authenticates; the gateway sends it as a bearer token."
            >
              <div className="inline-flex gap-1.5">
                {(
                  [
                    ['none', 'No auth'],
                    ['api_key', 'API key'],
                  ] as const
                ).map(([mode, label]) => (
                  <button
                    key={mode}
                    type="button"
                    className={`btn ${ollamaAuth === mode ? 'primary' : 'ghost'}`}
                    onClick={() => {
                      setOllamaAuth(mode)
                      setSecret('')
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </Field>
            <Field
              label="Server URL"
              hint={
                storedUrl
                  ? `Leave blank to keep the stored endpoint${storedUrlEnv ? ` (${storedUrlEnv})` : ''}.`
                  : 'Resolved by the gateway, not by your browser — and Vigil runs Ollama on the host while the gateway runs in Docker, so a literal here needs the host’s name from inside the container (http://host.docker.internal:11434), not localhost. env.OLLAMA_URL defers to whatever the deployment already set.'
              }
            >
              <TextInput
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={storedUrlEnv || storedUrl || 'env.OLLAMA_URL'}
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
            {ollamaAuth === 'api_key' && (
              <Field
                label="API key"
                hint={
                  storedToken
                    ? 'Leave blank to keep the stored token.'
                    : 'Whatever the proxy in front of Ollama expects. Stored encrypted in Vigil’s secret store and pushed to the gateway.'
                }
              >
                <PasswordInput
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                  placeholder={storedToken ? '•••••••• (unchanged)' : ''}
                  autoComplete="new-password"
                />
              </Field>
            )}
          </>
        ) : isVertex ? (
          <>
            <Field
              label="Authentication"
              hint="An API key only works against Vertex express mode — an AI Studio key belongs under the gemini provider instead. A service account is what a standard Vertex project takes."
            >
              <div className="inline-flex gap-1.5">
                {(
                  [
                    ['api_key', 'API key'],
                    ['service_account', 'Service account'],
                  ] as const
                ).map(([mode, label]) => (
                  <button
                    key={mode}
                    type="button"
                    className={`btn ${vertexAuth === mode ? 'primary' : 'ghost'}`}
                    onClick={() => {
                      setVertexAuth(mode)
                      setSecret('')
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </Field>
            {vertexAuth === 'service_account' ? (
              <>
                <div className="settings-grid-2" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
                  <Field
                    label="Project ID"
                    hint={
                      storedProject
                        ? 'Leave blank to keep the stored project.'
                        : 'The GCP project that owns the Vertex AI endpoint.'
                    }
                  >
                    <TextInput
                      value={projectId}
                      onChange={(e) => setProjectId(e.target.value)}
                      placeholder={storedProject || 'my-gcp-project'}
                    />
                  </Field>
                  <Field
                    label="Region"
                    hint={
                      storedRegion
                        ? 'Leave blank to keep the stored region.'
                        : 'Vertex location, e.g. us-central1.'
                    }
                  >
                    <TextInput
                      value={region}
                      onChange={(e) => setRegion(e.target.value)}
                      placeholder={storedRegion || 'us-central1'}
                    />
                  </Field>
                </div>
                <Field
                  label="Service account JSON"
                  hint={
                    existing
                      ? 'Leave blank to keep the stored service account — paste a new one only to rotate it.'
                      : 'The service-account key file — the one whose "type" is "service_account". A gcloud application-default login file is also accepted, but it is your own login and stops working on your org’s reauth schedule. Stored encrypted in Vigil’s secret store and pushed to the gateway.'
                  }
                >
                  <textarea
                    className="field-input font-mono text-xs"
                    rows={6}
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                    placeholder={existing ? '•••••••• (unchanged)' : '{\n  "type": "service_account",\n  ...\n}'}
                    autoComplete="off"
                    spellCheck={false}
                  />
                </Field>
              </>
            ) : (
              <Field
                label="API key"
                hint={
                  existing
                    ? 'Leave blank to keep the stored credential.'
                    : 'Stored encrypted in Vigil’s secret store and pushed to the gateway.'
                }
              >
                <PasswordInput
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                  placeholder={existing ? '•••••••• (unchanged)' : ''}
                  autoComplete="new-password"
                />
              </Field>
            )}
          </>
        ) : (
          <Field
            label="API key"
            hint={
              existing
                ? 'Leave blank to keep the stored credential — a key configured directly in Bifrost has none held here yet, and saving will ask for it.'
                : 'Stored encrypted in Vigil’s secret store and pushed to the gateway.'
            }
          >
            <PasswordInput
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={existing ? '•••••••• (unchanged)' : ''}
              autoComplete="new-password"
            />
          </Field>
        )}
        <div className="settings-grid-2" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
          <Field label="Weight" hint="Share of traffic when a provider has several keys.">
            <NumberInput value={weight} min={0} step={0.1} onChange={(e) => setWeight(Number(e.target.value))} />
          </Field>
          <Field label="Enabled" hint="A disabled key is kept but never selected.">
            <Toggle checked={enabled} onChange={setEnabled} label="Key enabled" />
          </Field>
        </div>
        <Field
          label="Model allow-list"
          hint="Bifrost refuses any model not on this key's list. Allow all unless you need to fence a key to specific models."
        >
          <Toggle checked={allowAll} onChange={setAllowAll} label="Allow all models" />
        </Field>
        {!allowAll && <ModelAllowList provider={provider} chosen={chosen} onChange={setChosen} />}
      </div>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button
          className="btn primary"
          disabled={
            saving || !name.trim() || missingCredential || missingVertexScope || missingOllamaToken
          }
          onClick={submit}
        >
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </Popup>
  )
}

/* ---------------- Model allow-list ---------------- */
/* Pick from what the provider can route rather than typing ids. One wrinkle drives
   the shape: /api/models?provider=X returns the *routable* set, which is the whole
   catalogue while a provider is unfenced but only the fence once a key has one — so
   an already-fenced key's own models are unioned in, and widening past them goes
   through the add-by-id field, which Bifrost's pricing lookup validates. */
function ModelAllowList({
  provider,
  chosen,
  onChange,
}: {
  provider: string
  chosen: string[]
  onChange: (next: string[]) => void
}) {
  const { models, phase, error } = useProviderModels(provider)
  const [filter, setFilter] = useState('')
  const [extra, setExtra] = useState('')
  const [checking, setChecking] = useState(false)
  const [addFailed, setAddFailed] = useState<string | null>(null)

  const known = useMemo(() => {
    const union = new Set([...models, ...chosen])
    return [...union].sort()
  }, [models, chosen])
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    return needle === '' ? known : known.filter((m) => m.toLowerCase().includes(needle))
  }, [known, filter])

  const toggle = (model: string) =>
    onChange(chosen.includes(model) ? chosen.filter((m) => m !== model) : [...chosen, model])

  // Validated against Bifrost's pricing catalogue, which resolves any model it knows
  // whether or not this key is fenced away from it — so widening a fence works here.
  const add = async () => {
    const model = extra.trim()
    if (model === '') return
    if (chosen.includes(model)) { setExtra(''); return }
    setChecking(true)
    setAddFailed(null)
    try {
      const { data } = await bifrostApi.modelParameters(model, provider)
      onChange([...chosen, model])
      setExtra('')
      if (data.deprecation_date) setAddFailed(`Added — but ${provider} retires ${model} on ${data.deprecation_date}.`)
    } catch {
      setAddFailed(`${provider} has no model called "${model}".`)
    } finally {
      setChecking(false)
    }
  }

  return (
    <Field
      label={`Allowed models${chosen.length > 0 ? ` · ${chosen.length} selected` : ''}`}
      hint="Bifrost refuses any model not ticked here."
    >
      {phase === 'error' && <div className="text-xs mb-2" style={{ color: 'var(--high)' }}>{error}</div>}
      <TextInput
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder={phase === 'loading' ? 'Loading catalogue…' : `Filter ${known.length} models…`}
      />
      <div
        className="mt-2"
        style={{ maxHeight: 190, overflowY: 'auto', border: '1px solid var(--line)', borderRadius: 6, padding: 8 }}
      >
        {shown.length === 0 && (
          <div className="text-xs text-tx-3">
            {phase === 'loading' ? 'Loading…' : known.length === 0 ? 'The gateway lists no models for this provider.' : 'Nothing matches that filter.'}
          </div>
        )}
        {shown.map((model) => (
          <label key={model} className="flex items-center gap-2 py-0.5 text-xs" style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={chosen.includes(model)} onChange={() => toggle(model)} />
            <span className="font-mono">{model}</span>
          </label>
        ))}
      </div>
      <div className="flex gap-2 mt-2">
        <TextInput
          className="grow"
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }}
          placeholder="Add a model the list does not show…"
        />
        <button className="btn ghost" disabled={checking || extra.trim() === ''} onClick={add}>
          {checking ? 'Checking…' : 'Add'}
        </button>
      </div>
      {addFailed && <div className="text-xs mt-1.5" style={{ color: 'var(--high)' }}>{addFailed}</div>}
    </Field>
  )
}
