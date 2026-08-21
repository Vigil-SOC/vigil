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
import { bifrostApi, secretText, type BifrostKey, type BifrostKeyWrite } from '../../../services/bifrostApi'
import type { SectionProps } from './types'

/* Bifrost accepts any provider it knows; these are the ones a SOC deployment
   reaches for. Free-text rather than a fixed enum so the list can't rot behind
   Bifrost's catalogue — an unknown name comes back as its own error. */
const COMMON_PROVIDERS = [
  'anthropic',
  'openai',
  'ollama',
  'vertex',
  'bedrock',
  'azure',
  'gemini',
  'mistral',
  'cohere',
  'openrouter',
  'together_ai',
  'xai',
]

function KeyStatusChip({ status }: { status?: string }) {
  if (status === 'success') return <span className="status closed">Healthy</span>
  if (!status || status === 'unknown') return <span className="chip">Unverified</span>
  return (
    <span className="chip" style={{ color: 'var(--crit)' }} title={status}>
      {status === 'list_models_failed' ? 'Rejected' : status}
    </span>
  )
}

export default function AiProvidersPanel({ notify }: SectionProps) {
  const { providers, keys, phase, error, reload, saveKey, removeKey, addProvider, removeProvider } =
    useBifrostProviders()
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
                          <td><KeyStatusChip status={k.status} /></td>
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
            // Bifrost validates the credential upstream as it stores it, so its
            // verdict is the only test result there is — surface it verbatim.
            if (saved?.status && saved.status !== 'success' && saved.status !== 'unknown') {
              notify('err', `Key stored, but Bifrost reports "${saved.status}" — check the credential.`)
            } else {
              notify('ok', 'Key saved.')
            }
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
function KeyDialog({
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
  const [name, setName] = useState(existing?.name || `${provider}-key`)
  const [secret, setSecret] = useState('')
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
      await onSave({
        name: name.trim(),
        weight: Number(weight) || 1,
        enabled,
        models: allowAll ? ['*'] : chosen,
        // Omitted when left blank: the backend substitutes the stored
        // credential, since Bifrost has no models-only update.
        ...(secret.trim() ? { value: secret.trim() } : {}),
      })
    } catch (e) {
      setErr(bifrostError(e, 'Save failed.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Popup open onClose={onClose} title={existing ? `Edit key · ${provider}` : `Add key · ${provider}`}>
      {err && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {err}</div>}
      <div className="flex flex-col gap-3.5">
        <Field label="Key name" hint="Must be unique across the gateway.">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
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
          disabled={saving || !name.trim() || (!existing && !secret.trim())}
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
