import axios from 'axios'
import { basePath } from '../config/basePath'
import type { Schema } from './apiTypes'
import type { operations } from './generated/schema'
import { USER_ACTION_HEADER, justActed } from './userAction'
import { endReasonOf, isMfaSetupRequired, reportMfaSetupRequired, reportSignInEnded } from './signInEnd'
import { askStepUp, stepUpDetailOf } from './stepUp'

// Auth is cookie-based; withCredentials sends the HttpOnly cookies on every
// request, including through the Vite dev proxy.
const api = axios.create({
  baseURL: `${basePath}/api`,
  withCredentials: true,
  // a dead local backend leaves Vite's proxy connection open indefinitely
  timeout: 15_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// LLM-backed calls can legitimately run for minutes. Streaming endpoints pass
// 0 to disable the timeout for the life of the SSE connection.
export const LLM_TIMEOUT = 180_000

// Switching an MCP server on starts it and waits for it. A first start of an
// npx/uvx server downloads the package, which can take well over 15s.
export const MCP_START_TIMEOUT = 180_000

// The backend seeds csrf_token on any request lacking one, so after the first
// /auth/me call it is always present.
function readCookie(name: string): string | null {
  const match = document.cookie.match(
    new RegExp('(?:^|; )' + name.replace(/[.$?*|{}()[\]\\/+^]/g, '\\$&') + '=([^;]*)')
  )
  return match ? decodeURIComponent(match[1]) : null
}

const MUTATING_METHODS = new Set(['post', 'put', 'patch', 'delete'])

// Double-submit cookie: a cross-site attacker can't read the cookie, so it
// can't forge a matching header.
api.interceptors.request.use(
  (config) => {
    if (config.method && MUTATING_METHODS.has(config.method.toLowerCase())) {
      const csrf = readCookie('csrf_token')
      if (csrf) {
        config.headers['X-CSRF-Token'] = csrf
      }
    }
    if (justActed()) config.headers[USER_ACTION_HEADER] = '1'
    return config
  },
  (error) => Promise.reject(error)
)

// One refresh serves every request that 401s while it is in flight. /refresh
// is limited to 30 a minute per user across all tabs, and each tab's polling
// expires together, so a refresh per failed request can exhaust it (429).
let refreshPromise: Promise<unknown> | null = null

function refreshSession(): Promise<unknown> {
  if (!refreshPromise) {
    refreshPromise = api.post('/auth/refresh').finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

// the browser carries the refresh_token cookie automatically
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config

    const isAuthEndpoint =
      originalRequest?.url?.startsWith('/auth/login') ||
      originalRequest?.url?.startsWith('/auth/refresh')

    if (isAuthEndpoint) {
      return Promise.reject(error)
    }

    if (isMfaSetupRequired(error)) reportMfaSetupRequired()

    // A privileged change outside the confirmation window: ask once, then retry it
    // as it was. A cancel leaves the original refusal for the caller to show.
    const stepUp = stepUpDetailOf(error)
    if (stepUp && !originalRequest._stepUp) {
      originalRequest._stepUp = true
      try {
        await askStepUp(stepUp)
      } catch {
        return Promise.reject(error)
      }
      return api(originalRequest)
    }

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true

      try {
        await refreshSession()
        return api(originalRequest)
      } catch (refreshError) {
        // A 401 on the refresh is the server saying the sign-in is over.
        if ((refreshError as any)?.response?.status === 401) {
          reportSignInEnded(endReasonOf(refreshError) ?? endReasonOf(error))
        }
        return Promise.reject(refreshError)
      }
    }

    return Promise.reject(error)
  }
)

// Axios buffers the whole response body, so SSE has to use fetch — with the
// same cookie credentials, CSRF header and one-shot 401 retry applied by hand.
// `path` is relative to the API base.
export async function streamFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${basePath}/api${path}`
  const build = (): RequestInit => {
    const headers = new Headers(init.headers || {})
    const csrf = readCookie('csrf_token')
    if (csrf) headers.set('X-CSRF-Token', csrf)
    if (justActed()) headers.set(USER_ACTION_HEADER, '1')
    return { ...init, headers, credentials: 'include' }
  }
  let res = await fetch(url, build())
  if (res.status === 401) {
    try {
      await refreshSession()
      res = await fetch(url, build())
    } catch (refreshError) {
      if ((refreshError as any)?.response?.status === 401) {
        reportSignInEnded(endReasonOf(refreshError))
      }
    }
  }
  return res
}

export type NeedsYouItem = Schema<'NeedsYouItem'>

// The shell and the screen under it poll some reads together; callers that overlap share one
// request. Only in-flight reads are shared, so a read that starts after one lands is always fresh.
const inflight = new Map<string, Promise<unknown>>()
function coalesce<T>(key: string, read: () => Promise<T>): Promise<T> {
  const running = inflight.get(key)
  if (running) return running as Promise<T>
  const promise = read().finally(() => {
    if (inflight.get(key) === promise) inflight.delete(key)
  })
  inflight.set(key, promise)
  return promise
}
// a read already in flight when a write lands predates it: don't hand it to the next caller
const afterWrite = <T>(key: string, write: Promise<T>) => write.finally(() => inflight.delete(key))

export const approvalsApi = {
  list: (params?: {
    status?: string
    workflow_run_id?: string
    limit?: number
  }) => api.get('/approvals', { params }),

  listPending: () => api.get('/approvals/pending'),

  needsYou: (caseId?: string) =>
    caseId
      ? api.get<{ count: number; items: NeedsYouItem[] }>('/approvals/needs-you', { params: { case_id: caseId } })
      : coalesce('needs-you', () => api.get<{ count: number; items: NeedsYouItem[] }>('/approvals/needs-you')),

  approve: (actionId: string, approved_by?: string) =>
    afterWrite('needs-you', api.post(`/approvals/${actionId}/approve`, { approved_by })),

  reject: (actionId: string, reason: string, rejected_by?: string) =>
    afterWrite('needs-you', api.post(`/approvals/${actionId}/reject`, { reason, rejected_by })),
}

/** how a findings read treats findings naming an analyst-excluded IP */
export type ExclusionView = 'include' | 'hide' | 'only'

export const findingsApi = {
  getAll: (params?: {
    severity?: string
    data_source?: string
    cluster_id?: number
    min_anomaly_score?: number
    search?: string
    limit?: number
    exclusions?: ExclusionView
    sort_by?: string
    sort_order?: 'asc' | 'desc'
  }, signal?: AbortSignal) => api.get('/findings', { params, signal }),
  
  getById: (id: string, signal?: AbortSignal) => api.get(`/findings/${id}`, { signal }),
  
  getSummary: (params?: { exclusions?: ExclusionView }) =>
    api.get('/findings/stats/summary', { params }),
  
  getEnrichment: (id: string, force_regenerate: boolean = false) =>
    // May restart the Bifrost gateway, then wait on the configured model. A local
    // model can take minutes and the server bounds the call, so wait for its answer:
    // giving up first reports a summary it goes on to write and cache as failed.
    api.post(`/findings/${id}/enrich`, null, {
      params: { force_regenerate },
      timeout: 0,
    }),

  deleteAll: () => api.delete('/findings/all'),

  markNoise: (id: string) =>
    api.post(`/findings/${encodeURIComponent(id)}/noise`),

  clearNoise: (id: string) =>
    api.delete(`/findings/${encodeURIComponent(id)}/noise`),

  launchIntake: (id: string) =>
    api.post<{ queued: boolean; already_queued: boolean; trigger_id?: number | null }>(
      `/findings/${encodeURIComponent(id)}/intake`,
    ),
}

export interface IpExclusion {
  exclusion_id: string
  ip: string
  reason: string
  origin: 'ad_hoc' | 'finding' | 'case' | 'run'
  origin_ref?: string | null
  created_by: string
  created_at?: string | null
  removed_at?: string | null
  removed_by?: string | null
  removal_reason?: string | null
  active: boolean
  /** stored findings naming this address; active rows only */
  hidden_findings?: number | null
}

export const exclusionsApi = {
  list: (includeRemoved = false) =>
    api.get<{ exclusions: IpExclusion[]; total: number; hidden_findings_total: number }>('/exclusions', {
      params: { include_removed: includeRemoved },
    }),
  create: (body: {
    ip: string
    reason: string
    origin?: IpExclusion['origin']
    origin_ref?: string
  }) => api.post<IpExclusion>('/exclusions', body),
  remove: (id: string, reason?: string) =>
    api.post<IpExclusion>(`/exclusions/${encodeURIComponent(id)}/remove`, { reason: reason || null }),
}

export type Note = Schema<'NoteOut'>

/** Analyst notes on an entity (#3605); keys are typed `type:value`, and none means every note. */
export const notesApi = {
  list: (entityKeys: string[]) =>
    api.get<Schema<'NoteListResponse'>>('/memory/notes', {
      params: { entity_key: entityKeys },
      paramsSerializer: { indexes: null }, // entity_key=a&entity_key=b, as FastAPI reads a repeated query
    }),
  create: (body: Schema<'NoteCreate'>) => api.post<Note>('/memory/notes', body),
  withdraw: (id: string) => api.delete<Note>(`/memory/notes/${encodeURIComponent(id)}`),
}

export type HistoryImport = Schema<'ImportOut'>

/** Alert and ticket history imported into agent memory (#3965); an id holds a ':'. */
export const importsApi = {
  list: () => api.get<Schema<'ImportListResponse'>>('/memory/imports'),
  remove: (id: string) => api.delete<HistoryImport>(`/memory/imports/${encodeURIComponent(id)}`),
}

export interface CaseRecordRow {
  id: string
  at: string
  kind: string
  source: string
  chained: boolean
  text: string
  /** The run a run row is from; absent on audit and activity rows. */
  run_id?: string | null
}

export interface CaseRecordResponse {
  run_id: string | null
  investigation_id: string | null
  rows: CaseRecordRow[]
}

export const casesApi = {
  getAll: (params?: {
    state?: string
    workflow?: string
    priority?: string
    data_source?: string
    sla_at_risk?: boolean
    assignee?: string
    closed?: boolean
    query?: string
    needs_you?: boolean
    working?: boolean
    closed_by?: 'agent' | 'person'
    alerts_from?: 'today' | 'all'
    kind?: string
    limit?: number
    offset?: number
  }) => api.get<Schema<'CaseListResponse'>>('/cases', { params }),

  getById: (id: string, signal?: AbortSignal) =>
    api.get<Schema<'CaseDetailResponse'>>(`/cases/${id}`, { signal }),

  create: (data: Schema<'CaseCreate'>) =>
    api.post<Schema<'CaseSchema'>>('/cases', data),

  update: (id: string, data: Schema<'CaseUpdate'>) =>
    api.patch<Schema<'CaseSuccessResponse'>>(`/cases/${id}`, data),

  delete: (id: string) =>
    api.delete<Schema<'CaseSuccessResponse'>>(`/cases/${id}`),

  deleteAll: () => api.delete<Schema<'CasePurgeResponse'>>('/cases/all'),

  getSummary: () => api.get<Schema<'CaseSummaryResponse'>>('/cases/stats/summary'),

  getComments: (id: string) =>
    api.get<Schema<'CaseCommentsResponse'>>(`/cases/${id}/comments`),
  addComment: (id: string, data: Schema<'CommentAdd'>) =>
    api.post<Schema<'CaseCommentSchema'>>(`/cases/${id}/comments`, data),

  /** Keeps the original of a hunt's attached document on the case, as one `document` evidence row. */
  attachDocument: (id: string, file: File, opts: { name?: string; pages: number }) => {
    const form = new FormData()
    form.append('file', file)
    if (opts.name) form.append('name', opts.name)
    form.append('pages', String(opts.pages))
    return api.post<Schema<'CaseEvidenceSchema'>>(`/cases/${id}/attachments`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
    })
  },
  getEvidence: (id: string) =>
    api.get<Schema<'CaseEvidenceListResponse'>>(`/cases/${id}/evidence`),
  addEvidence: (id: string, data: Schema<'EvidenceAdd'>) =>
    api.post<Schema<'CaseEvidenceSchema'>>(`/cases/${id}/evidence`, data),

  getIOCs: (id: string) =>
    api.get<Schema<'CaseIOCListResponse'>>(`/cases/${id}/iocs`),
  addIOC: (id: string, data: Schema<'IOCAdd'>) =>
    api.post<Schema<'CaseIOCSchema'>>(`/cases/${id}/iocs`, data),

  getTasks: (id: string) =>
    api.get<Schema<'CaseTasksResponse'>>(`/cases/${id}/tasks`),
  addTask: (id: string, data: Schema<'TaskAdd'>) =>
    api.post<Schema<'CaseTaskSchema'>>(`/cases/${id}/tasks`, data),
  updateTask: (id: string, taskId: number, data: Schema<'TaskUpdate'>) =>
    api.put<Schema<'CaseTaskSchema'>>(`/cases/${id}/tasks/${taskId}`, data),

  getSLA: (id: string) =>
    api.get<Schema<'CaseSLAStatusSchema'>>(`/cases/${id}/sla`),

  /** The proof card of each rule version that raised the case's alerts, worst first. */
  getRules: (id: string) =>
    api.get<Schema<'CaseRulesResponse'>>(`/cases/${id}/rules`),

  closeCase: (id: string, data: Schema<'ClosureInfo'>) =>
    api.post<Schema<'CaseCloseResponse'>>(`/cases/${id}/close`, data),

  escalate: (id: string, data: Schema<'EscalationAdd'>) =>
    api.post<Schema<'CaseSuccessResponse'>>(`/cases/${id}/escalate`, data),

  getEscalations: (id: string) =>
    api.get<Schema<'CaseEscalationsResponse'>>(`/cases/${id}/escalations`),

  /** Links to other Cases, each with that Case's title and status. */
  getRelationships: (id: string) =>
    api.get<Schema<'CaseRelationshipsResponse'>>(`/cases/${id}/relationships`),

  // The merged record. There is no audit-log route; this read replaced it.
  getRecord: (id: string) =>
    api.get<CaseRecordResponse>(`/cases/${id}/record`),

  merge: (targetCaseId: string, sourceCaseId: string) =>
    api.post<Schema<'CaseMergeResponse'>>(`/cases/${targetCaseId}/merge`, {
      source_case_id: sourceCaseId,
      merged_by: 'user',
    }),

  /** Exports the case to a Jira issue and returns the note to show; a refused export throws its error. */
  exportJira: async (id: string, projectKey: string): Promise<string> => {
    const res = await api.post<Schema<'JiraExportResponse'>>(`/cases/${encodeURIComponent(id)}/export/jira`, {
      project_key: projectKey,
    })
    if (res.data?.success === false) throw new Error(res.data.error || 'Jira export failed')
    return res.data?.issue_key ? `Created ${res.data.issue_key}` : 'Ticket created'
  },
}

export const slaPoliciesApi = {
  getAll: (params?: {
    active_only?: boolean
    priority_level?: string
    default_only?: boolean
  }) => api.get('/sla-policies/', { params }),
  
  getById: (policyId: string) => api.get(`/sla-policies/${policyId}`),
  
  create: (data: {
    policy_id: string
    name: string
    description?: string
    priority_level: string  // critical, high, medium, low
    response_time_hours: number
    resolution_time_hours: number
    business_hours_only?: boolean
    escalation_rules?: any
    notification_thresholds?: number[]
    is_active?: boolean
    is_default?: boolean
  }) => api.post('/sla-policies/', data),
  
  update: (policyId: string, data: {
    name?: string
    description?: string
    response_time_hours?: number
    resolution_time_hours?: number
    business_hours_only?: boolean
    escalation_rules?: any
    notification_thresholds?: number[]
    is_active?: boolean
    is_default?: boolean
  }) => api.put(`/sla-policies/${policyId}`, data),
  
  delete: (policyId: string) => api.delete(`/sla-policies/${policyId}`),
  
  
  getUsage: (policyId: string, params?: { since?: string }) =>
    api.get(`/sla-policies/${policyId}/usage`, { params }),
  
}

export interface MetricsWindow {
  start_date: string
  end_date: string
}

export const caseMetricsApi = {
  getSummary: (params: MetricsWindow) =>
    api.get<Schema<'CaseMetricsSummaryResponse'>>('/cases/metrics/summary', { params }),
  getMTTD: (params: MetricsWindow) =>
    api.get<Schema<'MttdResponse'>>('/cases/metrics/mttd', { params }),
  getMTTR: (params: MetricsWindow) =>
    api.get<Schema<'MttrResponse'>>('/cases/metrics/mttr', { params }),
  getByPriority: (params: MetricsWindow) =>
    api.get<Schema<'ByPriorityResponse'>>('/cases/metrics/by-priority', { params }),
  getByStatus: (params: MetricsWindow) =>
    api.get<Schema<'ByStatusResponse'>>('/cases/metrics/by-status', { params }),
  getAnalystPerformance: (params: MetricsWindow) =>
    api.get('/cases/metrics/analyst-performance', { params }),
}

export const caseSearchApi = {
  search: (data: Schema<'SearchRequest'>) =>
    api.post<Schema<'CaseSearchResponse'>>('/cases/search', data),
}

/** The command bar's one search: exact ids, cases, alerts, workflows, agents and skills. */
export const searchApi = {
  query: (q: string, signal?: AbortSignal) =>
    api.get<Schema<'SearchResults'>>('/search', { params: { q }, signal }),
}

export interface ReloadServersResponse {
  /** false when any server in `failed` could not be reconnected or disconnected */
  success: boolean
  message: string
  total_servers: number
  servers: string[]
  reconnected: string[]
  disconnected: string[]
  failed: Record<string, string | null>
}

export const mcpApi = {
  listServers: () => api.get('/mcp/servers'),

  getStatuses: () => api.get('/mcp/servers/status'),

  setServerEnabled: (name: string, enabled: boolean) =>
    api.put(`/mcp/servers/${name}/enabled`, { enabled }, { timeout: MCP_START_TIMEOUT }),

  // Re-reads mcp-config.json and integration URLs into the server catalog, then
  // reconnects changed servers and stops removed or disabled ones. A file that
  // does not parse answers 422 and changes nothing. Needs integrations.write.
  reloadServers: () => api.post<ReloadServersResponse>('/mcp/servers/reload'),

  // Vigil's own MCP surface: whether it listens, and what may open it.
  getSurface: () => api.get('/mcp/surface'),

  setSurfaceEnabled: (enabled: boolean) => api.put('/mcp/surface', { enabled }),

  // The token is in this response and nowhere else.
  mintCredential: (label: string, expiresInDays?: number) =>
    api.post('/mcp/surface/credentials', {
      label,
      expires_in_days: expiresInDays ?? null,
    }),

  revokeCredential: (credentialId: string) =>
    api.delete(`/mcp/surface/credentials/${credentialId}`),
}

export const agentsApi = {
  listAgents: () => api.get('/agents/agents'),
  // the router prefix and the route path both say "agents"
  setEnabled: (agent_id: string, enabled: boolean) =>
    api.put(`/agents/agents/${agent_id}/enabled`, { enabled }),

  getAvailableTools: () => api.get('/agents/custom/_meta/tools'),
  // any agent, built-in or custom, with its prompt
  getAgent: (agent_id: string) => api.get(`/agents/agents/${agent_id}`),
  // a built-in is never mutated; a fork stores it as a manifest under custom-<id>
  forkAgent: (source_agent_id: string) => api.post<AgentManifest>(`/agents/${source_agent_id}/fork`),
  // a draft manifest, checked as a PUT would be; a refused draft is a 422 with the reason
  generateManifest: (data: { description: string; current_draft?: string | null; feedback?: string }) =>
    api.post<{ manifest: string }>('/agents/custom/generate', data, { timeout: LLM_TIMEOUT }),

  // Operator agents are manifests (/api/v1/agents): read, PUT whole, delete.
  listManifests: () => api.get<Schema<'AgentManifestListResponse'>>('/v1/agents'),
  getManifest: (agent_id: string) => api.get<AgentManifest>(`/v1/agents/${agent_id}`),
  putManifest: (agent_id: string, manifest: string) => api.put<AgentManifest>(`/v1/agents/${agent_id}`, { manifest }),
  deleteManifest: (agent_id: string) => api.delete(`/v1/agents/${agent_id}`),
}

export type AgentManifest = Schema<'AgentManifestResponse'>

export interface AgentSummary {
  id: string
  name: string
  description?: string
}

/** Reads the shell folds itself. Health is public; processes and the provider map need a sign-in. */
export const consoleApi = {
  getHealth: () => api.get('/health'),
  getProcesses: () => api.get<Schema<'ProcessStates'>>('/processes'),
  getRoutability: () => api.get<Schema<'LlmPathHealth'>>('/llm/health'),
}

export interface IntegrationTestResult {
  success: boolean
  message?: string
  // "not_testable": a catalog-only entry with no MCP server behind it
  reason?: string
  servers?: { name: string; success: boolean; error?: string; missing_credentials?: string[] }[]
  /** the read-only check of the saved URL and credentials, for integrations that have one */
  credentials?: { success: boolean; message: string }
}

export const configApi = {
  
  getS3: () => api.get('/config/s3'),
  setS3: (data: {
    bucket_name: string
    region: string
    auth_method?: string
    aws_profile?: string
    access_key_id?: string
    secret_access_key?: string
    session_token?: string
    parquet_prefix?: string
  }) => api.post('/config/s3', data),
  
  getDemoMode: () => api.get('/config/demo-mode'),
  setDemoMode: (enabled: boolean) => api.post('/config/demo-mode', { enabled }),
  getSetupSteps: () =>
    coalesce('setup-steps', () => api.get<Schema<'SetupStepsResponse'>>('/config/setup-steps')),
  getSetupState: () => api.get<Schema<'SetupState'>>('/config/setup-state'),
  putSetupState: (update: Schema<'SetupStateUpdate'>) =>
    afterWrite('setup-steps', api.put<Schema<'SetupState'>>('/config/setup-state', update)),
  resetDemoData: () => api.post('/config/demo-mode/reset'),
  
  getIntegrations: () => api.get('/config/integrations'),
  setIntegrations: (data: {
    enabled_integrations: string[]
    integrations: Record<string, any>
  }) => api.post('/config/integrations', data),
  // Probes the stored config, so save first. 400 when nothing is saved.
  testIntegration: (id: string) =>
    api.post<IntegrationTestResult>(`/config/integrations/${encodeURIComponent(id)}/test`),
  
  getGeneral: () => api.get('/config/general'),
  setGeneral: (data: {
    show_notifications: boolean
    enable_keyring: boolean
  }) => api.post('/config/general', data),
  
  getTheme: () => api.get('/config/theme'),
  setTheme: (theme: string) => api.post('/config/theme', { theme }),

  getPostgreSQL: () => api.get('/config/postgresql'),
  setPostgreSQL: (connection_string: string) => api.post('/config/postgresql', { connection_string }),

  // settings live in the encrypted secrets store, so they're readable before
  // the engine boots. Restart-required.
  getPlatformDatabase: () => api.get<PlatformDatabaseProxyConfig>('/config/platform-database'),
  setPlatformDatabase: (data: {
    proxy_type: string
    proxy_host?: string
    proxy_port?: number
    proxy_username?: string
    proxy_password?: string
    ssh_private_key_path?: string
    ssh_key_passphrase?: string
    verify_proxy_tls?: boolean
  }) => api.post('/config/platform-database', data),

  getAIOperations: () => api.get('/config/ai-operations'),
  setAIOperations: (data: {
    local_ollama_recovery_enabled: boolean
    local_ollama_recovery_retry_limit: number
    local_ollama_recovery_restart_gateway: boolean
  }) => api.post('/config/ai-operations', data),

  getDarktrace: () => api.get('/config/darktrace'),
  setDarktrace: (data: {
    enabled: boolean
    url: string
    max_body_kb: number
    webhook_secret?: string
  }) => api.post('/config/darktrace', data),

  getOrchestrator: () => coalesce('orchestrator', () => api.get('/config/orchestrator')),
  getIntent: () => api.get('/config/intent'),
  setOrchestrator: (data: {
    enabled: boolean
    dry_run: boolean
    max_concurrent_agents: number
    max_iterations_per_agent: number
    max_runtime_per_investigation: number
    max_cost_per_investigation: number
    max_total_hourly_cost: number
    loop_interval: number
    stale_threshold: number
    workdir_base: string
  }) => {
    // GET also carries profiles, defaults and bounds; none are stored
    const rest: Record<string, unknown> = { ...data }
    for (const key of ['profiles', 'defaults', 'bounds']) delete rest[key]
    return afterWrite('orchestrator', api.post('/config/orchestrator', rest))
  },
}

export interface PlatformDatabaseProxyConfig {
  proxy_type: string
  proxy_host: string
  proxy_port: number
  proxy_username: string
  ssh_private_key_path: string
  verify_proxy_tls: boolean
  // Booleans only — secret values are never returned by the API.
  has_proxy_password: boolean
  has_ssh_key_passphrase: boolean
}

// the backend calls the connector BFF server-to-server, so the mint secret
// never reaches the browser
export const extensionsApi = {
  getSessionToken: (integrationId: string) =>
    api.get<{ token: string | null; expires_in: number | null; user: string }>(
      `/integrations/${integrationId}/session-token`,
    ),
}

export const customIntegrationsApi = {
  generate: (data: Schema<'CustomIntegrationRequest'>) =>
    api.post('/custom-integrations/generate', data, { timeout: LLM_TIMEOUT }),
  save: (data: Schema<'SaveIntegrationRequest'>) =>
    api.post('/custom-integrations/save', data),
  validate: (integrationId: string) =>
    api.post(`/custom-integrations/${integrationId}/validate`),
}

export interface LLMProvider {
  provider_id: string
  provider_type: 'anthropic' | 'openai' | 'ollama' | 'vertex' | 'openrouter'
  name: string
  base_url: string | null
  has_api_key: boolean
  default_model: string
  is_active: boolean
  is_default: boolean
  config: Record<string, any>
  last_test_at: string | null
  last_test_success: boolean | null
  last_error: string | null
  created_at: string | null
  updated_at: string | null
}

export const llmProviderApi = {
  list: () => api.get<LLMProvider[]>('/llm/providers/'),
}

export interface AIModelInfo {
  model_id: string
  provider_id: string
  provider_type: 'anthropic' | 'openai' | 'ollama' | string
  display_name: string
  context_window: number
  input_cost_per_1k: number
  output_cost_per_1k: number
  supports_tools: boolean
  supports_thinking: boolean
  supports_vision: boolean
}

export interface ComponentAssignment {
  component: string
  provider_id: string
  model_id: string
  settings: Record<string, any>
  updated_by: string | null
  updated_at: string | null
}

export interface AIConfigResponse {
  components: string[]
  assignments: Record<string, ComponentAssignment>
}

export const aiConfigApi = {
  getConfig: () => api.get<AIConfigResponse>('/ai/config'),
  setComponent: (
    component: string,
    payload: { provider_id: string; model_id: string; settings?: Record<string, any> },
  ) => api.put<ComponentAssignment>(`/ai/config/${component}`, payload),
  clearComponent: (component: string) =>
    api.delete<{ component: string; cleared: boolean }>(`/ai/config/${component}`),
  listModels: () => api.get<{ models: AIModelInfo[] }>('/ai/models'),
}

export interface IngestionJob {
  job_id: string
  filename: string
  format: string
  data_type: string
  status: 'running' | 'succeeded' | 'failed'
  determinate: boolean // false for CSV/JSONL — row count unknown until read ends
  processed: number
  total: number
  created_at: string
  finished_at: string | null
  message: string
  error: string | null
  stats: Record<string, number>
}

export const ingestionApi = {
  listS3Files: (prefix?: string) =>
    api.get('/ingest/s3-files', { params: { prefix: prefix ?? '' } }),
  ingestS3File: (key: string) =>
    api.post('/ingest/s3-file', { key }),
  // returns once the body is spooled; the ingest runs as a background job
  uploadFile: (file: File, dataType: string = 'finding', sourceSystem?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('data_type', dataType)
    if (sourceSystem) formData.append('source_system', sourceSystem)
    return api.post<IngestionJob>('/ingest/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
    })
  },
  // closed, classified records from a configured Defender or Sentinel, read into memory
  importConnectedHistory: (source: ConnectedHistorySource, days: number) =>
    api.post<IngestionJob>('/ingest/history/connected', { source, days }),
  listJobs: () => api.get<IngestionJob[]>('/ingest/jobs'),
  getJob: (jobId: string) => api.get<IngestionJob>(`/ingest/jobs/${jobId}`),
  // a fixed set of example alerts, stored as findings the daemon triages
  sampleAlerts: () => api.get<SampleAlertsStatus>('/ingest/sample-alerts'),
  loadSampleAlerts: () => api.post<SampleAlertsStatus>('/ingest/sample-alerts'),
  clearSampleAlerts: () => api.delete<SampleAlertsCleared>('/ingest/sample-alerts'),
}

export type ConnectedHistorySource = Schema<'ConnectedHistoryRequest'>['source']
export type SampleAlertsStatus = Schema<'SampleAlertsStatus'>
export type SampleAlertsCleared = Schema<'SampleAlertsCleared'>

export interface BudgetSettings {
  default_vk: string
}

interface BifrostBudgetTier {
  id: string
  max_limit: number
  current_usage: number
  reset_duration: string
  calendar_aligned: boolean
  last_reset: string
}

export interface BifrostRateLimit {
  id: string
  token_max_limit: number
  token_current_usage: number
  token_reset_duration: string
  request_max_limit: number | null
  request_current_usage: number
  request_reset_duration: string | null
}

export interface BudgetQuotaResponse {
  configured: boolean
  available?: boolean
  virtual_key_id?: string
  message?: string
  quota?: {
    virtual_key_name: string
    is_active: boolean
    budgets: BifrostBudgetTier[]
    rate_limit?: BifrostRateLimit
  }
}

export const budgetsApi = {
  get: () => api.get<BudgetSettings>('/analytics/budget'),
  set: (payload: BudgetSettings) =>
    api.put<BudgetSettings>('/analytics/budget', payload),
  getQuota: () => api.get<BudgetQuotaResponse>('/analytics/budget/quota'),
}

/** One workflow's tally in `agent_agreement` (core/reporting/analytics_service.py). */
export interface WorkflowAgreement {
  workflow_id: string
  agreed: number
  disagreed: number
  agreement_rate: number | null
  abstained: number
  closed_without_determination: number
  closed_by_agent_alone: number
}

export const analyticsApi = {
  /** The agent_agreement block of GET /analytics; the route returns an untyped dict, so only this part is typed. */
  getAgreement: (timeRange: '24h' | '7d' | '30d' | 'all' = '30d') =>
    api
      .get<{ agent_agreement?: { by_workflow?: WorkflowAgreement[] } }>('/analytics', { params: { time_range: timeRange } })
      .then((res) => res.data.agent_agreement?.by_workflow ?? []),
}

export const storageApi = {
  getStatus: () => api.get('/storage/status'),
  getHealth: () => api.get('/storage/health'),
  reconnect: () => api.post('/storage/reconnect'),
}

export const attackApi = {
  getTechniqueRollup: (
    min_confidence: number = 0.0,
    time_range: string = 'all',
    run_id?: string,
  ) =>
    api.get('/attack/techniques/rollup', {
      params: {
        min_confidence,
        time_range,
        ...(run_id ? { run_id } : {}),
      },
    }),
  
  getFindingsByTechnique: (technique_id: string, min_confidence: number, time_range: string) =>
    api.get(`/attack/techniques/${technique_id}/findings`, { params: { min_confidence, time_range } }),
}

export const timelineApi = {
  
  
  getTimelineRange: (params: {
    start?: string
    end?: string
    severity?: string
    data_source?: string
    limit?: number
  }) => api.get('/timeline/range', { params }),
  
}

/** A live rule only Vigil runs: Stop halts its detection. */
export interface OverlayOnlyRule {
  id: number
  name: string
  language: string
  /** Its format has a deploy target Vigil deploys to, so it can open a deploy pull request. */
  deploy_target: boolean
  /** Why its format's deploy target is refused (not a GitHub repo); null when it isn't. */
  target_refused: string | null
  /** What its deploy pull request came to on its current version; null before one.
   *  On a failure, error is the raw line and reason says what a person can do.
   *  supersede_error: closing an older version's PR failed (retried next sweep); the PR still stands. */
  deploy: {
    version: number
    at: string
    pr_url?: string
    in_repo?: string
    error?: string
    reason?: string
    supersede_error?: string
  } | null
}

/** One open rule-health flag: a check on a live rule that went pass -> fail. */
export interface HealthEntry {
  change_id: number
  rule_id: number
  name: string
  tool: string
  status: string
  /** A check key; or Vigil's own move, its change_id negative: 'revert' (live -> shadow) or 'off' (turned off from live or shadow). */
  check: string
  reason: string | null
  since: string
  read: boolean
}

/** Each rollout metric carries where its numbers come from and what they can't say. */
interface MetricNotes { source: string; unknown: string[] }

/** One SIEM's active copies of the rules declaring a technique. */
export interface CoverageSiem {
  copies: number
  proven: number
  failing: number
  state: 'proven' | 'unproven'
}

export type CoverageState = 'proven' | 'unproven' | 'no_rule'
export type CoverageSiemName = 'elastic' | 'splunk' | 'wazuh'

/** One ATT&CK technique on the coverage heat map. */
export interface CoverageCell {
  technique: string
  name: string
  tactic: string
  state: CoverageState
  rules: number
  disabled: number
  seen: number
  replayed: number
  siems: Partial<Record<CoverageSiemName, CoverageSiem>>
}

/** GET /detection-rules/coverage: ATT&CK coverage by technique and SIEM (#4103). */
export interface DetectionCoverage {
  techniques: CoverageCell[]
  tactics: string[]
  /** Every SIEM with a listed (active or disabled) rule, in order, whether or not its rules declare a technique. */
  siems: CoverageSiemName[]
  summary: Record<CoverageState, number>
  rule_total: number
  mapped: number
  source: string
}

/** GET /detection-rules/metrics: the rollout's success metrics from stored data. */
export interface DetectionMetrics {
  generated_at: string
  gap_to_shadow: MetricNotes & { gaps: number; closed: number; median_hours: number | null; within_target: number; target_hours: number }
  shadow_to_live: MetricNotes & { promoted: number; promoted_by_vigil: number; median_hours: number | null; in_shadow: number; exit_criteria: string }
  live_health: MetricNotes & { live_rules: number; passing: number; proven: number; not_seen: number; broken: number; share: number | null; target: number; blind_spots: number; rules_starved: number }
  first_week: MetricNotes & {
    started_at: string | null
    ends_at: string | null
    week_over: boolean
    silent: number
    no_data: number
    never_firing: number
    first_found_day: Partial<Record<'silent' | 'no_data' | 'never_firing', number>>
  }
  search_budget: MetricNotes & {
    integrations: Record<string, { searches_last_hour: number; max_searches_per_hour: number }>
    throttle_errors: number
  }
}

export interface HealthInbox {
  /** Unread entries, newest first; empty when the summary stands for them. */
  entries: HealthEntry[]
  /** The newest few read entries, for the panel's Earlier row. */
  earlier: HealthEntry[]
  earlier_total: number
  total: number
  unread: number
  /** Past five unread: the unread count by check and the rules they name. */
  summary: { count: number; by_check: Record<string, number>; rules: number; rule_ids: number[]; since: string } | null
}

/** One notice addressed to the signed-in person. */
export interface Notice {
  id: number
  kind: string
  title: string
  message: string
  /** An in-app path ('/cases?x=1'), when the notice has one. */
  link: string | null
  priority: string
  read: boolean
  created_at: string
}

/** GET /notices: the person's notices and the live warnings computed on this request. */
export interface Notices {
  /** Unread, newest first. */
  notices: Notice[]
  /** The newest few read ones, for the panel's Earlier row. */
  earlier: Notice[]
  unread: number
  total: number
  conditions: { message: string; link?: string | null }[]
}

/** One stage row of an overlay rule: a move between stages, by Vigil or a person. */
export interface RuleStageRow {
  stage_id: number
  version: number
  /** null on the rule's first row. */
  from_stage: 'shadow' | 'live' | 'off' | null
  to_stage: 'shadow' | 'live' | 'off'
  /** 'system' for Vigil's own moves, else the person's username. */
  actor: string
  reason: string
  at: string
}

export interface RuleStages {
  rule_id: number
  /** Newest first. */
  stages: RuleStageRow[]
  /** A person's Revert holds it in shadow until a person releases it. */
  held: boolean
}

/** What a person's Revert or release wrote. `hold` is true while the rule is held in shadow. */
export interface RuleStageChange {
  success: boolean
  rule_id: number
  stage: 'shadow' | 'live' | 'off'
  hold: boolean
}

/** The signed-in person's notices and live conditions (the bell's second source). */
export const noticesApi = {
  list: () => api.get<Notices>('/notices'),

  /** Marks these of the person's own notices read, or all of them; returns the list. */
  markRead: (ids: number[] | 'all') =>
    api.post<Notices>('/notices/read', ids === 'all' ? { all: true } : { ids }),
}

export const detectionRulesApi = {
  listSources: () => api.get('/detection-rules/sources'),

  /** Every listed rule copy's health and its reason, worst first; one health's when given. */
  rules: (params: { health?: string; tool?: string; q?: string; offset?: number; sort?: string; flag?: string; rules?: string; has_exceptions?: boolean; stage?: 'shadow' | 'live' | 'off' } = {}) =>
    api.get('/detection-rules/rules', { params }),

  /** The findings a rule copy raised in the fires window, newest first, as the rules list counts them. */
  ruleFindings: (ruleId: number) => api.get(`/detection-rules/rules/${ruleId}/findings`),

  /** A rule's stage history, newest first. */
  ruleStages: (ruleId: number) => api.get<RuleStages>(`/detection-rules/rules/${ruleId}/stages`),

  /** Moves a live rule to shadow and holds it there until a person releases it. */
  revertRule: (ruleId: number, reason: string) =>
    api.post<RuleStageChange>(`/detection-rules/rules/${ruleId}/revert`, { reason }),

  /** Lifts a person's hold: the normal rollout applies to the rule again. */
  releaseRule: (ruleId: number, reason: string) =>
    api.post<RuleStageChange>(`/detection-rules/rules/${ruleId}/release`, { reason }),

  /** The open rule-health flags, newest first, read or unread for the signed-in person. */
  healthInbox: () => api.get<HealthInbox>('/detection-rules/health-inbox'),

  /** The rollout's success metrics, each with its source and what it can't say. It
   * reads 30 days of check changes, so it gets longer than the default timeout. */
  metrics: () => api.get<DetectionMetrics>('/detection-rules/metrics'),

  /** ATT&CK coverage by technique and SIEM: the heat map. */
  coverage: () => api.get<DetectionCoverage>('/detection-rules/coverage'),

  /** Marks these entries read for the signed-in person, or every open one; returns the inbox. */
  markHealthRead: (changeIds: number[] | 'all') =>
    api.post<HealthInbox>(
      '/detection-rules/health-inbox/read',
      changeIds === 'all' ? { all: true } : { change_ids: changeIds },
    ),

  /** An overlay copy's exceptions, active and removed, with per-day matches. */
  exceptions: (ruleId: number) => api.get(`/detection-rules/rules/${ruleId}/exceptions`),

  addException: (ruleId: number, data: { match: Record<string, string>; note: string }) =>
    api.post(`/detection-rules/rules/${ruleId}/exceptions`, data),

  removeException: (ruleId: number, exceptionId: number, reason: string) =>
    api.delete(`/detection-rules/rules/${ruleId}/exceptions/${exceptionId}`, { params: { reason } }),
  
  
  addSource: (data: {
    name: string
    source_type: 'git' | 'local'
    format: 'sigma' | 'splunk' | 'elastic' | 'kql' | 'auto'
    url?: string
    path?: string
    subdirectory?: string
    story_subdirectory?: string
    deploy_target?: boolean
    github_enterprise?: boolean
  }) => api.post('/detection-rules/sources', data),

  /** Mark (or clear) the git source Vigil's live rules of its format go to. */
  setDeployTarget: (sourceId: string, deployTarget: boolean, githubEnterprise?: boolean) =>
    api.post(`/detection-rules/sources/${sourceId}/deploy-target`, {
      deploy_target: deployTarget,
      ...(githubEnterprise === undefined ? {} : { github_enterprise: githubEnterprise }),
    }),
  
  removeSource: (sourceId: string, deleteFiles: boolean = false) =>
    api.delete(`/detection-rules/sources/${sourceId}`, { params: { delete_files: deleteFiles } }),
  
  // A clone or pull runs for minutes (the server allows 300 s per repository).
  updateSource: (sourceId: string) =>
    api.post(`/detection-rules/sources/${sourceId}/update`, undefined, { timeout: 0 }),
  
  updateAll: () => api.post('/detection-rules/update-all', undefined, { timeout: 0 }),
  
  getStats: () => api.get('/detection-rules/stats'),

  /** Live overlay rules no SIEM rule runs: only Vigil detects with them. */
  getOverlayOnly: () =>
    api.get<{ rules: OverlayOnlyRule[] }>('/detection-rules/overlay-only'),

  /** Queue a deploy pull request for these live overlay-only rules (all when none given). */
  deployOverlayOnly: (ruleIds?: number[]) =>
    api.post<{
      /** Under Assist, the ones now waiting for a person's approval. */
      asked?: { rule_id: number; action_id: string }[]
      opened: { rule_id: number; pr_url?: string; in_repo?: string; supersede_error?: string }[]
      failed: { rule_id: number; error: string; reason: string }[]
      skipped: { rule_id: number; reason: string }[]
    }>('/detection-rules/overlay-only/deploy', { rule_ids: ruleIds ?? null }),
  
  
  reload: () => api.post('/detection-rules/reload'),

  /** The proof card zip (signed proof-card/v1 JSON, its .sig, a PDF, the public key):
   * these rule copies, or every SIEM and Vigil rule with coverage when none given. */
  proofCard: (ruleIds?: number[]) =>
    api.post<Blob>('/detection-rules/proof-card', { rule_ids: ruleIds ?? null }, { responseType: 'blob', timeout: 0 }),
}

export const localServicesApi = {
  getSplunkStatus: () => api.get('/services/splunk/status'),
  startSplunk: () => api.post('/services/splunk/start'),
  stopSplunk: () => api.post('/services/splunk/stop'),
  restartSplunk: () => api.post('/services/splunk/restart'),

  // service names are allowlisted server-side
  list: () => api.get('/services'),
  getStatus: (name: string) => api.get(`/services/${name}/status`),
  start: (name: string) => api.post(`/services/${name}/start`),
  stop: (name: string) => api.post(`/services/${name}/stop`),
  restart: (name: string) => api.post(`/services/${name}/restart`),

  setAutostart: (services: string[]) => api.put('/services/autostart', { services }),
}

interface WorkflowPhase {
  phase_id?: string
  order?: number
  agent_id: string
  name: string
  purpose?: string
  steps: string[]
  expected_output?: string
  approval_required?: boolean
}

/** What POST /workflows/generate returns: a definition that is not saved anywhere. */
export interface GeneratedDraft {
  name: string
  description: string
  use_case?: string
  trigger_examples?: string[]
  phases: WorkflowPhase[]
}

/** What a hunt lead was shown before one decision. Mirrors `Digest` in
 *  services/agent/plugins/hunt/types.ts; only what the console renders is typed. */
export interface ReplayDigest {
  iteration: number
  narrative: string
  hypotheses: { hypothesis_id: string; statement: string; status: string }[]
  recent_evidence: { evidence_id: string; source_system: string; summary: string; salience: string; why_notable: string; instruction_like: boolean }[]
  focus: { entity: string | null; hypothesis: string | null }
  omitted: { count: number; evidence_ids: string[] }
  open_questions: string[]
  budget_remaining: { iterations: number; cost_usd: number }
  directives: string[]
  notes: string[]
}
export interface ReplayedDecision {
  decision_id: string
  iteration: number
  action: string
  target: string | null
  cost_usd: number
  /** False when the digest was not rebuilt exactly; always false now nothing is rebuilt. */
  exact: boolean
  /** Null: the digest builder went with the controller, so the record is shown as journaled. */
  rebuilt: ReplayDigest | null
  recorded: ReplayDigest
  mismatch: string | null
}
export interface ReplayReport {
  hunt_id: string
  decisions: ReplayedDecision[]
  reproduced: number
  inexact: number
  /** Read off the run's own recall event, not a live memory read. */
  recalled: string[]
}

interface HuntDocument {
  text: string
  pages: number
  condensed: boolean
}

export const workflowApi = {
  listAll: () => api.get('/workflows'),
  get: (id: string) => api.get(`/workflows/${id}`),
  /** Who runs it, its model, what it may do, where it stops and pauses; a hunt kind adds capabilities and pricing. */
  preflight: (id: string) => api.get(`/workflows/${id}/preflight`),
  /** Turns a workflow on or off. A 409 carries the reason it cannot be turned off. */
  setEnabled: (id: string, enabled: boolean) => api.put(`/workflows/${id}/enabled`, { enabled }),
  execute: (id: string, params: {
    finding_id?: string
    case_id?: string
    context?: string
    hypothesis?: string
    /** Text a person attached (at most 64 KB), read by the run as material. */
    document?: string
    hypothesis_subjects?: Record<string, string[]>
    iterations?: number
    approve_hypotheses?: boolean
  }) => api.post<{ run_id?: string; case_id?: string }>(`/workflows/${id}/execute`, params, { timeout: LLM_TIMEOUT }),
  /** Reads an attached file on the server and keeps nothing. Over 64 KB it is condensed, and says so on line one. */
  readHuntDocument: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<HuntDocument>('/workflows/threat-hunt/document', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: LLM_TIMEOUT,
    })
  },
  // Read-only: is this report already hunted? Answers running | concluded | uncovered, each
  // with a `proposal` body execute() accepts as-is. Never starts anything.
  checkCoverage: (body: { report?: string; entity_keys?: string[]; techniques?: string[] }) =>
    api.post('/workflows/threat-hunt/coverage', body, { timeout: LLM_TIMEOUT }),

  // persisted to workflow_runs, so History lists past runs without retrieving
  // every prompt transcript
  listRuns: (id: string, params: { limit?: number; offset?: number; status?: string } = {}) =>
    api.get(`/workflows/${id}/runs`, { params }),
  // A hunt lists its newest rows; evidenceLimit asks for more of them.
  getRun: (runId: string, evidenceLimit?: number) =>
    api.get(`/workflows/runs/${runId}`, evidenceLimit ? { params: { evidence_limit: evidenceLimit } } : undefined),
  // What one decision was shown, rebuilt against the record. Folds the whole ledger on
  // the agent side, so it is asked for on a click and never on the getRun poll.
  getReplay: (runId: string, decisionId: string) =>
    api.get<ReplayReport>(`/workflows/runs/${runId}/replay`, { params: { decision_id: decisionId } }),
  replayRun: (runId: string) => api.get(`/workflows/runs/${runId}/replay`),
  verifyRun: (runId: string) => api.get(`/workflows/runs/${runId}/verify`),
  /** One call as journaled: the query it sent and the result it got back. */
  getCall: (runId: string, ref: string) => api.get(`/workflows/runs/${runId}/calls/${encodeURIComponent(ref)}`),
  // Hides a finished run from History. The row and its ledger stay: what the
  // agents did is still auditable by run_id after an operator tidies the list.
  deleteRun: (runId: string) => api.delete(`/workflows/runs/${runId}`),

  // Stop, as opposed to steer. Queues the abort so the run can settle itself and write
  // a report, and escalates behind that: steer('abort') alone leaves a wedged worker running.
  cancelRun: (runId: string, reason: string) =>
    api.post(`/workflows/runs/${runId}/cancel`, { reason }),

  // Another pass over the same ledger, for a finished run whose write-up reads badly.
  // The timeout sits above the backend's own 180s so its answer — the account is still
  // being written, reopen the run for it — is what an operator sees, not a client giving up.
  narrateRun: (runId: string) =>
    api.post(`/workflows/runs/${runId}/narrate`, null, { timeout: LLM_TIMEOUT + 20_000 }),

  // Steer a run that is already going. Queued rather than journalled: the worker holding
  // the ledger is what turns a directive into an event on it. `fields` carries the typed
  // half — the entity a benign suppresses, the grant an extend buys — which prose in
  // `text` cannot say unambiguously, and which the run's regex used to have to guess at.
  steer: (runId: string, kind: string, text = '', fields?: Record<string, unknown>) =>
    api.post(`/agent-runs/${runId}/directives`, { kind, text, ...(fields && { fields }) }),

  createCustom: (data: {
    name: string
    description: string
    use_case?: string
    trigger_examples?: string[]
    phases: WorkflowPhase[]
  }) => api.post('/workflows/custom', data),
  updateCustom: (id: string, data: Partial<{
    name: string
    description: string
    use_case: string
    trigger_examples: string[]
    phases: WorkflowPhase[]
    is_active: boolean
  }>) => api.put(`/workflows/custom/${id}`, data),
  deleteCustom: (id: string) => api.delete(`/workflows/custom/${id}`),

  generate: (description: string) =>
    api.post('/workflows/generate', { description }, { timeout: LLM_TIMEOUT }),
}

// The autonomy ceiling (#2918). Stop is orchestratorApi.kill; PUT has no stopped field.
export type Ceiling = Schema<'CeilingResponse'>
export type ToolClass = Schema<'ToolClassEntry'>
export type EffectClass = NonNullable<ToolClass['effect_class']>
// The rule-rollout limits (#4058): promotion cap, days in shadow, hits a day. The SIEM
// Quiet/Noisy/Silent thresholds ride along read only; they are edited on the integration.
export type RolloutLimits = Schema<'RolloutLimitsResponse'>
export const autonomyApi = {
  rolloutLimits: () => api.get<RolloutLimits>('/autonomy/rollout-limits'),
  updateRolloutLimits: (body: Schema<'RolloutLimitsBody'>) =>
    api.put<RolloutLimits>('/autonomy/rollout-limits', body),
  get: () => api.get<Ceiling>('/autonomy'),
  update: (body: Schema<'CeilingBody'>) => api.put<Ceiling>('/autonomy', body),
  resume: () => api.post<Ceiling>('/autonomy/resume'),
  tools: () => api.get<ToolClass[]>('/autonomy/tools'),
  classifyTool: (name: string, effect_class: EffectClass) =>
    api.put<ToolClass>(`/autonomy/tools/${encodeURIComponent(name)}`, { effect_class }),
  // tier null follows the fleet; the server decides whether the change is a raise
  setWorkflow: (id: string, body: Schema<'WorkflowAutonomyBody'>) =>
    api.put<Ceiling>(`/autonomy/workflows/${encodeURIComponent(id)}`, body),
}

export interface OrchestratorStatus {
  enabled?: boolean
  waiting_approval: number
  paused: number
  oldest_parked_age_seconds: number | null
  // Absent when the API has no orchestrator to ask; a banner then claims nothing.
  cost: { hourly_paused?: boolean; hourly_cost_usd?: number; hourly_limit_usd?: number }
}

export const orchestratorApi = {
  // What keeps alerts from moving, for the banners and the status line.
  getStatus: () => api.get<OrchestratorStatus>('/orchestrator/status'),
  kill: () => api.post('/orchestrator/kill'),
  purgeAll: () => api.post('/orchestrator/investigations/purge'),
}

/** The trail filter (core.audit.trail.make_query): the list and the export send the same one. */
export type AuditFilter = Omit<
  NonNullable<operations['get_api_audit_export']['parameters']['query']>,
  'after'
>

/** A principal an entry names, read from principals when the entry is read (#3308). */
export interface AuditPrincipal {
  id: string
  label: string | null
  kind: string | null
  deleted: boolean
  erased: boolean
}

export interface AuditEntry {
  seq: number
  occurred_at: string
  late: boolean
  entry_hash: string
  payload: Record<string, unknown>
  source_ip: string | null
  actor_text: string | null
  operator_text: string | null
  attempted_name: string | null
  personal: unknown
  actor?: AuditPrincipal | null
  on_behalf_of?: AuditPrincipal | null
  /** What the entry was, in words, from the server. */
  sentence?: string
}

export interface AuditTip { seq: number; hash: string }

/** The feed as the Audit section sees it: no host, no certificates (#3191). */
export interface AuditFeedHealth {
  enabled: boolean
  behind: number | null
  last_delivered_at: string | null
  last_delivered_seq: number | null
  last_error: string | null
  /** A restore entry not yet pushed, polled or exported. */
  unconfirmed: number | null
  /** New entries are refused until the push carries it, for up to 30 seconds. */
  paused: boolean
}

export interface AuditVerify {
  first: number | null
  last: number | null
  first_bad: number | null
  reason: string
  ok: boolean
}

export interface AuditErasure {
  principal_id: string
  on: string
  unconfirmed: boolean
}

export interface AuditRetention {
  effective_days: number | null
  pending_days: number | null
  pending_on: string | null
  unconfirmed: boolean
  erasure_unconfirmed: boolean
  erasures: AuditErasure[]
}

export const AUDIT_PAGE = 100

/** The filter as query params, empty ones left out. */
export function auditParams(filter: AuditFilter): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(filter)) {
    const text = value == null ? '' : String(value).trim()
    if (text) out[key] = text
  }
  return out
}

export const auditApi = {
  entries: (filter: AuditFilter, after?: number, limit = AUDIT_PAGE) =>
    api.get<{ entries: AuditEntry[]; tip: AuditTip; feed?: AuditFeedHealth }>('/audit/entries', {
      params: { ...auditParams(filter), ...(after != null ? { after } : {}), limit },
    }),
  exportTrail: (filter: AuditFilter) =>
    api.get<Blob>('/audit/export', { params: auditParams(filter), responseType: 'blob' }),
  /** Export the filter and save it as an NDJSON file. */
  download: async (filter: AuditFilter, filename = 'audit.ndjson') => {
    const res = await auditApi.exportTrail(filter).catch(async (e) => {
      // a blob response carries its error body as a Blob; read it so the caller sees the detail
      const data = e?.response?.data
      if (data instanceof Blob) {
        try { e.response.data = JSON.parse(await data.text()) } catch { /* not JSON */ }
      }
      throw e
    })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  },
  verify: (first?: number, last?: number) =>
    api.get<AuditVerify>('/audit/verify', { params: { first, last } }),
  retention: () => api.get<AuditRetention>('/audit/retention'),
  saveRetention: (days: number) => api.put<AuditRetention>('/audit/retention', { days }),
  erasePrincipal: (principal_id: string, reason: string) =>
    api.post<AuditRetention>('/audit/erasure', { principal_id, reason }),
}

export interface FederationSourceView {
  source_id: string
  enabled: boolean
  interval_seconds: number
  max_items: number
  min_severity: string | null
  cursor: Record<string, unknown> | null
  last_poll_at: string | null
  last_success_at: string | null
  last_error: string | null
  consecutive_errors: number
  /** Seconds since the last successful poll; null until one has succeeded. Absent on a PATCH response. */
  lag_seconds?: number | null
  /** The server's read of "has not kept up with interval_seconds". Absent on a PATCH response. */
  quiet?: boolean
  is_configured: boolean
  default_interval_seconds: number
}

interface FederationListResponse {
  sources: FederationSourceView[]
  global: { enabled: boolean }
}

export interface AuditFeed {
  enabled: boolean
  host: string
  port: number | null
  ca_set: boolean
  client_cert_set: boolean
  last_delivered_at: string | null
  last_delivered_seq: number | null
  last_error: string | null
  behind: number
  tip_seq: number
}

export const auditFeedApi = {
  get: () => api.get<AuditFeed>('/audit/feed'),
  save: (body: {
    enabled: boolean
    host: string
    port: number | null
    ca_pem: string | null
    client_cert_pem: string | null
    client_key_pem: string | null
  }) => api.put<AuditFeed>('/audit/feed', body),
  test: () => api.post<{ ok: boolean }>('/audit/feed/test'),
}

export const federationApi = {
  setSettings: (enabled: boolean) =>
    api.put<{ enabled: boolean }>('/federation/settings', { enabled }),
  listSources: () => api.get<FederationListResponse>('/federation/sources'),
  updateSource: (
    sourceId: string,
    patch: Partial<{
      enabled: boolean
      interval_seconds: number
      max_items: number
      min_severity: string | null
    }>,
  ) => api.patch<FederationSourceView>(`/federation/sources/${sourceId}`, patch),
  pollNow: (sourceId: string) =>
    api.post<{ ok: boolean; source_id: string }>(
      `/federation/sources/${sourceId}/poll-now`,
    ),
  getHealth: () => api.get('/federation/health'),
}

export const reasoningApi = {
  getSessionSummary: (sessionId: string) =>
    api.get(`/reasoning/${encodeURIComponent(sessionId)}`).then(r => r.data),

  listInteractions: (sessionId: string, params?: { limit?: number; offset?: number }) =>
    api.get(`/reasoning/${encodeURIComponent(sessionId)}/interactions`, { params }).then(r => r.data),

  getInteraction: (sessionId: string, interactionId: string) =>
    api
      .get(`/reasoning/${encodeURIComponent(sessionId)}/interactions/${encodeURIComponent(interactionId)}`)
      .then(r => r.data),
}

// returns raw axios responses (read `res.data`), matching casesApi
export interface ConversationSummary {
  id: string
  user_id: string | null
  title: string | null
  case_id: string | null
  page_context: string | null
  agent_id: string | null
  model: string | null
  archived: boolean
  message_count: number
  created_at: string | null
  updated_at: string | null
  last_message_at: string | null
}

interface ConversationMessage {
  id: number
  conversation_id: string
  seq: number
  role: 'user' | 'assistant' | 'system'
  content: string
  thinking: string | null
  tool_calls: unknown[]
  complete: boolean
  model: string | null
  input_tokens: number
  output_tokens: number
  cost_usd: number
  created_at: string | null
}

export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[]
}

export const conversationsApi = {
  // trailing slash avoids a 307 to the backend root route
  list: (params?: { archived?: boolean; limit?: number; offset?: number; q?: string; case_id?: string }) =>
    api.get('/conversations/', { params }),

  get: (id: string) => api.get(`/conversations/${encodeURIComponent(id)}`),

  update: (id: string, data: { title?: string; archived?: boolean; case_id?: string }) =>
    api.patch(`/conversations/${encodeURIComponent(id)}`, data),

  delete: (id: string) => api.delete(`/conversations/${encodeURIComponent(id)}`),
}

export const kafkaApi = {
  getConfig: () => api.get('/kafka/config'),
  setConfig: (config: {
    enabled: boolean
    bootstrap_servers: string
    consumer_group: string
    topics: string[]
    auto_offset_reset: string
    security_protocol: string
    sasl_mechanism?: string | null
    sasl_username?: string | null
    max_poll_records: number
    session_timeout_ms: number
  }) => api.put('/kafka/config', config),
  getStatus: () => api.get('/kafka/status'),
}

interface BootstrapStatus {
  required: boolean
}

interface BootstrapPayload {
  username: string
  email: string
  password: string
  full_name?: string
  setup_code?: string
}

// First-account creation: creating a user otherwise needs an existing admin.
// Self-closes once any user exists.
interface OverviewArrival {
  data_source: string
  count: number
  source_text: string
}

export interface OverviewOutcome {
  state: string
  label: string
  count: number | null
  // cases the alerts of a case node are in; null for the other nodes
  case_count: number | null
  // how many of Not picked up ran out of time in the queue; null when none or another node
  expired_count: number | null
  info: string | null
  unmeasured_text: string | null
}

export interface OverviewAgent {
  workflow_id: string
  name: string
  running: number
  sample_size: number
  rate: number | null
  level: 'good' | 'fair' | 'poor' | null
  current_step: string | null
}

export interface OverviewFeedItem {
  finding_id: string
  severity: string | null
  data_source: string
  terminal_state: string
  title: string | null
  description: string | null
  created_at: string | null
  source_evidence: Record<string, unknown> | null
  source_link: string | null
  case_id: string | null
  noise_marked: boolean
}

/** The span Overview and Triage count over: the UTC day, or every row. */
export type CountRange = 'today' | 'all'

export interface OverviewPayload {
  day: string
  range: CountRange
  empty: boolean
  arrivals: OverviewArrival[]
  outcomes: OverviewOutcome[]
  running_source: string
  step_source: string
  rate_info: string
  good_at: number
  fair_at: number
  agents: OverviewAgent[]
  feed: OverviewFeedItem[]
  feed_limit: number
  // Set when the role lacks a read: that part is empty and this names it.
  missing?: string[]
}

export const overviewApi = {
  get: (range: CountRange = 'today') =>
    api.get<OverviewPayload>('/overview', { params: { range } }),
  alert: (findingId: string) =>
    api.get<OverviewFeedItem>(`/overview/alerts/${encodeURIComponent(findingId)}`),
}

export interface TriageRow {
  id: number
  kind: string
  kind_label: string
  state: string
  state_label: string
  /** the alert's noise mark is the word shown */
  dismissed: boolean
  source: string
  severity_band: string
  age_seconds: number
  ttl_seconds: number
  last_quarter: boolean
  pickup_seconds: number | null
  workflow_id: string
  case_door: string | null
  /** The run this row started, when one is recorded. A merged alert has none of its own. */
  run_id: string | null
  merge_note: string | null
  /** Why a not-picked-up alert was not picked up, when its latest intake row says. */
  reason: string | null
  document: string | null
  source_link: string | null
  source_evidence: Record<string, unknown> | null
  title: string | null
  description: string | null
  /** What the daemon's triage saved; null on rows that are not alerts. */
  triage: TriageVerdict | null
  finding_id: string | null
  created_at: string | null
  decided_at: string | null
}

/** A saved verdict reads as stored: a field triage did not give is null, never defaulted. */
export type TriageVerdict =
  | {
      state: 'done'
      severity: 'critical' | 'high' | 'medium' | 'low' | null
      category: string | null
      confidence: number | null
      reasoning: string | null
      triaged_at: string | null
    }
  | { state: 'failed'; error: string }
  | { state: 'none' }

export interface TriageSource {
  data_source: string
  arrivals: number
  lag_seconds: number | null
  quiet: boolean | null
}

export interface TriageInfo {
  source: string
  calculation: string
}

export interface TriagePayload {
  rows: TriageRow[]
  strip: {
    picked_up: {
      launched_or_merged: number
      created_today: number
      share: number | null
    }
    /** alerts only, as Overview counts them */
    waiting: number
    /** queued schedule and ask rows, which are not alerts */
    waiting_other: number
    cases_created_today: number
  }
  /** rows that pass the filters, before the row cap */
  matched: number
  /** every intake row, before the filters and the row cap; zero entries are left out */
  counts: {
    total: number
    kind: Record<string, number>
    source: Record<string, number>
    state: Record<string, number>
  }
  sources: TriageSource[]
  range: CountRange
  arrival_info: string
  strip_info: {
    picked_up: TriageInfo
    waiting: TriageInfo
    cases_created_today: TriageInfo
    trust_floor: TriageInfo & { limit: string }
  }
  breakdown_info: { trust: string; weight: string; score: string; reason: string }
}

export interface TriageQuery {
  kind?: string
  source?: string
  state?: string
  range?: CountRange
}

export const triageApi = {
  get: (params: TriageQuery = {}) => api.get<TriagePayload>('/triage', { params }),
}

export const bootstrapApi = {
  status: () => api.get<BootstrapStatus>('/auth/bootstrap'),
  create: (payload: BootstrapPayload) => api.post('/auth/bootstrap', payload),
}

export const authApi = {
  // Public password reset. `request` answers the same whether or not the email
  // matches an account; the emailed link lands on /reset-password?token=…
  requestPasswordReset: (email: string) =>
    api.post<{ message: string }>('/auth/password-reset/request', {
      email,
    } satisfies Schema<'PasswordResetRequest'>),
  confirmPasswordReset: (payload: Schema<'PasswordResetConfirm'>) =>
    api.post<{ message: string }>('/auth/password-reset/confirm', payload),
  /** Revokes every token the user holds and clears the auth cookies, so the
   *  caller is signed out once this resolves. */
  changePassword: (body: Schema<'ChangePasswordRequest'>) =>
    api.post<{ message: string }>('/auth/change-password', body),
}

// Confirm it's you: opens the window the privileged changes read.
export const confirmApi = {
  confirm: (body: Schema<'ConfirmItRequest'>) => api.post<Schema<'ConfirmedResponse'>>('/auth/confirm', body),
}

export type SignIn = Schema<'SignInEntry'>
/** The one-factor check before ending sign-ins: the password, or a code with MFA. */
export type SignInConfirm = Schema<'ConfirmRequest'>

// A person's own sign-ins (account menu). Every ending needs a fresh check.
export const signInsApi = {
  mine: () => api.get<SignIn[]>('/auth/sign-ins'),
  endOne: (id: string, body: SignInConfirm) =>
    api.post<Schema<'EndedSignIns'>>(`/auth/sign-ins/${encodeURIComponent(id)}/end`, body),
  endOthers: (body: SignInConfirm) => api.post<Schema<'EndedSignIns'>>('/auth/sign-ins/end-others', body),
  endAll: (body: SignInConfirm) => api.post<Schema<'EndedSignIns'>>('/auth/sign-ins/end-all', body),
  // Another person's, for managers (users.write over the account). A typed reason is required.
  ofUser: (userId: string) => api.get<SignIn[]>(`/users/${encodeURIComponent(userId)}/sign-ins`),
  endUsersOne: (userId: string, id: string, reason: string) =>
    api.post<Schema<'EndedSignIns'>>(
      `/users/${encodeURIComponent(userId)}/sign-ins/${encodeURIComponent(id)}/end`,
      { reason } satisfies Schema<'EndSignInsRequest'>,
    ),
  endUsersAll: (userId: string, reason: string) =>
    api.post<Schema<'EndedSignIns'>>(`/users/${encodeURIComponent(userId)}/sign-ins/end`, {
      reason,
    } satisfies Schema<'EndSignInsRequest'>),
  // End a lock; how the manager confirmed the person's identity goes to the audit trail.
  unlock: (userId: string, identityConfirmedBy: string) =>
    api.post(`/users/${encodeURIComponent(userId)}/unlock`, {
      identity_confirmed_by: identityConfirmedBy as Schema<'UnlockRequest'>['identity_confirmed_by'],
    }),
  // The whole install, the caller's own sign-in included.
  endEveryone: (reason: string) =>
    api.post<Schema<'EndedSignIns'>>('/users/sign-ins/end-all', {
      reason,
    } satisfies Schema<'EndSignInsRequest'>),
}

// The session policy: how long a sign-in lasts. A save needs the version from the read.
export type SessionPolicy = Schema<'PolicyOut'>
export type SessionPolicyBody = Schema<'SessionPolicy'>
export const sessionPolicyApi = {
  get: () => api.get<SessionPolicy>('/session-policy'),
  put: (body: SessionPolicyBody, version: number) =>
    api.put<SessionPolicy>('/session-policy', body, { headers: { 'If-Match': `"${version}"` } }),
  // The server's own answer to whether saving this would loosen the policy; saves nothing.
  loosens: (body: SessionPolicyBody, version: number) =>
    api.put<Schema<'LooseningOut'>>('/session-policy', body, {
      params: { dry_run: true },
      headers: { 'If-Match': `"${version}"` },
    }),
}

// Self-service two-step sign-in (TOTP + recovery codes) for the signed-in user.
export const mfaApi = {
  setup: () => api.post<Schema<'MFASetupResponse'>>('/auth/mfa/setup'),
  verify: (code: string) =>
    api.post<Schema<'RecoveryCodesResponse'>>('/auth/mfa/verify', {
      code,
    } satisfies Schema<'MFAVerifyRequest'>),
  regenerateRecoveryCodes: (code: string) =>
    api.post<Schema<'RecoveryCodesResponse'>>('/auth/mfa/recovery-codes', {
      code,
    } satisfies Schema<'MFAVerifyRequest'>),
  // axios sends a DELETE body only through `data`
  disable: (code: string) =>
    api.delete<{ message: string }>('/auth/mfa', {
      data: { code } satisfies Schema<'MFAVerifyRequest'>,
    }),
}

export default api
