import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { getAllIntegrations } from '../../config/integrations'
import { DATA_SOURCE_CATEGORIES } from './setupSteps'
import { configApi, mcpApi } from '../../services/api'
import { Icon, type IconName } from '../../shared/icons'
import { TextInput } from '../../shared/ui'
import ChoiceCard from './ChoiceCard'
import ConnectSource, { type ConnectResult } from './ConnectSource'
import { fieldsOf, type ConnectConfig } from './connectConfig'
import { DemoSource, UploadSource } from './FileAndDemoSources'

// for the few ids whose mcp-config.json server key differs from the catalog id.
// Shared by the picker filter and connect-on-save, so the two can't diverge.
// Splunk maps to the self-hosted REST server: that is the one the form's URL and
// credentials configure. The official server takes SPLUNK_MCP_URL instead.
const CATALOG_TO_SERVER: Record<string, string> = {
  'aws-security-hub': 'aws-security',
  'elastic-siem': 'elastic',
  'splunk': 'splunk-selfhosted',
}
const serverFor = (catalogId: string) => CATALOG_TO_SERVER[catalogId] ?? catalogId

// catalog id -> federation source_id, for the ids that have a collector
const CATALOG_TO_SOURCE: Record<string, string> = {
  crowdstrike: 'crowdstrike',
  splunk: 'splunk',
  'elastic-siem': 'elastic',
  'azure-sentinel': 'azure_sentinel',
  'aws-security-hub': 'aws_security_hub',
  'microsoft-defender': 'microsoft_defender',
}

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, ConnectConfig>
  // per integration, which password fields already have a stored value (booleans only)
  secrets_set: Record<string, Record<string, boolean>>
}

// the board's tile icons, from the shared set (no vendor logos)
const CATEGORY_ICON: Record<string, IconName> = {
  SIEM: 'chart',
  'EDR/XDR': 'shield',
  'Cloud Security': 'lock',
  'Network Security': 'graph',
  'Data Pipeline': 'flow',
  'Detection & AI': 'brain',
}
const TILE_COUNT = 6

type Pick = string | null | undefined

const DataSourceDialog = ({ onAdvance }: { onAdvance: () => void }) => {
  // undefined = the first source, null = nothing (after "Connect another")
  const [picked, setPicked] = useState<Pick>(undefined)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')
  const [availableServers, setAvailableServers] = useState<Set<string> | null>(null)
  const [serversError, setServersError] = useState(false)
  const [cfgReady, setCfgReady] = useState(false)
  // loaded once, so the save merges instead of clobbering other integrations
  const cfg = useRef<IntegrationsConfig>({ enabled_integrations: [], integrations: {}, secrets_set: {} })

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
          secrets_set: data.secrets_set || {},
        }
      })
      .catch(() => {})
      .finally(() => alive && setCfgReady(true))
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

  const found = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return dataSources
    return dataSources.filter(
      (i) => i.name.toLowerCase().includes(q) || i.category.toLowerCase().includes(q),
    )
  }, [dataSources, query])

  // saves run one at a time: each reads and rewrites the shared config
  const saving = useRef<Promise<unknown>>(Promise.resolve())
  const handleSave = (integration: IntegrationMetadata, config: ConnectConfig) => {
    const run = saving.current.then(() => save(integration, config))
    saving.current = run.catch(() => {})
    return run
  }

  const save = async (integration: IntegrationMetadata, config: ConnectConfig): Promise<ConnectResult> => {
    const { id } = integration
    const cur = cfg.current
    const integrations = { ...cur.integrations, [id]: config }
    const alreadyEnabled = cur.enabled_integrations.includes(id)
    const enabled = alreadyEnabled ? cur.enabled_integrations : [...cur.enabled_integrations, id]
    await configApi.setIntegrations({ enabled_integrations: enabled, integrations })
    // keep the saved form for "Test again", but not the secrets: only that they are set
    const kept = { ...config }
    const secrets = { ...cur.secrets_set[id] }
    for (const f of fieldsOf(integration))
      if (f.type === 'password') {
        if (kept[f.name]) secrets[f.name] = true
        delete kept[f.name]
      }
    cfg.current = {
      enabled_integrations: enabled,
      integrations: { ...cur.integrations, [id]: kept },
      secrets_set: { ...cur.secrets_set, [id]: secrets },
    }

    const serverName = serverFor(id)
    const { data } = await mcpApi.setServerEnabled(serverName, true)
    // success:true only means the enabled bit persisted; `connected` is the real
    // result. null = MCP subsystem down, not a cred failure, so it is let through.
    const connected = data?.connected ?? null
    if (connected === false) {
      mcpApi.setServerEnabled(serverName, false).catch(() => {})
      // the checklist keys off enabled_integrations, so a source that never
      // connected must not count. Only when we just added it.
      if (!alreadyEnabled) {
        cfg.current.enabled_integrations = cur.enabled_integrations
        configApi
          .setIntegrations({ enabled_integrations: cur.enabled_integrations, integrations })
          .catch(() => {})
      }
    }
    return { connected, error: data?.error, missing_credentials: data?.missing_credentials }
  }

  const selectedId = picked === undefined ? dataSources[0]?.id : picked
  const selected = dataSources.find((i) => i.id === selectedId)
  // a source picked from the search takes the last source tile, so the selection stays visible
  const tiles = dataSources.slice(0, TILE_COUNT)
  if (selected && !tiles.includes(selected)) tiles[tiles.length - 1] = selected
  const pick = (id: string) => {
    setPicked(id)
    setSearching(false)
    setQuery('')
  }
  const tile = (i: IntegrationMetadata) => (
    <ChoiceCard
      key={i.id}
      title={i.name}
      body={i.category}
      icon={<Icon name={CATEGORY_ICON[i.category] ?? 'grid'} />}
      chip={i.id === 'loglm' ? 'DeepTempo' : undefined}
      selected={selectedId === i.id}
      onSelect={() => pick(i.id)}
    />
  )

  return (
    <>
      <div role="group" aria-label="Data source" className="su-choices">
        {tiles.map(tile)}
        <ChoiceCard
          title="Upload a file"
          body="An export from any tool"
          icon={<Icon name="upload" />}
          selected={selectedId === 'upload'}
          onSelect={() => pick('upload')}
        />
        <ChoiceCard
          title="Try demo data"
          body="Explore first, connect later"
          icon={<Icon name="sparkle" />}
          chip="No setup"
          chipTone="vio"
          selected={selectedId === 'demo'}
          onSelect={() => pick('demo')}
        />
      </div>
      {serversError ? (
        <p className="su-note">
          Couldn&apos;t load available sources.{' '}
          <button className="text-accent-2 hover:underline" onClick={loadServers}>
            Retry
          </button>
        </p>
      ) : availableServers === null ? (
        <p className="su-note">Loading available sources…</p>
      ) : (
        <>
          {dataSources.length === 0 && <p className="su-note">No connectable data sources found.</p>}
          {dataSources.length > TILE_COUNT && (
            <button
              type="button"
              className="su-more"
              aria-expanded={searching}
              onClick={() => setSearching((s) => !s)}
            >
              More sources
            </button>
          )}
        </>
      )}
      {searching && (
        <div className="flex flex-col gap-3">
          <TextInput
            value={query}
            placeholder="Search data sources (Splunk, CrowdStrike, Elastic…)"
            aria-label="Search data sources"
            onChange={(e) => setQuery(e.target.value)}
          />
          {found.length === 0 ? (
            <p className="su-note">No data sources match “{query.trim()}”.</p>
          ) : (
            <div className="su-choices">{found.map(tile)}</div>
          )}
        </div>
      )}
      {selectedId === 'upload' ? (
        <UploadSource />
      ) : selectedId === 'demo' ? (
        <DemoSource />
      ) : selected ? (
        cfgReady && (
          <ConnectSource
            key={selected.id}
            integration={selected}
            existingConfig={cfg.current.integrations[selected.id] ?? {}}
            secretsSet={cfg.current.secrets_set[selected.id] ?? {}}
            sourceId={CATALOG_TO_SOURCE[selected.id]}
            onTest={(config) => handleSave(selected, config)}
            onAdvance={onAdvance}
            onAnother={() => setPicked(null)}
          />
        )
      ) : (
        <p className="su-note">Pick a source above to connect it.</p>
      )}
    </>
  )
}

export default DataSourceDialog
