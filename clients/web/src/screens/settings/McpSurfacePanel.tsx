import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { TextInput } from '../../shared/ui'
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

function when(value: string | null): string {
  if (!value) return never
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? never : parsed.toLocaleString()
}

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
  const [busy, setBusy] = useState(false)
  const [label, setLabel] = useState('')
  // Held only until the operator navigates away: the server does not keep it,
  // so this is the one chance to copy it.
  const [minted, setMinted] = useState<string | null>(null)

  const load = useCallback(() => {
    mcpApi
      .getSurface()
      .then((r) => setSurface(r.data))
      .catch(() => notify('err', 'Could not read the MCP surface settings'))
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

  if (!surface) return null

  const noCredentials = surface.enabled && surface.credentials.length === 0

  return (
    <div className="panel">
      <h3>Vigil’s MCP server</h3>
      <p className="muted">
        Vigil’s own tools — findings, cases, the approval queue — served at{' '}
        <code>{surface.path}</code> so callers that are not Vigil can use them. Vigil’s own agent
        reaches the same tools either way, so leaving this off costs nothing internally.
      </p>

      <label className="row" style={{ gap: 8, alignItems: 'center' }}>
        <input
          type="checkbox"
          checked={surface.enabled}
          disabled={busy}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span>{surface.enabled ? 'Reachable from the network' : 'Not served'}</span>
      </label>

      {noCredentials && (
        <div className="callout warning" role="status">
          <Icon name="alert" /> The surface is on, but no credential exists, so every
          request is refused. Mint one below.
        </div>
      )}

      <h4>Credentials</h4>
      <p className="muted">
        A credential acts as you: what its holder may do is what your account may do. Mint one for
        each caller, so it can be revoked on its own.
      </p>

      {minted && (
        <div className="callout warning" role="alert">
          <strong>Copy this now. It is not stored and cannot be shown again.</strong>
          <pre className="token">{minted}</pre>
          <button className="btn" onClick={() => setMinted(null)}>
            I have copied it
          </button>
        </div>
      )}

      <div className="row" style={{ gap: 8 }}>
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
        <p className="muted">None yet.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Created</th>
              <th>Last used</th>
              <th>Expires</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {surface.credentials.map((c) => (
              <tr key={c.credential_id}>
                <td>{c.label}</td>
                <td>{when(c.created_at)}</td>
                <td>{c.last_used_at ? when(c.last_used_at) : 'never used'}</td>
                <td>{c.expires_at ? when(c.expires_at) : 'does not expire'}</td>
                <td>
                  <button
                    className="btn danger"
                    disabled={busy}
                    onClick={() => revoke(c.credential_id)}
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
