import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { HoldButton } from '../../shared/HoldButton'
import { ConfirmDialog, EmptyState, Select, SettingsCard, TextInput } from '../../shared/ui'
import {
  tokensApi,
  type ApiToken,
  type ApiTokenLimits,
  type ApiTokenHolder,
  type ApiTokenStatus,
} from '../../services/api'
import type { SectionProps } from './types'

// What a person may pick: these, up to the maximum the session policy sets, and the maximum itself.
const LIFETIME_STEPS = [30, 90, 180, 365]
const lifetimeLabel = (d: number) => (d === 365 ? '1 year' : `${d} day${d === 1 ? '' : 's'}`)
function lifetimeOptions(limits: ApiTokenLimits | null) {
  if (limits?.max_days == null) return []
  const days = [...new Set([...LIFETIME_STEPS.filter((d) => d <= limits.max_days!), limits.max_days])].sort((a, b) => a - b)
  return days.map((d) => ({ value: String(d), label: d === limits.default_days ? `${lifetimeLabel(d)} (default)` : lifetimeLabel(d) }))
}

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

/** The token's label; an OAuth grant is marked as a connected app, with its client's name (#4789). */
function Label({ t }: { t: ApiToken }) {
  const name = t.connected_app ? t.client_name || t.label : t.label
  return (
    <td style={{ whiteSpace: 'normal' }}>
      <div className="flex flex-col items-start gap-1">
        <span className="line-clamp-2 break-words" title={name}>{name}</span>
        {t.connected_app && <span className="text-xs text-tx-3">Connected app</span>}
      </div>
    </td>
  )
}

/** The one place a new token is shown. The server keeps only a hash of it. */
function Reveal({ token, rule, onDone }: { token: string; rule: string | null; onDone: () => void }) {
  return (
    <div className="mcp-callout" role="alert">
      <strong>Copy this now. It is not stored and cannot be shown again.</strong>
      <pre className="mcp-token">{token}</pre>
      {rule && <span className="text-xs text-tx-3">{rule}</span>}
      <button className="btn" onClick={onDone}>
        I have copied it
      </button>
    </div>
  )
}

/**
 * Your own API tokens, and for a `users.write` holder everyone's. With `account`,
 * one service account's instead: the holder mints and rotates them for it.
 *
 * A token calls the API and Vigil's MCP server as its holder, as a program and
 * never as a person: it cannot change an account or mint another token.
 */
export default function TokensCard({
  notify,
  canManage,
  account,
}: SectionProps & { canManage: boolean; account?: { user_id: string; username: string } }) {
  const [own, setOwn] = useState<ApiToken[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('')
  const [limits, setLimits] = useState<ApiTokenLimits | null>(null)
  const [revealed, setRevealed] = useState<{ token: string; rule: string | null } | null>(null)
  const [rotating, setRotating] = useState<ApiToken | null>(null)
  const [compromise, setCompromise] = useState(false)

  const [all, setAll] = useState<ApiToken[] | null>(null)
  const [holders, setHolders] = useState<ApiTokenHolder[]>([])
  const [holder, setHolder] = useState('')
  const [status, setStatus] = useState('')
  const [allFailed, setAllFailed] = useState(false)

  const accountId = account?.user_id
  const loadOwn = useCallback(() => {
    ;(accountId ? tokensApi.listAll({ holder: accountId }) : tokensApi.listOwn())
      .then((r) => {
        setOwn(r.data.tokens)
        setLimits(r.data.limits)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [accountId])

  const loadAll = useCallback(() => {
    if (!canManage || accountId) return
    tokensApi
      .listAll({ holder: holder || undefined, status: (status || undefined) as ApiTokenStatus | undefined })
      .then((r) => {
        setAll(r.data.tokens)
        setHolders(r.data.holders)
        setAllFailed(false)
      })
      .catch(() => setAllFailed(true))
  }, [canManage, accountId, holder, status])

  useEffect(loadOwn, [loadOwn])
  useEffect(loadAll, [loadAll])
  const reload = () => {
    loadOwn()
    loadAll()
  }

  const act = async (run: () => Promise<void>, fallback: string) => {
    setBusy(true)
    try {
      await run()
      reload()
    } catch (e) {
      notify('err', errText(e, fallback))
    } finally {
      setBusy(false)
    }
  }

  const mint = () =>
    act(async () => {
      // A service account's token has no length to choose: the server gives it its maximum.
      const r = await tokensApi.mint(label.trim(), limits?.fixed ? undefined : Number(chosen), accountId)
      setRevealed({ token: r.data.token, rule: r.data.unused_rule })
      setLabel('')
    }, 'Could not mint a token')

  const rotate = () =>
    rotating &&
    act(async () => {
      const r = await tokensApi.rotate(rotating.credential_id, compromise)
      setRevealed({ token: r.data.token, rule: r.data.unused_rule })
      setRotating(null)
      setCompromise(false)
    }, 'Could not rotate that token')

  const revoke = (t: ApiToken) =>
    act(async () => {
      await tokensApi.revoke(t.credential_id)
      notify('ok', t.connected_app ? 'App disconnected' : 'Token revoked')
    }, 'Could not revoke that token')

  const options = lifetimeOptions(limits)
  const chosen = options.some((o) => o.value === days) ? days : String(limits?.default_days ?? '')
  const maxText =
    limits === null
      ? null
      : limits.max_days == null
        ? 'The token policy can’t be read, so no token can be minted until Settings › Sessions is saved again.'
        : limits.fixed
          ? `A token here lasts ${limits.max_days} days, the maximum set for service-account tokens in Settings › Sessions.`
          : `A token lasts at most ${limits.max_days} days, the maximum set in Settings › Sessions.`

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
        id={account ? `tokens-${account.user_id}` : 'tokens'}
        title={account ? `Tokens of ${account.username}` : 'API tokens'}
        desc={
          account
            ? `A token lets the integration call Vigil’s API as this service account, with what its role allows. Each lasts ${limits?.max_days == null ? 'the maximum set for service-account tokens' : `${limits.max_days} days, the maximum set for service-account tokens`}; rotate before it ends. Minting and rotating is a person’s act: a token can never mint another token.`
            : 'A token lets a program call Vigil’s API and MCP server as you. What it may do is what your account may do, and it is recorded against your name. A token can never change an account or mint another token. Apps you approve through OAuth show here as connected apps; revoke one to disconnect it.'
        }
        actions={
          <button className="btn ghost" onClick={reload}>
            <Icon name="refresh" /> Refresh
          </button>
        }
      >
        <div className="flex flex-col gap-3.5">
          {revealed && <Reveal token={revealed.token} rule={revealed.rule} onDone={() => setRevealed(null)} />}

          <div className="flex gap-2 flex-wrap items-center">
            <div className="flex-1" style={{ minWidth: 220 }}>
              <TextInput
                value={label}
                maxLength={200}
                aria-label="Token label"
                placeholder={
                  account ? 'What holds it — “the audit forwarder”' : 'What holds it — “the platform”, “my laptop”'
                }
                onChange={(e) => setLabel(e.target.value)}
              />
            </div>
            {!limits?.fixed && options.length > 0 && <Select value={chosen} options={options} onSelect={setDays} name="Lasts" />}
            <button className="btn primary" disabled={busy || !label.trim() || limits?.max_days == null} onClick={mint}>
              <Icon name="plus" /> Mint
            </button>
          </div>
          {maxText && <span className="text-xs text-tx-3" style={{ marginTop: -6 }}>{maxText}</span>}

          {own === null ? (
            failed ? (
              <EmptyState
                compact
                error
                icon="alert"
                title={account ? 'Couldn’t load its tokens' : 'Couldn’t load your tokens'}
                primary={{ label: 'Retry', onClick: loadOwn, icon: 'refresh' }}
              />
            ) : (
              <EmptyState compact loading icon="link" title={account ? 'Loading its tokens…' : 'Loading your tokens…'} />
            )
          ) : own.length === 0 ? (
            <EmptyState
              compact
              icon="link"
              title="No tokens yet"
              body={account ? 'Mint one above to give the integration access.' : 'Mint one above to let a program call Vigil as you.'}
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
                      <Label t={t} />
                      <Dates t={t} />
                      <td className="col-act" style={{ textAlign: 'right' }}>
                        {/* an app's connection rotates through its own refresh: revoke only */}
                        {t.status === 'active' && !t.connected_app && (
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
                        {(t.status === 'rotated' || (t.status === 'active' && t.connected_app)) && (
                          <HoldButton label="Hold to revoke" name={`Revoke ${t.label}`} disabled={busy} onConfirm={() => revoke(t)} />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

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
      </SettingsCard>

      {canManage && !account && (
        <SettingsCard
          wide
          id="all-tokens"
          title="All API tokens"
          desc="Every token on this install, whoever holds it. You can revoke any of them, including one held by someone with more access than you; the holder is told. Only a token’s holder can rotate it, or for a service account, a holder of users.write (from its row under Service accounts)."
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
                      <Label t={t} />
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
