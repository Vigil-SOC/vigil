import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, Field, Popup, Select, SettingsCard, TextInput } from '../../shared/ui'
import { utcDayClock } from '../../shared/utc'
import { basePath } from '../../config/basePath'
import { invitesApi, type Invite, type InviteChannel, type InviteIssued } from '../../services/api'
import type { SectionProps } from './types'
import './invites.css'

// mirrors what the backend's Pydantic EmailStr accepts
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

type Option = { value: string; label: string }

/** New invite, or a new link for an open one (email and role fixed). Mounted
 *  only while open, so each opening starts fresh. */
export function InviteDialog({
  resending,
  roleOptions,
  defaultRoleId,
  emailUnavailable,
  onClose,
  onIssued,
  errText,
}: {
  resending: Invite | null
  roleOptions: Option[]
  defaultRoleId: string
  emailUnavailable: string | null
  onClose: (issued: boolean) => void
  onIssued: (email: string, channel: InviteChannel) => void
  errText: (e: unknown, fallback: string) => string
}) {
  const [email, setEmail] = useState(resending?.email ?? '')
  const [roleId, setRoleId] = useState(resending?.role_id ?? defaultRoleId)
  const [channel, setChannel] = useState<InviteChannel>(
    emailUnavailable ? 'copy' : ((resending?.channel as InviteChannel) ?? 'copy'),
  )
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // the copied link, shown once
  const [link, setLink] = useState('')
  const [copied, setCopied] = useState(false)
  const [issued, setIssued] = useState(false)
  const close = () => onClose(issued)

  const fullLink = (issued: InviteIssued) =>
    issued.link ?? `${window.location.origin}${basePath}${issued.path ?? ''}`

  const submit = async () => {
    const to = email.trim()
    if (!resending) {
      if (!EMAIL_RE.test(to)) return setError('Enter a valid email address')
      if (!roleId) return setError('Choose a role')
    }
    setBusy(true)
    setError('')
    try {
      const { data } = resending
        ? await invitesApi.resend(resending.link_id, channel)
        : await invitesApi.create({ email: to, role_id: roleId, channel })
      setIssued(true)
      onIssued(resending?.email ?? to, channel)
      if (channel === 'copy') setLink(fullLink(data))
      else onClose(true)
    } catch (e) {
      setError(errText(e, 'Failed to send the invite'))
    } finally {
      setBusy(false)
    }
  }

  const copy = () => {
    void navigator.clipboard?.writeText(link).then(() => setCopied(true))
  }

  return (
    <Popup
      open
      onClose={() => !busy && close()}
      title={link ? 'Invite link' : resending ? 'New invite link' : 'Invite'}
      width={480}
    >
      {link ? (
        <div className="flex flex-col gap-3.5">
          <span className="text-sm">
            Send this link to {email.trim()} yourself. It works once, lasts 7 days and is shown only now.
          </span>
          <div className="invite-link">
            <code aria-label="Invite link">{link}</code>
            <button className="btn ghost" onClick={copy}>
              <Icon name={copied ? 'check' : 'copy'} size={14} /> {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <div className="settings-banner info">
            <Icon name="info" size={14} /> Issuing a new link for this email cancels this one.
          </div>
          <div className="flex justify-end">
            <button className="btn primary" onClick={close}>Done</button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3.5">
          {error && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {error}
            </div>
          )}
          {resending ? (
            <span className="text-sm">
              A new link for {resending.email}. The earlier link stops working.
            </span>
          ) : (
            <>
              <Field label="Email" hint="They choose their own username and password at the link.">
                <TextInput type="email" value={email} autoFocus onChange={(e) => setEmail(e.target.value)} />
              </Field>
              <Field label="Role">
                <Select value={roleId} options={roleOptions} placeholder="Select a role…" onSelect={setRoleId} />
              </Field>
            </>
          )}
          <fieldset className="invite-channel">
            <legend>How the link reaches them</legend>
            <label>
              <input
                type="radio"
                name="invite-channel"
                checked={channel === 'email'}
                disabled={!!emailUnavailable}
                onChange={() => setChannel('email')}
              />
              <span>
                <b>Email</b>
                <span>{emailUnavailable ?? 'Vigil emails the link. It lasts 24 hours.'}</span>
              </span>
            </label>
            <label>
              <input
                type="radio"
                name="invite-channel"
                checked={channel === 'copy'}
                onChange={() => setChannel('copy')}
              />
              <span>
                <b>Copy</b>
                <span>You get the link to send yourself. It lasts 7 days.</span>
              </span>
            </label>
          </fieldset>
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={close} disabled={busy}>
              Cancel
            </button>
            <button className="btn primary" onClick={submit} disabled={busy}>
              {busy ? 'Sending…' : channel === 'email' ? 'Send invite' : 'Create link'}
            </button>
          </div>
        </div>
      )}
    </Popup>
  )
}

/** Open invites: who, as what, from whom, until when; Resend and Revoke. */
export function InvitesCard({
  invites,
  roleName,
  canWrite,
  onResend,
  reload,
  notify,
  errText,
}: {
  invites: Invite[]
  roleName: (id: string) => string
  canWrite: boolean
  onResend: (invite: Invite) => void
  reload: () => void
  notify: SectionProps['notify']
  errText: (e: unknown, fallback: string) => string
}) {
  const [revoking, setRevoking] = useState<Invite | null>(null)
  const [busy, setBusy] = useState(false)

  const revoke = async () => {
    if (!revoking) return
    setBusy(true)
    try {
      await invitesApi.revoke(revoking.link_id)
      notify('ok', `Revoked the invite for ${revoking.email}.`)
      setRevoking(null)
      reload()
    } catch (e) {
      notify('err', errText(e, 'Failed to revoke the invite'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard wide title="Invites" desc="People invited who haven't set up their account yet.">
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ minWidth: 'var(--col-main-min)' }}>Email</th>
              <th>Role</th>
              <th>Invited by</th>
              <th>Expires (UTC)</th>
              {canWrite && <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>}
            </tr>
          </thead>
          <tbody>
            {invites.length === 0 && (
              <tr>
                <td colSpan={canWrite ? 5 : 4} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                  No open invites.
                </td>
              </tr>
            )}
            {invites.map((i) => (
              <tr key={i.link_id}>
                <td className="invite-email">{i.email}</td>
                <td>
                  <span className="chip">{i.role_id ? roleName(i.role_id) : '—'}</span>
                </td>
                <td className="muted">{i.inviter_name ?? '—'}</td>
                <td className="muted">
                  {i.expired || i.too_many_tries ? (
                    <span className="status open">{i.expired ? 'Expired' : 'Too many tries'}</span>
                  ) : (
                    utcDayClock(i.expires_at)
                  )}
                </td>
                {canWrite && (
                  <td className="col-act" style={{ textAlign: 'right' }}>
                    <div className="inline-flex gap-1.5">
                      <button className="btn ghost icon" title="Resend" aria-label={`Resend to ${i.email}`} onClick={() => onResend(i)}>
                        <Icon name="send" size={15} />
                      </button>
                      <button className="btn ghost icon" title="Revoke" aria-label={`Revoke the invite for ${i.email}`} onClick={() => setRevoking(i)}>
                        <Icon name="close" size={15} />
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={!!revoking}
        title="Revoke invite?"
        body={`The link sent to ${revoking?.email ?? 'this address'} stops working. You can invite them again later.`}
        confirmLabel="Revoke"
        busy={busy}
        onConfirm={revoke}
        onClose={() => !busy && setRevoking(null)}
      />
    </SettingsCard>
  )
}
