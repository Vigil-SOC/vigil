// frontend/src/redesign/screens/setup/DataSourceDialog.tsx
//
// Onboarding step panel — pick a telemetry source and connect it (credentials +
// MCP enable) without leaving setup. The picker lists only sources that have a
// real MCP server behind them (catalog ∩ live mcpApi.listServers()), so it never
// offers a dead-end — the same server-first sourcing Settings uses. Picking one
// opens the redesign IntegrationWizard for the per-integration credential form.
// Save mirrors Settings: configApi.setIntegrations keyed by catalog id, then
// mcpApi.setServerEnabled by server name to actually connect.
import { useEffect, useMemo, useRef, useState } from 'react'
import IntegrationWizard from '../settings/IntegrationWizard'
import type { IntegrationMetadata } from '../../../components/settings/IntegrationWizard'
import { getAllIntegrations } from '../../../config/integrations'
import { DATA_SOURCE_CATEGORIES } from '../../../setup/setupSteps'
import { configApi, mcpApi } from '../../../services/api'
import { TextInput } from '../../shared/ui'

interface Props {
  onSaved: () => void
}

// Catalog id → MCP server name, for the data-source ids that differ from their
// server (the drift documented in setupSteps' MCP_ONLY_DATA_SOURCE_IDS). Used
// both to filter the picker to connectable sources and to connect on save, so
// the two can't drift apart again.
const CATALOG_TO_SERVER: Record<string, string> = {
  'aws-security-hub': 'aws-security',
  'gcp-security': 'gcp-scc',
  'elastic-siem': 'elastic',
}
const serverFor = (catalogId: string) => CATALOG_TO_SERVER[catalogId] ?? catalogId

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, Record<string, unknown>>
}

const DataSourceDialog = ({ onSaved }: Props) => {
  const [selected, setSelected] = useState<IntegrationMetadata | null>(null)
  const [query, setQuery] = useState('')
  // Real MCP servers (mcpApi.listServers) — the picker shows only catalog
  // sources that map to one of these. null = still loading.
  const [availableServers, setAvailableServers] = useState<Set<string> | null>(null)
  // Loaded once so the merge keeps other integrations' config intact and the
  // wizard can pre-fill an already-configured source.
  const cfg = useRef<IntegrationsConfig>({ enabled_integrations: [], integrations: {} })

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

    mcpApi
      .listServers()
      .then(({ data }) => {
        if (alive) setAvailableServers(new Set(data?.servers ?? []))
      })
      .catch(() => {
        if (alive) setAvailableServers(new Set())
      })

    return () => {
      alive = false
    }
  }, [])

  // Only offer sources with a real MCP backend: the catalog lists ~48 data
  // sources but only ~14 have a server, so picking the rest would be a dead-end
  // (mirrors Settings, which sources its list from the live servers). null
  // servers = still loading → empty until known.
  const dataSources = useMemo(() => {
    if (!availableServers) return []
    return getAllIntegrations().filter(
      (i) => DATA_SOURCE_CATEGORIES.has(i.category) && availableServers.has(serverFor(i.id)),
    )
  }, [availableServers])

  // The catalog is long (40+ sources across SIEM/EDR/cloud/network), so the
  // picker filters by name or category — keeps the panel a sane height instead
  // of a giant scroll of cards.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return dataSources
    return dataSources.filter(
      (i) => i.name.toLowerCase().includes(q) || i.category.toLowerCase().includes(q),
    )
  }, [dataSources, query])

  // Persist credentials, then connect the MCP server. Throwing surfaces the
  // message in the wizard's own error banner; resolving lets it close and we
  // finish the step (onSaved → collapse + refetch).
  const handleSave = async (id: string, config: Record<string, unknown>) => {
    const cur = cfg.current
    const integrations = { ...cur.integrations, [id]: config }
    const enabled = cur.enabled_integrations.includes(id)
      ? cur.enabled_integrations
      : [...cur.enabled_integrations, id]
    await configApi.setIntegrations({ enabled_integrations: enabled, integrations })

    const serverName = serverFor(id)
    const res = await mcpApi.setServerEnabled(serverName, true)
    if (res?.data && res.data.success === false) {
      throw new Error(res.data.error || `Could not connect ${serverName}`)
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
      {/* Fixed height (not max-h): a stable results window you scroll within, so
          filtering doesn't snap the panel smaller on each keystroke. ~3 rows tall. */}
      <div className="h-56 overflow-y-auto pr-1 -mr-1">
        {availableServers === null ? (
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
