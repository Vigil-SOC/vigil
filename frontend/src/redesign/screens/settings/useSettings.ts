/* ============================================================
   Data hooks for the Settings screen — fetch via the shared axios
   client in services/api.ts (auth/CSRF/401-refresh included),
   expose loading/error states. useEffect + local state, matching
   the rest of the redesign (no React-Query). See useCases.ts.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import api, {
  aiConfigApi,
  budgetsApi,
  configApi,
  detectionRulesApi,
  federationApi,
  kafkaApi,
  llmProviderApi,
  localServicesApi,
  mcpApi,
  orchestratorApi,
  storageApi,
  type AIConfigResponse,
  type AIModelInfo,
  type BudgetQuotaResponse,
  type BudgetSettings,
  type ComponentAssignment,
  type FederationSourceView,
  type LLMProvider,
  type MempalaceHealth,
  type PlatformDatabaseProxyConfig,
} from '../../../services/api'
import { loadCustomIntegrations } from '../../../config/integrations'

export type Phase = 'loading' | 'ready' | 'error'

/* ---------------- General settings ---------------- */
export interface GeneralConfig {
  auto_start_sync: boolean
  show_notifications: boolean
  enable_keyring: boolean
  /** preserved round-trip (setGeneral requires it) — not edited in the UI */
  theme: string
}

const DEFAULT_GENERAL: GeneralConfig = {
  auto_start_sync: false,
  show_notifications: false,
  enable_keyring: false,
  theme: 'dark',
}

export function useGeneralSettings() {
  const [config, setConfig] = useState<GeneralConfig>(DEFAULT_GENERAL)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    configApi
      .getGeneral()
      .then((res) => {
        if (cancelled) return
        const d = (res.data || {}) as Partial<GeneralConfig>
        setConfig({
          auto_start_sync: Boolean(d.auto_start_sync),
          show_notifications: Boolean(d.show_notifications),
          enable_keyring: Boolean(d.enable_keyring),
          theme: d.theme || 'dark',
        })
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load general settings')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback(
    (next: GeneralConfig) => configApi.setGeneral(next).then(() => setConfig(next)),
    [],
  )

  return { config, setConfig, phase, error, reload, save }
}

/* ---------------- Mempalace health ---------------- */
export function useMempalaceHealth() {
  const [health, setHealth] = useState<MempalaceHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    configApi
      .getMempalaceHealth()
      .then((res) => {
        if (!cancelled) setHealth(res.data)
      })
      .catch(() => {
        if (!cancelled) setHealth(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { health, loading, reload }
}

/* ---------------- Platform DB proxy ---------------- */
export type ProxyType = 'none' | 'pgbouncer' | 'ssh_tunnel'

export interface PlatformDbForm {
  proxy_type: ProxyType
  proxy_host: string
  proxy_port: number
  proxy_username: string
  proxy_password: string
  ssh_private_key_path: string
  ssh_key_passphrase: string
  verify_proxy_tls: boolean
}

const EMPTY_PLATFORM_DB: PlatformDbForm = {
  proxy_type: 'none',
  proxy_host: '',
  proxy_port: 0,
  proxy_username: '',
  proxy_password: '',
  ssh_private_key_path: '',
  ssh_key_passphrase: '',
  verify_proxy_tls: true,
}

export function usePlatformDatabase() {
  const [form, setForm] = useState<PlatformDbForm>(EMPTY_PLATFORM_DB)
  const [hasPassword, setHasPassword] = useState(false)
  const [hasPassphrase, setHasPassphrase] = useState(false)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    configApi
      .getPlatformDatabase()
      .then((res) => {
        if (cancelled) return
        const d = res.data as PlatformDatabaseProxyConfig
        const proxyType = (
          ['none', 'pgbouncer', 'ssh_tunnel'].includes(d.proxy_type) ? d.proxy_type : 'none'
        ) as ProxyType
        setForm({
          proxy_type: proxyType,
          proxy_host: d.proxy_host || '',
          proxy_port: Number(d.proxy_port) || 0,
          proxy_username: d.proxy_username || '',
          proxy_password: '',
          ssh_private_key_path: d.ssh_private_key_path || '',
          ssh_key_passphrase: '',
          verify_proxy_tls: d.verify_proxy_tls ?? true,
        })
        setHasPassword(Boolean(d.has_proxy_password))
        setHasPassphrase(Boolean(d.has_ssh_key_passphrase))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load platform DB proxy config')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback(
    (f: PlatformDbForm) =>
      configApi
        .setPlatformDatabase({
          proxy_type: f.proxy_type,
          proxy_host: f.proxy_host,
          proxy_port: f.proxy_port || 0,
          proxy_username: f.proxy_username,
          proxy_password: f.proxy_password,
          ssh_private_key_path: f.ssh_private_key_path,
          ssh_key_passphrase: f.ssh_key_passphrase,
          verify_proxy_tls: f.verify_proxy_tls,
        })
        .then(() => {
          // clear secret inputs so a later save doesn't overwrite with stale UI state
          if (f.proxy_password) setHasPassword(true)
          if (f.ssh_key_passphrase) setHasPassphrase(true)
          setForm((prev) => ({ ...prev, proxy_password: '', ssh_key_passphrase: '' }))
        }),
    [],
  )

  return { form, setForm, hasPassword, hasPassphrase, phase, error, reload, save }
}

/* ---------------- Federation sources ---------------- */
export function useFederation() {
  const [sources, setSources] = useState<FederationSourceView[]>([])
  const [globalEnabled, setGlobalEnabled] = useState(false)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    const load = (initial: boolean) => {
      if (initial) setPhase('loading')
      federationApi
        .listSources()
        .then((res) => {
          if (cancelled) return
          setSources(res.data.sources || [])
          setGlobalEnabled(Boolean(res.data.global?.enabled))
          setPhase('ready')
        })
        .catch((e) => {
          if (cancelled || !initial) return
          setError((e as { message?: string })?.message || 'Failed to load federation sources')
          setPhase('error')
        })
    }
    load(true)
    // refresh every 10s so last_success_at advances (matches legacy FederationTab)
    const t = setInterval(() => load(false), 10_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [reloadKey])

  const setGlobal = useCallback(
    async (enabled: boolean) => {
      setGlobalEnabled(enabled)
      try {
        await federationApi.setSettings(enabled)
      } catch (e) {
        setGlobalEnabled(!enabled)
        throw e
      }
    },
    [],
  )

  const patchSource = useCallback(
    async (sourceId: string, patch: Parameters<typeof federationApi.updateSource>[1]) => {
      const res = await federationApi.updateSource(sourceId, patch)
      setSources((prev) => prev.map((s) => (s.source_id === sourceId ? res.data : s)))
    },
    [],
  )

  /** optimistic local edit (e.g. typing in the interval field before blur) */
  const editSourceLocal = useCallback((sourceId: string, patch: Partial<FederationSourceView>) => {
    setSources((prev) => prev.map((s) => (s.source_id === sourceId ? { ...s, ...patch } : s)))
  }, [])

  const pollNow = useCallback((sourceId: string) => federationApi.pollNow(sourceId), [])

  return {
    sources,
    globalEnabled,
    phase,
    error,
    reload,
    setGlobal,
    patchSource,
    editSourceLocal,
    pollNow,
  }
}

/* ---------------- Users & roles ---------------- */
export interface User {
  user_id: string
  username: string
  email: string
  full_name: string
  role_id: string
  is_active: boolean
  is_verified: boolean
  mfa_enabled: boolean
  last_login: string | null
  login_count: number
}

export interface Role {
  role_id: string
  name: string
  description: string
  permissions: Record<string, boolean>
  is_system_role: boolean
}

export interface UserPayload {
  username: string
  email: string
  password: string
  full_name: string
  role_id: string
}

export function useUsers() {
  const [users, setUsers] = useState<User[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    Promise.all([api.get('/users/'), api.get('/users/roles/list')])
      .then(([usersRes, rolesRes]) => {
        if (cancelled) return
        const u = usersRes.data?.users
        const r = rolesRes.data?.roles
        if (!Array.isArray(u) || !Array.isArray(r)) throw new Error('Invalid users/roles data')
        setUsers(u)
        setRoles(r)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load users')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const createUser = useCallback(
    (payload: UserPayload) => api.post('/users/', payload).then(() => reload()),
    [reload],
  )
  const updateUser = useCallback(
    (userId: string, patch: Partial<UserPayload & { is_active: boolean }>) =>
      api.put(`/users/${userId}`, patch).then(() => reload()),
    [reload],
  )
  const deleteUser = useCallback(
    (userId: string) => api.delete(`/users/${userId}`).then(() => reload()),
    [reload],
  )

  return { users, roles, phase, error, reload, createUser, updateUser, deleteUser }
}

/* ---------------- Auto-investigate (orchestrator) ---------------- */
export interface OrchestratorConfig {
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
}

export const ORCHESTRATOR_DEFAULTS: OrchestratorConfig = {
  enabled: true,
  dry_run: false,
  auto_assign_findings: true,
  auto_assign_severities: ['critical', 'high'],
  max_concurrent_agents: 3,
  max_iterations_per_agent: 50,
  max_runtime_per_investigation: 3600,
  max_cost_per_investigation: 5.0,
  max_total_hourly_cost: 20.0,
  max_total_daily_cost: 100.0,
  loop_interval: 60,
  agent_loop_delay: 2,
  stale_threshold: 300,
  dedup_window_minutes: 30,
  context_max_chars: 10000,
  plan_model: 'claude-sonnet-4-5-20250929',
  review_model: 'claude-sonnet-4-5-20250929',
  workdir_base: 'data/investigations',
}

export interface OrchestratorStatus {
  enabled?: boolean
  active_agents?: number
  total_investigations?: number
  cost?: { total_cost_usd?: number }
}

export function useOrchestrator() {
  const [config, setConfig] = useState<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [status, setStatus] = useState<OrchestratorStatus | null>(null)
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    Promise.all([
      configApi.getOrchestrator().catch(() => ({ data: ORCHESTRATOR_DEFAULTS })),
      orchestratorApi.getStatus().catch(() => ({ data: null })),
      aiConfigApi.listModels().catch(() => ({ data: { models: [] } })),
    ])
      .then(([cfgRes, statusRes, modelsRes]) => {
        if (cancelled) return
        setConfig({ ...ORCHESTRATOR_DEFAULTS, ...(cfgRes.data as Partial<OrchestratorConfig>) })
        setStatus((statusRes.data as OrchestratorStatus | null) ?? null)
        setModels((modelsRes.data as { models?: AIModelInfo[] })?.models || [])
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('ready') // fall back to defaults — never block the screen
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback((next: OrchestratorConfig) => configApi.setOrchestrator(next), [])
  const purgeAll = useCallback(
    () => orchestratorApi.purgeAll().then((res) => (res.data?.deleted ?? 0) as number),
    [],
  )

  return { config, setConfig, status, models, phase, reload, save, purgeAll }
}

/* ---------------- Developer · storage / PostgreSQL ---------------- */
export interface StorageInfo {
  backend?: string
}
export interface StorageHealthInfo {
  findings_count?: number
  cases_count?: number
}

export function useStorage() {
  const [status, setStatus] = useState<StorageInfo | null>(null)
  const [health, setHealth] = useState<StorageHealthInfo | null>(null)
  const [configured, setConfigured] = useState(false)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    Promise.all([
      storageApi.getStatus(),
      storageApi.getHealth(),
      configApi.getPostgreSQL().catch(() => ({ data: { configured: false } })),
    ])
      .then(([s, h, pg]) => {
        if (cancelled) return
        setStatus(s.data as StorageInfo)
        setHealth(h.data as StorageHealthInfo)
        setConfigured(Boolean((pg.data as { configured?: boolean })?.configured))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load storage status')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const reconnect = useCallback(() => storageApi.reconnect().then(() => reload()), [reload])
  const savePostgres = useCallback(
    (connectionString: string) => configApi.setPostgreSQL(connectionString).then(() => reload()),
    [reload],
  )

  return { status, health, configured, phase, error, reload, reconnect, savePostgres }
}

/* ---------------- Developer · local Splunk Enterprise ---------------- */
export interface SplunkStatus {
  running?: boolean
  web_url?: string
  hec_url?: string
  username?: string
  note?: string
}

export function useSplunk() {
  const [status, setStatus] = useState<SplunkStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    localServicesApi
      .getSplunkStatus()
      .then((res) => {
        if (!cancelled) setStatus(res.data as SplunkStatus)
      })
      .catch(() => {
        if (!cancelled) setStatus(null)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  // Splunk state takes a moment to settle, so re-poll after the action resolves.
  const run = useCallback(
    (fn: () => Promise<unknown>, delayMs: number) => {
      setBusy(true)
      return fn()
        .then(() => new Promise((r) => setTimeout(r, delayMs)))
        .then(() => reload())
        .finally(() => setBusy(false))
    },
    [reload],
  )

  const start = useCallback(() => run(() => localServicesApi.startSplunk(), 2000), [run])
  const stop = useCallback(() => run(() => localServicesApi.stopSplunk(), 1000), [run])
  const restart = useCallback(() => run(() => localServicesApi.restartSplunk(), 2000), [run])

  return { status, busy, reload, start, stop, restart }
}

/* ---------------- AI Config · LLM providers ---------------- */
export function useLlmProviders() {
  const [providers, setProviders] = useState<LLMProvider[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    llmProviderApi
      .list()
      .then((res) => {
        if (cancelled) return
        setProviders(res.data)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load providers')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const test = useCallback((id: string) => llmProviderApi.test(id).then((r) => r.data), [])
  const remove = useCallback((id: string) => llmProviderApi.remove(id).then(() => reload()), [reload])
  const setDefault = useCallback(
    (id: string) => llmProviderApi.setDefault(id).then(() => reload()),
    [reload],
  )

  return { providers, phase, error, reload, test, remove, setDefault }
}

/* ---------------- AI Config · operations (cost/perf knobs) ---------------- */
export interface AIOperationsSettings {
  prompt_cache_enabled: boolean
  history_window: number
  tool_response_budget_default: number
  thinking_budget: number
}

export const AI_OPS_DEFAULTS: AIOperationsSettings = {
  prompt_cache_enabled: true,
  history_window: 20,
  tool_response_budget_default: 8000,
  thinking_budget: 10000,
}

export function useAiOperations() {
  const [settings, setSettings] = useState<AIOperationsSettings>(AI_OPS_DEFAULTS)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    configApi
      .getAIOperations()
      .then((res) => {
        if (cancelled) return
        setSettings({ ...AI_OPS_DEFAULTS, ...(res.data as Partial<AIOperationsSettings>) })
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('ready') // fall back to defaults
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback((next: AIOperationsSettings) => configApi.setAIOperations(next), [])

  return { settings, setSettings, phase, reload, save }
}

/* ---------------- AI Config · per-component model assignment ---------------- */
export function useModelAssignment() {
  const [components, setComponents] = useState<string[]>([])
  const [assignments, setAssignments] = useState<Record<string, ComponentAssignment>>({})
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    Promise.all([aiConfigApi.getConfig(), aiConfigApi.listModels()])
      .then(([cfg, mdl]) => {
        if (cancelled) return
        const c = cfg.data as AIConfigResponse
        setComponents(c.components)
        setAssignments(c.assignments)
        setModels(mdl.data.models)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load AI config')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const assign = useCallback(
    (component: string, providerId: string, modelId: string) =>
      aiConfigApi
        .setComponent(component, { provider_id: providerId, model_id: modelId })
        .then(() =>
          setAssignments((prev) => ({
            ...prev,
            [component]: {
              component,
              provider_id: providerId,
              model_id: modelId,
              settings: {},
              updated_by: null,
              updated_at: null,
            },
          })),
        ),
    [],
  )

  const clearAssign = useCallback(
    (component: string) =>
      aiConfigApi.clearComponent(component).then(() =>
        setAssignments((prev) => {
          const copy = { ...prev }
          delete copy[component]
          return copy
        }),
      ),
    [],
  )

  return { components, assignments, models, phase, error, reload, assign, clearAssign }
}

/* ---------------- AI Config · budgets (Bifrost virtual key) ---------------- */
export function useBudgets() {
  const [settings, setSettings] = useState<BudgetSettings>({
    default_vk: '',
    budget_limit_usd: 0,
    enforcement_mode: 'warning',
  })
  const [quota, setQuota] = useState<BudgetQuotaResponse | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    Promise.all([budgetsApi.get(), budgetsApi.getQuota().catch(() => ({ data: null }))])
      .then(([s, q]) => {
        if (cancelled) return
        setSettings(s.data)
        setQuota(q.data as BudgetQuotaResponse | null)
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback(async (payload: BudgetSettings) => {
    const res = await budgetsApi.set(payload)
    setSettings(res.data)
    const q = await budgetsApi.getQuota().catch(() => ({ data: null }))
    setQuota(q.data as BudgetQuotaResponse | null)
    return res.data
  }, [])

  return { settings, setSettings, quota, phase, reload, save }
}

/* ---------------- Integrations · MCP servers ---------------- */
export interface ToggleResult {
  ok: boolean
  error?: string | null
}

export function useMcpServers() {
  const [servers, setServers] = useState<string[]>([])
  const [statuses, setStatuses] = useState<Record<string, string>>({})
  const [enabled, setEnabled] = useState<Record<string, boolean>>({})
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  // initial=true drives the phase spinner; silent refreshes keep the grid mounted
  const fetchAll = useCallback((initial: boolean) => {
    if (initial) setPhase('loading')
    return Promise.all([mcpApi.listServers(), mcpApi.getStatuses()])
      .then(([s, st]) => {
        setServers(s.data.servers || [])
        const statusList = st.data.statuses || []
        const statusDict: Record<string, string> = {}
        const enabledDict: Record<string, boolean> = {}
        if (Array.isArray(statusList)) {
          statusList.forEach((item: { name?: string; status?: string; enabled?: boolean }) => {
            if (item.name && item.status) statusDict[item.name] = item.status
            if (item.name) enabledDict[item.name] = !!item.enabled
          })
        }
        setStatuses(statusDict)
        setEnabled(enabledDict)
        setPhase('ready')
      })
      .catch((e) => {
        if (initial) {
          setError((e as { message?: string })?.message || 'Failed to load MCP servers')
          setPhase('error')
        }
      })
  }, [])

  useEffect(() => {
    let cancelled = false
    setError(null)
    fetchAll(true).finally(() => {
      if (cancelled) return
    })
    return () => {
      cancelled = true
    }
  }, [fetchAll, reloadKey])

  // Toggle mirrors the legacy transactional behavior: enabling also triggers a
  // connect; if the connect fails we revert so UI and backend stay in lockstep.
  const setServerEnabled = useCallback(
    async (name: string, want: boolean): Promise<ToggleResult> => {
      try {
        const res = await mcpApi.setServerEnabled(name, want)
        const { connected, error: connErr } = (res?.data || {}) as {
          connected?: boolean | null
          error?: string | null
        }
        if (want && connected === false) {
          mcpApi.setServerEnabled(name, false).catch(() => {})
          setEnabled((prev) => ({ ...prev, [name]: false }))
          return { ok: false, error: connErr }
        }
        setEnabled((prev) => ({ ...prev, [name]: want }))
        fetchAll(false)
        return { ok: true }
      } catch (e) {
        return { ok: false, error: (e as { message?: string })?.message }
      }
    },
    [fetchAll],
  )

  return { servers, statuses, enabled, phase, error, reload, setServerEnabled }
}

/* ---------------- General · cost analytics ---------------- */
export type CostTimeRange = '24h' | '7d' | '30d' | 'all'

export interface CostTotals {
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
  cache_hit_rate: number
}

export interface CostModelRow {
  model: string
  provider_type: string
  pricing_source: 'exact' | 'heuristic' | 'zero' | 'unknown'
  calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  cache_hit_rate: number
}

export interface CostData {
  totals: CostTotals
  by_model: CostModelRow[]
}

export function useCostAnalytics(timeRange: CostTimeRange) {
  const [data, setData] = useState<CostData | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    api
      .get<CostData>('/analytics/cost', { params: { time_range: timeRange } })
      .then((res) => {
        if (cancelled) return
        setData(res.data)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load cost analytics')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [timeRange, reloadKey])

  return { data, phase, error, reload }
}

/* ---------------- Integrations · per-integration credentials ---------------- */
export interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, Record<string, unknown>>
}

export function useIntegrationsConfig() {
  const [config, setConfig] = useState<IntegrationsConfig>({
    enabled_integrations: [],
    integrations: {},
  })
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    // populate custom-integration metadata so getAllIntegrations() is complete,
    // then load which integrations are enabled + their stored (non-secret) config
    loadCustomIntegrations()
      .catch(() => {})
      .finally(() => {
        configApi
          .getIntegrations()
          .then((res) => {
            if (cancelled) return
            const d = res.data as Partial<IntegrationsConfig>
            setConfig({
              enabled_integrations: d.enabled_integrations || [],
              integrations: d.integrations || {},
            })
            setPhase('ready')
          })
          .catch(() => {
            if (!cancelled) setPhase('ready')
          })
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const saveIntegration = useCallback(
    async (integrationId: string, fieldConfig: Record<string, unknown>) => {
      const integrations = { ...config.integrations, [integrationId]: fieldConfig }
      const enabled_integrations = config.enabled_integrations.includes(integrationId)
        ? config.enabled_integrations
        : [...config.enabled_integrations, integrationId]
      await configApi.setIntegrations({ enabled_integrations, integrations })
      setConfig({ enabled_integrations, integrations })
    },
    [config],
  )

  return { config, phase, reload, saveIntegration }
}

/* ---------------- Integrations · S3 storage ---------------- */
export interface S3Config {
  bucket_name: string
  region: string
  auth_method: string
  aws_profile: string
  access_key_id: string
  secret_access_key: string
  session_token: string
  findings_path: string
  cases_path: string
  parquet_prefix: string
  configured: boolean
}

const S3_DEFAULTS: S3Config = {
  bucket_name: '',
  region: 'us-east-1',
  auth_method: 'credentials',
  aws_profile: '',
  access_key_id: '',
  secret_access_key: '',
  session_token: '',
  findings_path: 'findings.json',
  cases_path: 'cases.json',
  parquet_prefix: '',
  configured: false,
}

export function useS3() {
  const [config, setConfig] = useState<S3Config>(S3_DEFAULTS)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    configApi
      .getS3()
      .then((res) => {
        if (cancelled) return
        setConfig((prev) => ({ ...prev, ...(res.data as Partial<S3Config>) }))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load S3 config')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback(
    (next: S3Config) => configApi.setS3(next).then(() => setConfig({ ...next, configured: true })),
    [],
  )

  return { config, setConfig, phase, error, reload, save }
}

/* ---------------- Integrations · Kafka ---------------- */
export interface KafkaConfig {
  enabled: boolean
  bootstrap_servers: string
  consumer_group: string
  topics: string[]
  auto_offset_reset: string
  security_protocol: string
  sasl_mechanism: string | null
  sasl_username: string | null
  max_poll_records: number
  session_timeout_ms: number
}

export interface KafkaStats {
  connected: boolean
  messages_consumed: number
  messages_enqueued: number
  duplicates_skipped: number
  last_message_at: string | null
  last_error: string | null
}

export const KAFKA_DEFAULTS: KafkaConfig = {
  enabled: false,
  bootstrap_servers: 'localhost:9092',
  consumer_group: 'vigil-soc',
  topics: [],
  auto_offset_reset: 'latest',
  security_protocol: 'PLAINTEXT',
  sasl_mechanism: null,
  sasl_username: null,
  max_poll_records: 500,
  session_timeout_ms: 30000,
}

export function useKafka() {
  const [config, setConfig] = useState<KafkaConfig>(KAFKA_DEFAULTS)
  const [stats, setStats] = useState<KafkaStats | null>(null)
  const [daemonReachable, setDaemonReachable] = useState(false)
  const [phase, setPhase] = useState<Phase>('loading')

  const loadStatus = useCallback(() => {
    return kafkaApi
      .getStatus()
      .then((res) => {
        const data = res.data as { daemon_reachable?: boolean; stats?: KafkaStats; config?: Partial<KafkaConfig> }
        setDaemonReachable(!!data.daemon_reachable)
        setStats(data.stats || null)
        if (data.config) setConfig((prev) => ({ ...prev, ...data.config }))
      })
      .catch(() => setDaemonReachable(false))
  }, [])

  useEffect(() => {
    let cancelled = false
    kafkaApi
      .getConfig()
      .then((res) => {
        if (!cancelled) setConfig((prev) => ({ ...prev, ...(res.data as Partial<KafkaConfig>) }))
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) {
          loadStatus().finally(() => !cancelled && setPhase('ready'))
        }
      })
    const t = setInterval(() => !cancelled && loadStatus(), 5000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [loadStatus])

  const save = useCallback(
    (next: KafkaConfig) =>
      kafkaApi
        .setConfig({
          ...next,
          sasl_mechanism: next.sasl_mechanism || null,
          sasl_username: next.sasl_username || null,
        })
        .then(() => loadStatus()),
    [loadStatus],
  )

  return { config, setConfig, stats, daemonReachable, phase, save }
}

/* ---------------- Integrations · Darktrace webhook ---------------- */
export interface DarktraceConfig {
  enabled: boolean
  url: string
  max_body_kb: number
  webhook_secret: string
  configured: boolean
}

const DARKTRACE_DEFAULTS: DarktraceConfig = {
  enabled: false,
  url: '',
  max_body_kb: 1024,
  webhook_secret: '',
  configured: false,
}

export function useDarktrace() {
  const [config, setConfig] = useState<DarktraceConfig>(DARKTRACE_DEFAULTS)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    configApi
      .getDarktrace()
      .then((res) => {
        if (cancelled) return
        setConfig((prev) => ({ ...prev, ...(res.data as Partial<DarktraceConfig>), webhook_secret: '' }))
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('ready') // fall back to defaults
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback((next: DarktraceConfig) => {
    const payload: {
      enabled: boolean
      url: string
      max_body_kb: number
      webhook_secret?: string
    } = { enabled: next.enabled, url: next.url, max_body_kb: next.max_body_kb }
    if (next.webhook_secret) payload.webhook_secret = next.webhook_secret
    return configApi.setDarktrace(payload).then(() => setConfig((prev) => ({ ...prev, webhook_secret: '', configured: true })))
  }, [])

  return { config, setConfig, phase, reload, save }
}

/* ---------------- Integrations · detection rule sources ---------------- */
export interface DetectionSource {
  id: string
  name: string
  type: 'git' | 'local'
  git_url: string
  format: string
  subdirectory: string
  story_subdirectory: string
  rule_count: number
  last_updated: string | null
  status: string
}

export interface DetectionStats {
  total_rules: number
  sources_count: number
  by_format: Record<string, number>
}

export interface AddSourcePayload {
  name: string
  source_type: 'git' | 'local'
  format: 'sigma' | 'splunk' | 'elastic' | 'kql' | 'auto'
  url?: string
  path?: string
  subdirectory?: string
  story_subdirectory?: string
}

export function useDetectionRules() {
  const [sources, setSources] = useState<DetectionSource[]>([])
  const [stats, setStats] = useState<DetectionStats | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    Promise.all([detectionRulesApi.listSources(), detectionRulesApi.getStats()])
      .then(([s, st]) => {
        if (cancelled) return
        setSources((s.data.sources || []) as DetectionSource[])
        setStats(st.data as DetectionStats)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(e?.response?.data?.detail || e?.message || 'Failed to load detection rules')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const addSource = useCallback(
    (data: AddSourcePayload) => detectionRulesApi.addSource(data).then(() => reload()),
    [reload],
  )
  const removeSource = useCallback(
    (id: string, deleteFiles: boolean) =>
      detectionRulesApi.removeSource(id, deleteFiles).then(() => reload()),
    [reload],
  )
  const updateSource = useCallback(
    (id: string) => detectionRulesApi.updateSource(id).then(() => reload()),
    [reload],
  )
  const updateAll = useCallback(
    () =>
      detectionRulesApi.updateAll().then((res) => {
        reload()
        return (res.data?.results || []) as { success: boolean }[]
      }),
    [reload],
  )

  return { sources, stats, phase, error, reload, addSource, removeSource, updateSource, updateAll }
}
