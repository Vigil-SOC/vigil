import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { requestErrorMessage } from '../screens/login/requestErrorMessage'
import { signInsApi, type SignIn } from '../services/api'
import { Icon } from '../shared/icons'
import { Field, PasswordInput, Popup, TextInput } from '../shared/ui'
import SignInList from '../shared/SignInList'

type Action = { kind: 'one'; id: string; label: string } | { kind: 'others' } | { kind: 'all' }

const TITLES: Record<Action['kind'], string> = {
  one: "Confirm it's you to end a sign-in",
  others: "Confirm it's you to sign out everywhere else",
  all: "Confirm it's you to sign out everywhere",
}

/**
 * Your own sign-ins, with End one, Sign out everywhere else and Sign out everywhere.
 * Each ending asks for a one-factor check first, every time: the password, or an
 * authenticator or recovery code when two-step sign-in is on.
 */
export default function SignInsDialog({
  open,
  onClose,
  mfa,
  onSignedOut,
}: {
  open: boolean
  onClose: () => void
  /** two-step sign-in is on: the check is a code, not the password */
  mfa: boolean
  /** this sign-in was among those ended */
  onSignedOut: () => void
}) {
  const [rows, setRows] = useState<SignIn[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<Action | null>(null)
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setRows((await signInsApi.mine()).data)
      setError(null)
    } catch (err) {
      setError(requestErrorMessage(err, "Couldn't load your sign-ins."))
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setRows(null)
    setPending(null)
    setSecret('')
    void load()
  }, [open, load])

  const others = (rows ?? []).filter((s) => !s.ended_at && !s.current).length

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!pending || busy) return
    if (!secret.trim()) {
      setError(mfa ? 'Enter a code.' : 'Enter your password.')
      return
    }
    const body = mfa ? { code: secret.trim() } : { password: secret }
    setBusy(true)
    setError(null)
    try {
      const res = await (pending.kind === 'one'
        ? signInsApi.endOne(pending.id, body)
        : pending.kind === 'others'
          ? signInsApi.endOthers(body)
          : signInsApi.endAll(body))
      if (res.data.signed_out) return onSignedOut()
      setPending(null)
      setSecret('')
      await load()
    } catch (err) {
      setError(requestErrorMessage(err, "Couldn't end the sign-in. Try again."))
    } finally {
      setBusy(false)
    }
  }

  const ask = (action: Action) => {
    setPending(action)
    setSecret('')
    setError(null)
  }

  return (
    <Popup open={open} onClose={() => !busy && onClose()} title={pending ? TITLES[pending.kind] : 'Your sign-ins'} width={640}>
      {error && (
        <div className="settings-banner err mb-3" role="alert">
          <Icon name="alert" size={14} /> {error}
        </div>
      )}
      {pending ? (
        <form className="flex flex-col gap-3.5" onSubmit={submit} noValidate>
          <p className="text-sm text-tx-2 leading-relaxed" style={{ margin: 0 }}>
            {pending.kind === 'one' && `This ends ${pending.label}. Its next request is refused.`}
            {pending.kind === 'others' && `This ends your ${others} other open sign-in${others === 1 ? '' : 's'}. This one stays open.`}
            {pending.kind === 'all' && 'This ends every open sign-in, this one too. You will sign in again.'}
          </p>
          <Field label={mfa ? 'Authenticator or recovery code' : 'Password'}>
            {mfa ? (
              <TextInput
                autoFocus
                autoComplete="one-time-code"
                value={secret}
                disabled={busy}
                onChange={(e) => setSecret(e.target.value)}
              />
            ) : (
              <PasswordInput
                autoFocus
                autoComplete="current-password"
                value={secret}
                disabled={busy}
                onChange={(e) => setSecret(e.target.value)}
              />
            )}
          </Field>
          <div className="flex justify-end gap-2.5 mt-1">
            <button type="button" className="btn ghost" disabled={busy} onClick={() => { setPending(null); setError(null) }}>
              Cancel
            </button>
            <button type="submit" className="btn danger" disabled={busy}>
              {busy ? 'Ending…' : pending.kind === 'one' ? 'End sign-in' : pending.kind === 'others' ? 'Sign out everywhere else' : 'Sign out everywhere'}
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
                onEnd={(s) => ask({ kind: 'one', id: s.sign_in_id, label: `the ${s.browser} sign-in` })}
              />
            )
          )}
          <div className="flex flex-wrap justify-end gap-2.5">
            <button className="btn ghost" disabled={others === 0} onClick={() => ask({ kind: 'others' })}>
              Sign out everywhere else
            </button>
            <button className="btn ghost" disabled={rows === null} onClick={() => ask({ kind: 'all' })}>
              Sign out everywhere
            </button>
          </div>
        </div>
      )}
    </Popup>
  )
}
