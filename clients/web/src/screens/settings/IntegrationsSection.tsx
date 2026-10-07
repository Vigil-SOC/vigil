import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { EmptyState, TextInput } from '../../shared/ui'
import { useExtensions } from '../../extensions/ExtensionProvider'
import { getAllIntegrations } from '../../config/integrations'
import {
  MCP_CATEGORIES,
  SERVER_DESCRIPTIONS,
  SERVER_DISPLAY_NAMES,
  WIP_SERVERS,
  prettyServerName,
  tabFromQuery,
  type IntegrationsTab,
} from './integrationsData'
import { relativeTime, type ServerRow } from './integrationHealth'
import { useIntegrationsState } from './IntegrationsState'
import CustomIntegrationBuilder from './CustomIntegrationBuilder'
import IntegrationWizard from './IntegrationWizard'
import McpSurfacePanel from './McpSurfacePanel'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import type { SectionProps } from './types'

const displayName = (name: string) => SERVER_DISPLAY_NAMES.get(name) ?? prettyServerName(name)
const categoryOf = (name: string) => MCP_CATEGORIES.find((c) => c.servers.includes(name))?.label ?? 'Other'
const categoryRank = (name: string) => {
  const i = MCP_CATEGORIES.findIndex((c) => c.servers.includes(name))
  return i < 0 ? MCP_CATEGORIES.length : i
}
const canConfigure = (r: ServerRow) => !!r.integration?.fields?.length

export default function IntegrationsSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const requested = tabFromQuery(searchParams.get('tab'))
  const [tab, setTab] = useState<IntegrationsTab>(requested)
  const { mcp, int, phase, rows, attention } = useIntegrationsState()
  const { reload: reloadExtensions } = useExtensions()
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [builderOpen, setBuilderOpen] = useState(false)
  const [wizardFor, setWizardFor] = useState<IntegrationMetadata | null>(null)
  const { error, reload: reloadMcp, setServerEnabled } = mcp
  const { config: intCfg, reload: reloadInt, saveIntegration, setIntegrationEnabled } = int

  // state lives above the section, so a visit does not refetch: Refresh does
  const reload = () => {
    reloadMcp()
    reloadInt()
  }

  useEffect(() => {
    setTab(requested)
  }, [requested])

  const connected = useMemo(
    () => rows.filter((r) => r.connected).sort((a, b) => categoryRank(a.name) - categoryRank(b.name)),
    [rows],
  )
  const addable = useMemo(() => {
    const q = search.toLowerCase()
    const match = (r: ServerRow) =>
      !q || r.name.toLowerCase().includes(q) || (SERVER_DESCRIPTIONS.get(r.name) ?? '').toLowerCase().includes(q)
    return rows
      .filter((r) => !r.connected && match(r))
      .sort((a, b) => categoryRank(a.name) - categoryRank(b.name))
  }, [rows, search])
  const catalog = getAllIntegrations()
  const connectedIds = new Set([
    ...intCfg.enabled_integrations,
    ...connected.flatMap((r) => (r.integration ? [r.integration.id] : [])),
  ])
  const available = catalog.filter((i) => !connectedIds.has(i.id)).length
  const customCount = catalog.filter((i) => (i as { is_custom?: boolean }).is_custom).length
  const healthy = connected.filter((r) => r.level === 'good').length

  // gate M: MCP server on/off (agent tools)
  const onToggleMcp = async (name: string, want: boolean) => {
    setBusy(name)
    const res = await setServerEnabled(name, want)
    setBusy(null)
    if (res.ok) notify('ok', `${prettyServerName(name)} ${want ? 'enabled' : 'disabled'}.`)
    else notify('err', `Could not start ${prettyServerName(name)}${res.error ? `: ${res.error}` : ''}.`)
  }

  // master: one switch over both gates
  const onToggleMaster = async (name: string, id: string, want: boolean) => {
    setBusy(name)
    const res = await setServerEnabled(name, want)
    try {
      await setIntegrationEnabled(id, want)
      reloadExtensions()
    } catch {
      notify('err', `Could not ${want ? 'enable' : 'disable'} the ${id} panel.`)
    }
    setBusy(null)
    if (res.ok) notify('ok', `${prettyServerName(name)} ${want ? 'enabled' : 'disabled'}.`)
    else notify('err', `Could not start ${prettyServerName(name)}${res.error ? `: ${res.error}` : ''}.`)
  }

  const ready = phase === 'ready'
  const tabs: [IntegrationsTab, string, number | null][] = [
    ['connected', 'Connected', ready ? connected.length : null],
    ['add', 'Add integration', ready ? available : null],
    ['custom', 'Custom', ready ? customCount : null],
    ['surface', 'Vigil MCP server', null],
  ]
  const first = attention[0]
  const tiles: [string, number, string][] = [
    ['Connected', connected.length, 'var(--tx0)'],
    ['Healthy', healthy, 'var(--good)'],
    ['Need attention', attention.length, attention.length ? 'var(--fair)' : 'var(--tx0)'],
    ['Available to add', available, 'var(--tx0)'],
  ]

  // pb-20: the floating Ask Vigil button covers nothing at the end of the scroll
  return (
    <div className="settings-content-inner flex flex-col gap-4 pb-20" style={{ maxWidth: 1280 }}>
      {ready && (
        <>
          {first && (
            <div className="int-banner" role="status">
              <Icon name="alert" size={17} />
              <span className="int-banner-text">
                <b>{displayName(first.name)} needs a fix.</b> {first.note}
                {attention.length > 1 && <span className="int-banner-more">{attention.length - 1} more need attention.</span>}
              </span>
              {canConfigure(first) && (
                <button className="btn primary int-act" onClick={() => setWizardFor(first.integration!)}>Fix</button>
              )}
            </div>
          )}
          <div className="int-tiles">
            {tiles.map(([label, value, color]) => (
              <div key={label} className="int-tile">
                <span className="int-tile-k">{label}</span>
                <span className="int-tile-v" style={{ color }}>{value}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="wf-tabs" role="tablist" aria-label="Integrations views">
        {tabs.map(([k, label, n]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            aria-label={n === null ? label : `${label} ${n}`}
            className="wf-tab"
            onClick={() => setTab(k)}
          >
            {label}
            {n !== null && <span className="wf-count">{n}</span>}
          </button>
        ))}
      </div>

      {tab !== 'surface' && phase === 'loading' && <EmptyState loading icon="link" title="Loading integrations…" />}
      {tab !== 'surface' && phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load MCP servers" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
      )}

      {tab === 'connected' && ready && connected.length === 0 && (
        <EmptyState
          compact
          icon="link"
          title="Nothing is connected yet"
          body="Connect a tool so agents can read from it and act through it."
          primary={{ label: 'Add integration', onClick: () => setTab('add'), icon: 'plus' }}
        />
      )}
      {tab === 'connected' && ready && connected.length > 0 && (
        <div className="flex justify-end">
          <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
        </div>
      )}
      {tab === 'connected' && ready && connected.length > 0 && (
        <table className="int-table">
          <colgroup>
            <col />
            <col style={{ width: 120 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: '25%' }} />
            <col style={{ width: '22%' }} />
            <col style={{ width: 56 }} />
            <col style={{ width: 112 }} />
          </colgroup>
          <thead>
            <tr>
              <th>Integration</th>
              <th>Health</th>
              <th>
                Last verified
                <InfoTip
                  label="About Last verified"
                  align="start"
                  source="The last test of the integration, run through POST /api/config/integrations/{id}/test."
                  limit="Never-tested integrations show —. A failed test stays until the next test passes."
                />
              </th>
              <th>What agents may do with it</th>
              <th>Note</th>
              <th>On</th>
              <th><span className="sr-only">Action</span></th>
            </tr>
          </thead>
          <tbody>
            {connected.map((r) => (
              <tr key={r.name}>
                <td>
                  <span className="int-name">
                    {displayName(r.name)}
                    {WIP_SERVERS.has(r.name) && <span className="chip int-wip">WIP</span>}
                  </span>
                  <span className="int-sub">{categoryOf(r.name)}</span>
                </td>
                <td>
                  {r.level ? <LevelBadge level={r.level} variant="pill" /> : <span className="int-neutral">{r.word}</span>}
                </td>
                <td className="int-time">
                  {r.lastTest ? <time dateTime={r.lastTest.at} title={r.lastTest.at}>{relativeTime(r.lastTest.at)}</time> : <span title="Never tested">—</span>}
                </td>
                <td><Clamp text={SERVER_DESCRIPTIONS.get(r.name) || r.integration?.description || 'Custom MCP integration.'} /></td>
                <td className={r.level === 'poor' ? 'int-note poor' : 'int-note'}><Clamp text={r.note} /></td>
                <td><RowToggle row={r} busy={busy} onMcp={onToggleMcp} onMaster={onToggleMaster} /></td>
                <td>
                  {canConfigure(r) && (
                    <button className={`btn int-act${r.level === 'poor' ? ' primary' : ''}`} onClick={() => setWizardFor(r.integration!)}>
                      {r.level === 'poor' ? 'Fix' : 'Configure'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === 'add' && ready && (
        <>
          <div className="flex items-center gap-3 flex-wrap">
            <div className="search" style={{ flex: 1, minWidth: 220, maxWidth: 420 }}>
              <Icon name="search" size={15} />
              <TextInput placeholder="Search integrations…" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
          </div>
          {addable.length === 0 && (
            <EmptyState
              compact
              icon="filter"
              title={search ? 'No integrations match this search' : 'Everything available is connected'}
              body={search ? `No MCP servers match “${search}”.` : undefined}
              primary={search ? { label: 'Clear search', onClick: () => setSearch(''), icon: 'close' } : undefined}
            />
          )}
          {addable.length > 0 && (
            <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
              {addable.map((r) => (
                <div key={r.name} className="card card-sq p-3.5 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-semibold text-tx truncate flex-1">{displayName(r.name)}</span>
                    {WIP_SERVERS.has(r.name) && <span className="chip" style={{ color: 'var(--high)', fontSize: 10 }}>WIP</span>}
                    <RowToggle row={r} busy={busy} onMcp={onToggleMcp} onMaster={onToggleMaster} />
                  </div>
                  <p className="text-xs text-tx-3 leading-snug line-clamp-2 min-h-[2rem]">
                    {SERVER_DESCRIPTIONS.get(r.name) || r.integration?.description || 'Custom MCP integration.'}
                  </p>
                  {r.note && <p className="text-[11px] text-tx-3 leading-snug line-clamp-2" title={r.note}>{r.note}</p>}
                  <div className="flex items-center gap-1.5 mt-auto">
                    <span className="text-xs text-tx-3 flex-1">{r.word}</span>
                    {r.integration?.docs_url && (
                      <a className="btn ghost icon" title="Documentation" href={r.integration.docs_url} target="_blank" rel="noreferrer">
                        <Icon name="doc" size={14} />
                      </a>
                    )}
                    {canConfigure(r) && (
                      <button className="btn ghost icon" title="Configure credentials" onClick={() => setWizardFor(r.integration!)}>
                        <Icon name="gear" size={14} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {tab === 'custom' && ready && (
        <EmptyState
          compact
          icon="plus"
          title={customCount ? `${customCount} custom integration${customCount === 1 ? '' : 's'} saved` : 'No custom integrations yet'}
          body="Describe a tool Vigil does not ship with and it becomes an MCP server agents can use."
          primary={{ label: 'Build custom integration', onClick: () => setBuilderOpen(true), icon: 'plus' }}
        />
      )}

      {tab === 'surface' && <McpSurfacePanel notify={notify} />}

      {builderOpen && (
        <CustomIntegrationBuilder
          onClose={() => setBuilderOpen(false)}
          onSave={(id) => {
            setBuilderOpen(false)
            notify('ok', `Custom integration "${id}" saved. Restart the MCP servers to load it.`)
            reload()
          }}
        />
      )}

      {wizardFor && (
        <IntegrationWizard
          integration={wizardFor}
          existingConfig={intCfg.integrations[wizardFor.id] || {}}
          secretsSet={intCfg.secrets_set[wizardFor.id] || {}}
          lastTest={intCfg.last_test[wizardFor.id]}
          category={categoryOf(rows.find((r) => r.integration?.id === wizardFor.id)?.name ?? '')}
          onTested={reloadInt}
          onClose={() => setWizardFor(null)}
          onSave={async (id, cfg) => {
            await saveIntegration(id, cfg)
            // refresh the registry so a URL edit re-reads; saving alone doesn't enable
            const isExtension = wizardFor.fields?.some((f) => f.name === 'connectorUrl')
            if (isExtension) reloadExtensions()
            notify(
              'ok',
              isExtension
                ? `${wizardFor.name} connected. Turn it on with the toggle to enable it.`
                : `${wizardFor.name} configured. Enable it with the toggle if it isn’t already.`,
            )
          }}
        />
      )}
    </div>
  )
}

/** Prose in a table cell: wraps, stops at two lines, full text on hover. */
function Clamp({ text }: { text: string }) {
  return text ? <span className="int-clamp" title={text}>{text}</span> : null
}

/** On/off. A connector-backed integration is one master switch over two gates:
 * the extension (gate D) and the MCP server / agent tools (gate M). */
function RowToggle({ row, busy, onMcp, onMaster }: {
  row: ServerRow
  busy: string | null
  onMcp: (name: string, want: boolean) => void
  onMaster: (name: string, id: string, want: boolean) => void
}) {
  const { name, integration, isEnabled, masterOn, masterMixed, extConfigured } = row
  if (row.isExtension && integration) {
    return (
      <button
        type="button"
        role="switch"
        aria-checked={masterMixed ? 'mixed' : masterOn}
        aria-label={`Toggle ${name}`}
        title={
          !extConfigured
            ? 'Configure the connector first'
            : masterMixed
              ? 'Partially enabled — toggle again to retry'
              : undefined
        }
        disabled={busy === name || !extConfigured}
        className={`toggle${masterOn ? ' on' : ''}${masterMixed ? ' mixed' : ''}`}
        onClick={() => onMaster(name, integration.id, !masterOn)}
      >
        <span className="toggle-knob" />
      </button>
    )
  }
  return (
    <button
      type="button"
      role="switch"
      aria-checked={isEnabled}
      aria-label={`Toggle ${name}`}
      disabled={busy === name}
      className={`toggle${isEnabled ? ' on' : ''}`}
      onClick={() => onMcp(name, !isEnabled)}
    >
      <span className="toggle-knob" />
    </button>
  )
}
