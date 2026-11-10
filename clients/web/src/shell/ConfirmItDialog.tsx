import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { requestErrorMessage } from '../screens/login/requestErrorMessage'
import { confirmApi, mfaApi } from '../services/api'
import { cancelPrompt, confirmWithKey, KeyProblem, keysSupported, NoKeyUsed } from '../services/webauthn'
import { CONFIRMATION_REQUIRED, registerStepUp, type StepUpDetail } from '../services/stepUp'
import { Icon } from '../shared/icons'
import { Field, PasswordInput, Popup } from '../shared/ui'
import TwoStepDialog from './TwoStepDialog'
import { KEY_AFTER_CONTINUE, KeyWaiting } from './SecurityKeyParts'

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

/** "Confirm it's you to …": a current code or a key touch (the password with no MFA); strong adds the password. */
function ConfirmIt({ detail, mfa, onDone }: { detail: StepUpDetail; mfa: boolean; onDone: (ok: boolean) => void }) {
  const strong = detail.variant === 'strong'
  const needPassword = strong || !mfa
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<{ field: 'password' | 'code'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  // Keys: only where this install offers them and the person has one. A person with an app
  // and a key chooses; one with only keys has no code to type.
  const { user } = useAuth()
  const keysOn = mfa && !!user?.security_keys?.available && keysSupported()
  const [held, setHeld] = useState<{ keys: boolean; app: boolean }>({ keys: false, app: true })
  useEffect(() => {
    if (!keysOn) return
    void mfaApi
      .methods()
      .then((res) =>
        setHeld({
          keys: res.data.some((m) => m.type === 'security_key'),
          app: res.data.some((m) => m.type === 'authenticator_app'),
        }),
      )
      .catch(() => {})
  }, [keysOn])
  const [viaKey, setViaKey] = useState(false)
  const keyNow = keysOn && held.keys && (!held.app || viaKey)
  // a one-factor key touch waits in the dialog; a strong one happens after Continue
  const [waiting, setWaiting] = useState(false)
  const attempt = useRef(0)

  // The wrong answer, or the third in a row (401), which ends the sign-in: the console shows it signed out.
  const fail = (err: any, field: 'password' | 'code') => {
    if (err?.response?.status === 401) return onDone(false)
    setError({ field, text: requestErrorMessage(err, "That didn't work. Try again.") })
    setBusy(false)
  }

  const touch = async () => {
    const id = ++attempt.current
    setWaiting(true)
    setError(null)
    try {
      const { assertion, challenge } = await confirmWithKey()
      if (id !== attempt.current) return
      await confirmApi.confirm({ strong: false, assertion, challenge })
      onDone(true)
    } catch (err: any) {
      if (id !== attempt.current) return
      // a cancelled prompt is no key used: nothing was sent
      if (err instanceof KeyProblem && !(err instanceof NoKeyUsed)) setError({ field: 'code', text: err.message })
      else if (!(err instanceof NoKeyUsed)) fail(err, 'code')
      setWaiting(false)
    }
  }
  // closing the dialog ends a prompt still waiting
  useEffect(
    () => () => {
      attempt.current++
      cancelPrompt()
    },
    [],
  )
  // one-factor with a key: the prompt starts as the dialog (or the choice) opens
  useEffect(() => {
    if (!strong && keyNow) void touch()
    else {
      attempt.current++
      cancelPrompt()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strong, keyNow])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (needPassword && !password) return setError({ field: 'password', text: 'Enter your password.' })
    if (mfa && !keyNow && !code) return setError({ field: 'code', text: "That code didn't work. Check the app and try again; codes change every 30 seconds." })
    setBusy(true)
    setError(null)
    try {
      const factor = !mfa ? {} : keyNow ? await confirmWithKey() : { code }
      await confirmApi.confirm({ strong, ...(needPassword ? { password } : {}), ...factor })
      onDone(true)
    } catch (err: any) {
      if (err instanceof KeyProblem) {
        setError({ field: 'code', text: err.message })
        return setBusy(false)
      }
      fail(err, mfa ? 'code' : 'password')
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
        {!strong && keyNow ? (
          <KeyWaiting waiting={waiting}>
            {!waiting && (
              <button type="button" className="data-link" onClick={() => void touch()}>
                Try again
              </button>
            )}
            {held.app && (
              <button type="button" className="data-link" onClick={() => setViaKey(false)}>
                Use an authenticator code instead
              </button>
            )}
          </KeyWaiting>
        ) : null}
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
        {mfa && keyNow && strong && <p className="m-0 text-sm text-tx-2 leading-relaxed">{KEY_AFTER_CONTINUE}</p>}
        {mfa && keyNow && strong && held.app && (
          <button type="button" className="data-link self-start" style={{ padding: 0 }} onClick={() => setViaKey(false)}>
            Use an authenticator code instead
          </button>
        )}
        {mfa && !keyNow && (
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
        {mfa && !keyNow && keysOn && held.keys && (
          <button type="button" className="data-link self-start" style={{ padding: 0 }} onClick={() => setViaKey(true)}>
            Use a security key or passkey instead
          </button>
        )}
        {keyNow && err('code')}
        <div className="flex justify-end gap-2.5 mt-1">
          <button type="button" className="btn ghost" disabled={busy} onClick={() => onDone(false)}>
            Cancel
          </button>
          {(strong || !keyNow) && (
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? 'Checking…' : 'Continue'}
            </button>
          )}
        </div>
      </form>
    </Popup>
  )
}
