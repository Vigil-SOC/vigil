import { useEffect, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import QRCode from 'qrcode'
import { useAuth } from '../contexts/AuthContext'
import { keysApi, mfaApi, type Answer, type Factor } from '../services/api'
import { cancelPrompt, confirmWithKey, createCredential, keysSupported, KeyProblem } from '../services/webauthn'
import { Icon } from '../shared/icons'
import MfaMethodsList, { type MfaMethod } from '../shared/MfaMethodsList'
import { Field, PasswordInput, Popup, TextInput } from '../shared/ui'
import { confirmAtProvider, dropProviderReturn, proofLive, proofRefused, providerReturn } from './providerProof'
import { KEY_AFTER_CONTINUE, KeysUnavailable } from './SecurityKeyParts'

type View = 'main' | 'enrol' | 'codes' | 'regenerate' | 'disable' | 'addKey' | 'removeKey'

function errText(e: unknown, fallback: string): string {
  if (e instanceof KeyProblem) return e.message
  const err = e as { response?: { status?: number; data?: { detail?: unknown } }; message?: string }
  if (err.response?.status === 429) return 'Too many attempts. Wait a minute and try again.'
  const detail = err.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

export function RecoveryCodes({ codes }: { codes: string[] }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    void navigator.clipboard?.writeText(codes.join('\n')).then(() => setCopied(true))
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([codes.join('\n') + '\n'], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'vigil-recovery-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="settings-banner info">
        <Icon name="alert" size={14} />
        Save these now. Each code works once, and they cannot be shown again.
      </div>
      <ul
        className="grid grid-cols-1 min-[480px]:grid-cols-2 gap-2 m-0 p-3 list-none bg-bg-2 border border-line rounded-sm font-mono text-[13px] text-tx"
        aria-label="Your recovery codes"
      >
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className="flex gap-2.5">
        <button className="btn ghost" onClick={copy}>
          <Icon name="copy" size={14} /> {copied ? 'Copied' : 'Copy'}
        </button>
        <button className="btn ghost" onClick={download}>
          <Icon name="download" size={14} /> Download
        </button>
      </div>
    </div>
  )
}

// required: the MFA requirement holds this person here before anything else. Only the
// setup steps are offered, signing out is the way out, and the console opens on Done.
export default function TwoStepDialog({
  open,
  onClose,
  required = false,
  note,
}: {
  open: boolean
  onClose: () => void
  required?: boolean
  /** why it opened, above the setup prompt (a privileged change needs it) */
  note?: string
}) {
  const { user, refreshUser, logout } = useAuth()
  const enabled = !!user?.mfa_enabled
  const [view, setView] = useState<View>('main')
  const [secret, setSecret] = useState('')
  const [qr, setQr] = useState('')
  const [codes, setCodes] = useState<string[]>([])
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // your own methods, listed on the "on" screen
  const [methods, setMethods] = useState<MfaMethod[]>([])
  const [keyName, setKeyName] = useState('')
  // the key being removed
  const [target, setTarget] = useState<MfaMethod | null>(null)
  // answering a check with a key instead of a code (people with an app and a key choose)
  const [viaKey, setViaKey] = useState(false)
  // The first method saved while Require MFA holds the person here. Unset for a
  // voluntary setup and for someone who already has a method.
  const [justAdded, setJustAdded] = useState<'app' | 'key' | null>(null)
  // Signs in only through SSO: confirming at the provider stands in for the password (#4756).
  const viaSso = !!user?.sso_only
  const provider = user?.sso_provider || 'your SSO provider'
  const [proof, setProof] = useState<string | null>(null)
  const confirmed = viaSso && proofLive(proof)
  const answer: Answer = viaSso ? { sso_proof: proof ?? undefined } : { password }
  const answered = viaSso ? confirmed : !!password
  const location = useLocation()
  const navigate = useNavigate()

  // Keys are offered only when this install can use them (the server's reason is shown otherwise).
  const keys = user?.security_keys
  const keysOn = !!keys?.available && keysSupported()
  const keyRows = methods.filter((m) => m.type === 'security_key')
  const hasApp = methods.some((m) => m.type === 'authenticator_app')
  const canKey = keysOn && keyRows.length > 0
  // a method saved in this required setup counts, before the user is refreshed
  const methodOn = enabled || justAdded !== null
  // a person with only keys has no code to type; so does adding the app beside a key just saved
  const keyNow = justAdded === 'key' || (canKey && (!hasApp || viaKey))
  // Required first setup only, and only when this install has another method to offer.
  const offerOther: 'key' | 'app' | null =
    required && justAdded === 'app' && keysOn
      ? 'key'
      : required && justAdded === 'key' && !hasApp
        ? 'app'
        : null

  useEffect(() => {
    // every view: a step reopened on the way back from the provider needs the methods too
    if (open && enabled) {
      mfaApi
        .methods()
        .then((res) => setMethods(res.data))
        .catch(() => setMethods([]))
    }
  }, [open, enabled, view])

  useEffect(() => {
    if (!open) {
      setView('main')
      setSecret('')
      setQr('')
      setCodes([])
      setCode('')
      setPassword('')
      setError(null)
      setKeyName('')
      setTarget(null)
      setViaKey(false)
      setJustAdded(null)
      setProof(null)
      dropProviderReturn()
      cancelPrompt()
    }
  }, [open])

  // Back from confirming at the provider: reopen the step that asked, holding the proof.
  // Idempotent, so a remount while the page loads lands in the same place.
  useEffect(() => {
    if (!open) return
    const back = providerReturn((search) =>
      navigate({ pathname: location.pathname, search, hash: location.hash }, { replace: true }),
    )
    if (!back) return
    if (!back.proof) {
      setError(`Vigil couldn't confirm it's you at ${provider}. Try again, signing in as yourself.`)
      return
    }
    setProof(back.proof)
    const step = back.step
    setJustAdded(step?.justAdded ?? null)
    // enrol reopens on the first screen, where Set up now starts it: the new secret is made once
    if (step && ['regenerate', 'disable', 'addKey', 'removeKey'].includes(step.view)) {
      setView(step.view as View)
      setKeyName(step.keyName ?? '')
      setTarget(step.target ?? null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // Off to the provider; the dialog comes back at this step.
  const leave = (step: View) => {
    setBusy(true)
    confirmAtProvider({ view: step, keyName, target, justAdded })
  }

  const go = (next: View) => {
    setView(next)
    setCode('')
    setPassword('')
    setError(null)
    setViaKey(false)
  }

  // Runs one API call with shared busy/error handling; returns undefined on failure.
  const run = async <T,>(call: () => Promise<T>, fallback: string): Promise<T | undefined> => {
    setBusy(true)
    setError(null)
    try {
      return await call()
    } catch (e) {
      const text = errText(e, fallback)
      // a proof the server no longer takes: offer the provider again
      if (viaSso && proofRefused(text)) setProof(null)
      setError(text)
      return undefined
    } finally {
      setBusy(false)
    }
  }

  const startSetup = async () => {
    const res = await run(() => mfaApi.setup(), 'Could not start setup.')
    if (!res) return
    // The otpauth URI carries the secret: render it here, never via an external QR service.
    const dataUrl = await QRCode.toDataURL(res.data.qr_uri, { margin: 2, width: 176 }).catch(() => '')
    setSecret(res.data.secret)
    setQr(dataUrl)
    go('enrol')
  }

  const verify = async (e: FormEvent) => {
    e.preventDefault()
    const besideKey = justAdded === 'key'
    const res = await run(
      async () => mfaApi.verify(code.trim(), answer, besideKey ? await factor() : {}),
      'Invalid MFA code',
    )
    if (!res) return
    // The app beside a key just saved: the codes were already shown. Open the console.
    if (required && justAdded) {
      await refreshUser()
      return
    }
    const issued = res.data.recovery_codes ?? []
    // None issued (a break-glass account keeps the safe's codes): nothing to show.
    if (!issued.length) {
      await refreshUser()
      go('main')
      return
    }
    if (required && !enabled) setJustAdded('app')
    setCodes(issued)
    go('codes')
    // Required: the console must not open before the recovery codes have been seen.
    if (!required) await refreshUser()
  }

  // The strong check's second step: a code, or a key's assertion (the prompt comes after Continue).
  const factor = async (): Promise<Factor> => {
    if (!keyNow) return { code: code.trim() }
    const { assertion, challenge } = await confirmWithKey()
    return { assertion, challenge }
  }

  const regenerate = async (e: FormEvent) => {
    e.preventDefault()
    const res = await run(async () => mfaApi.regenerateRecoveryCodes(answer, await factor()), 'Invalid MFA code')
    if (!res) return
    setCodes(res.data.recovery_codes)
    go('codes')
  }

  const disable = async (e: FormEvent) => {
    e.preventDefault()
    const res = await run(async () => mfaApi.disable(answer, await factor()), 'Invalid MFA code')
    if (!res) return
    await refreshUser()
    go('main')
  }

  const addKey = async (e: FormEvent) => {
    e.preventDefault()
    const res = await run(async () => {
      // a first method needs only the password; after that, the strong check
      const check = methodOn ? await factor() : {}
      const { data: reg } = await keysApi.registrationOptions()
      const credential = await createCredential(reg.options)
      return keysApi.add({ name: keyName.trim(), credential, registration_challenge: reg.challenge, ...answer, ...check })
    }, 'Could not add the key.')
    if (!res) return
    const issued = res.data.recovery_codes ?? []
    if (issued.length) {
      if (required && !enabled && !justAdded) setJustAdded('key')
      setCodes(issued)
      go('codes')
      if (!required) await refreshUser()
      return
    }
    // A key beside the app just saved during required setup: the console opens.
    if (required && justAdded) {
      await refreshUser()
      return
    }
    go('main')
  }

  const removeKey = async (e: FormEvent) => {
    e.preventDefault()
    if (!target?.key_id) return
    const id = target.key_id
    const res = await run(async () => keysApi.remove(id, answer, await factor()), 'Could not remove the key.')
    if (!res) return
    // the last key of a key-only person turns two-step sign-in off
    await refreshUser()
    go('main')
  }

  // The first half of the check: the password, or for an SSO-only person the provider.
  const passwordField = !viaSso ? (
    <Field label="Password">
      <PasswordInput
        autoComplete="current-password"
        value={password}
        disabled={busy}
        onChange={(e) => setPassword(e.target.value)}
      />
    </Field>
  ) : confirmed ? (
    <div className="settings-banner ok">
      <Icon name="check" size={14} /> Confirmed at {provider}. Finish within 5 minutes.
    </div>
  ) : (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-sm text-tx-2 leading-relaxed">
        You sign in through SSO, so you confirm at {provider} in place of a password. You'll come back here.
      </p>
      {/* wraps: a provider's host can be long */}
      <button type="button" className="btn ghost self-start max-w-full break-all" disabled={busy} onClick={() => leave(view)}>
        Confirm at {provider}
      </button>
    </div>
  )
  // What every method change asks, in the help text.
  const asks = viaSso ? `you to confirm at ${provider}` : 'for your password'

  // The factor half of a strong check: a code, or the key prompt that follows Continue.
  const factorField = keyNow ? (
    <div className="flex flex-col gap-1.5">
      <p className="m-0 text-sm text-tx-2 leading-relaxed">{KEY_AFTER_CONTINUE}</p>
      {hasApp && (
        <button type="button" className="data-link self-start" style={{ padding: 0 }} onClick={() => setViaKey(false)}>
          Use an authenticator code instead
        </button>
      )}
    </div>
  ) : (
    <div className="flex flex-col gap-1.5">
      <Field
        label="Authentication or recovery code"
        hint="Enter a current code from your authenticator app, or an unused recovery code."
      >
        <TextInput value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" autoFocus />
      </Field>
      {canKey && (
        <button type="button" className="data-link self-start" style={{ padding: 0 }} onClick={() => setViaKey(true)}>
          Use a security key or passkey instead
        </button>
      )}
    </div>
  )
  const strongReady = answered && (keyNow || !!code.trim())
  const strongBack = (
    <button type="button" className="btn ghost" onClick={() => go('main')} disabled={busy}>
      Back
    </button>
  )

  const banner = error && (
    <div className="settings-banner err" role="alert">
      <Icon name="alert" size={14} /> {error}
    </div>
  )

  const title =
    view === 'enrol'
      ? 'Set up two-step sign-in'
      : view === 'codes'
        ? 'Recovery codes'
        : view === 'regenerate'
          ? 'Regenerate recovery codes'
          : view === 'disable'
            ? keyRows.length
              ? 'Remove authenticator app'
              : 'Turn off two-step sign-in'
            : view === 'addKey'
              ? 'Add a security key'
              : view === 'removeKey'
                ? 'Remove a security key'
                : 'Two-step sign-in'

  return (
    <Popup open={open} onClose={onClose} title={title} width={460} dismissible={!required}>
      <div className="flex flex-col gap-3.5">
        {banner}

        {view === 'main' && !enabled && (
          <>
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              {note ? `${note} ` : ''}
              {required
                ? 'Two-step sign-in is required for your account. '
                : 'Add a second step to signing in: '}
              {keysOn
                ? 'A 6-digit code from an authenticator app, or a security key or passkey, '
                : 'A 6-digit code from an authenticator app, '}
              {viaSso
                ? `in addition to signing in at ${provider}. You confirm there first.`
                : 'in addition to your password.'}
            </p>
            {keys && !keysOn && keys.available === false && <KeysUnavailable reason={keys.reason} />}
            {confirmed && passwordField}
            <div className="flex flex-wrap justify-end gap-2.5">
              {required && (
                <button className="btn ghost" onClick={() => void logout()}>
                  Sign out
                </button>
              )}
              {keysOn && (
                <button className="btn ghost" onClick={() => go('addKey')} disabled={busy}>
                  <Icon name="key" size={14} /> Add a security key
                </button>
              )}
              <button
                className="btn primary"
                onClick={viaSso && !confirmed ? () => leave('enrol') : startSetup}
                disabled={busy}
              >
                {busy ? 'Working…' : 'Set up'}
              </button>
            </div>
          </>
        )}

        {view === 'main' && enabled && (
          <>
            <div className="settings-banner ok">
              <Icon name="shield" size={14} /> Two-step sign-in is on.
            </div>
            <MfaMethodsList
              methods={methods}
              onRemove={keysOn ? (m) => { setTarget(m); go('removeKey') } : undefined}
            />
            {keys && keys.available === false && <KeysUnavailable reason={keys.reason} />}
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              If you lose your authenticator, a recovery code lets you sign in once. Regenerating makes
              the old codes stop working.
            </p>
            <div className="flex flex-wrap justify-end gap-2.5">
              {keysOn && (
                <button className="btn ghost" onClick={() => go('addKey')} disabled={busy}>
                  <Icon name="key" size={14} /> Add a security key
                </button>
              )}
              <button className="btn ghost" onClick={() => go('regenerate')} disabled={busy}>
                Regenerate recovery codes
              </button>
              {/* a person with only keys removes them one by one; the last one turns it off. With keys that can't be used now, the app stays. */}
              {((hasApp && (keysOn || !keyRows.length)) || methods.length === 0) && (
                <button className="btn danger" onClick={() => go('disable')} disabled={busy}>
                  {keyRows.length ? 'Remove authenticator app' : 'Turn off'}
                </button>
              )}
            </div>
          </>
        )}

        {view === 'enrol' && (
          <form className="flex flex-col gap-3.5" onSubmit={verify}>
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              Scan this with an authenticator app, or type the key in by hand. Then enter the 6-digit code
              it shows.
            </p>
            <div className="flex flex-col items-center gap-2.5">
              {qr && (
                <img
                  src={qr}
                  width={176}
                  height={176}
                  alt="QR code for your authenticator app"
                  className="rounded-sm bg-white"
                />
              )}
              <code
                className="font-mono text-[13px] text-tx tracking-wider break-all text-center select-all"
                aria-label="Setup key"
              >
                {secret}
              </code>
            </div>
            <Field label="Authentication code">
              <TextInput
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                autoFocus
              />
            </Field>
            {passwordField}
            {justAdded === 'key' && factorField}
            <div className="flex justify-end gap-2.5">
              <button
                type="button"
                className="btn ghost"
                onClick={justAdded ? () => go('codes') : required ? () => void logout() : onClose}
                disabled={busy}
              >
                {justAdded ? 'Back' : required ? 'Sign out' : 'Cancel'}
              </button>
              <button type="submit" className="btn primary" disabled={busy || code.trim().length < 6 || !answered}>
                {busy ? 'Verifying…' : 'Turn on'}
              </button>
            </div>
          </form>
        )}

        {view === 'codes' && (
          <>
            <RecoveryCodes codes={codes} />
            {offerOther === 'key' && (
              <p className="m-0 text-sm text-tx-2 leading-relaxed">
                Add a security key or passkey too, so you can still sign in if you lose the app. You can skip
                this and add one later.
              </p>
            )}
            {offerOther === 'app' && (
              <p className="m-0 text-sm text-tx-2 leading-relaxed">
                Add an authenticator app too, so you can still sign in if you lose the key. You can skip this
                and add one later.
              </p>
            )}
            <div className="flex flex-wrap justify-end gap-2.5">
              {offerOther ? (
                <>
                  <button className="btn ghost" onClick={() => void refreshUser()} disabled={busy}>
                    Skip
                  </button>
                  <button
                    className="btn primary"
                    disabled={busy}
                    onClick={offerOther === 'key' ? () => go('addKey') : () => void startSetup()}
                  >
                    {offerOther === 'key' ? (
                      <>
                        <Icon name="key" size={14} /> Add a security key
                      </>
                    ) : (
                      'Set up'
                    )}
                  </button>
                </>
              ) : (
                <button className="btn primary" onClick={required ? () => void refreshUser() : onClose}>
                  Done
                </button>
              )}
            </div>
          </>
        )}

        {(view === 'regenerate' || view === 'disable') && (
          <form className="flex flex-col gap-3.5" onSubmit={view === 'regenerate' ? regenerate : disable}>
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              Changes to your sign-in methods always ask {asks} and a second step.
              {view === 'regenerate'
                ? ' Your current recovery codes will stop working; new codes are shown once.'
                : ''}{' '}
              Your other sign-ins end.
            </p>
            {passwordField}
            {factorField}
            <div className="flex justify-end gap-2.5">
              {strongBack}
              <button
                type="submit"
                className={view === 'regenerate' ? 'btn primary' : 'btn danger'}
                disabled={busy || !strongReady}
              >
                {busy ? 'Working…' : view === 'regenerate' ? 'Regenerate' : keyRows.length ? 'Remove app' : 'Turn off'}
              </button>
            </div>
          </form>
        )}

        {view === 'addKey' && (
          <form className="flex flex-col gap-3.5" onSubmit={addKey}>
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              {methodOn
                ? `Changes to your sign-in methods always ask ${asks} and a second step. Adding a key ends no sign-in.`
                : `${viaSso ? `Confirming at ${provider}` : 'Your password'} is all that is needed to add your first method. Your browser then asks for the key.`}
            </p>
            <Field label="Key name" hint="Your admin can see this name.">
              <TextInput
                value={keyName}
                onChange={(e) => setKeyName(e.target.value)}
                maxLength={100}
                placeholder="e.g. YubiKey 5"
                autoFocus
              />
            </Field>
            {passwordField}
            {methodOn && factorField}
            <div className="flex flex-wrap justify-end gap-2.5">
              {justAdded ? (
                <button type="button" className="btn ghost" onClick={() => go('codes')} disabled={busy}>
                  Back
                </button>
              ) : (
                strongBack
              )}
              <button type="submit" className="btn primary" disabled={busy || !(methodOn ? strongReady : answered)}>
                {busy ? 'Working…' : 'Continue'}
              </button>
            </div>
          </form>
        )}

        {view === 'removeKey' && target && (
          <form className="flex flex-col gap-3.5" onSubmit={removeKey}>
            <div className="settings-banner info">
              <Icon name="alert" size={14} />
              {keyRows.length === 1 && !hasApp
                ? 'Removing your last method turns two-step sign-in off, and signs you out of your other sign-ins.'
                : 'Removing it signs you out of your other sign-ins. This sign-in stays open.'}
            </div>
            <p className="m-0 text-sm text-tx-2 leading-relaxed">
              Changes to your sign-in methods always ask {asks} and a second step.
            </p>
            {passwordField}
            {factorField}
            <div className="flex justify-end gap-2.5">
              {strongBack}
              <button type="submit" className="btn danger" disabled={busy || !strongReady}>
                {busy ? 'Working…' : `Remove ${target.name}`}
              </button>
            </div>
          </form>
        )}
      </div>
    </Popup>
  )
}
