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
const LLM_TIMEOUT = 180_000

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

export const approvalsApi = {
  list: (params?: {
    status?: string
    workflow_run_id?: string
    limit?: number
  }) => api.get('/approvals', { params }),

  listPending: () => api.get('/approvals/pending'),

  getById: (actionId: string) => api.get(`/approvals/${actionId}`),

  approve: (actionId: string, approved_by?: string) =>
    api.post(`/approvals/${actionId}/approve`, { approved_by }),

  reject: (actionId: string, reason: string, rejected_by?: string) =>
    api.post(`/approvals/${actionId}/reject`, { reason, rejected_by }),
}

export const findingsApi = {
  getAll: (params?: {
    severity?: string
    data_source?: string
    cluster_id?: number
    min_anomaly_score?: number
    limit?: number
    force_refresh?: boolean
  }) => api.get('/findings/', { params }),
  
  getById: (id: string) => api.get(`/findings/${id}`),
  
  getSummary: () => api.get('/findings/stats/summary'),
  
  export: (format: 'json' | 'jsonl' = 'json') =>
    api.post('/findings/export', null, { params: { output_format: format } }),
  
  update: (id: string, data: any) => api.patch(`/findings/${id}`, data),
  
  delete: (id: string) => api.delete(`/findings/${id}`),
  
  getEnrichment: (id: string, force_regenerate: boolean = false) =>
    // may restart a local Bifrost gateway, then wait on a local model
    api.post(`/findings/${id}/enrich`, null, {
      params: { force_regenerate },
      timeout: LLM_TIMEOUT,
    }),

  deleteAll: () => api.delete('/findings/all'),
}

export const casesApi = {
  getAll: (params?: {
    status?: string
    priority?: string
    force_refresh?: boolean
  }) => api.get<Schema<'CaseListResponse'>>('/cases/', { params }),

  getById: (id: string) => api.get<Schema<'CaseSchema'>>(`/cases/${id}`),

  create: (data: Schema<'CaseCreate'>) =>
    api.post<Schema<'CaseSchema'>>('/cases/', data),

  update: (id: string, data: Schema<'CaseUpdate'>) =>
    api.patch<Schema<'CaseSuccessResponse'>>(`/cases/${id}`, data),

  delete: (id: string) =>
    api.delete<Schema<'CaseSuccessResponse'>>(`/cases/${id}`),

  deleteAll: () => api.delete<Schema<'CasePurgeResponse'>>('/cases/all'),

  addActivity: (id: string, data: Schema<'ActivityAdd'>) =>
    api.post<Schema<'CaseSchema'>>(`/cases/${id}/activities`, data),

  addResolutionStep: (id: string, data: Schema<'ResolutionStepAdd'>) =>
    api.post<Schema<'CaseSchema'>>(`/cases/${id}/resolution-steps`, data),

  addFinding: (id: string, finding_id: string) =>
    api.post<Schema<'CaseSchema'>>(`/cases/${id}/findings/${finding_id}`),

  removeFinding: (id: string, finding_id: string) =>
    api.delete<Schema<'CaseSchema'>>(`/cases/${id}/findings/${finding_id}`),

  generateReport: (id: string) =>
    api.post<Schema<'CaseReportResponse'>>(`/cases/${id}/generate-report`, null, {
      timeout: LLM_TIMEOUT,
    }),

  getSummary: () => api.get<Schema<'CaseSummaryResponse'>>('/cases/stats/summary'),

  getComments: (id: string) =>
    api.get<Schema<'CaseCommentsResponse'>>(`/cases/${id}/comments`),
  addComment: (id: string, data: Schema<'CommentAdd'>) =>
    api.post<Schema<'CaseCommentSchema'>>(`/cases/${id}/comments`, data),

  getWatchers: (id: string) =>
    api.get<Schema<'CaseWatchersResponse'>>(`/cases/${id}/watchers`),
  addWatcher: (id: string, userId: string) =>
    api.post<Schema<'CaseWatcherSchema'>>(`/cases/${id}/watchers`, { user_id: userId }),
  removeWatcher: (id: string, userId: string) =>
    api.delete<Schema<'CaseSuccessResponse'>>(`/cases/${id}/watchers/${userId}`),

  // No /cases/{id}/tags route — left untyped on purpose. See #699.
  updateTags: (id: string, tags: string[]) =>
    api.put(`/cases/${id}/tags`, { tags }),

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
  assignSLA: (id: string, data: Schema<'SLAAssign'>) =>
    api.post<Schema<'CaseSLASchema'>>(`/cases/${id}/sla`, data),
  pauseSLA: (id: string) =>
    api.post<Schema<'CaseSuccessResponse'>>(`/cases/${id}/sla/pause`),
  resumeSLA: (id: string) =>
    api.post<Schema<'CaseSuccessResponse'>>(`/cases/${id}/sla/resume`),

  linkCase: (
    id: string,
    relatedCaseId: string,
    relationshipType: string,
    createdBy = 'SOC Analyst',
  ) =>
    api.post<Schema<'CaseRelationshipSchema'>>(`/cases/${id}/relationships`, {
      related_case_id: relatedCaseId,
      relationship_type: relationshipType,
      created_by: createdBy,
    }),
  getLinkedCases: (id: string) =>
    api.get<Schema<'CaseRelationshipsResponse'>>(`/cases/${id}/relationships`),

  closeCase: (id: string, data: Schema<'ClosureInfo'>) =>
    api.post<Schema<'CaseCloseResponse'>>(`/cases/${id}/close`, data),

  escalate: (id: string, data: Schema<'EscalationAdd'>) =>
    api.post<Schema<'CaseSuccessResponse'>>(`/cases/${id}/escalate`, data),

  // No /cases/{id}/audit-log route — left untyped on purpose. See #699.
  getAuditLog: (id: string) => api.get(`/cases/${id}/audit-log`),

  merge: (targetCaseId: string, sourceCaseId: string) =>
    api.post<Schema<'CaseMergeResponse'>>(`/cases/${targetCaseId}/merge`, {
      source_case_id: sourceCaseId,
      merged_by: 'user',
    }),

  // No /cases/bulk-update route — left untyped on purpose. See #699.
  bulkUpdate: (data: {
    case_ids: string[]
    updates: {
      status?: string
      priority?: string
      assignee?: string
      tags?: string[]
    }
  }) => api.post('/cases/bulk-update', data),
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
  
  delete: (policyId: string, force?: boolean) =>
    api.delete(`/sla-policies/${policyId}`, { params: { force } }),
  
  setDefault: (policyId: string) =>
    api.post(`/sla-policies/${policyId}/set-default`),
  
  getUsage: (policyId: string) =>
    api.get(`/sla-policies/${policyId}/usage`),
  
  getCases: (policyId: string, params?: {
    status?: string
    breached_only?: boolean
  }) => api.get(`/sla-policies/${policyId}/cases`, { params }),
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

export const mcpApi = {
  listServers: () => api.get('/mcp/servers'),

  getStatuses: () => api.get('/mcp/servers/status'),

  getServerStatus: (name: string) => api.get(`/mcp/servers/${name}/status`),

  // start/stop were removed: every server in mcp-config.json is stdio-based and
  // those endpoints refused stdio. setServerEnabled below triggers the connect.

  getLogs: (name: string, lines: number = 100) =>
    api.get(`/mcp/servers/${name}/logs`, { params: { lines } }),

  testServer: (name: string) => api.get(`/mcp/servers/${name}/test`),

  getEnabledStates: () => api.get('/mcp/servers/enabled'),

  setServerEnabled: (name: string, enabled: boolean) =>
    api.put(`/mcp/servers/${name}/enabled`, { enabled }),
}

export const claudeApi = {
  uploadFile: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return api.post('/claude/upload-file', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
  },
  
  getModels: () => api.get('/claude/models'),
  
  summarizeConversation: (data: {
    messages: Array<{
      role: string
      content: string | Array<{
        type: string
        text?: string
        source?: any
      }>
    }>
    model?: string
  }) => api.post('/claude/summarize', data, { timeout: LLM_TIMEOUT }),

  analyzeFinding: (finding_id: string, context?: string) =>
    api.post('/claude/analyze-finding', null, {
      params: { finding_id, context },
      timeout: LLM_TIMEOUT,
    }),
  
  generateChatReport: (data: {
    tab_title: string
    messages: Array<{
      role: string
      content: string | Array<{
        type: string
        text?: string
        source?: any
      }>
    }>
    notes?: string
  }) => api.post('/claude/generate-chat-report', data, { timeout: LLM_TIMEOUT }),
}

export const agentsApi = {
  listAgents: () => api.get('/agents/agents'),
  
  getAgent: (agent_id: string) => api.get(`/agents/agents/${agent_id}`),
  
  setCurrentAgent: (agent_id: string) => 
    api.post('/agents/agents/set-current', null, { params: { agent_id } }),
  
  startInvestigation: (data: {
    finding_id: string
    agent_id?: string
    additional_context?: string
  }) => api.post('/agents/agents/investigate', data, { timeout: LLM_TIMEOUT }),

  listCustom: () => api.get('/agents/custom'),
  getCustom: (agent_id: string) => api.get(`/agents/custom/${agent_id}`),
  createCustom: (data: CustomAgentPayload) => api.post('/agents/custom', data),
  updateCustom: (agent_id: string, data: Partial<CustomAgentPayload>) =>
    api.patch(`/agents/custom/${agent_id}`, data),
  deleteCustom: (agent_id: string) => api.delete(`/agents/custom/${agent_id}`),
  getAvailableTools: () => api.get('/agents/custom/_meta/tools'),
  // built-ins are never mutated; a fork is a new editable copy
  forkAgent: (source_agent_id: string, new_name?: string) =>
    api.post(`/agents/${source_agent_id}/fork`, { new_name }),

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

export interface CustomAgentPayload {
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

export const configApi = {
  getClaude: () => api.get('/config/claude'),
  setClaude: (api_key: string) => api.post('/config/claude', { api_key }),
  
  getS3: () => api.get('/config/s3'),
  setS3: (data: {
    bucket_name: string
    region: string
    auth_method?: string
    aws_profile?: string
    access_key_id?: string
    secret_access_key?: string
    session_token?: string
    findings_path?: string
    cases_path?: string
    parquet_prefix?: string
  }) => api.post('/config/s3', data),
  
  getDemoMode: () => api.get('/config/demo-mode'),
  setDemoMode: (enabled: boolean) => api.post('/config/demo-mode', { enabled }),
  resetDemoData: () => api.post('/config/demo-mode/reset'),
  
  getIntegrations: () => api.get('/config/integrations'),
  setIntegrations: (data: {
    enabled_integrations: string[]
    integrations: Record<string, any>
  }) => api.post('/config/integrations', data),
  
  getGeneral: () => api.get('/config/general'),
  setGeneral: (data: {
    auto_start_sync: boolean
    show_notifications: boolean
    theme: string
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
    prompt_cache_enabled: boolean
    history_window: number
    tool_response_budget_default: number
    thinking_budget: number
  }) => api.post('/config/ai-operations', data),

  getDarktrace: () => api.get('/config/darktrace'),
  setDarktrace: (data: {
    enabled: boolean
    url: string
    max_body_kb: number
    webhook_secret?: string
  }) => api.post('/config/darktrace', data),

  getOrchestrator: () => api.get('/config/orchestrator'),
  setOrchestrator: (data: {
    enabled: boolean
    dry_run: boolean
    auto_assign_findings: boolean
    auto_assign_severities: string[]
    max_concurrent_agents: number
    max_iterations_per_agent: number
    max_runtime_per_investigation: number
    max_cost_per_investigation: number
    max_total_hourly_cost: number
    max_total_daily_cost: number
    loop_interval: number
    agent_loop_delay: number
    stale_threshold: number
    dedup_window_minutes: number
    context_max_chars: number
    plan_model: string
    review_model: string
    workdir_base: string
  }) => api.post('/config/orchestrator', data),
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
  provider_type: 'anthropic' | 'openai' | 'ollama' | 'vertex'
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

export interface LLMProviderCreate {
  provider_id?: string
  provider_type: 'anthropic' | 'openai' | 'ollama' | 'vertex'
  name: string
  base_url?: string
  api_key?: string
  default_model: string
  is_active?: boolean
  is_default?: boolean
  config?: Record<string, any>
}

export interface LLMProviderUpdate {
  name?: string
  base_url?: string
  api_key?: string
  default_model?: string
  is_active?: boolean
  is_default?: boolean
  config?: Record<string, any>
}

export const llmProviderApi = {
  list: () => api.get<LLMProvider[]>('/llm/providers/'),
  create: (data: LLMProviderCreate) => api.post<LLMProvider>('/llm/providers/', data),
  discoverModels: (data: {
    provider_type: string
    base_url?: string
    api_key?: string
    organization?: string
  }) => api.post<{ models: string[] }>('/llm/providers/discover-models', data),
  update: (providerId: string, data: LLMProviderUpdate) =>
    api.put<LLMProvider>(`/llm/providers/${providerId}`, data),
  remove: (providerId: string) => api.delete(`/llm/providers/${providerId}`),
  test: (providerId: string) =>
    api.post<{ success: boolean; provider_id: string; error: string | null }>(
      `/llm/providers/${providerId}/test`,
    ),
  // no provider row required, so the wizard can verify before anything persists
  testConnection: (data: {
    provider_type: string
    base_url?: string
    api_key?: string
    default_model?: string
    organization?: string
  }) =>
    api.post<{ success: boolean; error: string | null }>(
      '/llm/providers/test-connection',
      data,
    ),
  listModels: (providerId: string) =>
    api.get<{ models: string[] }>(`/llm/providers/${providerId}/models`),
  setDefault: (providerId: string) =>
    api.post<LLMProvider>(`/llm/providers/${providerId}/set-default`),
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
  getModelInfo: (modelId: string, providerId?: string) =>
    api.get<AIModelInfo>(`/ai/models/${encodeURIComponent(modelId)}/info`, {
      params: providerId ? { provider_id: providerId } : undefined,
    }),
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

export interface CostEstimate {
  provider_type: string
  model_id: string
  input_tokens: number
  output_tokens_max: number
  low_usd: number
  high_usd: number
  pricing_source: 'exact' | 'heuristic' | 'zero' | 'unknown'
  token_count_method: 'anthropic_count_tokens' | 'tiktoken' | 'char_heuristic'
}

export interface RecalculateCostResult {
  total_matched: number
  updated: number
  skipped: number
  remaining: number
}

export interface BudgetSettings {
  default_vk: string
  budget_limit_usd: number
  enforcement_mode: 'warning' | 'hard_stop'
}

export interface BifrostBudgetTier {
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

export const analyticsApi = {
  // token_count_method tells the UI how trustworthy the count is
  estimateCost: (payload: {
    provider_type: string
    model_id: string
    messages: Array<{ role: string; content: any }>
    system_prompt?: string
    tools?: any[]
    max_tokens?: number
  }) => api.post<CostEstimate>('/analytics/estimate-cost', payload),

  // Bifrost caps each call at 1000 rows; the caller loops on `remaining`.
  recalculateCost: (payload?: {
    providers?: string[]
    models?: string[]
    start_time?: string
    end_time?: string
    missing_cost_only?: boolean
    limit?: number
  }) => api.post<RecalculateCostResult>('/analytics/recalculate-cost', payload || {}),
}

export const storageApi = {
  getStatus: () => api.get('/storage/status'),
  getHealth: () => api.get('/storage/health'),
  reconnect: () => api.post('/storage/reconnect'),
}

export const timesketchApi = {
  getStatus: () => api.get('/timesketch/status'),
  
  listSketches: () => api.get('/timesketch/sketches'),
  
  createSketch: (data: { name: string; description?: string }) =>
    api.post('/timesketch/sketches', data),
  
  getSketch: (id: number) => api.get(`/timesketch/sketches/${id}`),
  
  getDockerStatus: () => api.get('/timesketch/docker/status'),
  
  startDocker: (port: number = 5000) =>
    api.post('/timesketch/docker/start', null, { params: { port } }),
  
  stopDocker: () => api.post('/timesketch/docker/stop'),
  
  exportToTimesketch: (data: {
    sketch_id?: string
    sketch_name?: string
    sketch_description?: string
    finding_ids?: string[]
    case_id?: string
    timeline_name: string
  }) => api.post('/timesketch/export', data),
}

export const attackApi = {
  getLayer: () => api.get('/attack/layer'),
  
  getTechniqueRollup: (min_confidence: number = 0.0, time_range: string = 'all') =>
    api.get('/attack/techniques/rollup', { params: { min_confidence, time_range } }),
  
  getFindingsByTechnique: (technique_id: string) =>
    api.get(`/attack/techniques/${technique_id}/findings`),
  
  getTacticsSummary: () => api.get('/attack/tactics/summary'),
}

export const timelineApi = {
  getCaseTimeline: (case_id: string) => api.get(`/timeline/case/${case_id}`),
  
  getFindingContext: (finding_id: string, time_window_minutes: number = 60) =>
    api.get(`/timeline/finding/${finding_id}/context`, { params: { time_window_minutes } }),
  
  getTimelineRange: (params: {
    start?: string
    end?: string
    severity?: string
    data_source?: string
    limit?: number
  }) => api.get('/timeline/range', { params }),
  
  getClusterTimeline: (cluster_id: string) => api.get(`/timeline/cluster/${cluster_id}`),
}

export const detectionRulesApi = {
  listSources: () => api.get('/detection-rules/sources'),
  
  getSource: (sourceId: string) => api.get(`/detection-rules/sources/${sourceId}`),
  
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
  
  updateSource: (sourceId: string) =>
    api.post(`/detection-rules/sources/${sourceId}/update`),
  
  updateAll: () => api.post('/detection-rules/update-all'),
  
  getStats: () => api.get('/detection-rules/stats'),
  
  getMcpEnv: () => api.get('/detection-rules/mcp-env'),
  
  reload: () => api.post('/detection-rules/reload'),
}

export const localServicesApi = {
  getSplunkStatus: () => api.get('/services/splunk/status'),
  startSplunk: () => api.post('/services/splunk/start'),
  stopSplunk: () => api.post('/services/splunk/stop'),
  restartSplunk: () => api.post('/services/splunk/restart'),

  getPostgresStatus: () => api.get('/services/postgres/status'),

  // service names are allowlisted server-side
  list: () => api.get('/services'),
  getStatus: (name: string) => api.get(`/services/${name}/status`),
  start: (name: string) => api.post(`/services/${name}/start`),
  stop: (name: string) => api.post(`/services/${name}/stop`),
  restart: (name: string) => api.post(`/services/${name}/restart`),

  getAutostart: () => api.get('/services/autostart'),
  setAutostart: (services: string[]) => api.put('/services/autostart', { services }),
}

export interface WorkflowPhase {
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

export const workflowApi = {
  listAll: () => api.get('/workflows'),
  get: (id: string) => api.get(`/workflows/${id}`),
  execute: (id: string, params: {
    finding_id?: string
    case_id?: string
    context?: string
    hypothesis?: string
    iterations?: number
    approve_hypotheses?: boolean
  }) => api.post(`/workflows/${id}/execute`, params, { timeout: LLM_TIMEOUT }),
  reloadFiles: () => api.post('/workflows/reload'),

  // persisted to workflow_runs, so History lists past runs without retrieving
  // every prompt transcript
  listRuns: (id: string, params: { limit?: number; offset?: number; status?: string } = {}) =>
    api.get(`/workflows/${id}/runs`, { params }),
  getRun: (runId: string) => api.get(`/workflows/runs/${runId}`),
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
    graph_layout?: Record<string, any>
    created_by?: string
  }) => api.post('/workflows/custom', data),
  updateCustom: (id: string, data: Partial<{
    name: string
    description: string
    use_case: string
    trigger_examples: string[]
    phases: WorkflowPhase[]
    graph_layout: Record<string, any>
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
  createInvestigation: (params: {
    skill_id?: string
    finding_ids?: string[]
    case_id?: string
    hypothesis?: string
    priority?: string
  }) => api.post('/orchestrator/investigations', params),
  wakeInvestigation: (id: string) => api.post(`/orchestrator/investigations/${id}/wake`),
  killInvestigation: (id: string) => api.post(`/orchestrator/investigations/${id}/kill`),
  getInvestigationFile: (id: string, filename: string) =>
    api.get(`/orchestrator/investigations/${id}/files/${filename}`),
  scanFindings: (severities?: string[]) =>
    api.post('/orchestrator/scan-findings', {
      severities: severities || ['critical', 'high'],
    }, { timeout: LLM_TIMEOUT }),
  reviewInvestigation: (id: string, action: 'approve' | 'rework', notes?: string) =>
    api.post(`/orchestrator/investigations/${id}/review`, { action, notes }),

  getCost: () => api.get('/orchestrator/cost'),

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
  last_poll_at: string | null
  last_success_at: string | null
  last_error: string | null
  consecutive_errors: number
  is_configured: boolean
  default_interval_seconds: number
}

export interface FederationListResponse {
  sources: FederationSourceView[]
  global: { enabled: boolean }
}

export const federationApi = {
  getSettings: () => api.get<{ enabled: boolean }>('/federation/settings'),
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
  agent_id: string | null
  model: string | null
  archived: boolean
  message_count: number
  created_at: string | null
  updated_at: string | null
  last_message_at: string | null
}

export interface ConversationMessage {
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
  list: (params?: { archived?: boolean; limit?: number; offset?: number }) =>
    api.get('/conversations/', { params }),

  get: (id: string) => api.get(`/conversations/${encodeURIComponent(id)}`),

  update: (id: string, data: { title?: string; archived?: boolean }) =>
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

export interface BootstrapStatus {
  required: boolean
}

export interface BootstrapPayload {
  username: string
  email: string
  password: string
  full_name?: string
}

// First-account creation: creating a user otherwise needs an existing admin.
// Self-closes once any user exists.
export const bootstrapApi = {
  status: () => api.get<BootstrapStatus>('/auth/bootstrap'),
  create: (payload: BootstrapPayload) => api.post('/auth/bootstrap', payload),
}

export default api
