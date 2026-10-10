import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { PageHead } from '../../shared/PageHead'
import { LevelBadge } from '../../shared/LevelBadge'
import { EmptyState, Toggle } from '../../shared/ui'
import { useExtensions } from '../../extensions/ExtensionProvider'
import { basePath } from '../../config/basePath'
import { getAllIntegrations } from '../../config/integrations'
import {
  MCP_CATEGORIES,
  SERVER_DESCRIPTIONS,
  SERVER_DISPLAY_NAMES,
  WIP_SERVERS,
  prettyServerName,
  INTEGRATIONS_DESC,
  tabFromQuery,
  type IntegrationsTab,
} from './integrationsData'
import { relativeTime, type ServerRow } from './integrationHealth'
import { useIntegrationsState } from './IntegrationsState'
import AddIntegrationTab from './AddIntegrationTab'
import { buildCatalog } from './integrationCatalog'
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
  const [busy, setBusy] = useState<string | null>(null)
  // saved custom integrations, from GET /api/custom-integrations/list; null until known or when it fails (non-admin)
  const [customCount, setCustomCount] = useState<number | null>(null)
  const [wizardFor, setWizardFor] = useState<IntegrationMetadata | null>(null)
  const { error, reload: reloadMcp, setServerEnabled } = mcp
  const { config: intCfg, reload: reloadInt, saveIntegration, setIntegrationEnabled } = int

  // state lives above the section, so a visit does not refetch: Refresh does
  const reload = () => {
    reloadMcp()
    reloadInt()
  }

  const reloadCustom = useCallback(async () => {
    try {
      const r = await fetch(`${basePath}/api/custom-integrations/list`, { credentials: 'include' })
      const d = r.ok ? await r.json() : null
      setCustomCount(Array.isArray(d?.integrations) ? d.integrations.length : null)
    } catch {
      setCustomCount(null)
    }
  }, [])

  useEffect(() => {
    setTab(requested)
  }, [requested])

  useEffect(() => {
    reloadCustom()
  }, [reloadCustom])

  const connected = useMemo(
    () => rows.filter((r) => r.connected).sort((a, b) => categoryRank(a.name) - categoryRank(b.name)),
    [rows],
  )
  const catalog = getAllIntegrations()
  const connectedIds = useMemo(
    () => new Set([...intCfg.enabled_integrations, ...connected.flatMap((r) => (r.integration ? [r.integration.id] : []))]),
    [intCfg.enabled_integrations, connected],
  )
  const entries = useMemo(() => buildCatalog(catalog, rows, connectedIds), [catalog, rows, connectedIds])
  const available = entries.filter((e) => !e.connected).length
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
    ['custom', 'Custom', customCount],
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
    <>
    <PageHead
      title="Integrations"
      description={INTEGRATIONS_DESC}
      actions={
        <>
          <button className="btn ghost" onClick={() => setTab('custom')}><Icon name="sparkle" /> Build custom</button>
          <button className="btn primary" onClick={() => setTab('add')}><Icon name="plus" /> Add integration</button>
        </>
      }
    />
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

      {(tab === 'connected' || tab === 'add') && phase === 'loading' && <EmptyState loading icon="link" title="Loading integrations…" />}
      {(tab === 'connected' || tab === 'add') && phase === 'error' && (
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
        <AddIntegrationTab
          entries={entries}
          busy={busy}
          onConnect={setWizardFor}
          onTurnOn={(name) => onToggleMcp(name, true)}
          onRefresh={reload}
        />
      )}

      {/* mounted on every tab so a draft survives a visit elsewhere */}
      <div hidden={tab !== 'custom'}>
        <CustomIntegrationBuilder
          notify={notify}
          onWrote={reloadCustom}
          onSaved={() => {
            reloadCustom()
            reload()
          }}
        />
      </div>

      {tab === 'surface' && <McpSurfacePanel notify={notify} />}

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
    </>
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
      <Toggle
        checked={masterOn}
        mixed={masterMixed}
        label={`Toggle ${name}`}
        title={
          !extConfigured
            ? 'Configure the connector first'
            : masterMixed
              ? 'Partially enabled — toggle again to retry'
              : undefined
        }
        disabled={busy === name || !extConfigured}
        onChange={(v) => onMaster(name, integration.id, v)}
      />
    )
  }
  return <Toggle checked={isEnabled} label={`Toggle ${name}`} disabled={busy === name} onChange={(v) => onMcp(name, v)} />
}
