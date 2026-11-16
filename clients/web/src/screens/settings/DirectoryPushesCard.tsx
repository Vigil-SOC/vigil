import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import '../../shared/kit.css'
import { ConfirmDialog, Field, Select, SettingsCard } from '../../shared/ui'
import api, { directoryPushesApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import type { User } from './useSettings'
import { DIRECTORY_PUSHES_ID, type SectionProps } from './types'

type Pending = Schema<'PendingPush'>
type Count = Schema<'SyncWindowCount'>

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  return (e as { message?: string })?.message || fallback
}

// UTC, 24-hour, as the rest of the console writes time
const utc = (iso: string) => `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`
// a datetime-local value is the viewer's zone; the server takes UTC
const asIso = (local: string) => (local ? new Date(local).toISOString() : '')
// the input stops at the minute: until covers the whole of it
const untilIso = (local: string) => (local ? new Date(new Date(local).getTime() + 59_999).toISOString() : '')
const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`

/** Pushes that wait for a link to an existing account, and undoing a window of sync deactivations.
 *  Shown to users.write holders while sync is on, or while a push still waits. */
export default function DirectoryPushesCard({ notify, onChanged }: SectionProps & { onChanged: () => void }) {
  const [pending, setPending] = useState<Pending[]>([])
  const [syncOn, setSyncOn] = useState(false)
  const [loadError, setLoadError] = useState('')

  const [linking, setLinking] = useState<Pending | null>(null)
  const [accounts, setAccounts] = useState<User[] | null>(null)
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)

  const [windowOpen, setWindowOpen] = useState(false)
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [count, setCount] = useState<Count | null>(null)
  const [countError, setCountError] = useState('')

  const reload = useCallback(() => {
    directoryPushesApi.pending().then(
      ({ data }) => {
        setPending(data.pending)
        setSyncOn(data.sync_enabled)
        setLoadError('')
      },
      (e) => setLoadError(errText(e, 'Couldn’t load waiting pushes')),
    )
  }, [])
  useEffect(reload, [reload])

  // the count is shown before the confirm: asked again whenever the window changes
  useEffect(() => {
    setCount(null)
    setCountError('')
    if (!windowOpen || !since || !until) return
    let cancelled = false
    directoryPushesApi.countWindow(asIso(since), untilIso(until)).then(
      ({ data }) => !cancelled && setCount(data),
      (e) => !cancelled && setCountError(errText(e, 'Couldn’t count the accounts')),
    )
    return () => {
      cancelled = true
    }
  }, [windowOpen, since, until])

  if (!syncOn && pending.length === 0 && !loadError) return null

  const openLink = (row: Pending) => {
    setLinking(row)
    setTarget(row.holders[0]?.user_id ?? '')
    setAccounts(null)
    // people only: a service account can't take a link
    api
      .get('/users/', { params: { status: 'current', service_account: false, limit: 1000 } })
      .then(({ data }) => setAccounts(data.users), () => setAccounts([]))
  }

  // the accounts holding the pushed names first, then everyone else
  const held = new Set(linking?.holders.map((h) => h.user_id))
  const options = [
    ...(linking?.holders ?? []).map((h) => ({ value: h.user_id, label: `${h.username} · holds this name` })),
    ...(accounts ?? [])
      .filter((u) => !held.has(u.user_id))
      .map((u) => ({ value: u.user_id, label: u.full_name ? `${u.username} · ${u.full_name}` : u.username })),
  ]

  const handleLink = async () => {
    if (!linking || !target) return
    setBusy(true)
    try {
      await directoryPushesApi.link(linking.pending_id, target)
      const name = options.find((o) => o.value === target)?.label.split(' · ')[0] ?? 'the account'
      notify('ok', `Linked ${linking.username} to ${name}. The provider’s next retry takes the account over.`)
      setLinking(null)
      reload()
      onChanged()
    } catch (e) {
      notify('err', errText(e, 'Failed to link'))
    } finally {
      setBusy(false)
    }
  }

  const back = count ? count.count - count.skipped : 0
  const handleWindow = async () => {
    setBusy(true)
    try {
      const { data } = await directoryPushesApi.reactivateWindow(asIso(since), untilIso(until))
      notify(
        'ok',
        `Reactivated ${plural(data.reactivated, 'account')}.` +
          (data.skipped ? ` Skipped ${data.skipped}: their role grants more than you hold.` : '') +
          ' Their old tokens stay revoked.',
      )
      setWindowOpen(false)
      onChanged()
    } catch (e) {
      notify('err', errText(e, 'Failed to reactivate'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard
      wide
      id={DIRECTORY_PUSHES_ID}
      title="Directory pushes"
      desc="People the provider pushed whose username or email an existing account already holds. Link each to that account and the provider’s next retry takes it over."
      actions={
        <button
          className="btn ghost"
          onClick={() => {
            setSince('')
            setUntil('')
            setWindowOpen(true)
          }}
        >
          <Icon name="play" /> Reactivate sync deactivations…
        </button>
      }
    >
      {loadError && (
        <div className="settings-banner err mb-3">
          <Icon name="alert" size={14} /> {loadError}
        </div>
      )}
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ minWidth: 'var(--col-main-min)' }}>Pushed person</th>
              <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {pending.length === 0 && (
              <tr>
                <td colSpan={2} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                  No pushes wait for a link.
                </td>
              </tr>
            )}
            {pending.map((r) => (
              <tr key={r.pending_id}>
                <td className="cell-wrap">
                  <div className="cell-clamp" title={r.display_name || r.username}>
                    {r.display_name || r.username}
                  </div>
                  <div className="muted cell-clamp" title={`${r.username} · ${r.email}`}>
                    {r.username} · {r.email}
                  </div>
                  <div className="muted cell-clamp font-mono text-xs" title={r.external_id}>
                    {r.external_id}
                  </div>
                  {/* under the person, in the column that stays on screen, as Invites does */}
                  <div className="cell-clamp mt-1">
                    <span className="muted">Held by · </span>
                    {r.holders.length ? r.holders.map((h) => h.username).join(', ') : '—'}
                  </div>
                  <div className="muted text-xs">
                    Last push <span className="font-mono">{utc(r.received_at)}</span>
                  </div>
                </td>
                <td className="col-act" style={{ textAlign: 'right' }}>
                  <button className="btn ghost" onClick={() => openLink(r)} aria-label={`Link ${r.username}`}>
                    <Icon name="link" size={14} /> Link
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={!!linking}
        title={linking ? `Link ${linking.username}?` : ''}
        danger={false}
        body={
          linking && (
            <div className="flex flex-col gap-3.5">
              <span>
                The provider pushed {linking.display_name || linking.username} ({linking.email}). Pick the account that is
                them. The provider’s next retry takes that account over: directory sync manages it from then on, its password stays,
                and its role follows the group mapping once the provider pushes its groups.
              </span>
              <Field label="Account">
                <Select
                  value={target}
                  options={options}
                  searchable
                  placeholder={accounts === null && !options.length ? 'Loading accounts…' : 'Choose an account…'}
                  disabled={busy}
                  onSelect={setTarget}
                />
              </Field>
            </div>
          )
        }
        confirmLabel="Link"
        busy={busy}
        confirmDisabled={!target}
        onConfirm={handleLink}
        onClose={() => !busy && setLinking(null)}
      />

      <ConfirmDialog
        open={windowOpen}
        title="Reactivate sync deactivations?"
        danger={false}
        body={
          <div className="flex flex-col gap-3.5">
            <span>
              Brings back every account directory sync deactivated in this window. Accounts a person or dormancy
              deactivated stay deactivated, their old tokens stay revoked, and nobody is sent a notice.
            </span>
            {/* side by side while each fits its date and time; stacked on a narrow screen */}
            <div className="grid gap-2.5" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))' }}>
              <Field label="Since">
                <input
                  type="datetime-local"
                  aria-label="Since"
                  className="field-input"
                  value={since}
                  disabled={busy}
                  onChange={(e) => setSince(e.target.value)}
                />
              </Field>
              <Field label="Until">
                <input
                  type="datetime-local"
                  aria-label="Until"
                  className="field-input"
                  value={until}
                  disabled={busy}
                  onChange={(e) => setUntil(e.target.value)}
                />
              </Field>
            </div>
            {countError ? (
              <div className="settings-banner err" role="alert">
                <Icon name="alert" size={14} /> <span className="min-w-0">{countError}</span>
              </div>
            ) : count ? (
              <span role="status">
                {plural(back, 'account')} will come back.
                {count.skipped > 0 && ` ${count.skipped} more have a role that grants more than you hold and are skipped.`}
              </span>
            ) : (
              <span className="text-tx-3">{since && until ? 'Counting…' : 'Pick a since and an until.'}</span>
            )}
          </div>
        }
        confirmLabel={count && back ? `Reactivate ${plural(back, 'account')}` : 'Reactivate'}
        busy={busy}
        confirmDisabled={!back}
        onConfirm={handleWindow}
        onClose={() => !busy && setWindowOpen(false)}
      />
    </SettingsCard>
  )
}
