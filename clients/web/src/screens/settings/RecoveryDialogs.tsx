import { useEffect, useRef, useState } from 'react'
import { basePath } from '../../config/basePath'
import { requestErrorMessage } from '../login/requestErrorMessage'
import { signInsApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { Icon } from '../../shared/icons'
import { Field, Popup, Select, TextInput } from '../../shared/ui'

type Notify = (kind: 'ok' | 'err', msg: string) => void
type Person = { user_id: string; username: string }
type ConfirmedBy = NonNullable<Schema<'ResetLinkRequest'>['identity_confirmed_by']>

export const CONFIRMED_BY = [
  { value: 'in_person', label: 'In person' },
  { value: 'video_call', label: 'On a video call' },
  { value: 'phone_call_back', label: 'By calling them back on a known number' },
  { value: 'other_verified_channel', label: 'Another verified channel' },
]

// UTC, 24-hour, as the rest of the console writes time
const utc = (iso: string) => `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`

/** The box on every recovery dialog: the person's API tokens go too. */
export function CompromiseBox({
  checked,
  disabled,
  onChange,
}: {
  checked: boolean
  disabled?: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex items-start gap-2 text-sm text-tx-2 leading-relaxed">
      <input type="checkbox" className="mt-0.5" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>
        <b>Suspected compromise</b>: also revoke every API token they hold. They mint new ones after.
      </span>
    </label>
  )
}

/** Send a person a password reset link, or copy one to hand over; see and revoke the open one. */
export function ResetLinkDialog({ user, notify, onClose }: { user: Person; notify: Notify; onClose: () => void }) {
  const [state, setState] = useState<Schema<'ResetLinkState'> | null>(null)
  const [error, setError] = useState('')
  const [channel, setChannel] = useState<'email' | 'copy'>('copy')
  const [confirmedBy, setConfirmedBy] = useState('')
  const [compromise, setCompromise] = useState(false)
  const [busy, setBusy] = useState(false)
  // the copied link, shown once
  const [shown, setShown] = useState<{ link: string; expires: string } | null>(null)
  const loaded = useRef(false)

  const load = () =>
    signInsApi
      .resetLink(user.user_id)
      .then(({ data }) => {
        // Email by default when it works, once: a reload keeps the admin's choice.
        if (!loaded.current && data.email_missing.length === 0) setChannel('email')
        loaded.current = true
        setState(data)
      })
      .catch((e) => setError(requestErrorMessage(e, "Couldn't load their reset link.")))
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user.user_id])

  const emailOn = !!state && state.email_missing.length === 0
  const open = state?.reset_link

  const issue = async () => {
    setBusy(true)
    setError('')
    try {
      const { data } = await signInsApi.issueResetLink(user.user_id, {
        channel,
        identity_confirmed_by: channel === 'copy' ? (confirmedBy as ConfirmedBy) : null,
        suspected_compromise: compromise,
      })
      const tokens = data.tokens_revoked ? ` ${data.tokens_revoked} API token${data.tokens_revoked === 1 ? '' : 's'} revoked.` : ''
      if (channel === 'copy') {
        setShown({
          link: data.link ?? `${window.location.origin}${basePath}/reset-password?token=${data.token}`,
          expires: data.reset_link.expires_at,
        })
        if (tokens) notify('ok', tokens.trim())
      } else {
        notify('ok', `Sent ${user.username} a reset link. It works once, for one hour.${tokens}`)
        onClose()
      }
    } catch (e) {
      setError(requestErrorMessage(e, "Couldn't issue a reset link."))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async () => {
    setBusy(true)
    setError('')
    try {
      await signInsApi.revokeResetLink(user.user_id)
      notify('ok', `Revoked ${user.username}'s reset link.`)
      setShown(null)
      await load()
    } catch (e) {
      setError(requestErrorMessage(e, "Couldn't revoke the link."))
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

  if (shown) {
    return (
      <Popup open onClose={onClose} title={`Reset link · ${user.username}`} width={520}>
        <div className="flex flex-col gap-3.5">
          {error && (
            <div className="settings-banner err" role="alert">
              <Icon name="alert" size={14} /> {error}
            </div>
          )}
          <span className="text-sm text-tx-2">
            Give this to {user.username}. It works once and expires {utc(shown.expires)}.
          </span>
          <div className="font-mono text-xs break-all rounded-[10px] bg-bg2 border border-ln0 p-3 select-all">{shown.link}</div>
          <div className="settings-banner">
            <Icon name="info" size={14} /> Shown only now. A new link, or a change to their password, cancels this one.
          </div>
          <div className="flex justify-end gap-2.5 flex-wrap">
            <button className="btn ghost" onClick={revoke} disabled={busy}>
              <Icon name="close" /> Revoke link
            </button>
            <button className="btn ghost" onClick={onClose} disabled={busy}>
              Done
            </button>
            <button className="btn primary" onClick={copy}>
              <Icon name="copy" /> Copy link
            </button>
          </div>
        </div>
      </Popup>
    )
  }

  const ready = channel === 'email' ? emailOn : !!confirmedBy
  return (
    <Popup open onClose={() => !busy && onClose()} title={`Reset password · ${user.username}`} width={480}>
      <div className="flex flex-col gap-3.5">
        {error && (
          <div className="settings-banner err" role="alert">
            <Icon name="alert" size={14} /> {error}
          </div>
        )}
        <span className="text-sm text-tx-2 leading-relaxed">
          A single-use link to choose a new password, valid for one hour. Their two-step sign-in stays as it is. They
          and the other people who manage users are told.
        </span>
        {open && (
          <div className="settings-banner flex-wrap">
            <Icon name="link" size={14} />
            <span className="flex-1 min-w-0">
              An unused link{open.issued_by_name ? ` from ${open.issued_by_name}` : ''} expires {utc(open.expires_at)}. A new one
              replaces it.
            </span>
            <button className="btn ghost" onClick={revoke} disabled={busy}>
              Revoke
            </button>
          </div>
        )}
        <fieldset className="flex flex-col gap-2 border-0 p-0 m-0">
          <legend className="field-label mb-1">How they get the link</legend>
          <label className={`flex items-start gap-2 text-sm ${emailOn ? '' : 'opacity-60'}`}>
            <input
              type="radio"
              name="reset-channel"
              className="mt-1"
              checked={channel === 'email'}
              disabled={!emailOn || busy}
              onChange={() => setChannel('email')}
            />
            <span>
              <b>Email it</b>
              <span className="block text-xs text-tx-3">
                {emailOn || !state
                  ? 'Vigil sends it to the address on their account.'
                  : `Not available: set ${state.email_missing.join(' and ')} on the server.`}
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="radio"
              name="reset-channel"
              className="mt-1"
              checked={channel === 'copy'}
              disabled={busy}
              onChange={() => setChannel('copy')}
            />
            <span>
              <b>Copy the link</b>
              <span className="block text-xs text-tx-3">You hand it over yourself. It is shown once.</span>
            </span>
          </label>
        </fieldset>
        {channel === 'copy' && (
          <Field label="How you confirmed it is them" hint="Kept in the audit trail.">
            <Select value={confirmedBy} options={CONFIRMED_BY} placeholder="Choose…" disabled={busy} onSelect={setConfirmedBy} />
          </Field>
        )}
        <CompromiseBox checked={compromise} disabled={busy} onChange={setCompromise} />
        <div className="flex justify-end gap-2.5 mt-1">
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn primary" onClick={issue} disabled={busy || !state || !ready}>
            {busy ? 'Working…' : channel === 'email' ? 'Send link' : 'Create link'}
          </button>
        </div>
      </div>
    </Popup>
  )
}

/** Remove every two-step method a person has and end their sign-ins, with a reason. */
export function MfaResetDialog({
  user,
  notify,
  onClose,
  onDone,
}: {
  user: Person
  notify: Notify
  onClose: () => void
  onDone?: () => void
}) {
  const [reason, setReason] = useState('')
  const [confirmedBy, setConfirmedBy] = useState('')
  const [compromise, setCompromise] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      const { data } = await signInsApi.resetMfa(user.user_id, {
        reason: reason.trim(),
        identity_confirmed_by: confirmedBy as ConfirmedBy,
        suspected_compromise: compromise,
      })
      const tokens = data.tokens_revoked ? `, ${data.tokens_revoked} API token${data.tokens_revoked === 1 ? '' : 's'} revoked` : ''
      notify('ok', `Reset ${user.username}'s two-step sign-in: ${data.ended} sign-in${data.ended === 1 ? '' : 's'} ended${tokens}.`)
      if (data.warning) notify('err', data.warning)
      onDone?.()
      onClose()
    } catch (e) {
      setError(requestErrorMessage(e, "Couldn't reset their two-step sign-in."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open onClose={() => !busy && onClose()} title={`Reset two-step sign-in · ${user.username}`} width={480}>
      <div className="flex flex-col gap-3.5">
        {error && (
          <div className="settings-banner err" role="alert">
            <Icon name="alert" size={14} /> {error}
          </div>
        )}
        <span className="text-sm text-tx-2 leading-relaxed">
          Removes their authenticator app, security keys and recovery codes, and ends their sign-ins. Their password
          stays. Where two-step sign-in is required of them, they set it up again when they next sign in. They and the
          other people who manage users are told.
        </span>
        <Field label="Reason" hint="Kept in the audit trail.">
          <TextInput maxLength={500} value={reason} disabled={busy} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Field label="How you confirmed it is them" hint="Kept in the audit trail.">
          <Select value={confirmedBy} options={CONFIRMED_BY} placeholder="Choose…" disabled={busy} onSelect={setConfirmedBy} />
        </Field>
        <CompromiseBox checked={compromise} disabled={busy} onChange={setCompromise} />
        <div className="flex justify-end gap-2.5 mt-1">
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn danger" onClick={submit} disabled={busy || !reason.trim() || !confirmedBy}>
            {busy ? 'Working…' : 'Reset two-step sign-in'}
          </button>
        </div>
      </div>
    </Popup>
  )
}
