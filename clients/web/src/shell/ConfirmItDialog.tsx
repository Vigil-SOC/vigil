import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { requestErrorMessage } from '../screens/login/requestErrorMessage'
import { confirmApi } from '../services/api'
import { CONFIRMATION_REQUIRED, registerStepUp, type StepUpDetail } from '../services/stepUp'
import { Icon } from '../shared/icons'
import { Field, PasswordInput, Popup } from '../shared/ui'
import TwoStepDialog from './TwoStepDialog'

type Pending = { detail: StepUpDetail; resolve: () => void; reject: (reason?: unknown) => void }

// The one place a refused privileged change asks "Confirm it's you" (api.ts hands the refusal
// here and retries the request once this resolves). Mounted once, in the console.
export default function ConfirmItHost() {
  const { user } = useAuth()
  const [pending, setPending] = useState<Pending | null>(null)
  const current = useRef<{ promise: Promise<void>; pending: Pending } | null>(null)
  const mfaOn = useRef(false)
  mfaOn.current = !!user?.mfa_enabled

  useEffect(
    () =>
      registerStepUp((detail) => {
        // Requests refused together share one dialog.
        if (current.current) return current.current.promise
        let pendingEntry!: Pending
        const promise = new Promise<void>((resolve, reject) => {
          pendingEntry = { detail, resolve, reject }
        })
        current.current = { promise, pending: pendingEntry }
        setPending(pendingEntry)
        return promise
      }),
    [],
  )

  const finish = (ok: boolean) => {
    const entry = current.current?.pending
    current.current = null
    setPending(null)
    if (ok) entry?.resolve()
    else entry?.reject(new Error('cancelled'))
  }

  if (!pending) return null
  return pending.detail.code === CONFIRMATION_REQUIRED ? (
    <ConfirmIt detail={pending.detail} mfa={!!user?.mfa_enabled} onDone={finish} />
  ) : (
    // No two-step sign-in yet: set it up (that starts the window), then the change goes through.
    <TwoStepDialog
      open
      note={`Two-step sign-in is needed to ${pending.detail.action}.`}
      onClose={() => finish(mfaOn.current)}
    />
  )
}

/** "Confirm it's you to …": a current code (the password with no MFA); strong adds the password. */
function ConfirmIt({ detail, mfa, onDone }: { detail: StepUpDetail; mfa: boolean; onDone: (ok: boolean) => void }) {
  const strong = detail.variant === 'strong'
  const needPassword = strong || !mfa
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<{ field: 'password' | 'code'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (needPassword && !password) return setError({ field: 'password', text: 'Enter your password.' })
    if (mfa && !code) return setError({ field: 'code', text: "That code didn't work. Check the app and try again; codes change every 30 seconds." })
    setBusy(true)
    setError(null)
    try {
      await confirmApi.confirm({ strong, ...(needPassword ? { password } : {}), ...(mfa ? { code } : {}) })
      onDone(true)
    } catch (err: any) {
      // The third failure ends the sign-in (401): the console shows the signed-out state.
      if (err?.response?.status === 401) return onDone(false)
      setError({
        field: mfa ? 'code' : 'password',
        text: requestErrorMessage(err, "That didn't work. Try again."),
      })
      setBusy(false)
    }
  }

  const err = (field: 'password' | 'code') =>
    error?.field === field && (
      <span className="field-hint err flex items-start gap-1.5" role="alert" style={{ fontSize: 12 }}>
        <Icon name="alert" size={12} className="flex-none mt-0.5" /> {error.text}
      </span>
    )
  const bad = (field: 'password' | 'code') => (error?.field === field ? { borderColor: 'var(--crit)' } : undefined)

  return (
    <Popup open onClose={() => !busy && onDone(false)} title={`Confirm it's you to ${detail.action}`} width={480}>
      <form className="flex flex-col gap-3.5" onSubmit={submit} noValidate>
        <p className="m-0 text-sm text-tx-2 leading-relaxed">
          {strong
            ? 'Changes to your sign-in methods always ask for your password and a second step.'
            : `You haven't confirmed in the last ${detail.window_minutes ?? 10} minutes. Vigil asks again before changes like this.`}
        </p>
        {needPassword && (
          <Field label="Password">
            <PasswordInput
              autoFocus
              autoComplete="current-password"
              aria-invalid={error?.field === 'password'}
              style={bad('password')}
              value={password}
              disabled={busy}
              onChange={(e) => setPassword(e.target.value)}
            />
            {err('password')}
          </Field>
        )}
        {mfa && (
          <Field label="Authenticator or recovery code">
            <input
              className="field-input"
              autoFocus={!needPassword}
              autoComplete="one-time-code"
              inputMode="text"
              maxLength={16}
              placeholder="000000"
              aria-invalid={error?.field === 'code'}
              style={{ height: 36, fontFamily: 'var(--mono, ui-monospace, monospace)', letterSpacing: 4, ...bad('code') }}
              value={code}
              disabled={busy}
              // a 6-digit code, or a recovery code
              onChange={(e) => setCode(e.target.value.replace(/[^a-z0-9]/gi, ''))}
            />
            {err('code')}
          </Field>
        )}
        <div className="flex justify-end gap-2.5 mt-1">
          <button type="button" className="btn ghost" disabled={busy} onClick={() => onDone(false)}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? 'Checking…' : 'Continue'}
          </button>
        </div>
      </form>
    </Popup>
  )
}
