import { useCallback, useEffect, useState } from 'react'
import { requestErrorMessage } from '../login/requestErrorMessage'
import { signInsApi, type SignIn } from '../../services/api'
import { Icon } from '../../shared/icons'
import { Field, Popup, TextInput } from '../../shared/ui'
import SignInList from '../../shared/SignInList'
import type { SectionProps } from './types'

type Target = { kind: 'one'; id: string; label: string } | { kind: 'all' }

/**
 * Someone else's sign-ins, for a holder of users.write over the account. Read-only
 * except for ending: one or all, with a typed reason that goes to the audit trail.
 * The person is told their sign-in was ended, though not the reason.
 */
export default function UserSignInsDialog({
  user,
  onClose,
  notify,
}: {
  user: { user_id: string; username: string }
  onClose: () => void
  notify: SectionProps['notify']
}) {
  const [rows, setRows] = useState<SignIn[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setRows((await signInsApi.ofUser(user.user_id)).data)
      setError(null)
    } catch (err) {
      setError(requestErrorMessage(err, "Couldn't load their sign-ins."))
    }
  }, [user.user_id])

  useEffect(() => {
    void load()
  }, [load])

  const open = (rows ?? []).filter((s) => !s.ended_at).length

  const end = async () => {
    if (!target || !reason.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const { data } =
        target.kind === 'one'
          ? await signInsApi.endUsersOne(user.user_id, target.id, reason.trim())
          : await signInsApi.endUsersAll(user.user_id, reason.trim())
      notify('ok', `Ended ${data.ended} sign-in${data.ended === 1 ? '' : 's'} of ${user.username}.`)
      setTarget(null)
      setReason('')
      await load()
    } catch (err) {
      setError(requestErrorMessage(err, "Couldn't end the sign-in."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open onClose={() => !busy && onClose()} title={`Sign-ins · ${user.username}`} width={640}>
      {error && (
        <div className="settings-banner err mb-3" role="alert">
          <Icon name="alert" size={14} /> {error}
        </div>
      )}
      {target ? (
        <form
          className="flex flex-col gap-3.5"
          onSubmit={(e) => {
            e.preventDefault()
            void end()
          }}
        >
          <p className="text-sm text-tx-2 leading-relaxed" style={{ margin: 0 }}>
            {target.kind === 'one'
              ? `This ends ${target.label}.`
              : `This ends all ${open} open sign-in${open === 1 ? '' : 's'} of ${user.username}.`}{' '}
            Their next request is refused, and they are told.
          </p>
          <Field label="Reason" hint="Kept in the audit trail. They are not shown it.">
            <TextInput autoFocus maxLength={500} value={reason} disabled={busy} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <div className="flex justify-end gap-2.5 mt-1">
            <button type="button" className="btn ghost" disabled={busy} onClick={() => setTarget(null)}>
              Cancel
            </button>
            <button type="submit" className="btn danger" disabled={busy || !reason.trim()}>
              {busy ? 'Ending…' : target.kind === 'one' ? 'End sign-in' : 'End all sign-ins'}
            </button>
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-3.5">
          {rows === null && !error ? (
            <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
          ) : (
            rows && (
              <SignInList
                signIns={rows}
                onEnd={(s) => {
                  setTarget({ kind: 'one', id: s.sign_in_id, label: `the ${s.browser} sign-in` })
                  setReason('')
                }}
              />
            )
          )}
          <div className="flex flex-wrap justify-end gap-2.5">
            <button className="btn ghost" onClick={onClose}>
              Close
            </button>
            <button
              className="btn danger"
              disabled={open === 0}
              onClick={() => {
                setTarget({ kind: 'all' })
                setReason('')
              }}
            >
              End all sign-ins
            </button>
          </div>
        </div>
      )}
    </Popup>
  )
}
