// frontend/src/redesign/screens/setup/DataSourceDialog.tsx
//
// Onboarding step dialog — pick a telemetry source from the catalog and connect
// it inline (credentials + MCP enable) without leaving setup. Reuses the
// redesign IntegrationWizard for the per-integration credential form; the save
// path mirrors Settings: configApi.setIntegrations keyed by catalog id, then
// mcpApi.setServerEnabled by server name to actually connect.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Popup } from '../../shared/ui'
import IntegrationWizard from '../settings/IntegrationWizard'
import type { IntegrationMetadata } from '../../../components/settings/IntegrationWizard'
import { getAllIntegrations } from '../../../config/integrations'
import { DATA_SOURCE_CATEGORIES } from '../../../setup/setupSteps'
import { configApi, mcpApi } from '../../../services/api'

interface Props {
  onClose: () => void
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

const DataSourceDialog = ({ onClose, onSaved }: Props) => {
  const [selected, setSelected] = useState<IntegrationMetadata | null>(null)
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

  // Persist credentials, then connect the MCP server. Throwing surfaces the
  // message in the wizard's own error banner; resolving lets it close and we
  // finish the step (onSaved → close + refetch).
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
    <Popup
      open
      onClose={onClose}
      title="Connect a data source"
      width={560}
      dismissOnBackdrop={false}
    >
      <div className="flex flex-col gap-3.5">
        <p className="text-sm text-tx-2">
          Connect a SIEM, EDR, or other telemetry source so Vigil has alerts to triage. Pick one
          to configure — you can add more anytime in Settings → Integrations.
        </p>
        <div className="grid grid-cols-2 gap-2">
          {dataSources.map((i) => (
            <button key={i.id} className="card card-sq text-left p-3" onClick={() => setSelected(i)}>
              <div className="text-[13px] font-semibold text-tx">{i.name}</div>
              <div className="text-xs text-tx-3 mt-0.5">{i.category}</div>
            </button>
          ))}
        </div>
      </div>
    </Popup>
  )
}

export default DataSourceDialog
