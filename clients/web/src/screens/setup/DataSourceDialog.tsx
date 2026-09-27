import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import IntegrationWizard from '../settings/IntegrationWizard'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { getAllIntegrations } from '../../config/integrations'
import { DATA_SOURCE_CATEGORIES } from './setupSteps'
import { configApi, mcpApi } from '../../services/api'
import { TextInput } from '../../shared/ui'

interface Props {
  onSaved: () => void
}

// for the few ids whose mcp-config.json server key differs from the catalog id.
// Shared by the picker filter and connect-on-save, so the two can't diverge.
// Splunk maps to the self-hosted REST server: that is the one the form's URL and
// credentials configure. The official server takes SPLUNK_MCP_URL instead.
const CATALOG_TO_SERVER: Record<string, string> = {
  'aws-security-hub': 'aws-security',
  'gcp-security': 'gcp-scc',
  'elastic-siem': 'elastic',
  'splunk': 'splunk-selfhosted',
}
const serverFor = (catalogId: string) => CATALOG_TO_SERVER[catalogId] ?? catalogId

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, Record<string, unknown>>
}

const DataSourceDialog = ({ onSaved }: Props) => {
  const [selected, setSelected] = useState<IntegrationMetadata | null>(null)
  const [query, setQuery] = useState('')
  const [availableServers, setAvailableServers] = useState<Set<string> | null>(null)
  const [serversError, setServersError] = useState(false)
  // loaded once, so the save merges instead of clobbering other integrations
  const cfg = useRef<IntegrationsConfig>({ enabled_integrations: [], integrations: {} })

  // a fetch failure is kept distinct from "no servers", so it can't masquerade
  // as an empty picker
  const loadServers = useCallback(() => {
    setServersError(false)
    setAvailableServers(null)
    mcpApi
      .listServers()
      .then(({ data }) => setAvailableServers(new Set(data?.servers ?? [])))
      .catch(() => setServersError(true))
  }, [])

  useEffect(() => {
    let alive = true
    configApi
      .getIntegrations()
      .then(({ data }) => {
        if (!alive || !data) return
        cfg.current = {
          enabled_integrations: data.enabled_integrations || [],
          integrations: data.integrations || {},
        }
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    loadServers()
  }, [loadServers])

  // only sources with a live MCP server behind them; the rest are dead ends
  const dataSources = useMemo(() => {
    if (!availableServers) return []
    return getAllIntegrations().filter(
      (i) => DATA_SOURCE_CATEGORIES.has(i.category) && availableServers.has(serverFor(i.id)),
    )
  }, [availableServers])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return dataSources
    return dataSources.filter(
      (i) => i.name.toLowerCase().includes(q) || i.category.toLowerCase().includes(q),
    )
  }, [dataSources, query])

  const handleSave = async (id: string, config: Record<string, unknown>) => {
    const cur = cfg.current
    const integrations = { ...cur.integrations, [id]: config }
    const alreadyEnabled = cur.enabled_integrations.includes(id)
    const enabled = alreadyEnabled
      ? cur.enabled_integrations
      : [...cur.enabled_integrations, id]
    await configApi.setIntegrations({ enabled_integrations: enabled, integrations })

    const serverName = serverFor(id)
    const { data } = await mcpApi.setServerEnabled(serverName, true)
    // success:true only means the enabled bit persisted; `connected` is the real
    // result. null = MCP subsystem down, not a cred failure, so let it through.
    if (data?.connected === false) {
      mcpApi.setServerEnabled(serverName, false).catch(() => {})
      // the checklist keys off enabled_integrations, so a source that never
      // connected must not count. Only when we just added it.
      if (!alreadyEnabled)
        configApi
          .setIntegrations({ enabled_integrations: cur.enabled_integrations, integrations })
          .catch(() => {})
      const missing = data.missing_credentials?.length
        ? `Missing required credentials: ${data.missing_credentials.join(', ')}.`
        : null
      throw new Error(
        data.error ||
          missing ||
          `Couldn't connect to ${selected?.name ?? serverName}. Check the credentials and try again.`,
      )
    }
    onSaved()
  }

  if (selected) {
    return (
      <IntegrationWizard
        integration={selected}
        existingConfig={cfg.current.integrations[selected.id] ?? {}}
        onClose={() => setSelected(null)}
        onSave={handleSave}
      />
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-tx-2">
        Connect a SIEM, EDR, or other telemetry source so Vigil has alerts to triage. Search and
        pick one — you can add more anytime in Settings → Integrations.
      </p>
      <TextInput
        value={query}
        placeholder="Search data sources (Splunk, CrowdStrike, Elastic…)"
        onChange={(e) => setQuery(e.target.value)}
      />
      {/* Fixed height (not max-h) so filtering doesn't resize the panel per keystroke. */}
      <div className="h-56 overflow-y-auto pr-1 -mr-1">
        {serversError ? (
          <div className="py-6 text-center text-sm text-tx-3">
            Couldn&apos;t load available sources.{' '}
            <button className="text-accent-2 hover:underline" onClick={loadServers}>
              Retry
            </button>
          </div>
        ) : availableServers === null ? (
          <div className="py-6 text-center text-sm text-tx-3">Loading available sources…</div>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {filtered.map((i) => (
              <button
                key={i.id}
                className="card card-sq text-left p-3"
                onClick={() => setSelected(i)}
              >
                <div className="text-[13px] font-semibold text-tx">{i.name}</div>
                <div className="text-xs text-tx-3 mt-0.5">{i.category}</div>
              </button>
            ))}
            {filtered.length === 0 && (
              <div className="col-span-2 py-6 text-center text-sm text-tx-3">
                {query.trim()
                  ? `No data sources match “${query.trim()}”.`
                  : 'No connectable data sources found.'}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default DataSourceDialog
