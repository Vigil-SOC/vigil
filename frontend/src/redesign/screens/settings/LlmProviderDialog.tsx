/* ============================================================
   LLM provider add/edit wizard. Steps: pick type → connection (test runs inline
   here) → model & save. Draft-upsert so /test and /models can run before the
   final save; retries update the draft row rather than re-POSTing (avoids 409).
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, Popup, PasswordInput, Select, TextInput, Toggle } from '../../shared/ui'
import { Banner, extractApiError } from '../../shared/formKit'
import {
  llmProviderApi,
  type LLMProvider,
  type LLMProviderCreate,
} from '../../../services/api'

type ProviderType = 'anthropic' | 'openai' | 'ollama'

const STEPS = ['Provider', 'Connection', 'Model & Save']

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
  // Hide the footer Cancel button. The inline setup screen sets this false — its
  // accordion row already has a Close toggle, so an in-panel Cancel is redundant.
  showCancel?: boolean
}

// Body without modal chrome; the default export below wraps it in a Popup for Settings.
export const LlmProviderWizard = ({
  existing,
  onClose,
  onSaved,
  showCancel = true,
}: Props) => {
  const editing = !!existing
  const [step, setStep] = useState(editing ? 1 : 0)

  const initialType = (existing?.provider_type as ProviderType) ?? 'ollama'
  const [providerType, setProviderType] = useState<ProviderType>(initialType)
  const [name, setName] = useState(existing?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? DEFAULT_BASE_URLS[initialType])
  const [apiKey, setApiKey] = useState('')
  const [organization, setOrganization] = useState<string>(existing?.config?.organization ?? '')
  const [defaultModel, setDefaultModel] = useState(
    existing?.default_model ?? DEFAULT_MODEL[initialType],
  )
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? false)

  const [testing, setTesting] = useState(false)
  const [testError, setTestError] = useState<string | null>(null)
  const [tested, setTested] = useState(false)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [saveError, setSaveError] = useState<string | null>(null)

  // A connection-affecting edit (or a provider-type switch) invalidates a prior
  // successful test, so the model picker / Save hide and a re-test is required.
  const invalidateTest = () => {
    if (tested || testError || availableModels.length) {
      setTested(false)
      setTestError(null)
      setAvailableModels([])
    }
  }

  const selectProviderType = (t: ProviderType) => {
    if (t === providerType) return
    // Connection fields are type-specific (an OpenAI URL/key is meaningless for
    // Ollama), so switching type resets them to the new type's defaults rather
    // than carrying the previous provider's values over.
    setProviderType(t)
    setBaseUrl(DEFAULT_BASE_URLS[t])
    setDefaultModel(DEFAULT_MODEL[t])
    setApiKey('')
    setOrganization('')
    invalidateTest()
  }

  const testAndLoadModels = async (): Promise<void> => {
    setTesting(true)
    setTestError(null)
    try {
      if (editing && existing) {
        // Edit: the row already exists, so persist the entered fields then
        // test + list models against it — no new provider id is ever claimed.
        await llmProviderApi.update(existing.provider_id, {
          name,
          base_url: baseUrl || undefined,
          api_key: apiKey || undefined,
          default_model: defaultModel,
          config: organization ? { organization } : {},
        })
        const testResp = await llmProviderApi.test(existing.provider_id)
        if (!testResp.data.success) {
          setTestError(testResp.data.error || 'Connection test failed')
          return
        }
        const modelsResp = await llmProviderApi.listModels(existing.provider_id)
        setAvailableModels(modelsResp.data.models || [])
        setTested(true)
        return
      }

      // Create: test and discover models statelessly. Nothing is persisted
      // until the final Save, so cancelling here never strands a provider id.
      const testResp = await llmProviderApi.testConnection({
        provider_type: providerType,
        base_url: baseUrl || undefined,
        api_key: apiKey || undefined,
        default_model: defaultModel || DEFAULT_MODEL[providerType],
        organization: organization || undefined,
      })
      if (!testResp.data.success) {
        setTestError(testResp.data.error || 'Connection test failed')
        return
      }
      const modelsResp = await llmProviderApi.discoverModels({
        provider_type: providerType,
        base_url: baseUrl || undefined,
        api_key: apiKey || undefined,
        organization: organization || undefined,
      })
      setAvailableModels(modelsResp.data.models || [])
      setTested(true)
    } catch (e) {
      setTestError(extractApiError(e, 'Test failed'))
    } finally {
      setTesting(false)
    }
  }

  const finalSave = async () => {
    setSaveError(null)
    try {
      if (editing && existing) {
        // Edit: connection fields were already persisted at test time; only
        // the model / default flag are still mutable here.
        if (isDefault || defaultModel !== existing.default_model) {
          await llmProviderApi.update(existing.provider_id, {
            default_model: defaultModel,
            is_default: isDefault || undefined,
          })
        }
        onSaved()
        return
      }
      // Create: this is the single point where the provider is persisted.
      const payload: LLMProviderCreate = {
        provider_type: providerType,
        name: name || `${providerType} provider`,
        base_url: baseUrl || undefined,
        api_key: apiKey || undefined,
        default_model: defaultModel || DEFAULT_MODEL[providerType],
        is_active: true,
        is_default: isDefault || false,
        config: organization ? { organization } : {},
      }
      await llmProviderApi.create(payload)
      onSaved()
    } catch (e) {
      setSaveError(extractApiError(e, 'Save failed'))
    }
  }

  return (
    <>
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
                onChange={(e) => {
                  setBaseUrl(e.target.value)
                  invalidateTest()
                }}
              />
            </Field>
          )}
          {providerType !== 'ollama' && (
            <Field label="API Key">
              <PasswordInput
                value={apiKey}
                placeholder={editing ? 'Leave blank to keep existing key' : ''}
                onChange={(e) => {
                  setApiKey(e.target.value)
                  invalidateTest()
                }}
              />
            </Field>
          )}
          {providerType === 'openai' && (
            <Field label="Organization (optional)">
              <TextInput
                value={organization}
                onChange={(e) => {
                  setOrganization(e.target.value)
                  invalidateTest()
                }}
              />
            </Field>
          )}

          {testing && (
            <div className="flex items-center gap-2 text-sm text-tx-2">
              <Icon name="refresh" size={15} /> Testing connection…
            </div>
          )}
          {testError && <Banner kind="err">{testError}</Banner>}
          {!testing && !tested && !testError && (
            <p className="text-tx-3 text-xs">
              Test the connection to verify it works and load this provider&apos;s models.
            </p>
          )}
          {tested && <Banner kind="ok">Connection OK — continue to pick a model.</Banner>}
        </div>
      )}

      {step === 2 && (
        <div className="flex flex-col gap-3">
          {saveError && <Banner kind="err">{saveError}</Banner>}
          <Field
            label="Model"
            hint={
              availableModels.length
                ? `${availableModels.length} model(s) available from this provider.`
                : 'Enter the model ID to use.'
            }
          >
            {availableModels.length > 0 ? (
              <Select
                value={defaultModel}
                placeholder="Select a model"
                options={availableModels.map((m) => ({ value: m, label: m }))}
                onSelect={setDefaultModel}
              />
            ) : (
              <TextInput
                value={defaultModel}
                placeholder={DEFAULT_MODEL[providerType]}
                onChange={(e) => setDefaultModel(e.target.value)}
              />
            )}
          </Field>
          <label className="flex items-center gap-2.5 text-sm text-tx-2 mt-1">
            <Toggle checked={isDefault} onChange={setIsDefault} />
            Set as default for this provider type
          </label>
        </div>
      )}

      {/* footer */}
      <div className="flex justify-end gap-2.5 mt-6">
        {showCancel && (
          <button className="btn ghost" onClick={onClose}>
            Cancel
          </button>
        )}
        {step > 0 && !testing && (
          <button className="btn ghost" onClick={() => setStep(step - 1)}>Back</button>
        )}
        {step === 0 && (
          <button className="btn primary" onClick={() => setStep(1)}>Next</button>
        )}
        {step === 1 && !tested && (
          <button className="btn primary" disabled={testing} onClick={testAndLoadModels}>
            {testing ? 'Testing…' : 'Test connection'}
          </button>
        )}
        {step === 1 && tested && (
          <button className="btn primary" onClick={() => setStep(2)}>
            Continue
          </button>
        )}
        {step === 2 && (
          <button className="btn primary" onClick={finalSave}>
            Save
          </button>
        )}
      </div>
    </>
  )
}

// Modal form used by Settings (Add/Edit provider). The setup screen renders
// <LlmProviderWizard> directly, inline, instead of this wrapper.
const LlmProviderDialog = (props: Props) => {
  return (
    <Popup
      open
      onClose={props.onClose}
      title={props.existing ? 'Edit provider' : 'Add LLM provider'}
      width={520}
    >
      <LlmProviderWizard {...props} />
    </Popup>
  )
}

export default LlmProviderDialog
