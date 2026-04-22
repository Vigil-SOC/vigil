import { useEffect, useRef, useState } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Stepper,
  Step,
  StepLabel,
  Box,
  TextField,
  RadioGroup,
  FormControlLabel,
  Radio,
  FormControl,
  FormLabel,
  Alert,
  CircularProgress,
  Select,
  MenuItem,
  InputLabel,
  Typography,
  IconButton,
  Tooltip,
  Stack,
} from '@mui/material'
import RefreshIcon from '@mui/icons-material/Refresh'
import { llmProviderApi, LLMProvider, LLMProviderCreate } from '../../services/api'

type ProviderType = 'anthropic' | 'openai' | 'ollama'

interface Props {
  existing: LLMProvider | null
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

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

export default function LLMProviderDialog({ existing, onClose, onSaved, onError }: Props) {
  const editing = !!existing
  const [step, setStep] = useState(editing ? 1 : 0)

  const [providerType, setProviderType] = useState<ProviderType>(
    (existing?.provider_type as ProviderType) ?? 'ollama',
  )
  const [name, setName] = useState(existing?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [organization, setOrganization] = useState(existing?.config?.organization ?? '')
  const [defaultModel, setDefaultModel] = useState(existing?.default_model ?? '')
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? false)

  // Tracks the provider id after it's been created (or for edits, the
  // existing one). This is what finalSave and retry-of-test both need;
  // if we relied on `existing?.provider_id` we'd lose the id in the new-
  // provider flow, and retrying the test after a failure would re-POST
  // the same slug and 409.
  const [draftProviderId, setDraftProviderId] = useState<string | null>(
    existing?.provider_id ?? null,
  )

  // Step 2 state
  const [testing, setTesting] = useState(false)
  const [testError, setTestError] = useState<string | null>(null)
  const [tested, setTested] = useState(false)
  const [availableModels, setAvailableModels] = useState<string[]>([])

  // Step 1 model discovery (pre-save) — populates the Default model dropdown.
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
      // If user hadn't picked a model yet, default to the first one.
      if (!defaultModel && ids.length > 0) setDefaultModel(ids[0])
      // If current default isn't in the list, flip to custom so the user sees
      // the free-text value rather than an empty Select.
      if (defaultModel && ids.length > 0 && !ids.includes(defaultModel)) {
        setUseCustomModel(true)
      }
    } catch (e: any) {
      setDiscoverError(e?.response?.data?.detail || e?.message || 'Failed to list models')
      setDiscoveredModels([])
    } finally {
      setDiscovering(false)
    }
  }

  // Auto-discover when we have enough info. Debounced so typing the key
  // doesn't fire one request per keystroke.
  useEffect(() => {
    if (step !== 1) return
    // Anthropic has no discovery endpoint — return static list anyway.
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

  // First-load helper when switching provider type
  const selectProviderType = (t: ProviderType) => {
    setProviderType(t)
    if (!baseUrl) setBaseUrl(DEFAULT_BASE_URLS[t])
    if (!defaultModel) setDefaultModel(DEFAULT_MODEL[t])
  }

  const saveDraftAndTest = async (): Promise<string | null> => {
    // Upsert the provider so we can call /test and /models against it.
    // On retry (user fixed settings and clicked Test again), we update the
    // draft row we already created instead of re-POSTing and hitting 409.
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
        return providerId
      }
      const modelsResp = await llmProviderApi.listModels(providerId)
      setAvailableModels(modelsResp.data.models || [])
      setTested(true)
      return providerId
    } catch (e: any) {
      setTestError(e?.response?.data?.detail || e?.message || 'Test failed')
      return null
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
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Save failed')
    }
  }

  const renderStep0 = () => (
    <FormControl>
      <FormLabel>Provider type</FormLabel>
      <RadioGroup
        value={providerType}
        onChange={(e) => selectProviderType(e.target.value as ProviderType)}
      >
        <FormControlLabel value="ollama" control={<Radio />} label="Ollama (local or remote)" />
        <FormControlLabel value="openai" control={<Radio />} label="OpenAI (or OpenAI-compatible)" />
        <FormControlLabel value="anthropic" control={<Radio />} label="Anthropic (additional account)" />
      </RadioGroup>
    </FormControl>
  )

  const renderStep1 = () => (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <TextField
        label="Name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder={`My ${providerType}`}
        fullWidth
      />
      {providerType !== 'anthropic' && (
        <TextField
          label="Base URL"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder={DEFAULT_BASE_URLS[providerType]}
          fullWidth
          helperText={
            providerType === 'ollama'
              ? 'Ollama server URL (e.g. http://localhost:11434)'
              : 'OpenAI-compatible endpoint'
          }
        />
      )}
      {providerType !== 'ollama' && (
        <TextField
          label="API Key"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={editing ? 'Leave blank to keep existing key' : ''}
          fullWidth
        />
      )}
      {providerType === 'openai' && (
        <TextField
          label="Organization (optional)"
          value={organization}
          onChange={(e) => setOrganization(e.target.value)}
          fullWidth
        />
      )}
      {discoveredModels.length > 0 && !useCustomModel ? (
        <Box>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <FormControl fullWidth size="small">
              <InputLabel>Default model</InputLabel>
              <Select
                label="Default model"
                value={discoveredModels.includes(defaultModel) ? defaultModel : ''}
                onChange={(e) => {
                  const v = e.target.value as string
                  if (v === '__custom__') {
                    setUseCustomModel(true)
                  } else {
                    setDefaultModel(v)
                  }
                }}
                displayEmpty
              >
                <MenuItem value="" disabled>
                  <em>Select a model</em>
                </MenuItem>
                {discoveredModels.map((m) => (
                  <MenuItem key={m} value={m}>{m}</MenuItem>
                ))}
                <MenuItem value="__custom__">
                  <em>Custom model ID…</em>
                </MenuItem>
              </Select>
            </FormControl>
            <Tooltip title="Re-fetch model list">
              <span>
                <IconButton
                  size="small"
                  onClick={runDiscovery}
                  disabled={discovering}
                  sx={{ mt: 0.5 }}
                >
                  {discovering ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
                </IconButton>
              </span>
            </Tooltip>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            {discoveredModels.length} model{discoveredModels.length === 1 ? '' : 's'} available from this provider.
          </Typography>
        </Box>
      ) : (
        <Box>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField
              label="Default model"
              size="small"
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              placeholder={DEFAULT_MODEL[providerType]}
              helperText={
                discoverError
                  ? `Model discovery failed — enter a model ID manually. (${discoverError})`
                  : discovering
                    ? 'Fetching available models…'
                    : 'Enter the model ID, or click refresh to fetch a list from the provider.'
              }
              fullWidth
              error={Boolean(discoverError)}
            />
            <Tooltip title="Fetch model list from provider">
              <span>
                <IconButton
                  size="small"
                  onClick={runDiscovery}
                  disabled={discovering}
                  sx={{ mt: 0.5 }}
                >
                  {discovering ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
                </IconButton>
              </span>
            </Tooltip>
          </Stack>
          {useCustomModel && discoveredModels.length > 0 && (
            <Button
              size="small"
              onClick={() => setUseCustomModel(false)}
              sx={{ mt: 0.5 }}
            >
              Back to model list
            </Button>
          )}
        </Box>
      )}
    </Box>
  )

  const renderStep2 = () => (
    <Box>
      {testing && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <CircularProgress size={20} />
          <Typography>Testing connection…</Typography>
        </Box>
      )}
      {testError && <Alert severity="error">{testError}</Alert>}
      {tested && (
        <>
          <Alert severity="success" sx={{ mb: 2 }}>Connection OK</Alert>
          {availableModels.length > 0 && (
            <FormControl fullWidth sx={{ mb: 2 }}>
              <InputLabel>Model</InputLabel>
              <Select
                label="Model"
                value={defaultModel}
                onChange={(e) => setDefaultModel(e.target.value as string)}
              >
                {availableModels.map((m) => (
                  <MenuItem key={m} value={m}>{m}</MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          <FormControlLabel
            control={<Radio checked={isDefault} onClick={() => setIsDefault(!isDefault)} />}
            label="Set as default for this provider type"
          />
        </>
      )}
    </Box>
  )

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{editing ? 'Edit provider' : 'Add LLM provider'}</DialogTitle>
      <DialogContent>
        <Stepper activeStep={step} sx={{ mb: 3 }}>
          {STEPS.map((s) => (
            <Step key={s}><StepLabel>{s}</StepLabel></Step>
          ))}
        </Stepper>
        {step === 0 && renderStep0()}
        {step === 1 && renderStep1()}
        {step === 2 && renderStep2()}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        {step > 0 && !testing && (
          <Button onClick={() => setStep(step - 1)}>Back</Button>
        )}
        {step === 0 && (
          <Button variant="contained" onClick={() => setStep(1)}>Next</Button>
        )}
        {step === 1 && (
          <Button
            variant="contained"
            onClick={async () => {
              setStep(2)
              await saveDraftAndTest()
            }}
          >
            Test &amp; continue
          </Button>
        )}
        {step === 2 && (
          <Button
            variant="contained"
            disabled={!tested}
            onClick={finalSave}
          >
            Save
          </Button>
        )}
      </DialogActions>
    </Dialog>
  )
}
