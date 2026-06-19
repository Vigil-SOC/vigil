/* ============================================================
   LLM provider add/edit wizard (redesign port of LLMProviderDialog).
   3 steps: pick type → connection + model discovery → test & save.
   Draft-upsert so /test and /models can run before the final save;
   retries update the draft row rather than re-POSTing (avoids 409).
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, Popup, PasswordInput, Select, TextInput, Toggle } from '../../shared/ui'
import {
  llmProviderApi,
  type LLMProvider,
  type LLMProviderCreate,
} from '../../../services/api'

type ProviderType = 'anthropic' | 'openai' | 'ollama'

const STEPS = ['Provider', 'Connection', 'Test & Save']

const DEFAULT_BASE_URLS: Record<ProviderType, string> = {
  anthropic: '',
  openai: 'https://api.openai.com/v1',
  ollama: 'http://localhost:11434',
}
const DEFAULT_MODEL: Record<ProviderType, string> = {
  anthropic: 'claude-sonnet-4-5-20250929',
  openai: 'gpt-4o-mini',
  ollama: 'llama3.1:8b',
}
const TYPE_OPTIONS: { value: ProviderType; label: string; desc: string }[] = [
  { value: 'ollama', label: 'Ollama', desc: 'Local or remote Ollama server' },
  { value: 'openai', label: 'OpenAI', desc: 'OpenAI or OpenAI-compatible endpoint' },
  { value: 'anthropic', label: 'Anthropic', desc: 'Additional Anthropic account' },
]

interface Props {
  existing: LLMProvider | null
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

export default function LlmProviderDialog({ existing, onClose, onSaved, onError }: Props) {
  const editing = !!existing
  const [step, setStep] = useState(editing ? 1 : 0)

  const [providerType, setProviderType] = useState<ProviderType>(
    (existing?.provider_type as ProviderType) ?? 'ollama',
  )
  const [name, setName] = useState(existing?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [organization, setOrganization] = useState<string>(existing?.config?.organization ?? '')
  const [defaultModel, setDefaultModel] = useState(existing?.default_model ?? '')
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? false)
  const [draftProviderId, setDraftProviderId] = useState<string | null>(
    existing?.provider_id ?? null,
  )

  const [testing, setTesting] = useState(false)
  const [testError, setTestError] = useState<string | null>(null)
  const [tested, setTested] = useState(false)
  const [availableModels, setAvailableModels] = useState<string[]>([])

  const [discoveredModels, setDiscoveredModels] = useState<string[]>([])
  const [discovering, setDiscovering] = useState(false)
  const [discoverError, setDiscoverError] = useState<string | null>(null)
  const [useCustomModel, setUseCustomModel] = useState(false)
  const discoverDebounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  const runDiscovery = async () => {
    setDiscovering(true)
    setDiscoverError(null)
    try {
      const res = await llmProviderApi.discoverModels({
        provider_type: providerType,
        base_url: baseUrl || undefined,
        api_key: apiKey || undefined,
        organization: organization || undefined,
      })
      const ids = res.data.models || []
      setDiscoveredModels(ids)
      if (!defaultModel && ids.length > 0) setDefaultModel(ids[0])
      if (defaultModel && ids.length > 0 && !ids.includes(defaultModel)) setUseCustomModel(true)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setDiscoverError(err?.response?.data?.detail || err?.message || 'Failed to list models')
      setDiscoveredModels([])
    } finally {
      setDiscovering(false)
    }
  }

  // Auto-discover on step 1 once we have enough info (debounced).
  useEffect(() => {
    if (step !== 1) return
    const needsKey = providerType === 'openai' || providerType === 'anthropic'
    if (needsKey && !apiKey && !editing) return
    if (providerType === 'ollama' && !baseUrl) return
    if (discoverDebounce.current) clearTimeout(discoverDebounce.current)
    discoverDebounce.current = setTimeout(runDiscovery, 500)
    return () => {
      if (discoverDebounce.current) clearTimeout(discoverDebounce.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, providerType, baseUrl, apiKey, organization])

  const selectProviderType = (t: ProviderType) => {
    setProviderType(t)
    if (!baseUrl) setBaseUrl(DEFAULT_BASE_URLS[t])
    if (!defaultModel) setDefaultModel(DEFAULT_MODEL[t])
  }

  const saveDraftAndTest = async (): Promise<void> => {
    setTesting(true)
    setTestError(null)
    try {
      let providerId = draftProviderId
      const alreadyPersisted = editing || !!providerId
      if (!alreadyPersisted) {
        const payload: LLMProviderCreate = {
          provider_type: providerType,
          name: name || `${providerType} provider`,
          base_url: baseUrl || undefined,
          api_key: apiKey || undefined,
          default_model: defaultModel || DEFAULT_MODEL[providerType],
          is_active: true,
          is_default: false,
          config: organization ? { organization } : {},
        }
        const resp = await llmProviderApi.create(payload)
        providerId = resp.data.provider_id
        setDraftProviderId(providerId)
      } else if (providerId) {
        await llmProviderApi.update(providerId, {
          name,
          base_url: baseUrl || undefined,
          api_key: apiKey || undefined,
          default_model: defaultModel,
          config: organization ? { organization } : {},
        })
      }
      if (!providerId) throw new Error('Failed to create provider')

      const testResp = await llmProviderApi.test(providerId)
      if (!testResp.data.success) {
        setTestError(testResp.data.error || 'Connection test failed')
        return
      }
      const modelsResp = await llmProviderApi.listModels(providerId)
      setAvailableModels(modelsResp.data.models || [])
      setTested(true)
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      setTestError(err?.response?.data?.detail || err?.message || 'Test failed')
    } finally {
      setTesting(false)
    }
  }

  const finalSave = async () => {
    try {
      const providerId = draftProviderId ?? existing?.provider_id
      if (!providerId) throw new Error('No provider id')
      if (isDefault || defaultModel !== existing?.default_model) {
        await llmProviderApi.update(providerId, {
          default_model: defaultModel,
          is_default: isDefault || undefined,
        })
      }
      onSaved()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      onError(err?.response?.data?.detail || 'Save failed')
    }
  }

  const modelOptions = discoveredModels.map((m) => ({ value: m, label: m }))

  return (
    <Popup open onClose={onClose} title={editing ? 'Edit provider' : 'Add LLM provider'} width={520}>
      {/* step indicator */}
      <div className="flex items-center gap-2 mb-5">
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span
              className={`flex items-center justify-center w-5 h-5 rounded-full text-[11px] font-semibold ${
                i === step
                  ? 'bg-[var(--accent)] text-white'
                  : i < step
                    ? 'bg-[var(--accent-dim)] text-accent-2'
                    : 'bg-[var(--bg-3)] text-tx-3'
              }`}
            >
              {i + 1}
            </span>
            <span className={`text-xs ${i === step ? 'text-tx' : 'text-tx-3'}`}>{s}</span>
            {i < STEPS.length - 1 && <span className="w-6 h-px bg-line" />}
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="flex flex-col gap-2">
          {TYPE_OPTIONS.map((o) => (
            <button
              key={o.value}
              className={`card card-sq text-left p-3 ${providerType === o.value ? 'border-accent-line bg-[var(--accent-dim)]' : ''}`}
              style={providerType === o.value ? { borderColor: 'var(--accent-line)' } : undefined}
              onClick={() => selectProviderType(o.value)}
            >
              <div className="text-[13px] font-semibold text-tx">{o.label}</div>
              <div className="text-xs text-tx-3">{o.desc}</div>
            </button>
          ))}
        </div>
      )}

      {step === 1 && (
        <div className="flex flex-col gap-3.5">
          <Field label="Name">
            <TextInput
              value={name}
              placeholder={`My ${providerType}`}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          {providerType !== 'anthropic' && (
            <Field
              label="Base URL"
              hint={providerType === 'ollama' ? 'Ollama server URL (e.g. http://localhost:11434)' : 'OpenAI-compatible endpoint'}
            >
              <TextInput
                value={baseUrl}
                placeholder={DEFAULT_BASE_URLS[providerType]}
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </Field>
          )}
          {providerType !== 'ollama' && (
            <Field label="API Key">
              <PasswordInput
                value={apiKey}
                placeholder={editing ? 'Leave blank to keep existing key' : ''}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </Field>
          )}
          {providerType === 'openai' && (
            <Field label="Organization (optional)">
              <TextInput value={organization} onChange={(e) => setOrganization(e.target.value)} />
            </Field>
          )}

          <Field
            label="Default model"
            error={discoverError ? `Model discovery failed — enter a model ID manually. (${discoverError})` : undefined}
            hint={
              discovering
                ? 'Fetching available models…'
                : discoveredModels.length && !useCustomModel
                  ? `${discoveredModels.length} model(s) available from this provider.`
                  : 'Enter the model ID, or click refresh to fetch a list from the provider.'
            }
          >
            <div className="flex items-start gap-2">
              <div className="flex-1">
                {discoveredModels.length > 0 && !useCustomModel ? (
                  <Select
                    value={discoveredModels.includes(defaultModel) ? defaultModel : ''}
                    placeholder="Select a model"
                    options={[...modelOptions, { value: '__custom__', label: 'Custom model ID…' }]}
                    onSelect={(v) => (v === '__custom__' ? setUseCustomModel(true) : setDefaultModel(v))}
                  />
                ) : (
                  <TextInput
                    value={defaultModel}
                    placeholder={DEFAULT_MODEL[providerType]}
                    onChange={(e) => setDefaultModel(e.target.value)}
                  />
                )}
              </div>
              <button
                className="btn ghost icon"
                title="Fetch model list from provider"
                onClick={runDiscovery}
                disabled={discovering}
              >
                <Icon name="refresh" size={15} />
              </button>
            </div>
          </Field>
          {useCustomModel && discoveredModels.length > 0 && (
            <button className="btn ghost self-start" onClick={() => setUseCustomModel(false)}>
              Back to model list
            </button>
          )}
        </div>
      )}

      {step === 2 && (
        <div className="flex flex-col gap-3">
          {testing && (
            <div className="flex items-center gap-2 text-sm text-tx-2">
              <Icon name="refresh" size={15} /> Testing connection…
            </div>
          )}
          {testError && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {testError}
            </div>
          )}
          {tested && (
            <>
              <div className="settings-banner ok">
                <Icon name="check2" size={14} /> Connection OK
              </div>
              {availableModels.length > 0 && (
                <Field label="Model">
                  <Select
                    value={defaultModel}
                    options={availableModels.map((m) => ({ value: m, label: m }))}
                    onSelect={setDefaultModel}
                  />
                </Field>
              )}
              <label className="flex items-center gap-2.5 text-sm text-tx-2 mt-1">
                <Toggle checked={isDefault} onChange={setIsDefault} />
                Set as default for this provider type
              </label>
            </>
          )}
        </div>
      )}

      {/* footer */}
      <div className="flex justify-end gap-2.5 mt-6">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        {step > 0 && !testing && (
          <button className="btn ghost" onClick={() => setStep(step - 1)}>Back</button>
        )}
        {step === 0 && (
          <button className="btn primary" onClick={() => setStep(1)}>Next</button>
        )}
        {step === 1 && (
          <button
            className="btn primary"
            onClick={async () => {
              setStep(2)
              await saveDraftAndTest()
            }}
          >
            Test &amp; continue
          </button>
        )}
        {step === 2 && (
          <button className="btn primary" disabled={!tested} onClick={finalSave}>
            Save
          </button>
        )}
      </div>
    </Popup>
  )
}
