import axios from 'axios'
import { basePath } from '../config/basePath'
import type { Schema } from './apiTypes'

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
    return config
  },
  (error) => Promise.reject(error)
)

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

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true

      try {
        await api.post('/auth/refresh')
        return api(originalRequest)
      } catch (refreshError) {
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
    return { ...init, headers, credentials: 'include' }
  }
  let res = await fetch(url, build())
  if (res.status === 401) {
    try {
      await api.post('/auth/refresh')
      res = await fetch(url, build())
    } catch {
      /* empty */
    }
  }
  return res
}

export const aiDecisionsApi = {
  create: (data: {
    decision_id: string
    agent_id: string
    decision_type: string
    confidence_score: number
    reasoning: string
    recommended_action: string
    finding_id?: string
    case_id?: string
    workflow_id?: string
    decision_metadata?: any
  }) => api.post('/ai/decisions', data),
  
  getById: (decisionId: string) => api.get(`/ai/decisions/${decisionId}`),
  
  list: (params?: {
    agent_id?: string
    finding_id?: string
    case_id?: string
    has_feedback?: boolean
    limit?: number
    offset?: number
  }) => api.get('/ai/decisions', { params }),
  
  submitFeedback: (decisionId: string, data: {
    human_reviewer: string
    human_decision: string
    feedback_comment?: string
    accuracy_grade?: number
    reasoning_grade?: number
    action_appropriateness?: number
    actual_outcome?: string
    time_saved_minutes?: number
  }) => api.post(`/ai/decisions/${decisionId}/feedback`, data),
  
  getStats: (params?: {
    agent_id?: string
    days?: number
  }) => api.get('/ai/decisions/stats', { params }),
  
  getPendingFeedback: (limit?: number) =>
    api.get('/ai/decisions/pending-feedback', { params: { limit } }),
}

export interface NeedsYouItem {
  /** `parked`: a hunt stopped at a limit, answered with Keep going on its case; its source_id is the run id. */
  kind: 'checkpoint' | 'approval' | 'parked'
  source_id: string
  title: string
  reason: string
  /** '' when nothing recorded when it started waiting. */
  created_at: string
  reversibility: string
  case_id: string | null
  run_id?: string | null
  /** The run's own checkpoint this item answers, so a page reading the run can tell it was answered. */
  checkpoint_id?: string | null
  checkpoint_class?: string | null
  /** A hypothesis approval's explanations, each with where it came from; base_rate is the benign one to beat. */
  hypotheses?: { hypothesis_id: string; statement: string; provenance: string }[]
}

export const approvalsApi = {
  list: (params?: {
    status?: string
    workflow_run_id?: string
    limit?: number
  }) => api.get('/approvals', { params }),

  listPending: () => api.get('/approvals/pending'),

  needsYou: (caseId?: string) =>
    api.get<{ count: number; items: NeedsYouItem[] }>('/approvals/needs-you', {
      params: caseId ? { case_id: caseId } : undefined,
    }),

  getById: (actionId: string) => api.get(`/approvals/${actionId}`),

  approve: (actionId: string, approved_by?: string) =>
    api.post(`/approvals/${actionId}/approve`, { approved_by }),

  reject: (actionId: string, reason: string, rejected_by?: string) =>
    api.post(`/approvals/${actionId}/reject`, { reason, rejected_by }),
}

/** how a findings read treats findings naming an analyst-excluded IP */
export type ExclusionView = 'include' | 'hide' | 'only'

export const findingsApi = {
  getAll: (params?: {
    severity?: string
    data_source?: string
    cluster_id?: number
    min_anomaly_score?: number
    limit?: number
    exclusions?: ExclusionView
    sort_by?: string
    sort_order?: 'asc' | 'desc'
  }) => api.get('/findings', { params }),
  
  getById: (id: string) => api.get(`/findings/${id}`),
  
  getSummary: (params?: { exclusions?: ExclusionView }) =>
    api.get('/findings/stats/summary', { params }),
  
  export: (format: 'json' | 'jsonl' = 'json') =>
    api.post('/findings/export', null, { params: { output_format: format } }),
  
  update: (id: string, data: any) => api.patch(`/findings/${id}`, data),
  
  delete: (id: string) => api.delete(`/findings/${id}`),
  
  getEnrichment: (id: string, force_regenerate: boolean = false) =>
    // may restart the Bifrost gateway, then wait on the configured model
    api.post(`/findings/${id}/enrich`, null, {
      params: { force_regenerate },
      timeout: LLM_TIMEOUT,
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

export interface CaseRecordRow {
  id: string
  at: string
  kind: string
  source: string
  chained: boolean
  text: string
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
    kind?: string
    limit?: number
    offset?: number
  }) => api.get<Schema<'CaseListResponse'>>('/cases', { params }),

  getById: (id: string) => api.get<Schema<'CaseDetailResponse'>>(`/cases/${id}`),

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

  closeCase: (id: string, data: Schema<'ClosureInfo'>) =>
    api.post<Schema<'CaseCloseResponse'>>(`/cases/${id}/close`, data),

  escalate: (id: string, data: Schema<'EscalationAdd'>) =>
    api.post<Schema<'CaseSuccessResponse'>>(`/cases/${id}/escalate`, data),

  getEscalations: (id: string) =>
    api.get<Schema<'CaseEscalationsResponse'>>(`/cases/${id}/escalations`),

  // The merged record. There is no audit-log route; this read replaced it.
  getRecord: (id: string) =>
    api.get<CaseRecordResponse>(`/cases/${id}/record`),

  merge: (targetCaseId: string, sourceCaseId: string) =>
    api.post<Schema<'CaseMergeResponse'>>(`/cases/${targetCaseId}/merge`, {
      source_case_id: sourceCaseId,
      merged_by: 'user',
    }),

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

export const caseMetricsApi = {
  getSummary: () => api.get('/cases/metrics/summary'),
  getMTTD: (params?: { start_date?: string; end_date?: string; priority?: string }) =>
    api.get('/cases/metrics/mttd', { params }),
  getMTTR: (params?: { start_date?: string; end_date?: string; priority?: string }) =>
    api.get('/cases/metrics/mttr', { params }),
  getByPriority: () => api.get('/cases/metrics/by-priority'),
  getByStatus: () => api.get('/cases/metrics/by-status'),
  getAnalystPerformance: () => api.get('/cases/metrics/analyst-performance'),
}

export const caseSearchApi = {
  search: (data: {
    query: string
    filters?: Record<string, any>
    sort_by?: string
    sort_order?: string
    limit?: number
    offset?: number
  }) => api.post('/case-search/', data),
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

  listCustom: () => api.get('/agents/custom'),
  getCustom: (agent_id: string) => api.get(`/agents/custom/${agent_id}`),
  createCustom: (data: CustomAgentPayload) => api.post('/agents/custom', data),
  updateCustom: (agent_id: string, data: Partial<CustomAgentPayload>) =>
    api.patch(`/agents/custom/${agent_id}`, data),
  deleteCustom: (agent_id: string) => api.delete(`/agents/custom/${agent_id}`),
  getAvailableTools: () => api.get('/agents/custom/_meta/tools'),
  // any agent, built-in or custom, with its prompt, model and fallback
  getAgent: (agent_id: string) => api.get(`/agents/agents/${agent_id}`),
  // built-ins are never mutated; a fork is a new editable copy, with these fields replacing the source's
  forkAgent: (source_agent_id: string, overrides: Partial<CustomAgentPayload> = {}) =>
    api.post(`/agents/${source_agent_id}/fork`, overrides),

  generateCustom: (data: {
    description: string
    current_draft?: GeneratedAgentDraft | null
    feedback?: string
  }) =>
    api.post<{ draft: GeneratedAgentDraft }>('/agents/custom/generate', data, {
      timeout: LLM_TIMEOUT,
    }),
}

export interface GeneratedAgentDraft {
  name: string
  description: string
  specialization: string
  icon: string
  color: string
  role: string
  extra_principles: string
  methodology: string
  recommended_tools: string[]
  max_tokens: number
  enable_thinking: boolean
}

interface CustomAgentPayload {
  name: string
  role: string
  description?: string | null
  icon?: string | null
  color?: string | null
  specialization?: string | null
  extra_principles?: string | null
  methodology?: string | null
  system_prompt_override?: string | null
  recommended_tools?: string[]
  max_tokens?: number
  enable_thinking?: boolean
  model?: string | null
  fallback_model?: string | null
}

export interface AgentSummary {
  id: string
  name: string
  description?: string
  icon?: string
  color?: string
  specialization?: string
  /** Action id this agent's decisions are logged under (#476). */
  decision_id?: string
}

/** Reads the shell folds itself. Health is public; routability is admin-only. */
export const consoleApi = {
  getHealth: () => api.get('/health'),
  getRoutability: () =>
    api.get<{ providers: Record<string, boolean> }>('/bifrost/routability'),
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
    api.get<{
      steps: { id: string; title: string; state_line: string; done: boolean; href: string }[]
      alerts_exist: number
      demo_enabled: boolean
    }>('/config/setup-steps'),
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
    auto_start_sync: boolean
    show_notifications: boolean
    theme: string
    enable_keyring: boolean
  }) => api.post('/config/general', data),
  
  getTheme: () => api.get('/config/theme'),
  setTheme: (theme: string) => api.post('/config/theme', { theme }),

  getAutonomy: () =>
    api.get<{ auto_response_enabled: boolean; force_manual_approval: boolean }>(
      '/config/autonomy',
    ),
  
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

  getOrchestrator: () => api.get('/config/orchestrator'),
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
    return api.post('/config/orchestrator', rest)
  },

  getForceManualApproval: () => api.get('/config/force-manual-approval'),
  setForceManualApproval: (enabled: boolean) =>
    api.post('/config/force-manual-approval', { enabled }),
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
  uploadFile: (file: File, dataType: string = 'finding') => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('data_type', dataType)
    return api.post<IngestionJob>('/ingest/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
    })
  },
  listJobs: () => api.get<IngestionJob[]>('/ingest/jobs'),
  getJob: (jobId: string) => api.get<IngestionJob>(`/ingest/jobs/${jobId}`),
}

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
  
  getFindingsByTechnique: (technique_id: string) =>
    api.get(`/attack/techniques/${technique_id}/findings`),
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

export const detectionRulesApi = {
  listSources: () => api.get('/detection-rules/sources'),
  
  
  addSource: (data: {
    name: string
    source_type: 'git' | 'local'
    format: 'sigma' | 'splunk' | 'elastic' | 'kql' | 'auto'
    url?: string
    path?: string
    subdirectory?: string
    story_subdirectory?: string
  }) => api.post('/detection-rules/sources', data),
  
  removeSource: (sourceId: string, deleteFiles: boolean = false) =>
    api.delete(`/detection-rules/sources/${sourceId}`, { params: { delete_files: deleteFiles } }),
  
  // A clone or pull runs for minutes (the server allows 300 s per repository).
  updateSource: (sourceId: string) =>
    api.post(`/detection-rules/sources/${sourceId}/update`, undefined, { timeout: 0 }),
  
  updateAll: () => api.post('/detection-rules/update-all', undefined, { timeout: 0 }),
  
  getStats: () => api.get('/detection-rules/stats'),
  
  
  reload: () => api.post('/detection-rules/reload'),
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
  tools: string[]
  steps: string[]
  expected_output?: string
  timeout_seconds?: number
  approval_required?: boolean
  conditions?: any
  parallel_group?: string | null
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
  }) => api.post(`/workflows/${id}/execute`, params, { timeout: LLM_TIMEOUT }),
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
  // Hides a finished run from History. The row and its ledger stay: what the
  // agents did is still auditable by run_id after an operator tidies the list.
  deleteRun: (runId: string) => api.delete(`/workflows/runs/${runId}`),

  // Stop, as opposed to steer. Queues the abort so the run can settle itself and write
  // a report, and escalates behind that: steer('abort') alone leaves a wedged worker running.
  cancelRun: (runId: string, reason: string, rejectedBy?: string) =>
    api.post(`/workflows/runs/${runId}/cancel`, { reason, ...(rejectedBy && { rejected_by: rejectedBy }) }),

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

  listCustom: (activeOnly: boolean = true) =>
    api.get('/workflows/custom', { params: { active_only: activeOnly } }),
  getCustom: (id: string) => api.get(`/workflows/custom/${id}`),
  createCustom: (data: {
    name: string
    description: string
    use_case?: string
    trigger_examples?: string[]
    phases: WorkflowPhase[]
    created_by?: string
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

export const orchestratorApi = {
  getStatus: () => api.get('/orchestrator/status'),
  enable: () => api.post('/orchestrator/enable'),
  disable: () => api.post('/orchestrator/disable'),
  kill: () => api.post('/orchestrator/kill'),
  purgeAll: () => api.post('/orchestrator/investigations/purge'),
  
  listInvestigations: (status?: string) =>
    api.get('/orchestrator/investigations', { params: status ? { status } : {} }),
  getInvestigation: (id: string) => api.get(`/orchestrator/investigations/${id}`),
  wakeInvestigation: (id: string) => api.post(`/orchestrator/investigations/${id}/wake`),
  killInvestigation: (id: string) => api.post(`/orchestrator/investigations/${id}/kill`),
  getInvestigationFile: (id: string, filename: string) =>
    api.get(`/orchestrator/investigations/${id}/files/${filename}`),
  scanFindings: (severities?: string[]) =>
    api.post('/orchestrator/scan-findings', {
      severities: severities || ['critical', 'high'],
    }, { timeout: LLM_TIMEOUT }),

  getChainOfCustody: (id: string) =>
    api.get(`/orchestrator/investigations/${id}/chain-of-custody`),

  exportInvestigation: (id: string) =>
    api.get(`/orchestrator/investigations/${id}/export`, {
      responseType: 'blob',
    }),
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

  listInvestigationInteractions: (investigationId: string, params?: { limit?: number; offset?: number }) =>
    api
      .get(`/reasoning/investigation/${encodeURIComponent(investigationId)}/interactions`, { params })
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

export interface ImportConversationInput {
  id: string
  title?: string | null
  agent_id?: string | null
  model?: string | null
  messages: Array<{
    role: string
    content: string
    thinking?: string | null
    tool_calls?: unknown[]
    complete?: boolean
    model?: string | null
  }>
}

export const conversationsApi = {
  // trailing slash avoids a 307 to the backend root route
  list: (params?: { archived?: boolean; limit?: number; offset?: number; q?: string }) =>
    api.get('/conversations/', { params }),

  get: (id: string) => api.get(`/conversations/${encodeURIComponent(id)}`),

  update: (id: string, data: { title?: string; archived?: boolean; case_id?: string }) =>
    api.patch(`/conversations/${encodeURIComponent(id)}`, data),

  delete: (id: string) => api.delete(`/conversations/${encodeURIComponent(id)}`),

  importHistory: (conversations: ImportConversationInput[]) =>
    api.post('/conversations/import', { conversations }),
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
  source_text: string
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
  status: string
  terminal_state: string
  terminal_label: string
  title: string | null
  description: string | null
  created_at: string | null
  evidence_links: Array<{ ref?: string }>
  source_evidence: Record<string, unknown> | null
  source_link: string | null
  case_id: string | null
  noise_marked: boolean
}

export interface OverviewPayload {
  day: string
  empty: boolean
  arrivals: OverviewArrival[]
  engine: { source_text: string }
  outcomes: OverviewOutcome[]
  running_source: string
  step_source: string
  rate_info: string
  good_at: number
  fair_at: number
  agents: OverviewAgent[]
  feed: OverviewFeedItem[]
  feed_limit: number
}

export const overviewApi = {
  get: () => api.get<OverviewPayload>('/overview'),
  alert: (findingId: string) =>
    api.get<OverviewFeedItem>(`/overview/alerts/${encodeURIComponent(findingId)}`),
}

export interface TriageRow {
  id: number
  kind: string
  kind_label: string
  state: string
  state_label: string
  source: string
  severity_band: string
  age_seconds: number
  ttl_seconds: number
  last_quarter: boolean
  score: null
  trust: null
  weight: null
  pickup_seconds: number | null
  workflow_id: string
  case_door: string | null
  document: string | null
  source_link: string | null
  source_evidence: Record<string, unknown> | null
  title: string | null
  description: string | null
  finding_id: string | null
  created_at: string | null
  decided_at: string | null
}

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
    waiting: number
    cases_created_today: number
    trust_floor: string
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
  arrival_info: string
  strip_info: {
    picked_up: TriageInfo
    waiting: TriageInfo
    cases_created_today: TriageInfo
    trust_floor: TriageInfo & { limit: string }
  }
  breakdown_info: { trust: string; weight: string; score: string }
  unmeasured_text: string
}

export interface TriageQuery {
  kind?: string
  source?: string
  state?: string
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

// Self-service two-step sign-in (TOTP + recovery codes) for the signed-in user.
export const mfaApi = {
  setup: () => api.post<Schema<'MFASetupResponse'>>('/auth/mfa/setup'),
  verify: (code: string) =>
    api.post<Schema<'RecoveryCodesResponse'>>('/auth/mfa/verify', {
      code,
    } satisfies Schema<'MFAVerifyRequest'>),
  regenerateRecoveryCodes: () =>
    api.post<Schema<'RecoveryCodesResponse'>>('/auth/mfa/recovery-codes'),
  // axios sends a DELETE body only through `data`
  disable: (code: string) =>
    api.delete<{ message: string }>('/auth/mfa', {
      data: { code } satisfies Schema<'MFAVerifyRequest'>,
    }),
}

export default api
