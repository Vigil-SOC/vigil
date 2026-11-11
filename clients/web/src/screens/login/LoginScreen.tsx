import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth, type LoginResult, type MfaOffer } from '../../contexts/AuthContext'
import { basePath } from '../../config/basePath'
import { authApi, bootstrapApi } from '../../services/api'
import { assertWith, cancelPrompt, KeyProblem, NoKeyUsed, SECURITY_KEYS_DOCS } from '../../services/webauthn'
import { Icon } from '../../shared/icons'
import TwoStepDialog, { RecoveryCodes } from '../../shell/TwoStepDialog'
import { KeyWaiting } from '../../shell/SecurityKeyParts'
import AuthLayout from './AuthLayout'
import { requestErrorMessage } from './requestErrorMessage'

// The provider's callback lands on /login?sso_error=<code>; the real reason is on the trail (decision 7).
const SSO_ERRORS: Record<string, string> = {
  failed: "Vigil couldn't sign you in with SSO. Try again. If it keeps failing, tell your Vigil administrator.",
  deactivated: 'Your Vigil account is deactivated. Ask your Vigil administrator to turn it back on.',
  email_not_verified:
    "Your company sign-in hasn't verified your email address, so Vigil can't sign you in. Ask your IT team to verify it.",
}
// A plain navigation: the server answers with the provider's redirect.
const SSO_START = `${basePath}/api/auth/sso/start`

export default function LoginScreen() {
  const navigate = useNavigate()
  const location = useLocation()
  const { login } = useAuth()
  // the address a signed-out person was on; sign-in returns there
  const from = (location.state as { from?: { pathname: string; search?: string; hash?: string } } | null)?.from
  const goBack = () => navigate(from && from.pathname !== '/login' ? { pathname: from.pathname, search: from.search, hash: from.hash } : '/')

  const [usernameOrEmail, setUsernameOrEmail] = useState('')
  const [password, setPassword] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  // fresh recovery codes the sign-in issued; shown once before the console
  const [newCodes, setNewCodes] = useState<string[] | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [showMfa, setShowMfa] = useState(false)
  // the "Use a recovery code" screen, in place of the authenticator code
  const [recovery, setRecovery] = useState(false)
  // what the first call offered (an app, a key), and whether the key prompt is the one showing
  const [offer, setOffer] = useState<MfaOffer | null>(null)
  const [useKey, setUseKey] = useState(false)
  const [keyWaiting, setKeyWaiting] = useState(false)
  // the prompt now waiting; an answer from an earlier one (switched away from, or retried) is dropped
  const prompting = useRef(0)
  // a recovery code removed this person's methods: setup opens over this screen
  const [setupOpen, setSetupOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const ssoError = new URLSearchParams(location.search).get('sso_error')
  const [error, setError] = useState(ssoError ? SSO_ERRORS[ssoError] ?? SSO_ERRORS.failed : '')
  const [ssoEnabled, setSsoEnabled] = useState(false)
  // the reset screen hands off a "password updated" notice, or asks to open
  // the forgot-password view
  const navState = location.state as { notice?: string; forgot?: boolean } | null
  const [notice, setNotice] = useState(navState?.notice ?? '')
  const [forgot, setForgot] = useState(!!navState?.forgot)

  // drop the handoff from history so a reload doesn't replay it
  useEffect(() => {
    if (navState?.notice || navState?.forgot || ssoError) navigate('.', { replace: true, state: from ? { from } : null })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const [resetSent, setResetSent] = useState(false)
  const mfaInputRef = useRef<HTMLInputElement>(null)

  // null until the bootstrap check resolves; true means the instance has no
  // account yet, so show first-account creation instead of sign-in.
  const [needsBootstrap, setNeedsBootstrap] = useState<boolean | null>(null)
  const [email, setEmail] = useState('')
  const [setupCode, setSetupCode] = useState('')

  useEffect(() => {
    if (showMfa) setTimeout(() => mfaInputRef.current?.focus(), 80)
  }, [showMfa])

  useEffect(() => {
    // Fail closed to sign-in: a transient error shouldn't expose account
    // creation on an instance that already has users.
    bootstrapApi
      .status()
      .then((res) => {
        setNeedsBootstrap(res.data.required)
        setSsoEnabled(!!res.data.sso_enabled)
      })
      .catch(() => setNeedsBootstrap(false))
  }, [])

  const handleBootstrap = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await bootstrapApi.create({
        username: usernameOrEmail,
        email,
        password,
        setup_code: setupCode.trim(),
      })
      await login(usernameOrEmail, password)
      goBack()
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not create your account.'))
    } finally {
      setLoading(false)
    }
  }

  const finish = ({ recoveryCodes, mfaRemoved }: LoginResult) => {
    if (mfaRemoved) setSetupOpen(true)
    else if (recoveryCodes?.length) setNewCodes(recoveryCodes)
    else goBack()
  }

  // The 401 offered a key: the browser's prompt comes straight after it (the challenge is short-lived).
  // A cancelled prompt is "no key used": nothing is sent, so nothing counts against the account.
  const askKey = async (o: MfaOffer) => {
    if (!o.securityKey) return
    const id = ++prompting.current
    setKeyWaiting(true)
    setError('')
    try {
      const assertion = await assertWith(o.securityKey.options)
      if (id !== prompting.current) return
      setLoading(true)
      finish(await login(usernameOrEmail, password, undefined, { assertion, challenge: o.securityKey.challenge }))
    } catch (err: any) {
      if (id !== prompting.current) return
      if (err instanceof KeyProblem && !(err instanceof NoKeyUsed)) setError(err.message)
      else if (!(err instanceof NoKeyUsed) && !forcedChange(err))
        setError(requestErrorMessage(err, "That security key didn't work. Try again."))
    } finally {
      setLoading(false)
      if (id === prompting.current) setKeyWaiting(false)
    }
  }

  // A listed, expired or short password is a forced change after the key (or code) is accepted:
  // the one reset path, with the reason.
  const forcedChange = (err: any) => {
    if (err?.message !== 'PASSWORD_CHANGE_REQUIRED') return false
    navigate(`/reset-password?token=${encodeURIComponent(err.resetToken)}`, { state: { reason: err.reason } })
    return true
  }

  // Try again, or switch to the key from a code: repeat call 1 for a fresh challenge.
  const retryKey = async () => {
    const id = ++prompting.current
    setError('')
    setUseKey(true)
    setLoading(true)
    try {
      finish(await login(usernameOrEmail, password))
    } catch (err: any) {
      // left the key view while this was in flight: no prompt
      if (id !== prompting.current) return
      if (err?.message === 'MFA_REQUIRED') {
        setOffer(err.offer)
        void askKey(err.offer)
      } else if (!forcedChange(err)) setError(requestErrorMessage(err, 'Sign in failed. Please try again.'))
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      finish(await login(usernameOrEmail, password, showMfa ? mfaCode : undefined))
    } catch (err: any) {
      if (forcedChange(err)) return
      if (err?.message === 'MFA_REQUIRED') {
        const o: MfaOffer = err.offer
        const key = !!o.securityKey
        const app = o.methods.includes('authenticator_app')
        setOffer(o)
        setShowMfa(true)
        setMfaCode('')
        setUseKey(key)
        setRecovery(false)
        if (key) void askKey(o)
        else setError(!app && o.detail ? o.detail : 'Enter your 2FA code to continue.')
      } else {
        setError(
          requestErrorMessage(
            err,
            err?.response?.status === 401
              ? 'Sign in failed. Check your credentials.'
              : 'Sign in failed. Please try again.',
          ),
        )
      }
    } finally {
      setLoading(false)
    }
  }

  // no app and no usable key (keys are off for this install): nothing to answer with, the server says why
  const app = !!offer?.methods.includes('authenticator_app')
  const noMethod = !!offer && !offer.securityKey && !app

  // off the key view: its prompt ends, and nothing it answers later counts
  const leaveKey = () => {
    prompting.current++
    cancelPrompt()
    setKeyWaiting(false)
    setUseKey(false)
  }

  const backToCredentials = () => {
    leaveKey()
    setShowMfa(false)
    setRecovery(false)
    setOffer(null)
    setMfaCode('')
    setError('')
  }

  const openForgot = () => {
    setForgot(true)
    setResetSent(false)
    setError('')
    setNotice('')
    if (usernameOrEmail.includes('@')) setEmail(usernameOrEmail)
  }

  const closeForgot = () => {
    setForgot(false)
    setError('')
    setNotice('')
  }

  // The server answers identically whether or not the account exists, so
  // show its message verbatim and claim nothing more.
  const handleResetRequest = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await authApi.requestPasswordReset(email.trim())
      setNotice(res.data.message)
      setResetSent(true)
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not request a reset. Check the email and try again.'))
    } finally {
      setLoading(false)
    }
  }

  const toggleRecovery = (on: boolean) => {
    setRecovery(on)
    setMfaCode('')
    setError('')
  }

  const title = newCodes
    ? 'New recovery codes'
    : needsBootstrap
    ? 'Create your account'
    : forgot
      ? 'Reset your password'
      : showMfa
        ? recovery
          ? 'Use a recovery code'
          : 'Two-factor authentication'
        : 'Sign in'
  const subtitle = newCodes
    ? 'Your old recovery codes no longer work. Keep these instead.'
    : needsBootstrap
    ? 'No account exists yet. Set up the first administrator.'
    : forgot
      ? 'Enter the email on your account to request a reset link.'
      : showMfa
        ? recovery
          ? 'This removes your sign-in methods and signs you out everywhere else.'
          : 'Confirm your identity to finish signing in.'
        : ssoEnabled
          ? 'Use your company sign-in or your Vigil password.'
          : 'Authenticate to access the operations console.'

  return (
    <AuthLayout
      screenLabel={forgot ? 'Forgot password' : 'Sign in'}
      overlay={
        setupOpen && (
          <TwoStepDialog
            open
            onClose={goBack}
            note="You signed in with a recovery code, so your authenticator app and recovery codes were removed."
          />
        )
      }
      title={title}
      subtitle={subtitle}
      message={
        error ? { text: error, info: showMfa && !forgot } : { text: notice, info: true }
      }
    >
      {newCodes ? (
        <div className="flex flex-col gap-4">
          <RecoveryCodes codes={newCodes} />
          <button className="btn-signin" type="button" onClick={goBack}>
            Continue
            <Icon name="arrowR" />
          </button>
        </div>
      ) : needsBootstrap ? (
        <form onSubmit={handleBootstrap} autoComplete="on" noValidate>
          <div className="field">
            <label htmlFor="bs-code">Setup code</label>
            <div className="ctrl">
              <Icon name="shield" className="lead" />
              <input
                id="bs-code"
                name="setup-code"
                type="text"
                placeholder="From the API log"
                autoComplete="off"
                autoFocus
                spellCheck={false}
                disabled={loading}
                value={setupCode}
                onChange={(e) => setSetupCode(e.target.value)}
                aria-describedby="bs-code-hint"
              />
            </div>
            <p id="bs-code-hint" className="field-hint">
              The API prints this once the database is ready, while no account
              exists. Read it with docker compose logs (Compose), kubectl logs
              (Helm), or in the ./start.sh terminal.
            </p>
          </div>

          <div className="field">
            <label htmlFor="bs-user">Username</label>
            <div className="ctrl">
              <Icon name="user" className="lead" />
              <input
                id="bs-user"
                name="username"
                type="text"
                placeholder="admin"
                autoComplete="username"
                disabled={loading}
                value={usernameOrEmail}
                onChange={(e) => setUsernameOrEmail(e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="bs-email">Email</label>
            <div className="ctrl">
              <Icon name="send" className="lead" />
              <input
                id="bs-email"
                name="email"
                type="email"
                placeholder="you@company.com"
                autoComplete="email"
                disabled={loading}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="bs-pass">Password</label>
            <div className="ctrl">
              <Icon name="lock" className="lead" />
              <input
                id="bs-pass"
                name="new-password"
                type={showPassword ? 'text' : 'password'}
                placeholder="A few unrelated words"
                autoComplete="new-password"
                disabled={loading}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                className="reveal"
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                title={showPassword ? 'Hide password' : 'Show password'}
              >
                <Icon name="eye" size={17} />
              </button>
            </div>
          </div>

          <button
            className="btn-signin"
            type="submit"
            disabled={loading || !setupCode.trim() || !usernameOrEmail || !email || !password}
          >
            {loading ? (
              <span className="spin" aria-hidden="true" />
            ) : (
              <>
                Create account
                <Icon name="arrowR" />
              </>
            )}
          </button>
        </form>
      ) : forgot ? (
        <form onSubmit={handleResetRequest} autoComplete="on" noValidate>
          {!resetSent && (
            <>
              <div className="field">
                <label htmlFor="reset-email">Email</label>
                <div className="ctrl">
                  <Icon name="send" className="lead" />
                  <input
                    id="reset-email"
                    name="email"
                    type="email"
                    placeholder="analyst@company.com"
                    autoComplete="email"
                    autoFocus
                    disabled={loading}
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                  />
                </div>
              </div>

              <button
                className="btn-signin"
                type="submit"
                disabled={loading || !email.trim()}
              >
                {loading ? (
                  <span className="spin" aria-hidden="true" />
                ) : (
                  <>
                    Send reset link
                    <Icon name="arrowR" />
                  </>
                )}
              </button>
            </>
          )}
          <div className="mfa-back">
            <button type="button" className="link" onClick={closeForgot}>
              ← Back to sign in
            </button>
          </div>
        </form>
      ) : (
      <form onSubmit={handleSubmit} autoComplete="on" noValidate>
        {!showMfa ? (
          <>
            {ssoEnabled && (
              <>
                <a className="btn-sso" href={SSO_START}>
                  <Icon name="key" size={17} />
                  Sign in with SSO
                </a>
                <div className="auth-divider" role="separator">
                  <span>or use your password</span>
                </div>
              </>
            )}
            <div className="field">
              <label htmlFor="auth-user">Username or email</label>
              <div className="ctrl">
                <Icon name="user" className="lead" />
                <input
                  id="auth-user"
                  name="username"
                  type="text"
                  placeholder="analyst@company.com"
                  autoComplete="username"
                  autoFocus
                  disabled={loading}
                  value={usernameOrEmail}
                  onChange={(e) => setUsernameOrEmail(e.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label htmlFor="auth-pass">Password</label>
              <div className="ctrl">
                <Icon name="lock" className="lead" />
                <input
                  id="auth-pass"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  placeholder="••••••••••••"
                  autoComplete="current-password"
                  disabled={loading}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  className="reveal"
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  title={showPassword ? 'Hide password' : 'Show password'}
                >
                  <Icon name="eye" size={17} />
                </button>
              </div>
            </div>

            <div className="row-end">
              <button type="button" className="link" onClick={openForgot}>
                Forgot password?
              </button>
            </div>
          </>
        ) : useKey ? (
          <div className="field">
            <KeyWaiting waiting={keyWaiting}>
              {!keyWaiting && (
                <button type="button" className="link" onClick={() => void retryKey()} disabled={loading}>
                  Try again
                </button>
              )}
              {app && (
                <>
                  <button type="button" className="link" onClick={leaveKey}>
                    Use an authenticator code instead
                  </button>
                  <button
                    type="button"
                    className="link"
                    onClick={() => {
                      leaveKey()
                      setRecovery(true)
                    }}
                  >
                    Use a recovery code
                  </button>
                </>
              )}
            </KeyWaiting>
            <div className="mfa-back">
              <button type="button" className="link" onClick={backToCredentials}>
                ← Back to sign in
              </button>
            </div>
          </div>
        ) : noMethod ? (
          <>
            <div className="mfa-back">
              <a className="link" href={SECURITY_KEYS_DOCS} target="_blank" rel="noreferrer">
                About security keys
              </a>
            </div>
            <div className="mfa-back">
              <button type="button" className="link" onClick={backToCredentials}>
                ← Back to sign in
              </button>
            </div>
          </>
        ) : (
          <div className="field">
            <label htmlFor="auth-mfa">{recovery ? 'Recovery code' : 'Authentication code'}</label>
            <div className="ctrl">
              <Icon name="shield" className="lead" />
              <input
                id="auth-mfa"
                name="mfa"
                type="text"
                autoComplete="one-time-code"
                maxLength={16}
                placeholder={recovery ? '0000000000000000' : '000000'}
                ref={mfaInputRef}
                disabled={loading}
                value={mfaCode}
                onChange={(e) => {
                  // 6-digit authenticator code, 16-character recovery code, or an 8-character
                  // one from before the upgrade
                  const v = e.target.value.replace(/[^a-z0-9]/gi, '')
                  if (v.length <= 16) setMfaCode(v)
                }}
              />
            </div>
            {recovery && (
              <p className="field-hint">
                The code is spent. You will set up two-step sign-in again next, and get a new set of
                recovery codes.
              </p>
            )}
            <div className="mfa-back">
              <button type="button" className="link" onClick={backToCredentials}>
                ← Back to sign in
              </button>
            </div>
            {offer?.methods.includes('security_key') && offer.securityKey && (
              <div className="mfa-back">
                <button type="button" className="link" onClick={() => void retryKey()}>
                  Use a security key or passkey instead
                </button>
              </div>
            )}
            {(recovery ? app : true) && (
              <div className="mfa-back">
                <button type="button" className="link" onClick={() => toggleRecovery(!recovery)}>
                  {recovery ? 'Use my authenticator app instead' : 'Use a recovery code'}
                </button>
              </div>
            )}
          </div>
        )}

        {!(showMfa && (useKey || noMethod)) && (
        <button
          className="btn-signin"
          type="submit"
          disabled={loading || (showMfa && !(recovery ? [16] : [6, 8, 16]).includes(mfaCode.length))}
        >
          {loading ? (
            <span className="spin" aria-hidden="true" />
          ) : (
            <>
              {showMfa ? (recovery ? 'Remove my methods & sign in' : 'Verify & sign in') : 'Sign in'}
              <Icon name="arrowR" />
            </>
          )}
        </button>
        )}
      </form>
      )}
    </AuthLayout>
  )
}
