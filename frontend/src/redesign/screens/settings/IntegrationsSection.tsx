/* ============================================================
   Settings · Integrations — MCP server grid grouped by category,
   with search, summary chips, status indicators, and enable/disable
   toggles (with revert-on-failed-connect), per-server credential
   wizard + "Not Configured" state, docs links, the custom-integration
   builder, plus Data Ingestion and Detection Rules sub-tabs. Full
   parity with the legacy Integrations tab.
   ============================================================ */
import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { TextInput } from '../../shared/ui'
import { useMcpServers, useIntegrationsConfig } from './useSettings'
import {
  getIntegrationForServer,
  HIDDEN_MCP_SERVERS,
  MCP_CATEGORIES,
  SERVER_DESCRIPTIONS,
  WIP_SERVERS,
  prettyServerName,
} from './integrationsData'
import DataIngestionPanel from './DataIngestion'
import DetectionRulesPanel from './DetectionRulesPanel'
import CustomIntegrationBuilder from './CustomIntegrationBuilder'
import IntegrationWizard from './IntegrationWizard'
import type { IntegrationMetadata } from '../../../components/settings/IntegrationWizard'
import type { SectionProps } from './types'

type IntegrationsTab = 'servers' | 'ingestion' | 'detection'
const TABS: [IntegrationsTab, string][] = [
  ['servers', 'MCP Servers'],
  ['ingestion', 'Data Ingestion'],
  ['detection', 'Detection Rules'],
]

export default function IntegrationsSection({ notify }: SectionProps) {
  const [tab, setTab] = useState<IntegrationsTab>('servers')
  return (
    <>
      <div className="tabs" style={{ gap: 4 }}>
        {TABS.map(([k, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'servers' && <ServersPanel notify={notify} />}
      {tab === 'ingestion' && <DataIngestionPanel notify={notify} />}
      {tab === 'detection' && <DetectionRulesPanel notify={notify} />}
    </>
  )
}

function ServersPanel({ notify }: SectionProps) {
  const { servers, statuses, enabled, phase, error, reload, setServerEnabled } = useMcpServers()
  const { config: intCfg, reload: reloadInt, saveIntegration } = useIntegrationsConfig()
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [builderOpen, setBuilderOpen] = useState(false)
  const [wizardFor, setWizardFor] = useState<IntegrationMetadata | null>(null)

  const visible = useMemo(
    () => servers.filter((n) => !HIDDEN_MCP_SERVERS.has(n)),
    [servers],
  )

  // group visible servers into the ordered categories, with an "Other" bucket
  const grouped = useMemo(() => {
    const q = search.toLowerCase()
    const match = (n: string) =>
      !q || n.toLowerCase().includes(q) || (SERVER_DESCRIPTIONS[n] || '').toLowerCase().includes(q)
    const claimed = new Set<string>()
    const out: { label: string; servers: string[] }[] = []
    for (const cat of MCP_CATEGORIES) {
      const inCat = visible.filter((n) => cat.servers.includes(n))
      inCat.forEach((n) => claimed.add(n))
      const shown = inCat.filter(match)
      if (shown.length) out.push({ label: cat.label, servers: shown })
    }
    const other = visible.filter((n) => !claimed.has(n)).filter(match)
    if (other.length) out.push({ label: 'Other', servers: other })
    return out
  }, [visible, search])

  const enabledCount = visible.filter((n) => enabled[n]).length
  const runningCount = visible.filter((n) => statuses[n] === 'running').length

  const onToggle = async (name: string, want: boolean) => {
    setBusy(name)
    const res = await setServerEnabled(name, want)
    setBusy(null)
    if (res.ok) notify('ok', `${prettyServerName(name)} ${want ? 'enabled' : 'disabled'}.`)
    else notify('err', `Could not start ${prettyServerName(name)}${res.error ? `: ${res.error}` : ''}.`)
  }

  return (
    <div className="settings-content-inner flex flex-col gap-4" style={{ maxWidth: 1280 }}>
      {/* header / toolbar */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex gap-2 flex-wrap flex-1">
          <span className="chip" style={{ color: 'var(--accent-2)' }}>{enabledCount} Enabled</span>
          <span className="chip" style={{ color: 'var(--ok)' }}>{runningCount} Running</span>
          <span className="chip">{visible.length} Active</span>
        </div>
        <div className="search" style={{ minWidth: 220 }}>
          <Icon name="search" size={15} />
          <TextInput
            placeholder="Search integrations…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button className="btn ghost" onClick={() => setBuilderOpen(true)}><Icon name="plus" /> Build Custom</button>
        <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
      </div>


      {phase === 'loading' && <div className="text-sm text-tx-3 py-16 text-center">Loading integrations…</div>}
      {phase === 'error' && (
        <div className="py-16 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load MCP servers: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      )}

      {phase === 'ready' && grouped.length === 0 && (
        <div className="text-sm text-tx-3 py-10 text-center">No integrations match “{search}”.</div>
      )}

      {phase === 'ready' &&
        grouped.map((cat) => (
          <section key={cat.label}>
            <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-5">
              {cat.label}
            </h4>
            <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
              {cat.servers.map((name) => {
                const isEnabled = !!enabled[name]
                const isRunning = statuses[name] === 'running'
                const integration = getIntegrationForServer(name)
                const isConfigured = integration
                  ? intCfg.enabled_integrations.includes(integration.id)
                  : false
                const needsConfig = !!integration && !isConfigured
                // green = on · gray = needs config · red = off
                const dotColor = isEnabled ? 'var(--ok)' : needsConfig ? 'var(--tx-faint)' : 'var(--crit)'
                const label = isEnabled ? (isRunning ? 'Running' : 'Enabled') : needsConfig ? 'Not Configured' : 'Off'
                const canConfigure = !!integration?.fields?.length
                return (
                  <div key={name} className="card card-sq p-3.5 flex flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-semibold text-tx truncate flex-1">
                        {prettyServerName(name)}
                      </span>
                      {WIP_SERVERS.has(name) && (
                        <span className="chip" style={{ color: 'var(--high)', fontSize: 10 }}>WIP</span>
                      )}
                      <button
                        type="button"
                        role="switch"
                        aria-checked={isEnabled}
                        aria-label={`Toggle ${name}`}
                        disabled={busy === name}
                        className={`toggle${isEnabled ? ' on' : ''}`}
                        onClick={() => onToggle(name, !isEnabled)}
                      >
                        <span className="toggle-knob" />
                      </button>
                    </div>
                    <p className="text-xs text-tx-3 leading-snug line-clamp-2 min-h-[2rem]">
                      {SERVER_DESCRIPTIONS[name] || integration?.description || 'Custom MCP integration.'}
                    </p>
                    <div className="flex items-center gap-1.5 mt-auto">
                      <span style={{ width: 7, height: 7, borderRadius: '50%', background: dotColor }} />
                      <span className="text-xs text-tx-3 flex-1">{label}</span>
                      {integration?.docs_url && (
                        <a className="btn ghost icon" title="Documentation" href={integration.docs_url} target="_blank" rel="noreferrer">
                          <Icon name="doc" size={14} />
                        </a>
                      )}
                      {canConfigure && (
                        <button className="btn ghost icon" title="Configure credentials" onClick={() => setWizardFor(integration!)}>
                          <Icon name="gear" size={14} />
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        ))}

      {builderOpen && (
        <CustomIntegrationBuilder
          onClose={() => setBuilderOpen(false)}
          onSave={(id) => {
            setBuilderOpen(false)
            notify('ok', `Custom integration "${id}" saved. Restart MCP servers to load it.`)
            reload()
            reloadInt()
          }}
        />
      )}

      {wizardFor && (
        <IntegrationWizard
          integration={wizardFor}
          existingConfig={intCfg.integrations[wizardFor.id] || {}}
          onClose={() => setWizardFor(null)}
          onSave={async (id, cfg) => {
            await saveIntegration(id, cfg)
            notify('ok', `${wizardFor.name} configured. Enable it with the toggle if it isn’t already.`)
          }}
        />
      )}
    </div>
  )
}
