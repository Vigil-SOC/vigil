/* ============================================================
   Setup · Connect an AI provider (Bifrost)

   The onboarding twin of Settings → AI Config → Providers & Keys. A provider
   routes only once it holds a key whose credential Bifrost has verified, so the
   flow is: pick/create a provider → add a key → the setup step flips ready.
   Reuses the same vertex-aware KeyDialog as Settings so vertex (service-account
   JSON + project/region) is addable here too.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field } from '../../shared/ui'
import { Banner } from '../../shared/formKit'
import { useBifrostProviders, bifrostError } from '../settings/useBifrost'
import { KeyDialog } from '../settings/AiProvidersPanel'
import { COMMON_PROVIDERS, keyRefusal } from '../../services/bifrostApi'

export default function SetupProviderStep({ onSaved }: { onSaved: () => void }) {
  const { providers, keys, verdicts, phase, error, reload, saveKey, addProvider } =
    useBifrostProviders()
  const [newProvider, setNewProvider] = useState('')
  const [busy, setBusy] = useState(false)
  const [localErr, setLocalErr] = useState<string | null>(null)
  // Which provider we're adding a key to (KeyDialog target), or null when closed.
  const [addingKeyFor, setAddingKeyFor] = useState<string | null>(null)

  const handleAddProvider = async () => {
    const name = newProvider.trim().toLowerCase()
    if (!name) return
    setBusy(true)
    setLocalErr(null)
    try {
      // Idempotent-ish: if Bifrost already knows the provider, skip straight to
      // the key. Otherwise create it, then open the key dialog for it.
      if (!providers.some((p) => p.name === name)) {
        await addProvider(name)
      }
      setNewProvider('')
      setAddingKeyFor(name)
    } catch (e) {
      setLocalErr(bifrostError(e, 'Bifrost rejected that provider name.'))
    } finally {
      setBusy(false)
    }
  }

  if (phase === 'loading') {
    return <p className="text-tx-3 text-sm py-2">Loading gateway config…</p>
  }
  if (phase === 'error') {
    return (
      <div className="flex flex-col gap-2">
        <Banner kind="err">{error || 'Couldn’t reach the Bifrost gateway.'}</Banner>
        <button className="btn ghost self-start" onClick={reload}>
          <Icon name="refresh" size={14} /> Retry
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {localErr && <Banner kind="err">{localErr}</Banner>}

      {/* A missing verdict marks every provider unroutable, so say why. */}
      {verdicts === null && (
        <Banner kind="err">
          Couldn’t check whether the gateway’s keys can route — a key you add may show
          as unroutable until this succeeds.
        </Banner>
      )}

      {providers.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {providers.map((p) => {
            const pk = keys[p.name] || []
            const routable = verdicts?.providers[p.name] ?? false
            return (
              <div
                key={p.name}
                className="flex items-center gap-2.5 px-3 py-2 text-sm"
                style={{ border: '1px solid var(--line)', borderRadius: 6 }}
              >
                <span className="font-medium">{p.name}</span>
                {routable ? (
                  <span className="status closed">Routable</span>
                ) : (
                  <span className="chip" style={{ color: 'var(--high)' }}>
                    {pk.length === 0 ? 'No key' : 'Key unverified'}
                  </span>
                )}
                <span className="grow" />
                <button className="btn ghost" onClick={() => setAddingKeyFor(p.name)}>
                  <Icon name="plus" size={14} /> Add key
                </button>
              </div>
            )
          })}
        </div>
      )}

      <Field
        label="Add a provider"
        hint="Bifrost's own identifier for the upstream — e.g. anthropic, openai, vertex. It validates the name and reports back if it doesn't know it."
      >
        <div className="flex gap-2">
          <input
            className="field-input grow"
            list="setup-bf-providers"
            value={newProvider}
            onChange={(e) => setNewProvider(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                handleAddProvider()
              }
            }}
            placeholder="anthropic"
          />
          <datalist id="setup-bf-providers">
            {COMMON_PROVIDERS.filter((n) => !providers.some((p) => p.name === n)).map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
          <button
            className="btn primary"
            disabled={!newProvider.trim() || busy}
            onClick={handleAddProvider}
          >
            <Icon name="plus" size={14} /> {busy ? 'Adding…' : 'Add'}
          </button>
        </div>
      </Field>

      {addingKeyFor && (
        <KeyDialog
          provider={addingKeyFor}
          existing={null}
          onClose={() => setAddingKeyFor(null)}
          onSave={async (data) => {
            const saved = await saveKey(addingKeyFor, null, data)
            setAddingKeyFor(null)
            const refusal = await keyRefusal(saved?.id)
            if (refusal) {
              setLocalErr(`Key stored, but it cannot route: ${refusal}`)
              reload()
            } else {
              onSaved()
            }
          }}
        />
      )}
    </div>
  )
}
