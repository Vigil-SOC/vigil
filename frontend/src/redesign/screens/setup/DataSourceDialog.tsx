// frontend/src/redesign/screens/setup/DataSourceDialog.tsx
//
// Onboarding step panel — pick a telemetry source from the catalog and connect
// it (credentials + MCP enable) without leaving setup. The picker renders inline
// on the setup screen; picking one opens the redesign IntegrationWizard for the
// per-integration credential form (a focused modal — the shared component we
// reuse as-is). The save path mirrors Settings: configApi.setIntegrations keyed
// by catalog id, then mcpApi.setServerEnabled by server name to actually connect.
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

// Catalog id → MCP server name. Identity for all but the two that drift
// (inverse of Settings' SERVER_TO_INTEGRATION). A wrong name just leaves the
// server unconnected, so the step stays honestly not-ready.
const INTEGRATION_TO_SERVER: Record<string, string> = {
  'aws-security-hub': 'aws-security',
  'gcp-security': 'gcp-scc',
}

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, Record<string, unknown>>
}

const DataSourceDialog = ({ onSaved }: Props) => {
  const [selected, setSelected] = useState<IntegrationMetadata | null>(null)
  const [query, setQuery] = useState('')
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
    return () => {
      alive = false
    }
  }, [])

  const dataSources = useMemo(
    () => getAllIntegrations().filter((i) => DATA_SOURCE_CATEGORIES.has(i.category)),
    [],
  )

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

    const serverName = INTEGRATION_TO_SERVER[id] ?? id
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
        placeholder="Search data sources (Splunk, CrowdStrike, Wiz…)"
        onChange={(e) => setQuery(e.target.value)}
      />
      {/* Fixed height (not max-h): a stable results window you scroll within, so
          filtering doesn't snap the panel smaller on each keystroke. ~3 rows tall. */}
      <div className="h-56 overflow-y-auto pr-1 -mr-1">
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
              No data sources match “{query.trim()}”.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default DataSourceDialog
