/* Bifrost config API, reached through the backend's authenticated passthrough
   at /api/bifrost (services/api/routers/bifrost_config.py). Bifrost's own
   config store is the source of truth for providers, keys, model allow-lists,
   pricing and virtual keys — these types mirror its wire shapes rather than
   any Vigil table.

   Two shapes to know about, both Bifrost's:
   - Secrets read back masked and wrapped ({value, env_var, from_env}); writes
     take a bare string. Send `value` only when actually setting a credential —
     the passthrough substitutes the stored one otherwise.
   - Every key carries its own `status`, which is Bifrost's verdict after it
     validated the credential upstream. That is the health signal; there is no
     separate test call. */
import api from './api'

export interface BifrostSecret {
  value: string
  env_var: string
  from_env: boolean
}

export interface BifrostProvider {
  name: string
  network_config?: {
    default_request_timeout_in_seconds?: number
    max_retries?: number
    stream_idle_timeout_in_seconds?: number
    retry_backoff_initial?: number
    retry_backoff_max?: number
  }
  concurrency_and_buffer_size?: { concurrency?: number; buffer_size?: number }
  proxy_config?: unknown | null
  provider_status?: string
  status?: string
}

export interface BifrostKey {
  id: string
  name: string
  /** Masked + wrapped on read. Absent on write unless setting a new credential. */
  value?: BifrostSecret | string
  models: string[]
  blacklisted_models?: string[]
  weight: number
  enabled: boolean
  /** Bifrost's own verdict: "success", "unknown", "list_models_failed", ... */
  status?: string
  description?: string
  use_for_batch_api?: boolean
  ollama_key_config?: { url: BifrostSecret | string }
  vertex_key_config?: Record<string, unknown>
}

export interface BifrostKeyWrite {
  name: string
  weight: number
  enabled: boolean
  models: string[]
  /** Omit to keep the stored credential — the passthrough fills it in. */
  value?: string
  use_for_batch_api?: boolean
}

export interface BifrostModel {
  name: string
  provider: string
  max_input_tokens?: number
  max_output_tokens?: number
}

/** Pricing + capabilities for one model, from Bifrost's synced datasheet.
    Rates are per token, not per million. */
export interface BifrostModelParameters {
  provider: string
  base_model?: string
  mode?: string
  max_input_tokens?: number
  max_output_tokens?: number
  max_tokens?: number
  input_cost_per_token?: number
  output_cost_per_token?: number
  cache_read_input_token_cost?: number
  cache_creation_input_token_cost?: number
  supports_function_calling?: boolean
  supports_prompt_caching?: boolean
  supports_reasoning?: boolean
  supports_vision?: boolean
  supports_web_search?: boolean
  deprecation_date?: string
}

export interface BifrostBudget {
  id?: string
  max_limit: number
  reset_duration: string
  current_usage?: number
  last_reset?: string
}

export interface BifrostRateLimit {
  id?: string
  token_max_limit?: number
  token_reset_duration?: string
  request_max_limit?: number
  request_reset_duration?: string
}

export interface BifrostVirtualKey {
  id: string
  name: string
  description?: string
  /** The sk-bf-… secret. Returned in full on create; masked afterwards. */
  value?: string
  is_active: boolean
  allowed_models?: string[]
  allowed_providers?: string[]
  budget?: BifrostBudget | null
  rate_limit?: BifrostRateLimit | null
  team_id?: string | null
  customer_id?: string | null
}

export interface BifrostVirtualKeyWrite {
  name: string
  description?: string
  is_active?: boolean
  allowed_models?: string[]
  allowed_providers?: string[]
  budget?: BifrostBudget | null
  rate_limit?: BifrostRateLimit | null
}

const bf = '/bifrost'

export const bifrostApi = {
  listProviders: () => api.get<{ providers: BifrostProvider[] }>(`${bf}/providers`),
  // `provider` in, `name` back out — Bifrost's create payload does not use the
  // field its response does, and sending `name` fails with "Missing provider".
  createProvider: (provider: string) => api.post<BifrostProvider>(`${bf}/providers`, { provider }),
  updateProvider: (name: string, data: Partial<BifrostProvider>) =>
    api.put<BifrostProvider>(`${bf}/providers/${encodeURIComponent(name)}`, data),
  removeProvider: (name: string) => api.delete(`${bf}/providers/${encodeURIComponent(name)}`),

  listKeys: (provider: string) =>
    api.get<{ keys: BifrostKey[]; total: number }>(
      `${bf}/providers/${encodeURIComponent(provider)}/keys`,
    ),
  createKey: (provider: string, data: BifrostKeyWrite) =>
    api.post<BifrostKey>(`${bf}/providers/${encodeURIComponent(provider)}/keys`, data),
  updateKey: (provider: string, keyId: string, data: BifrostKeyWrite) =>
    api.put<BifrostKey>(
      `${bf}/providers/${encodeURIComponent(provider)}/keys/${encodeURIComponent(keyId)}`,
      data,
    ),
  removeKey: (provider: string, keyId: string) =>
    api.delete(
      `${bf}/providers/${encodeURIComponent(provider)}/keys/${encodeURIComponent(keyId)}`,
    ),

  listModels: (query?: string) =>
    api.get<{ models: BifrostModel[]; total: number }>(`${bf}/models`, {
      params: query ? { query } : undefined,
    }),
  // What this provider can route. Unfenced (no keys, or a `*` key) that is its whole
  // catalogue; fenced, it is only the fence — so callers widening one must union it.
  providerModels: (provider: string) =>
    api.get<{ models: BifrostModel[]; total: number }>(`${bf}/models`, {
      params: { provider, limit: 1000 },
    }),
  modelDetails: (query?: string) =>
    api.get<{ models: BifrostModel[]; total: number }>(`${bf}/models/details`, {
      params: query ? { query } : undefined,
    }),
  modelParameters: (model: string, provider: string) =>
    api.get<BifrostModelParameters>(`${bf}/models/parameters`, { params: { model, provider } }),

  listVirtualKeys: () =>
    api.get<{ virtual_keys: BifrostVirtualKey[] | null; count: number }>(
      `${bf}/governance/virtual-keys`,
    ),
  createVirtualKey: (data: BifrostVirtualKeyWrite) =>
    api.post<BifrostVirtualKey>(`${bf}/governance/virtual-keys`, data),
  updateVirtualKey: (id: string, data: Partial<BifrostVirtualKeyWrite>) =>
    api.put<BifrostVirtualKey>(`${bf}/governance/virtual-keys/${encodeURIComponent(id)}`, data),
  removeVirtualKey: (id: string) =>
    api.delete(`${bf}/governance/virtual-keys/${encodeURIComponent(id)}`),
}

/** Unwrap Bifrost's masked-secret wrapper for display. */
export function secretText(v: BifrostSecret | string | undefined): string {
  if (!v) return ''
  return typeof v === 'string' ? v : v.value
}

/** Bifrost prices per token; the console talks in dollars per million. */
export function perMillion(perToken: number | undefined): number | null {
  return typeof perToken === 'number' ? perToken * 1_000_000 : null
}
