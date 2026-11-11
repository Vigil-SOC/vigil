import { useCallback, useEffect, useState } from 'react'
import { basePath } from '../../config/basePath'
import { Icon } from '../../shared/icons'
import '../../shared/kit.css'
import { ConfirmDialog, Field, Popup, Select, SettingsCard, TextInput } from '../../shared/ui'
import { invitesApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import type { SectionProps } from './types'

type Entry = Schema<'InviteEntry'>
type Channel = Entry['channel']
type Option = { value: string; label: string }

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string })?.msg || JSON.stringify(d)).join(', ')
  return (e as { message?: string })?.message || fallback
}

// UTC, 24-hour, as the rest of the console writes time
const utc = (iso: string) => `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`

function timeLeft(iso: string): string {
  const mins = Math.floor((new Date(iso).getTime() - Date.now()) / 60000)
  if (mins <= 0) return 'Expired'
  if (mins < 60) return `${mins} min left`
  if (mins < 2880) return `${Math.floor(mins / 60)} h left`
  return `${Math.floor(mins / 1440)} d left`
}

/** What the person is sent: the server's address when it has one, else this browser's own. */
const fullLink = (issued: Schema<'InviteIssued'>) =>
  issued.link ?? `${window.location.origin}${basePath}/accept-invite?token=${issued.token}`

/** Invite someone to set up their own account, and manage the invites still waiting. */
export default function InvitesCard({
  notify,
  roleOptions,
  roleName,
  defaultRoleId,
}: SectionProps & {
  /** only the roles the caller may hand out */
  roleOptions: Option[]
  roleName: (id: string) => string
  defaultRoleId?: string
}) {
  const [invites, setInvites] = useState<Entry[]>([])
  const [emailMissing, setEmailMissing] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState('')

  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [roleId, setRoleId] = useState('')
  const [channel, setChannel] = useState<Channel>('copy')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // the link a Copy invite made, shown once
  const [shown, setShown] = useState<{ email: string; link: string; expires: string } | null>(null)
  const [revoking, setRevoking] = useState<Entry | null>(null)
  const [resending, setResending] = useState<string | null>(null)

  const reload = useCallback(() => {
    invitesApi
      .list()
      .then(({ data }) => {
        setInvites(data.invites)
        setEmailMissing(data.email_missing)
        setLoadError('')
      })
      .catch((e) => setLoadError(errText(e, 'Failed to load invites')))
      .finally(() => setLoaded(true))
  }, [])
  useEffect(reload, [reload])

  const emailOn = emailMissing.length === 0

  const openInvite = () => {
    setEmail('')
    setRoleId(roleOptions.some((o) => o.value === defaultRoleId) ? (defaultRoleId ?? '') : (roleOptions[0]?.value ?? ''))
    setChannel('copy')
    setError('')
    setOpen(true)
  }

  // an issued link: Copy shows it once, Email says where it went
  const issued = (res: Schema<'InviteIssued'>) => {
    if (res.invite.channel === 'copy') {
      setShown({ email: res.invite.email, link: fullLink(res), expires: res.invite.expires_at })
    } else {
      notify('ok', `Sent an invite to ${res.invite.email}. The link works for 24 hours.`)
    }
    reload()
  }

  const handleInvite = async () => {
    const to = email.trim()
    if (!EMAIL_RE.test(to)) return setError('Enter a valid email address')
    if (!roleId) return setError('Role is required')
    setBusy(true)
    setError('')
    try {
      const { data } = await invitesApi.create({ email: to, role_id: roleId, channel })
      setOpen(false)
      issued(data)
    } catch (e) {
      setError(errText(e, 'Failed to invite'))
    } finally {
      setBusy(false)
    }
  }

  const handleResend = async (row: Entry) => {
    setResending(row.link_id)
    try {
      // an emailed invite goes by email again while that works; otherwise it is copied
      const via: Channel = row.channel === 'email' && emailOn ? 'email' : 'copy'
      const { data } = await invitesApi.resend(row.link_id, via)
      issued(data)
    } catch (e) {
      notify('err', errText(e, 'Failed to resend'))
    } finally {
      setResending(null)
    }
  }

  const handleRevoke = async () => {
    if (!revoking) return
    setBusy(true)
    try {
      await invitesApi.revoke(revoking.link_id)
      notify('ok', `Revoked the invite to ${revoking.email}.`)
      setRevoking(null)
      reload()
    } catch (e) {
      notify('err', errText(e, 'Failed to revoke'))
    } finally {
      setBusy(false)
    }
  }

  const copy = async () => {
    if (!shown) return
    try {
      await navigator.clipboard.writeText(shown.link)
      notify('ok', 'Link copied.')
    } catch {
      notify('err', 'Could not copy. Select the link and copy it by hand.')
    }
  }

  return (
    <SettingsCard
      wide
      title="Invites"
      desc="People set their own username and password through a single-use link. Nobody types a password for them."
      actions={
        <button className="btn primary" onClick={openInvite} disabled={roleOptions.length === 0}>
          <Icon name="plus" /> Invite
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
              <th style={{ minWidth: 'var(--col-main-min)' }}>Email</th>
              <th>Role</th>
              <th>Invited by</th>
              <th>Sent</th>
              <th>Expires</th>
              <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {invites.length === 0 && (
              <tr>
                <td colSpan={6} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                  {loaded ? 'No pending invites.' : 'Loading invites…'}
                </td>
              </tr>
            )}
            {invites.map((r) => (
              <tr key={r.link_id}>
                <td>{r.email}</td>
                <td>
                  <span className="chip">{roleName(r.role_id)}</span>
                </td>
                <td className="muted">{r.invited_by_name ?? '—'}</td>
                <td className="muted">{r.channel === 'email' ? 'By email' : 'Copied link'}</td>
                <td>
                  <span className="font-mono text-xs">{utc(r.expires_at)}</span>
                  <div className="mt-1">
                    <span className={`state-pill ${r.expired ? 'poor' : ''}`}>
                      <span className="state-dot" aria-hidden="true" />
                      {r.expired ? 'Expired' : timeLeft(r.expires_at)}
                    </span>
                  </div>
                </td>
                <td className="col-act" style={{ textAlign: 'right' }}>
                  <div className="inline-flex gap-1.5">
                    <button
                      className="btn ghost icon"
                      disabled={resending === r.link_id}
                      onClick={() => handleResend(r)}
                      title="Resend: a new link; the earlier one stops working"
                      aria-label={`Resend the invite to ${r.email}`}
                    >
                      <Icon name="refresh" size={15} />
                    </button>
                    <button
                      className="btn ghost icon"
                      onClick={() => setRevoking(r)}
                      title="Revoke"
                      aria-label={`Revoke the invite to ${r.email}`}
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Popup open={open} onClose={() => !busy && setOpen(false)} title="Invite someone" width={460}>
        <div className="flex flex-col gap-3.5">
          {error && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {error}
            </div>
          )}
          <Field label="Email" hint="They choose their own username and password. Inviting an address again replaces its pending invite.">
            <TextInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
          </Field>
          <Field label="Role">
            <Select
              value={roleId}
              options={roleOptions}
              placeholder="Select a role…"
              disabled={busy}
              onSelect={setRoleId}
            />
          </Field>
          <fieldset className="flex flex-col gap-2 border-0 p-0 m-0">
            <legend className="field-label mb-1">How they get the link</legend>
            <label className={`flex items-start gap-2 text-sm ${emailOn ? '' : 'opacity-60'}`}>
              <input
                type="radio"
                name="invite-channel"
                className="mt-1"
                checked={channel === 'email'}
                disabled={!emailOn || busy}
                onChange={() => setChannel('email')}
              />
              <span>
                <b>Email it</b>
                <span className="block text-xs text-tx-3">
                  {emailOn
                    ? 'Vigil sends it. The link works for 24 hours.'
                    : `Not available: set ${emailMissing.join(' and ')} on the server.`}
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="invite-channel"
                className="mt-1"
                checked={channel === 'copy'}
                disabled={busy}
                onChange={() => setChannel('copy')}
              />
              <span>
                <b>Copy the link</b>
                <span className="block text-xs text-tx-3">
                  You pass it on yourself. The link works for 7 days and is shown once.
                </span>
              </span>
            </label>
          </fieldset>
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
            <button className="btn primary" onClick={handleInvite} disabled={busy}>
              {busy ? 'Working…' : channel === 'email' ? 'Send invite' : 'Create link'}
            </button>
          </div>
        </div>
      </Popup>

      <Popup open={!!shown} onClose={() => setShown(null)} title="Invite link" width={520}>
        {shown && (
          <div className="flex flex-col gap-3.5">
            <span className="text-sm text-tx-2">
              Send this to {shown.email}. It works once and expires {utc(shown.expires)}.
            </span>
            <div className="font-mono text-xs break-all rounded-[10px] bg-bg2 border border-ln0 p-3 select-all">
              {shown.link}
            </div>
            <div className="settings-banner">
              <Icon name="info" size={14} /> Shown only now. Issuing a new link for this address cancels this one.
            </div>
            <div className="flex justify-end gap-2.5">
              <button className="btn ghost" onClick={() => setShown(null)}>
                Done
              </button>
              <button className="btn primary" onClick={copy}>
                <Icon name="copy" /> Copy link
              </button>
            </div>
          </div>
        )}
      </Popup>

      <ConfirmDialog
        open={!!revoking}
        title="Revoke invite?"
        body={`The link sent to ${revoking?.email ?? ''} stops working. You can invite them again later.`}
        confirmLabel="Revoke"
        busy={busy}
        onConfirm={handleRevoke}
        onClose={() => !busy && setRevoking(null)}
      />
    </SettingsCard>
  )
}
