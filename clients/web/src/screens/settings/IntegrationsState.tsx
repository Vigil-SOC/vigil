import { createContext, useContext, useMemo, useRef, type ReactNode } from 'react'
import { buildRows, keepConnected, needsAttention, type ServerRow } from './integrationHealth'
import { useIntegrationsConfig, useMcpServers, type Phase } from './useSettings'

interface IntegrationsState {
  mcp: ReturnType<typeof useMcpServers>
  /** ready only once both fetches landed, so health never shows half-loaded */
  phase: Phase
  int: ReturnType<typeof useIntegrationsConfig>
  rows: ServerRow[]
  /** the Need attention tile, the banner and the Settings nav badge */
  attention: ServerRow[]
}

const Ctx = createContext<IntegrationsState | null>(null)

/** One fetch of MCP status and integration config, shared by the Integrations
 * section and the Settings nav badge (which shows while another section is open). */
export function IntegrationsStateProvider({ children }: { children: ReactNode }) {
  const mcp = useMcpServers()
  const int = useIntegrationsConfig()
  const { servers, statuses, enabled, errors, missingCredentials } = mcp
  const { config } = int
  const kept = useRef(new Set<string>())
  const phase: Phase = mcp.phase === 'error' ? 'error' : mcp.phase === 'ready' && int.phase === 'ready' ? 'ready' : 'loading'
  const rows = useMemo(
    () =>
      phase !== 'ready' ? [] : keepConnected(buildRows({
        servers,
        statuses,
        enabled,
        errors,
        missingCredentials,
        enabledIntegrations: config.enabled_integrations,
        integrations: config.integrations,
        lastTest: config.last_test,
      }), kept.current),
    [phase, servers, statuses, enabled, errors, missingCredentials, config],
  )
  const attention = useMemo(() => rows.filter(needsAttention), [rows])
  return <Ctx.Provider value={{ mcp, int, phase, rows, attention }}>{children}</Ctx.Provider>
}

export function useIntegrationsState(): IntegrationsState {
  const v = useContext(Ctx)
  if (!v) throw new Error('useIntegrationsState needs an IntegrationsStateProvider')
  return v
}
