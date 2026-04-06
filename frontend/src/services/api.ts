import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
})

// Add auth token to requests
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('access_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Handle 401 errors (token expired)
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config

    const isAuthEndpoint = originalRequest?.url?.startsWith('/auth/login') ||
      originalRequest?.url?.startsWith('/auth/register')

    if (isAuthEndpoint) {
      return Promise.reject(error)
    }

    // If 401 and we haven't retried yet, try to refresh token
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true

      try {
        const refreshToken = localStorage.getItem('refresh_token')
        if (refreshToken) {
          const response = await axios.post('/api/auth/refresh', {
            refresh_token: refreshToken,
          })

          const { access_token, refresh_token: newRefreshToken } = response.data

          localStorage.setItem('access_token', access_token)
          localStorage.setItem('refresh_token', newRefreshToken)

          // Retry original request with new token
          originalRequest.headers.Authorization = `Bearer ${access_token}`
          return api(originalRequest)
        }
      } catch (refreshError) {
        // Refresh failed, redirect to login
        localStorage.removeItem('access_token')
        localStorage.removeItem('refresh_token')
        window.location.href = '/login'
        return Promise.reject(refreshError)
      }
    }

    return Promise.reject(error)
  }
)

// AI Decisions API
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

// Findings API
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
    api.post(`/findings/${id}/enrich`, null, { params: { force_regenerate } }),

  deleteAll: () => api.delete('/findings/all'),
}

// Cases API
export const casesApi = {
  getAll: (params?: {
    status?: string
    priority?: string
    force_refresh?: boolean
  }) => api.get('/cases/', { params }),
  
  getById: (id: string) => api.get(`/cases/${id}`),
  
  create: (data: {
    title: string
    description?: string
    finding_ids: string[]
    priority?: string
    status?: string
  }) => api.post('/cases/', data),
  
  update: (id: string, data: {
    title?: string
    description?: string
    status?: string
    priority?: string
    notes?: string
    assignee?: string
  }) => api.patch(`/cases/${id}`, data),
  
  delete: (id: string) => api.delete(`/cases/${id}`),
  
  addActivity: (id: string, data: {
    activity_type: string
    description: string
    details?: any
  }) => api.post(`/cases/${id}/activities`, data),
  
  addResolutionStep: (id: string, data: {
    description: string
    action_taken: string
    result?: string
  }) => api.post(`/cases/${id}/resolution-steps`, data),
  
  addFinding: (id: string, finding_id: string) =>
    api.post(`/cases/${id}/findings/${finding_id}`),
  
  removeFinding: (id: string, finding_id: string) =>
    api.delete(`/cases/${id}/findings/${finding_id}`),
  
  generateReport: (id: string) => api.post(`/cases/${id}/generate-report`),
  
  getSummary: () => api.get('/cases/stats/summary'),
  
  // Comments
  getComments: (id: string) => api.get(`/cases/${id}/comments`),
  addComment: (id: string, data: { content: string; author: string; parent_comment_id?: string }) =>
    api.post(`/cases/${id}/comments`, data),
  
  // Watchers
  getWatchers: (id: string) => api.get(`/cases/${id}/watchers`),
  addWatcher: (id: string, userId: string) =>
    api.post(`/cases/${id}/watchers`, { user_id: userId }),
  removeWatcher: (id: string, userId: string) =>
    api.delete(`/cases/${id}/watchers/${userId}`),
  
  // Tags
  updateTags: (id: string, tags: string[]) =>
    api.put(`/cases/${id}/tags`, { tags }),
  
  // Evidence
  getEvidence: (id: string) => api.get(`/cases/${id}/evidence`),
  addEvidence: (id: string, data: {
    name: string
    description?: string
    file_path?: string
    url?: string
    evidence_type: string
  }) => api.post(`/cases/${id}/evidence`, data),
  
  // IOCs
  getIOCs: (id: string) => api.get(`/cases/${id}/iocs`),
  addIOC: (id: string, data: {
    ioc_type: string
    value: string
    description?: string
    source?: string
    tags?: string[]
  }) => api.post(`/cases/${id}/iocs`, data),
  
  // Tasks
  getTasks: (id: string) => api.get(`/cases/${id}/tasks`),
  addTask: (id: string, data: {
    title: string
    description?: string
    assignee?: string
    due_date?: string
    priority?: string
  }) => api.post(`/cases/${id}/tasks`, data),
  updateTask: (id: string, taskId: string, data: {
    status?: string
    completed_at?: string
  }) => api.patch(`/cases/${id}/tasks/${taskId}`, data),
  
  // SLA
  getSLA: (id: string) => api.get(`/cases/${id}/sla`),
  assignSLA: (id: string, data: {
    sla_policy_id?: string  // Optional - if not provided, uses default for priority
  }) => api.post(`/cases/${id}/sla`, data),
  pauseSLA: (id: string) => api.post(`/cases/${id}/sla/pause`),
  resumeSLA: (id: string) => api.post(`/cases/${id}/sla/resume`),
  
  // Case Linking
  linkCase: (id: string, relatedCaseId: string, relationshipType: string) =>
    api.post(`/cases/${id}/links`, { related_case_id: relatedCaseId, relationship_type: relationshipType }),
  getLinkedCases: (id: string) => api.get(`/cases/${id}/links`),
  
  // Closure
  closeCase: (id: string, data: {
    resolution_summary: string
    root_cause?: string
    lessons_learned?: string
    recommendations?: string
  }) => api.post(`/cases/${id}/close`, data),
  
  // Escalation
  escalate: (id: string, data: {
    escalation_reason: string
    escalated_to?: string
    priority_override?: string
  }) => api.post(`/cases/${id}/escalate`, data),
  
  // Audit Log
  getAuditLog: (id: string) => api.get(`/cases/${id}/audit-log`),
  
  // Merge
  merge: (targetCaseId: string, sourceCaseId: string) =>
    api.post(`/cases/${targetCaseId}/merge`, { source_case_id: sourceCaseId, merged_by: 'user' }),

  // Bulk Operations
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

// SLA Policies API
export const slaPoliciesApi = {
  // List all policies
  getAll: (params?: {
    active_only?: boolean
    priority_level?: string
    default_only?: boolean
  }) => api.get('/sla-policies/', { params }),
  
  // Get specific policy
  getById: (policyId: string) => api.get(`/sla-policies/${policyId}`),
  
  // Create new policy
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
  
  // Update policy
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
  
  // Delete policy
  delete: (policyId: string, force?: boolean) =>
    api.delete(`/sla-policies/${policyId}`, { params: { force } }),
  
  // Set as default for priority level
  setDefault: (policyId: string) =>
    api.post(`/sla-policies/${policyId}/set-default`),
  
  // Get usage statistics
  getUsage: (policyId: string) =>
    api.get(`/sla-policies/${policyId}/usage`),
  
  // Get cases using this policy
  getCases: (policyId: string, params?: {
    status?: string
    breached_only?: boolean
  }) => api.get(`/sla-policies/${policyId}/cases`, { params }),
}

// Case Templates API
export const caseTemplatesApi = {
  getAll: () => api.get('/case-templates/'),
  getById: (id: string) => api.get(`/case-templates/${id}`),
  create: (data: {
    name: string
    description?: string
    default_title: string
    default_description?: string
    default_priority?: string
    default_status?: string
    default_tags?: string[]
    default_assignee?: string
    workflow_id?: string
  }) => api.post('/case-templates/', data),
  createFromTemplate: (templateId: string, variables: Record<string, any>) =>
    api.post(`/case-templates/${templateId}/create-case`, { variables }),
}

// Case Metrics API
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

// Case Search API
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

// Webhooks API
export const webhooksApi = {
  getAll: () => api.get('/webhooks/'),
  create: (data: {
    name: string
    url: string
    events: string[]
    secret?: string
    is_active?: boolean
  }) => api.post('/webhooks/', data),
  delete: (id: string) => api.delete(`/webhooks/${id}`),
  test: (id: string) => api.post(`/webhooks/${id}/test`),
}

// MCP Servers API
export const mcpApi = {
  listServers: () => api.get('/mcp/servers'),
  
  getStatuses: () => api.get('/mcp/servers/status'),
  
  getServerStatus: (name: string) => api.get(`/mcp/servers/${name}/status`),
  
  startServer: (name: string) => api.post(`/mcp/servers/${name}/start`),
  
  stopServer: (name: string) => api.post(`/mcp/servers/${name}/stop`),
  
  startAll: () => api.post('/mcp/servers/start-all'),
  
  stopAll: () => api.post('/mcp/servers/stop-all'),
  
  getLogs: (name: string, lines: number = 100) =>
    api.get(`/mcp/servers/${name}/logs`, { params: { lines } }),
  
  testServer: (name: string) => api.get(`/mcp/servers/${name}/test`),
  
  getEnabledStates: () => api.get('/mcp/servers/enabled'),
  
  setServerEnabled: (name: string, enabled: boolean) =>
    api.put(`/mcp/servers/${name}/enabled`, { enabled }),
}

// Claude API
export const claudeApi = {
  chat: (data: {
    messages: Array<{ 
      role: string
      content: string | Array<{
        type: string
        text?: string
        source?: {
          type: string
          media_type: string
          data: string
        }
      }>
    }>
    system_prompt?: string
    model?: string
    max_tokens?: number
    enable_thinking?: boolean
    thinking_budget?: number
    agent_id?: string
    streaming?: boolean
    use_agent_sdk?: boolean
  }) => api.post('/claude/chat', data),
  
  chatStream: (data: {
    messages: Array<{ 
      role: string
      content: string | Array<{
        type: string
        text?: string
        source?: {
          type: string
          media_type: string
          data: string
        }
      }>
    }>
    system_prompt?: string
    model?: string
    max_tokens?: number
    enable_thinking?: boolean
    thinking_budget?: number
    agent_id?: string
  }) => api.post('/claude/chat/stream', data, {
    responseType: 'stream',
    headers: {
      'Accept': 'text/event-stream',
    }
  }),
  
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
  
  getSdkStatus: () => api.get('/claude/sdk-status'),
  
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
  }) => api.post('/claude/summarize', data),
  
  analyzeFinding: (finding_id: string, context?: string) =>
    api.post('/claude/analyze-finding', null, {
      params: { finding_id, context },
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
  }) => api.post('/claude/generate-chat-report', data),
  
  // Agent SDK endpoints
  runAgentTask: (data: {
    task: string
    system_prompt?: string
    allowed_tools?: string[]
    max_turns?: number
    model?: string
    session_id?: string
    agent_id?: string
  }) => api.post('/claude/agent/task', data),
  
  streamAgentTask: (data: {
    task: string
    system_prompt?: string
    allowed_tools?: string[]
    max_turns?: number
    model?: string
    session_id?: string
    agent_id?: string
  }) => api.post('/claude/agent/stream', data, {
    responseType: 'stream',
    headers: {
      'Accept': 'text/event-stream',
    }
  }),
}

// Agents API
export const agentsApi = {
  listAgents: () => api.get('/agents/agents'),
  
  getAgent: (agent_id: string) => api.get(`/agents/agents/${agent_id}`),
  
  setCurrentAgent: (agent_id: string) => 
    api.post('/agents/agents/set-current', null, { params: { agent_id } }),
  
  startInvestigation: (data: {
    finding_id: string
    agent_id?: string
    additional_context?: string
  }) => api.post('/agents/agents/investigate', data),
  
  runAgent: (data: {
    finding_id?: string
    case_id?: string
    task?: string
    agent_id?: string
    use_agent_sdk?: boolean
  }) => api.post('/agents/agents/run', data),
}

// Config API
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

// Ingestion API
export const ingestionApi = {
  listS3Files: (prefix?: string) =>
    api.get('/ingest/s3-files', { params: { prefix: prefix ?? '' } }),
  ingestS3File: (key: string) =>
    api.post('/ingest/s3-file', { key }),
  uploadFile: (file: File, dataType: string = 'finding') => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('data_type', dataType)
    return api.post('/ingest/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

// Storage API
export const storageApi = {
  getStatus: () => api.get('/storage/status'),
  getHealth: () => api.get('/storage/health'),
  reconnect: () => api.post('/storage/reconnect'),
  switchBackend: (backend: string) => api.post('/storage/switch-backend', { backend }),
}

// Timesketch API
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

// ATT&CK API
export const attackApi = {
  getLayer: () => api.get('/attack/layer'),
  
  getTechniqueRollup: (min_confidence: number = 0.0) =>
    api.get('/attack/techniques/rollup', { params: { min_confidence } }),
  
  getFindingsByTechnique: (technique_id: string) =>
    api.get(`/attack/techniques/${technique_id}/findings`),
  
  getTacticsSummary: () => api.get('/attack/tactics/summary'),
}

// Timeline API
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
  
  // Event Visualization - comprehensive event details for incident analysis
  getEventVisualization: (event_id: string, params?: {
    time_window_minutes?: number
    include_ai_analysis?: boolean
  }) => api.get(`/timeline/event/${event_id}/visualization`, { params }),
  
  // Get timeline events for a specific finding
  getFindingEvents: (finding_id: string) => 
    api.get(`/timeline/finding/${finding_id}/context`, { params: { time_window_minutes: 60 } }),
}

// Graph API
export const graphApi = {
  getEntityGraph: (params: {
    finding_ids?: string
    case_id?: string
    cluster_id?: string
    limit?: number
  }) => api.get('/graph/entities', { params }),
  
  getAttackPath: (case_id: string) => api.get(`/graph/attack-path/${case_id}`),
  
  getClusterGraph: (cluster_id: string) => api.get(`/graph/cluster/${cluster_id}`),
  
  getTechniqueGraph: (technique_id: string, limit: number = 100) =>
    api.get(`/graph/technique/${technique_id}`, { params: { limit } }),
  
  getSummary: (limit: number = 100) => api.get('/graph/summary', { params: { limit } }),
}

// Detection Rules API (manages detection rule sources for MCP)
export const detectionRulesApi = {
  // List all registered detection rule sources
  listSources: () => api.get('/detection-rules/sources'),
  
  // Get a specific source by ID
  getSource: (sourceId: string) => api.get(`/detection-rules/sources/${sourceId}`),
  
  // Add a new detection rule source
  addSource: (data: {
    name: string
    source_type: 'git' | 'local'
    format: 'sigma' | 'splunk' | 'elastic' | 'kql' | 'auto'
    url?: string
    path?: string
    subdirectory?: string
    story_subdirectory?: string
  }) => api.post('/detection-rules/sources', data),
  
  // Remove a detection rule source
  removeSource: (sourceId: string, deleteFiles: boolean = false) =>
    api.delete(`/detection-rules/sources/${sourceId}`, { params: { delete_files: deleteFiles } }),
  
  // Update a specific source (git pull or rescan)
  updateSource: (sourceId: string) =>
    api.post(`/detection-rules/sources/${sourceId}/update`),
  
  // Update all sources
  updateAll: () => api.post('/detection-rules/update-all'),
  
  // Get aggregate statistics
  getStats: () => api.get('/detection-rules/stats'),
  
  // Get MCP environment variables
  getMcpEnv: () => api.get('/detection-rules/mcp-env'),
  
  // Reload the entire service (re-read config + rescan)
  reload: () => api.post('/detection-rules/reload'),
}

// Local Services API (Docker containers management)
export const localServicesApi = {
  // Splunk management
  getSplunkStatus: () => api.get('/services/splunk/status'),
  startSplunk: () => api.post('/services/splunk/start'),
  stopSplunk: () => api.post('/services/splunk/stop'),
  restartSplunk: () => api.post('/services/splunk/restart'),
  
  // PostgreSQL management
  getPostgresStatus: () => api.get('/services/postgres/status'),
}

// Skills API (workflow skill management and execution)
export const skillsApi = {
  // List all available skills
  listSkills: () => api.get('/workflows'),

  // Get full details for a specific skill (including markdown body)
  getSkill: (skillId: string) => api.get(`/workflows/${skillId}`),

  // Execute a skill workflow
  executeSkill: (skillId: string, params: {
    finding_id?: string
    case_id?: string
    context?: string
    hypothesis?: string
  }) => api.post(`/workflows/${skillId}/execute`, params),

  // Force reload skills from disk
  reloadSkills: () => api.post('/workflows/reload'),
}

// Orchestrator API (autonomous investigation management)
export const orchestratorApi = {
  getStatus: () => api.get('/orchestrator/status'),
  enable: () => api.post('/orchestrator/enable'),
  disable: () => api.post('/orchestrator/disable'),
  kill: () => api.post('/orchestrator/kill'),
  
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
    }),
  reviewInvestigation: (id: string, action: 'approve' | 'rework', notes?: string) =>
    api.post(`/orchestrator/investigations/${id}/review`, { action, notes }),

  getCost: () => api.get('/orchestrator/cost'),
}

export default api

