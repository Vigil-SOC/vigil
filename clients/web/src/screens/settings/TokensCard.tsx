import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { HoldButton } from '../../shared/HoldButton'
import { ConfirmDialog, EmptyState, Select, SettingsCard, TextInput } from '../../shared/ui'
import {
  tokensApi,
  type ApiToken,
  type ApiTokenHolder,
  type ApiTokenStatus,
} from '../../services/api'
import type { SectionProps } from './types'

// The server's rule: a person's token defaults to 90 days and lasts a year at most.
const LIFETIMES = [
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days (default)' },
  { value: '180', label: '180 days' },
  { value: '365', label: '1 year' },
]

const STATUSES = [
  { value: '', label: 'Any status' },
  { value: 'active', label: 'Active' },
  { value: 'rotated', label: 'Rotated' },
  { value: 'expired', label: 'Expired' },
  { value: 'revoked', label: 'Revoked' },
]

const STATUS_CLASS: Record<ApiTokenStatus, string> = {
  active: 'closed',
  rotated: 'open',
  expired: 'dropped',
  revoked: 'dropped',
}

const day = (d: Date) => d.toDateString()

/** The board's short form: “14:02 today”, “12 Sep”, with the year once it is not this one. */
function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value + 'Z')
  if (Number.isNaN(d.getTime())) return '—'
  const now = new Date()
  if (day(d) === day(now)) return `${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} today`
  return d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}

const full = (value: string | null) => (value ? new Date(value + 'Z').toLocaleString() : undefined)

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

/** One token's shared cells. The dates are UTC with no zone mark, as the server sends them. */
function Dates({ t }: { t: ApiToken }) {
  return (
    <>
      <td title={full(t.created_at)}>{when(t.created_at)}</td>
      <td title={full(t.last_used_at)}>{t.last_used_at ? when(t.last_used_at) : 'never used'}</td>
      <td title={full(t.expires_at)}>{when(t.expires_at)}</td>
      <td>
        <span className={`status ${STATUS_CLASS[t.status]}`}>{t.status}</span>
      </td>
    </>
  )
}

/** The one place a new token is shown. The server keeps only a hash of it. */
function Reveal({ token, onDone }: { token: string; onDone: () => void }) {
  return (
    <div className="mcp-callout" role="alert">
      <strong>Copy this now. It is not stored and cannot be shown again.</strong>
      <pre className="mcp-token">{token}</pre>
      <button className="btn" onClick={onDone}>
        I have copied it
      </button>
    </div>
  )
}

/**
 * One holder's tokens: mint, the list, rotate and revoke. Your own, or with
 * `holder` a service account's (users.write), whose tokens always last a year.
 */
export function HeldTokens({
  notify,
  holder,
  onChange,
  refreshKey = 0,
}: Pick<SectionProps, 'notify'> & {
  holder?: { user_id: string; username: string }
  onChange?: () => void
  /** reloads the list when it changes, keeping a token being shown */
  refreshKey?: number
}) {
  const [own, setOwn] = useState<ApiToken[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('90')
  const [revealed, setRevealed] = useState<string | null>(null)
  const [rotating, setRotating] = useState<ApiToken | null>(null)
  const [compromise, setCompromise] = useState(false)
  const holderId = holder?.user_id

  const load = useCallback(() => {
    ;(holderId ? tokensApi.listAll({ holder: holderId }) : tokensApi.listOwn())
      .then((r) => {
        setOwn(r.data.tokens)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [holderId])

  useEffect(load, [load, refreshKey])

  const act = async (run: () => Promise<void>, fallback: string) => {
    setBusy(true)
    try {
      await run()
      load()
      onChange?.()
    } catch (e) {
      notify('err', errText(e, fallback))
    } finally {
      setBusy(false)
    }
  }

  const mint = () =>
    act(async () => {
      const r = await tokensApi.mint(label.trim(), holderId ? undefined : Number(days), holderId)
      setRevealed(r.data.token)
      setLabel('')
    }, 'Could not mint a token')

  const rotate = () =>
    rotating &&
    act(async () => {
      const r = await tokensApi.rotate(rotating.credential_id, compromise)
      setRevealed(r.data.token)
      setRotating(null)
      setCompromise(false)
    }, 'Could not rotate that token')

  const revoke = (t: ApiToken) =>
    act(async () => {
      await tokensApi.revoke(t.credential_id)
      notify('ok', 'Token revoked')
    }, 'Could not revoke that token')

  const whose = holder ? holder.username + '’s' : 'your'

  return (
    <div className="flex flex-col gap-3.5">
      {revealed && <Reveal token={revealed} onDone={() => setRevealed(null)} />}

      <div className="flex gap-2 flex-wrap items-center">
        <div className="flex-1" style={{ minWidth: 220 }}>
          <TextInput
            value={label}
            maxLength={200}
            aria-label={holder ? `Token label for ${holder.username}` : 'Token label'}
            placeholder={holder ? 'Where it is used — “the SIEM forwarder”' : 'What holds it — “the platform”, “my laptop”'}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
        {holder ? (
          <span className="text-xs text-tx-3 whitespace-nowrap">Lasts 1 year</span>
        ) : (
          <Select value={days} options={LIFETIMES} onSelect={setDays} name="Lasts" />
        )}
        <button className="btn primary" disabled={busy || !label.trim()} onClick={mint}>
          <Icon name="plus" /> Mint
        </button>
      </div>

      {own === null ? (
        failed ? (
          <EmptyState
            compact
            error
            icon="alert"
            title={`Couldn’t load ${whose} tokens`}
            primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
          />
        ) : (
          <EmptyState compact loading icon="link" title={`Loading ${whose} tokens…`} />
        )
      ) : own.length === 0 ? (
        <EmptyState
          compact
          icon="link"
          title="No tokens yet"
          body={holder ? `Mint one above for the integration that signs in as ${holder.username}.` : 'Mint one above to let a program call Vigil as you.'}
        />
      ) : (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Token</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Expires</th>
                <th>Status</th>
                <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {own.map((t) => (
                <tr key={t.credential_id}>
                  <td style={{ whiteSpace: 'normal' }}>
                    <span className="line-clamp-2 break-words" title={t.label}>{t.label}</span>
                  </td>
                  <Dates t={t} />
                  <td className="col-act" style={{ textAlign: 'right' }}>
                    {t.status === 'active' && (
                      <div className="inline-flex gap-1.5 items-center">
                        <button
                          className="btn ghost"
                          disabled={busy}
                          onClick={() => {
                            setCompromise(false)
                            setRotating(t)
                          }}
                        >
                          <Icon name="refresh" size={14} /> Rotate
                        </button>
                        <HoldButton label="Hold to revoke" name={`Revoke ${t.label}`} disabled={busy} onConfirm={() => revoke(t)} />
                      </div>
                    )}
                    {t.status === 'rotated' && (
                      <HoldButton label="Hold to revoke" name={`Revoke ${t.label}`} disabled={busy} onConfirm={() => revoke(t)} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={!!rotating}
        title="Rotate this token?"
        danger={false}
        body={
          <div className="flex flex-col gap-3.5">
            <span>
              A new token with the same label is minted and shown once. “{rotating?.label}” keeps working for 24
              hours so the program using it can be moved over, then stops.
            </span>
            <label className="flex items-start gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={compromise}
                disabled={busy}
                onChange={(e) => setCompromise(e.target.checked)}
              />
              <span>
                <b>Suspected compromise.</b> Stop the old token working now instead of in 24 hours.
              </span>
            </label>
          </div>
        }
        confirmLabel="Rotate"
        busy={busy}
        onConfirm={rotate}
        onClose={() => !busy && setRotating(null)}
      />
    </div>
  )
}

/**
 * Your own API tokens, and for a `users.write` holder everyone's.
 *
 * A token calls the API and Vigil's MCP server as its holder, as a program and
 * never as a person: it cannot change an account or mint another token.
 */
export default function TokensCard({
  notify,
  canManage,
  changes = 0,
}: SectionProps & { canManage: boolean; changes?: number }) {
  const [ownKey, setOwnKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [all, setAll] = useState<ApiToken[] | null>(null)
  const [holders, setHolders] = useState<ApiTokenHolder[]>([])
  const [holder, setHolder] = useState('')
  const [status, setStatus] = useState('')
  const [allFailed, setAllFailed] = useState(false)

  const loadAll = useCallback(() => {
    if (!canManage) return
    tokensApi
      .listAll({ holder: holder || undefined, status: (status || undefined) as ApiTokenStatus | undefined })
      .then((r) => {
        setAll(r.data.tokens)
        setHolders(r.data.holders)
        setAllFailed(false)
      })
      .catch(() => setAllFailed(true))
  }, [canManage, holder, status])

  useEffect(loadAll, [loadAll, changes])
  const reload = () => {
    setOwnKey((k) => k + 1)
    loadAll()
  }

  const revoke = async (t: ApiToken) => {
    setBusy(true)
    try {
      await tokensApi.revoke(t.credential_id)
      notify('ok', 'Token revoked')
      reload()
    } catch (e) {
      notify('err', errText(e, 'Could not revoke that token'))
    } finally {
      setBusy(false)
    }
  }

  const holderOptions = [
    { value: '', label: 'Any holder' },
    ...holders.map((h) => ({
      value: h.user_id,
      label: h.service_account ? `${h.username} (service account)` : h.username,
    })),
  ]

  return (
    <>
      <SettingsCard
        wide
        id="tokens"
        title="API tokens"
        desc="A token lets a program call Vigil’s API and MCP server as you. What it may do is what your account may do, and it is recorded against your name. A token can never change an account or mint another token."
        actions={
          <button className="btn ghost" onClick={reload}>
            <Icon name="refresh" /> Refresh
          </button>
        }
      >
        <HeldTokens refreshKey={ownKey} notify={notify} onChange={loadAll} />
      </SettingsCard>

      {canManage && (
        <SettingsCard
          wide
          id="all-tokens"
          title="All API tokens"
          desc="Every token on this install, whoever holds it. You can revoke any of them, including one held by someone with more access than you; the holder is told. Only a token’s holder can rotate it; a service account’s are rotated in Service accounts."
          actions={
            <>
              <Select value={holder} options={holderOptions} onSelect={setHolder} name="Holder" searchable />
              <Select value={status} options={STATUSES} onSelect={setStatus} name="Status" />
            </>
          }
        >
          {all === null ? (
            allFailed ? (
              <EmptyState
                compact
                error
                icon="alert"
                title="Couldn’t load the tokens"
                primary={{ label: 'Retry', onClick: loadAll, icon: 'refresh' }}
              />
            ) : (
              <EmptyState compact loading icon="link" title="Loading tokens…" />
            )
          ) : all.length === 0 ? (
            <EmptyState compact icon="link" title="No tokens match" body="Nothing is held under this filter." />
          ) : (
            <div className="table-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Holder</th>
                    <th>Token</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th>Expires</th>
                    <th>Status</th>
                    <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {all.map((t) => (
                    <tr key={t.credential_id}>
                      <td>
                        <div className="flex flex-col items-start gap-1">
                          <span>{t.holder.username}</span>
                          {t.holder.service_account && <span className="text-xs text-tx-3">Service account</span>}
                        </div>
                      </td>
                      <td style={{ whiteSpace: 'normal' }}>
                        <span className="line-clamp-2 break-words" title={t.label}>{t.label}</span>
                      </td>
                      <Dates t={t} />
                      <td className="col-act" style={{ textAlign: 'right' }}>
                        {(t.status === 'active' || t.status === 'rotated') && (
                          <HoldButton
                            label="Hold to revoke"
                            name={`Revoke ${t.label} held by ${t.holder.username}`}
                            disabled={busy}
                            onConfirm={() => revoke(t)}
                          />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SettingsCard>
      )}
    </>
  )
}
