import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { PageHead } from '../../shared/PageHead'
import { LevelBadge } from '../../shared/LevelBadge'
import { ConfirmDialog, EmptyState, Toggle } from '../../shared/ui'
import { extractApiError } from '../../shared/formKit'
import { useExtensions } from '../../extensions/ExtensionProvider'
import { getAllIntegrations } from '../../config/integrations'
import { configApi, detectionRulesApi, mcpApi } from '../../services/api'
import {
  MCP_CATEGORIES,
  SERVER_DESCRIPTIONS,
  baseServerOf,
  logoForServer,
  serverForIntegration,
  serverTitle,
  INTEGRATIONS_DESC,
  tabFromQuery,
  type IntegrationsTab,
} from './integrationsData'
import { factsPhrase, rowPhrase, type ServerRow } from './integrationHealth'
import { useIntegrationsState } from './IntegrationsState'
import AddIntegrationTab from './AddIntegrationTab'
import AuditFeedCard from './AuditFeedCard'
import { buildCatalog, savedCustomWithoutServer } from './integrationCatalog'
import CustomIntegrationBuilder from './CustomIntegrationBuilder'
import DeclaredSourcesCard from './DeclaredSourcesCard'
import IntegrationMark from './IntegrationMark'
import IntegrationWizard from './IntegrationWizard'
import McpSurfacePanel from './McpSurfacePanel'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import type { InstanceCreated } from './useSettings'
import type { SectionProps } from './types'

const categoryOf = (name: string) => MCP_CATEGORIES.find((c) => c.servers.includes(name))?.label ?? 'Other'
const categoryRank = (name: string) => {
  const i = MCP_CATEGORIES.findIndex((c) => c.servers.includes(name))
  return i < 0 ? MCP_CATEGORIES.length : i
}
const canConfigure = (r: ServerRow) => !!r.integration?.fields?.length
/** security-detections' one setup action: fetch its rule repositories */
const FETCH_RULES = 'fetch_detection_rules'

/** What a new instance's save copied from its base, for the notice. */
const copiedPhrase = (c?: InstanceCreated) =>
  !c
    ? ''
    : c.not_copied
      ? ` Its tool classes were not copied, because ${c.not_copied}; its tools ask first.`
      : ` Copied ${c.classes} tool class${c.classes === 1 ? '' : 'es'} and ${c.bindings} binding${c.bindings === 1 ? '' : 's'} from its integration.`

export default function IntegrationsSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const requested = tabFromQuery(searchParams.get('tab'))
  const [tab, setTab] = useState<IntegrationsTab>(requested)
  const { mcp, int, health, phase, rows, attention } = useIntegrationsState()
  const { reload: reloadExtensions } = useExtensions()
  const [busy, setBusy] = useState<string | null>(null)
  const [reloadingServers, setReloadingServers] = useState(false)
  const [wizardFor, setWizardFor] = useState<IntegrationMetadata | null>(null)
  // the drawer adds another instance of wizardFor instead of editing it
  const [addingInstance, setAddingInstance] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)
  const { error, reload: reloadMcp, setServerEnabled } = mcp
  const { config: intCfg, custom, reloadCustom, reload: reloadInt, saveIntegration, setIntegrationEnabled } = int
  const { instances, instances_allowed: instancesAllowed } = intCfg
  // an instance's row borrows its base's mark, category and description, under its own name
  const displayName = (name: string) => serverTitle(name, instances)
  const base = (name: string) => baseServerOf(name, instances)
  const openWizard = (i: IntegrationMetadata, adding = false) => {
    setAddingInstance(adding)
    setWizardFor(i)
  }

  // state lives above the section, so a visit does not refetch: Refresh does
  const reload = () => {
    reloadMcp()
    reloadInt()
    health.reload()
  }

  // Refresh only re-reads; this asks the backend to re-read mcp-config.json and
  // apply it: changed servers reconnect, removed or disabled ones stop
  const onReloadServers = async () => {
    setReloadingServers(true)
    try {
      const { data } = await mcpApi.reloadServers()
      const n = data?.total_servers
      const failed = Object.entries(data?.failed ?? {})
      const parts = [typeof n === 'number' ? `${n} server${n === 1 ? '' : 's'} configured.` : '']
      if (data?.reconnected?.length) parts.push(`Reconnected ${data.reconnected.map(displayName).join(', ')}.`)
      if (data?.disconnected?.length) parts.push(`Stopped ${data.disconnected.map(displayName).join(', ')}.`)
      if (failed.length) {
        const why = failed.map(([name, err]) => `${displayName(name)}${err ? ` (${err})` : ''}`).join(', ')
        notify('err', ['Tool servers reloaded, but not every server came back.', ...parts, `Failed: ${why}.`].filter(Boolean).join(' '))
      } else {
        notify('ok', ['Tool servers reloaded.', ...parts].filter(Boolean).join(' '))
      }
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
      const detail = err.response?.data?.detail
      notify('err', `Could not reload tool servers: ${typeof detail === 'string' ? detail : err.message || 'unknown error'}.`)
    } finally {
      setReloadingServers(false)
      // sessions may have changed even when some failed; a 4xx changed nothing, and the re-read is cheap
      reload()
    }
  }

  useEffect(() => {
    setTab(requested)
  }, [requested])

  // by category, keeping the server list's order; an instance sorts right after its base
  const connected = useMemo(() => {
    const order = new Map(rows.map((r, i) => [r.name, i]))
    const key = (name: string) => {
      const b = baseServerOf(name, instances)
      return [categoryRank(b), order.get(b) ?? order.get(name)!, b === name ? 0 : 1, order.get(name)!]
    }
    return rows
      .filter((r) => r.connected)
      .sort((a, b) => {
        const [x, y] = [key(a.name), key(b.name)]
        return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || x[3] - y[3]
      })
  }, [rows, instances])
  const catalog = getAllIntegrations()
  const connectedIds = useMemo(
    () => new Set([...intCfg.enabled_integrations, ...connected.flatMap((r) => (r.integration ? [r.integration.id] : []))]),
    [intCfg.enabled_integrations, connected],
  )
  const entries = useMemo(() => buildCatalog(catalog, rows, connectedIds), [catalog, rows, connectedIds])
  // a saved custom integration with no server is not available to add
  const available = entries.filter((e) => !e.connected && !e.notRunning).length
  const healthy = connected.filter((r) => r.level === 'good').length

  // gate M: MCP server on/off (agent tools)
  const onToggleMcp = async (name: string, want: boolean) => {
    setBusy(name)
    const res = await setServerEnabled(name, want)
    setBusy(null)
    if (res.ok) notify('ok', `${displayName(name)} ${want ? 'enabled' : 'disabled'}.`)
    else notify('err', `Could not start ${displayName(name)}${res.error ? `: ${res.error}` : ''}.`)
  }

  // a server waiting on setup: the fetch restarts it once its rules are on disk
  const onFetchRules = async (name: string) => {
    setBusy(name)
    try {
      const res = await detectionRulesApi.updateAll()
      const results = ((res.data as { results?: { success?: boolean }[] })?.results ?? [])
      const ok = results.filter((r) => r.success).length
      notify(ok && ok === results.length ? 'ok' : 'err', `Fetched ${ok} of ${results.length} detection rule sources.`)
    } catch (e) {
      notify('err', `Could not fetch detection rules: ${(e as { message?: string })?.message || 'request failed'}.`)
    } finally {
      setBusy(null)
      reloadMcp()
    }
  }

  const onRemove = async () => {
    const name = removing
    if (!name) return
    setBusy(name)
    try {
      await configApi.deleteIntegration(name)
      notify('ok', `${displayName(name)} removed: its settings, stored credentials, on/off state and tool classes were deleted, and its server stopped.`)
    } catch (e) {
      notify('err', `Could not remove ${displayName(name)}: ${extractApiError(e, 'request failed')}.`)
    } finally {
      setBusy(null)
      setRemoving(null)
      reload()
    }
  }

  // master: one switch over both gates. An alert source's integration gate is its
  // collection; switching on never enables collection for a source with no saved settings.
  const onToggleMaster = async (name: string, id: string, want: boolean, collects = false, saved = true) => {
    setBusy(name)
    const res = await setServerEnabled(name, want)
    try {
      if (!want || saved) await setIntegrationEnabled(id, want)
      reloadExtensions()
      health.reload()
    } catch {
      notify('err', `Could not ${want ? 'enable' : 'disable'} ${collects ? `alert collection from ${displayName(name)}` : `the ${id} panel`}.`)
    }
    setBusy(null)
    if (res.ok) notify('ok', `${displayName(name)} ${want ? 'enabled' : 'disabled'}.`)
    else notify('err', `Could not start ${displayName(name)}${res.error ? `: ${res.error}` : ''}.`)
  }

  // the row the drawer belongs to: a connected one first (Splunk has two servers)
  const wizardRow = wizardFor
    ? (rows.find((r) => r.integration?.id === wizardFor.id && r.connected) ?? rows.find((r) => r.integration?.id === wizardFor.id))
    : undefined
  const ready = phase === 'ready'
  const tabs: [IntegrationsTab, string, number | null][] = [
    ['connected', 'Connected', ready ? connected.length : null],
    ['add', 'Add integration', ready ? available : null],
    ['custom', 'Custom', custom?.length ?? null],
    ['surface', 'Vigil MCP server', null],
  ]
  const first = attention[0]
  const tiles: [string, number, string][] = [
    ['Connected', connected.length, 'var(--tx0)'],
    ['Healthy', healthy, 'var(--good)'],
    ['Need attention', attention.length, attention.length ? 'var(--fair)' : 'var(--tx0)'],
    ['Available to add', available, 'var(--tx0)'],
  ]

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
    <div className="settings-content-inner flex flex-col gap-4">
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
                <button className="btn primary int-act" onClick={() => openWizard(first.integration!)}>Fix</button>
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

      <AuditFeedCard notify={notify} />

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
        <div className="flex justify-end gap-2">
          <button className="btn ghost" onClick={reload} title="Re-reads the status shown here. Changes nothing on the server.">
            <Icon name="refresh" /> Refresh
          </button>
          <button
            className="btn ghost"
            onClick={onReloadServers}
            disabled={reloadingServers}
            aria-busy={reloadingServers}
            title="Re-reads tool server settings and applies them without restarting Vigil: new servers appear, changed ones reconnect, and removed or disabled ones stop. Unchanged servers keep their session."
          >
            <Icon name="gear" /> {reloadingServers ? 'Reloading…' : 'Reload servers'}
          </button>
        </div>
      )}
      {tab === 'connected' && ready && connected.length > 0 && (
        <div className="int-table-wrap">
        <table className="int-table">
          <thead>
            <tr>
              <th>
                Integration
                <span className="col-low-note">Last verified and what agents may do are hidden while Ask Vigil is open</span>
              </th>
              <th>Health</th>
                <th className="col-low">
                Last verified
                <InfoTip
                  label="About Last verified"
                  align="start"
                  source="Integration Health for this integration, from GET /api/integration-health."
                  calculation="Healthy shows when it was last confirmed working. Any other state is the server's own wording."
                  limit="No record shows —. A failed read shows Could not read, never Good."
                />
              </th>
              <th className="col-low">What agents may do with it</th>
              <th>Note</th>
              <th>On</th>
              <th className="col-act"><span className="sr-only">Action</span></th>
            </tr>
          </thead>
          <tbody>
            {connected.map((r) => {
              const phrase = rowPhrase(r)
              const facts = r.judgement?.state === 'switched_off' ? '' : factsPhrase(r.judgement?.record?.facts)
              return (
              <tr key={r.name}>
                <td>
                  <div className="int-ident">
                    <IntegrationMark logo={logoForServer(base(r.name))} category={categoryOf(base(r.name))} />
                    <div>
                      <span className="int-name">
                        {displayName(r.name)}
                      </span>
                      <span className="int-sub">{categoryOf(base(r.name))}</span>
                      {facts && <span className="int-sub int-facts"><Clamp text={facts} /></span>}
                    </div>
                  </div>
                </td>
                <td>
                  {r.level ? <LevelBadge level={r.level} variant="pill" /> : <span className="int-neutral"><Clamp text={phrase || r.word || '—'} /></span>}
                </td>
                <td className="col-low">
                  {phrase ? (
                    r.judgement?.since && r.judgement.state === 'healthy' ? (
                      <time dateTime={r.judgement.since} title={r.judgement.since}><Clamp text={phrase} /></time>
                    ) : (
                      <Clamp text={phrase} />
                    )
                  ) : (
                    <span>—</span>
                  )}
                </td>
                <td className="col-low"><Clamp text={SERVER_DESCRIPTIONS.get(base(r.name)) || r.integration?.description || (r.judgement?.kind === 'mcp_source' ? 'Declared source: see Declared sources below.' : 'Custom MCP integration.')} /></td>
                <td className={r.level === 'poor' ? 'int-note poor' : 'int-note'}><Clamp text={r.note} /></td>
                <td><RowToggle row={r} busy={busy} onMcp={onToggleMcp} onMaster={onToggleMaster} /></td>
                <td className="col-act">
                  {r.setupAction === FETCH_RULES ? (
                    <button className="btn primary int-act" disabled={busy === r.name} onClick={() => void onFetchRules(r.name)}>
                      {busy === r.name ? 'Fetching…' : 'Fetch rules'}
                    </button>
                  ) : (
                    <div className="int-acts">
                      {canConfigure(r) && (
                        <button className={`btn int-act${r.level === 'poor' ? ' primary' : ''}`} onClick={() => openWizard(r.integration!)}>
                          {r.level === 'poor' ? 'Fix' : 'Configure'}
                        </button>
                      )}
                      {/* only an instance is removed; an integration's own row never is */}
                      {instances[r.name] && (
                        <button className="btn ghost int-act" disabled={busy === r.name} onClick={() => setRemoving(r.name)}>
                          Remove
                        </button>
                      )}
                    </div>
                  )}
                </td>
              </tr>
              )
            })}
          </tbody>
        </table>
        </div>
      )}

      {tab === 'connected' && ready && (
        <DeclaredSourcesCard
          notify={notify}
          health={health.sources ?? []}
          healthFailed={health.failed}
          saveCredentials={saveIntegration}
          onChanged={reload}
        />
      )}

      {tab === 'add' && ready && (
        <AddIntegrationTab
          entries={entries}
          busy={busy}
          onConnect={(i) => openWizard(i)}
          onTurnOn={(name) => onToggleMcp(name, true)}
          onRefresh={reload}
        />
      )}

      {/* mounted on every tab so a draft survives a visit elsewhere */}
      <div hidden={tab !== 'custom'}>
        <CustomIntegrationBuilder
          notify={notify}
          onWrote={reloadCustom}
          onSaved={reload}
        />
      </div>

      {tab === 'surface' && <McpSurfacePanel notify={notify} />}

      <ConfirmDialog
        open={!!removing}
        title={`Remove ${removing ? displayName(removing) : ''}?`}
        confirmLabel="Remove"
        busy={!!removing && busy === removing}
        onClose={() => setRemoving(null)}
        onConfirm={() => void onRemove()}
        body={
          <>
            Deletes its settings, stored credentials, on/off state and <span className="mono">{removing}_</span> tool classes, and stops its server.
            The integration it is an instance of is not changed. This cannot be undone.
          </>
        }
      />

      {wizardFor && (
        <IntegrationWizard
          key={`${wizardFor.id}:${addingInstance}`}
          integration={wizardFor}
          newInstance={addingInstance}
          savedIds={Object.keys(intCfg.integrations)}
          existingConfig={addingInstance ? {} : intCfg.integrations[wizardFor.id] || {}}
          secretsSet={addingInstance ? {} : intCfg.secrets_set[wizardFor.id] || {}}
          lastTest={addingInstance ? undefined : intCfg.last_test[wizardFor.id]}
          health={addingInstance ? undefined : wizardRow?.level}
          category={categoryOf(addingInstance ? serverForIntegration(wizardFor.id) : base(wizardRow?.name ?? ''))}
          // a saved integration's own drawer offers another instance, or says why it can't
          instancesAllowed={!addingInstance && !instances[wizardFor.id] && intCfg.integrations[wizardFor.id] ? instancesAllowed[wizardFor.id] : undefined}
          onAddAnother={() => openWizard(wizardFor, true)}
          onTested={() => {
            reloadInt()
            health.reload()
          }}
          onClose={() => setWizardFor(null)}
          onSave={async (id, cfg) => {
            const created = (await saveIntegration(id, cfg))?.[id]
            const title = addingInstance ? serverTitle(id, { [id]: wizardFor.id }) : wizardFor.name
            health.reload()
            // the save that created an instance (a later save from the same drawer only edits it)
            if (created) {
              // load its server now rather than on the next sources pass
              await mcpApi.reloadServers().catch(() => undefined)
              reloadMcp()
              notify('ok', `${title} added.${copiedPhrase(created)} Turn it on with its switch to give agents its tools.`)
              return
            }
            // refresh the registry so a URL edit re-reads; saving alone doesn't enable
            const isExtension = wizardFor.fields?.some((f) => f.name === 'connectorUrl')
            if (isExtension) reloadExtensions()
            // no server row, so there is no toggle to point at
            const notRunning = savedCustomWithoutServer(wizardFor, rows)
            notify(
              'ok',
              isExtension
                ? `${title} connected. Turn it on with the toggle to enable it.`
                : notRunning
                  ? `${title} configured.`
                  : `${title} configured. Enable it with the toggle if it isn’t already.`,
            )
          }}
        />
      )}
    </div>
    </>
  )
}

/** Prose in a table cell: wraps, stops at two lines, full text on hover and while the row holds keyboard focus. */
function Clamp({ text }: { text: string }) {
  return text ? <span className="int-clamp" title={text}>{text}</span> : null
}

/** On/off. A connector-backed integration is one master switch over two gates:
 * the extension (gate D) and the MCP server / agent tools (gate M). A built-in alert
 * source's switch is the same over its collection and its agent tools. */
function RowToggle({ row, busy, onMcp, onMaster }: {
  row: ServerRow
  busy: string | null
  onMcp: (name: string, want: boolean) => void
  onMaster: (name: string, id: string, want: boolean, collects?: boolean, saved?: boolean) => void
}) {
  const { name, integration, isEnabled, masterOn, masterMixed, extConfigured, collects, saved } = row
  if (collects && integration) {
    return (
      <Toggle
        checked={masterOn}
        mixed={masterMixed}
        label={`Toggle ${name}`}
        title={masterMixed ? 'Partly on — switch off to stop alert collection and agent tools' : undefined}
        disabled={busy === name}
        onChange={(v) => onMaster(name, integration.id, v, true, saved)}
      />
    )
  }
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
