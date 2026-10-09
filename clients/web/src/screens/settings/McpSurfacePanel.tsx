import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { HoldButton } from '../../shared/HoldButton'
import { EmptyState, TextInput, Toggle } from '../../shared/ui'
import { mcpApi } from '../../services/api'
import type { SectionProps } from './types'

type Credential = {
  credential_id: string
  label: string
  created_at: string | null
  last_used_at: string | null
  expires_at: string | null
}

type Surface = {
  enabled: boolean
  path: string
  credentials: Credential[]
}

const never = '—'

const day = (d: Date) => d.toDateString()

/** The board's short form: “14:02 today”, “12 Sep”, with the year once it is not this one. */
function when(value: string | null): string {
  if (!value) return never
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return never
  const now = new Date()
  if (day(d) === day(now)) return `${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} today`
  return d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}

const full = (value: string | null) => (value ? new Date(value).toLocaleString() : undefined)

/**
 * Vigil's own MCP server: whether anything outside Vigil can reach its tools,
 * and which credentials open it.
 *
 * Off is the state an install ships in. Turning it on opens another front door
 * into the SOC, so the panel says what that means rather than presenting a
 * switch with a label.
 */
export default function McpSurfacePanel({ notify }: SectionProps) {
  const [surface, setSurface] = useState<Surface | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [label, setLabel] = useState('')
  // Held only until the operator navigates away: the server does not keep it,
  // so this is the one chance to copy it.
  const [minted, setMinted] = useState<string | null>(null)

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

  const mint = async () => {
    if (!label.trim()) return
    setBusy(true)
    try {
      const r = await mcpApi.mintCredential(label.trim())
      setMinted(r.data.token)
      setLabel('')
      load()
    } catch {
      notify('err', 'Could not mint a credential')
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (credentialId: string) => {
    setBusy(true)
    try {
      await mcpApi.revokeCredential(credentialId)
      notify('ok', 'Credential revoked')
      load()
    } catch {
      notify('err', 'Could not revoke that credential')
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
        body="Vigil’s MCP surface could not be read, so its state and credentials are not shown."
        primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
      />
    ) : (
      <EmptyState loading icon="link" title="Loading the MCP server…" />
    )
  }

  const noCredentials = surface.enabled && surface.credentials.length === 0

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

      {noCredentials && (
        <div className="mcp-callout" role="status">
          <span>
            <Icon name="alert" size={16} /> The surface is on, but no credential exists, so every
            request is refused. Mint one below.
          </span>
        </div>
      )}

      <div className="mcp-head">
        <h4>Credentials</h4>
        <p>
          A credential carries your standing: what its holder may do is what your account may do,
          and what it does is recorded against your name. It is still a program acting, not you — a
          case it closes is closed by an agent with your credential, never by you at a keyboard.
          Mint one for each caller, so it can be revoked on its own.
        </p>
      </div>

      {minted && (
        <div className="mcp-callout" role="alert">
          <strong>Copy this now. It is not stored and cannot be shown again.</strong>
          <pre className="mcp-token">{minted}</pre>
          <button className="btn" onClick={() => setMinted(null)}>
            I have copied it
          </button>
        </div>
      )}

      <div className="flex gap-2">
        <TextInput
          value={label}
          placeholder="What holds it — “the platform”, “my laptop”"
          onChange={(e) => setLabel(e.target.value)}
        />
        <button className="btn" disabled={busy || !label.trim()} onClick={mint}>
          Mint
        </button>
      </div>

      {surface.credentials.length === 0 ? (
        <EmptyState
          compact
          icon="link"
          title="No credentials yet"
          body="Mint one above to let a caller outside Vigil use its tools."
        />
      ) : (
        <table className="int-table">
          <colgroup>
            <col />
            <col style={{ width: 150 }} />
            <col style={{ width: 150 }} />
            <col style={{ width: 150 }} />
            <col style={{ width: 150 }} />
          </colgroup>
          <thead>
            <tr>
              <th>Credential</th>
              <th>Created</th>
              <th>Last used</th>
              <th>Expires</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {surface.credentials.map((c) => (
              <tr key={c.credential_id}>
                <td><span className="int-name">{c.label}</span></td>
                <td title={full(c.created_at)}>{when(c.created_at)}</td>
                <td title={full(c.last_used_at)}>{c.last_used_at ? when(c.last_used_at) : 'never used'}</td>
                <td title={full(c.expires_at)}>{c.expires_at ? when(c.expires_at) : 'does not expire'}</td>
                <td>
                  <HoldButton label="Hold to revoke" disabled={busy} onConfirm={() => revoke(c.credential_id)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
