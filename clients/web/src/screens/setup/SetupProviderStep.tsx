/* ============================================================
   Setup · Choose where AI runs (Bifrost)

   The onboarding twin of Settings → AI Config → Providers & Keys. A provider
   routes only once it holds a key whose credential Bifrost has verified, so the
   flow is: pick a card → add a key (inline, or the Settings KeyDialog for a
   cloud account or another provider) → Test → read the verdict banner.
   Board: boards/OnboardingModel.dc.html
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { Icon, type IconName } from '../../shared/icons'
import { Field } from '../../shared/ui'
import { Banner } from '../../shared/formKit'
import { LevelBadge } from '../../shared/LevelBadge'
import { useBifrostProviders, bifrostError } from '../settings/useBifrost'
import { KeyDialog } from '../settings/AiProvidersPanel'
import { COMMON_PROVIDERS, secretEnvRef, secretText, type BifrostKey } from '../../services/bifrostApi'
import { llmProviderApi, type LLMProvider } from '../../services/api'
import ChoiceCard from './ChoiceCard'
import ModelPlan from './ModelPlan'
import ProviderKeyCard, { OutcomeBanner } from './ProviderKeyCard'
import { checkKey, type Outcome } from './providerOutcome'
import {
  CLOUD_PROVIDERS,
  bifrostStaysInCloud,
  bifrostStaysOnSite,
  legacyStaysOnSite,
  residencyCopy,
} from './providerResidency'

type CardId = 'anthropic' | 'openai' | 'cloud' | 'ollama'

// copy is the board's, verbatim
const CARDS: { id: CardId; title: string; name: string; body: string; providers: readonly string[]; kind: 'key' | 'cloud' | 'local' }[] = [
  {
    id: 'anthropic',
    title: 'Anthropic',
    name: 'Anthropic',
    body: 'Best results on investigations and hunts. Case data is sent to Anthropic under your agreement.',
    providers: ['anthropic'],
    kind: 'key',
  },
  {
    id: 'openai',
    title: 'OpenAI',
    name: 'OpenAI',
    body: 'Good results. Case data is sent to OpenAI under your agreement.',
    providers: ['openai'],
    kind: 'key',
  },
  {
    id: 'cloud',
    title: 'Your cloud account',
    name: 'cloud',
    body: 'AWS Bedrock, Google Vertex or Azure. Models run in your own cloud account, under your cloud agreement.',
    providers: CLOUD_PROVIDERS,
    kind: 'cloud',
  },
  {
    id: 'ollama',
    title: 'Local (Ollama)',
    name: 'Ollama',
    body: 'Everything stays on this server. Slower, and weaker on hard investigations. Needs a GPU with 48 GB of memory.',
    providers: ['ollama'],
    kind: 'local',
  },
]

export default function SetupProviderStep({ onRoutable }: { onRoutable: () => void }) {
  const { providers, keys, verdicts, phase, error, reload, saveKey, addProvider } = useBifrostProviders()
  const [picked, setPicked] = useState<CardId | null>(null)
  const [cloudPick, setCloudPick] = useState<string | null>(null)
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({})
  const [busy, setBusy] = useState(false)
  const [localErr, setLocalErr] = useState<string | null>(null)
  const [newProvider, setNewProvider] = useState('')
  // Which provider the key dialog is open for, and the last one added by name.
  const [dialogFor, setDialogFor] = useState<string | null>(null)
  const [another, setAnother] = useState<string | null>(null)
  const [legacy, setLegacy] = useState<LLMProvider[]>([])
  // A key written a moment ago, before the reload that lists it has landed, so a quick retest updates it.
  const written = useRef<Record<string, BifrostKey>>({})
  // The hook goes back to 'loading' on every write; only the first load replaces the screen.
  const loaded = useRef(false)
  if (phase === 'ready') loaded.current = true

  useEffect(() => {
    let live = true
    llmProviderApi
      .list()
      .then((res) => live && setLegacy(res.data || []))
      .catch(() => live && setLegacy([]))
    return () => {
      live = false
    }
  }, [phase])

  const routable = !!verdicts && Object.values(verdicts.providers || {}).some(Boolean)
  useEffect(() => {
    if (routable) onRoutable()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routable])

  const held = (name: string) => providers.some((p) => p.name === name)
  const keyOf = (name: string): BifrostKey | undefined => (keys[name] || [])[0] ?? written.current[name]
  const record = (name: string, o: Outcome) => setOutcomes((prev) => ({ ...prev, [name]: o }))

  // first card, in card order, whose provider Bifrost already lists
  const selectedId = picked ?? CARDS.find((c) => c.providers.some(held))?.id ?? null
  const card = CARDS.find((c) => c.id === selectedId)
  const cloudProvider = cloudPick ?? CLOUD_PROVIDERS.find(held) ?? 'bedrock'
  const provider = card ? (card.id === 'cloud' ? cloudProvider : card.providers[0]) : null

  const ensureProvider = async (name: string) => {
    if (!held(name)) await addProvider(name)
  }

  /** Save (when `write` returns a key id) and then check the key; the field's value is never echoed. */
  const run = async (name: string, write: () => Promise<string | undefined>) => {
    setBusy(true)
    setLocalErr(null)
    try {
      await ensureProvider(name)
      record(name, await checkKey(name, await write()))
    } catch (e) {
      record(name, { tone: 'poor', text: bifrostError(e, 'Save failed.') })
    } finally {
      setBusy(false)
    }
  }

  const save = async (name: string, data: Parameters<typeof saveKey>[2]) => {
    const existing = keyOf(name)
    const saved = await saveKey(name, existing?.id ?? null, data)
    if (saved) written.current[name] = saved
    return saved?.id
  }

  const keep = (name: string) => ({
    name: keyOf(name)?.name ?? `${name}-key`,
    weight: keyOf(name)?.weight ?? 1,
    enabled: keyOf(name)?.enabled ?? true,
    models: keyOf(name)?.models?.length ? keyOf(name)!.models : ['*'],
  })

  const test = async (value: string) => {
    if (!provider || !card) return
    const existing = keyOf(provider)
    if (card.kind === 'local') {
      await run(provider, async () =>
        existing && !value
          ? existing.id
          : save(provider, {
              ...keep(provider),
              // an empty URL defers to whatever the deployment set, as KeyDialog does
              ollama_key_config: { url: value || secretEnvRef(existing?.ollama_key_config?.url) || 'env.OLLAMA_URL' },
              ...(secretText(existing?.value) ? {} : { value: '' }),
            }),
      )
    } else {
      await run(provider, async () => (value ? save(provider, { ...keep(provider), value }) : existing?.id))
    }
  }

  const openDialog = async (name: string) => {
    setLocalErr(null)
    try {
      await ensureProvider(name)
      setDialogFor(name)
      return true
    } catch (e) {
      setLocalErr(bifrostError(e, 'Bifrost rejected that provider name.'))
      return false
    }
  }

  const addAnother = async () => {
    const name = newProvider.trim().toLowerCase()
    if (!name) return
    setBusy(true)
    if (await openDialog(name)) {
      setNewProvider('')
      setAnother(name)
    }
    setBusy(false)
  }

  if (phase === 'loading' && !loaded.current) {
    return <p className="text-tx-3 text-sm py-2">Loading gateway config…</p>
  }

  const footer = (c: (typeof CARDS)[number]) => {
    const p = c.providers[0]
    const inCloud = bifrostStaysInCloud(p)
    const stays = bifrostStaysOnSite(p, keys[p] || [])
    const icon: IconName = inCloud ? 'cloud' : stays ? 'lock' : 'globe'
    return (
      <span className={`su-resid${inCloud || stays ? ' stays' : ''}`}>
        <Icon name={icon} size={13} />
        {residencyCopy(stays, inCloud)}
      </span>
    )
  }

  // "could not ask" shows no badge; it is not a Poor verdict
  const badge = (c: (typeof CARDS)[number]) => {
    const names = c.providers.filter(held)
    if (!verdicts || !names.length) return null
    return <LevelBadge variant="pill" level={names.some((n) => verdicts.providers[n]) ? 'good' : 'poor'} />
  }

  const another_ = another ? outcomes[another] : undefined

  return (
    <div className="flex flex-col gap-4">
      {phase === 'error' && (
        <div className="flex flex-col gap-2">
          <Banner kind="err">{error || 'Couldn’t reach the Bifrost gateway.'}</Banner>
          <button className="btn ghost self-start" onClick={reload}>
            <Icon name="refresh" size={14} /> Retry
          </button>
        </div>
      )}
      {localErr && <Banner kind="err">{localErr}</Banner>}

      {/* A missing verdict marks every provider unroutable, so say why. */}
      {phase === 'ready' && verdicts === null && (
        <Banner kind="err">
          Couldn’t check whether the gateway’s keys can route — a key you add may show
          as unroutable until this succeeds.
        </Banner>
      )}

      {phase !== 'error' && (
        <>
          <div className="su-choices two">
            {CARDS.map((c) => (
              <ChoiceCard
                key={c.id}
                title={c.title}
                body={c.body}
                footer={footer(c)}
                badge={badge(c)}
                selected={c.id === selectedId}
                onSelect={() => setPicked(c.id)}
              />
            ))}
          </div>

          {card && provider && (
            <ProviderKeyCard
              key={provider}
              kind={card.kind}
              provider={provider}
              name={card.name}
              existing={keyOf(provider)}
              outcome={outcomes[provider]}
              busy={busy}
              onTest={test}
              onPickCloud={setCloudPick}
              onOpenDialog={() => openDialog(provider)}
            />
          )}
          {!card && phase === 'ready' && (
            <p className="su-note">Pick where AI runs to add its key. You can come back to this later.</p>
          )}

          <div className="flex flex-col gap-2">
            <Field
              label="Another provider"
              hint="Bifrost's own identifier for the upstream — e.g. gemini, mistral. It validates the name and reports back if it doesn't know it."
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
                      addAnother()
                    }
                  }}
                  placeholder="gemini"
                />
                <datalist id="setup-bf-providers">
                  {COMMON_PROVIDERS.filter((n) => !held(n)).map((n) => (
                    <option key={n} value={n} />
                  ))}
                </datalist>
                <button className="btn ghost" disabled={!newProvider.trim() || busy} onClick={addAnother}>
                  <Icon name="plus" size={14} /> {busy ? 'Adding…' : 'Add'}
                </button>
              </div>
            </Field>
            {another_ && <OutcomeBanner outcome={another_} />}
            {legacy.length > 0 && (
              <p className="su-note">
                Also set up outside the gateway:{' '}
                {legacy.map((p, i) => (
                  <span key={p.provider_id}>
                    {i > 0 && '; '}
                    <span className="font-medium">{p.name}</span> ({residencyCopy(legacyStaysOnSite(p))})
                  </span>
                ))}
              </p>
            )}
          </div>
        </>
      )}

      <ModelPlan />

      {dialogFor && (
        <KeyDialog
          provider={dialogFor}
          existing={keyOf(dialogFor) ?? null}
          onClose={() => setDialogFor(null)}
          onSave={async (data) => {
            const name = dialogFor
            const id = await save(name, data)
            setDialogFor(null)
            record(name, await checkKey(name, id))
          }}
        />
      )}
    </div>
  )
}
