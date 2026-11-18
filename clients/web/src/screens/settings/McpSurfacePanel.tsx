import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { EmptyState, Toggle } from '../../shared/ui'
import { mcpApi } from '../../services/api'
import type { SectionProps } from './types'

type Surface = {
  enabled: boolean
  path: string
  // Whether any token that is not revoked or expired exists on the install.
  has_token: boolean
  oauth: OAuth
}

// OAuth on the surface. Every URL is computed from VIGIL_FRONTEND_URL.
type OAuth = {
  enabled: boolean // effective: switched on and nothing refuses it
  requested: boolean // the switch itself
  refusal: string | null
  issuer: string
  resource: string
  authorization_server_metadata: string
  protected_resource_metadata: string | null
}

/**
 * Vigil's own MCP server: whether anything outside Vigil can reach its tools,
 * and, through API tokens, who can open it.
 *
 * Off is the state an install ships in. Turning it on opens another front door
 * into the SOC, so the panel says what that means rather than presenting a
 * switch with a label.
 */
export default function McpSurfacePanel({ notify }: SectionProps) {
  const [surface, setSurface] = useState<Surface | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    mcpApi
      .getSurface()
      .then((r) => {
        setSurface(r.data)
        setFailed(false)
      })
      .catch(() => {
        setFailed(true)
        notify('err', 'Could not read the MCP surface settings')
      })
  }, [notify])

  useEffect(load, [load])

  const toggle = async (enabled: boolean) => {
    setBusy(true)
    try {
      await mcpApi.setSurfaceEnabled(enabled)
      notify(
        enabled ? 'info' : 'ok',
        enabled
          ? 'MCP surface is on. Vigil’s tools are reachable from the network.'
          : 'MCP surface is off.',
      )
      load()
    } catch {
      notify('err', 'Could not change the MCP surface')
    } finally {
      setBusy(false)
    }
  }

  const toggleOAuth = async (enabled: boolean) => {
    setBusy(true)
    try {
      await mcpApi.setSurfaceOAuthEnabled(enabled)
      notify('ok', enabled ? 'OAuth for MCP clients is on.' : 'OAuth for MCP clients is off.')
      load()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      notify('err', typeof detail === 'string' ? detail : 'Could not change OAuth for MCP clients')
    } finally {
      setBusy(false)
    }
  }

  if (!surface) {
    return failed ? (
      <EmptyState
        error
        icon="alert"
        title="Couldn’t load the MCP server settings"
        body="Vigil’s MCP surface could not be read, so its state is not shown."
        primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
      />
    ) : (
      <EmptyState loading icon="link" title="Loading the MCP server…" />
    )
  }

  const noToken = surface.enabled && !surface.has_token

  return (
    <div className="mcp-card">
      <div className="mcp-head">
        <h3>Vigil’s own MCP server</h3>
        <p>
          Vigil’s own tools — findings, cases, the approval queue — served at{' '}
          <code>{surface.path}</code> so callers that are not Vigil can use them. Vigil’s own agent
          reaches the same tools either way, so leaving this off costs nothing internally.
        </p>
      </div>

      <div className="mcp-onoff">
        <span className="mcp-onoff-text">
          <b>{surface.enabled ? 'Reachable from the network' : 'Not served'}</b>
          <span>When off, Vigil’s MCP server is not served at all</span>
        </span>
        <Toggle checked={surface.enabled} label="Vigil MCP server" disabled={busy} onChange={toggle} />
      </div>

      <div className="mcp-onoff">
        <span className="mcp-onoff-text">
          <b>{surface.oauth.enabled ? 'MCP clients sign in with OAuth' : 'OAuth off'}</b>
          <span>Lets MCP clients sign in through Vigil instead of pasting an API token</span>
        </span>
        <Toggle
          checked={surface.oauth.enabled}
          label="OAuth for MCP clients"
          disabled={busy}
          onChange={toggleOAuth}
        />
      </div>

      <dl className="mcp-urls">
        <dt>Issuer</dt>
        <dd><code>{surface.oauth.issuer}</code></dd>
        <dt>Resource</dt>
        <dd><code>{surface.oauth.resource}</code></dd>
        <dt>Server metadata</dt>
        <dd><code>{surface.oauth.authorization_server_metadata}</code></dd>
        {surface.oauth.protected_resource_metadata && (
          <>
            <dt>Resource metadata</dt>
            <dd><code>{surface.oauth.protected_resource_metadata}</code></dd>
          </>
        )}
      </dl>

      {surface.oauth.requested && surface.oauth.refusal && (
        <div className="mcp-callout" role="status">
          <span>
            <Icon name="alert" size={16} /> OAuth is switched on but not in effect.{' '}
            {surface.oauth.refusal}
          </span>
        </div>
      )}

      {noToken && (
        <div className="mcp-callout" role="status">
          <span>
            <Icon name="alert" size={16} /> The surface is on, but no API token exists, so every
            request is refused. Mint one under <Link className="underline" to="/settings?section=users">Settings › Users</Link>.
          </span>
        </div>
      )}
    </div>
  )
}
