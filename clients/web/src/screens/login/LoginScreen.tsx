import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { bootstrapApi, passwordResetApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthShell from './AuthShell'
import { requestErrorMessage } from './requestErrorMessage'

export default function LoginScreen() {
  const navigate = useNavigate()
  const { login } = useAuth()
  // set by the reset screen after a successful password change
  const notice = (useLocation().state as { notice?: string } | null)?.notice

  const [usernameOrEmail, setUsernameOrEmail] = useState('')
  const [password, setPassword] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [remember, setRemember] = useState(true)
  const [showMfa, setShowMfa] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const mfaInputRef = useRef<HTMLInputElement>(null)

  // null until the bootstrap check resolves; true means the instance has no
  // account yet, so show first-account creation instead of sign-in.
  const [needsBootstrap, setNeedsBootstrap] = useState<boolean | null>(null)
  const [email, setEmail] = useState('')

  const [forgot, setForgot] = useState(false)
  const [resetEmail, setResetEmail] = useState('')
  const [resetSent, setResetSent] = useState('')

  useEffect(() => {
    if (showMfa) setTimeout(() => mfaInputRef.current?.focus(), 80)
  }, [showMfa])

  useEffect(() => {
    // Fail closed to sign-in: a transient error shouldn't expose account
    // creation on an instance that already has users.
    bootstrapApi
      .status()
      .then((res) => setNeedsBootstrap(res.data.required))
      .catch(() => setNeedsBootstrap(false))
  }, [])

  const handleBootstrap = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await bootstrapApi.create({ username: usernameOrEmail, email, password })
      await login(usernameOrEmail, password)
      navigate('/')
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not create your account.'))
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(usernameOrEmail, password, showMfa ? mfaCode : undefined)
      navigate('/')
    } catch (err: any) {
      if (err?.message === 'MFA_REQUIRED') {
        setShowMfa(true)
        setMfaCode('')
        setError('Enter your 2FA code to continue.')
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

  const handleForgot = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await passwordResetApi.request({ email: resetEmail })
      setResetSent(res.data.message)
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not request a reset. Please try again.'))
    } finally {
      setLoading(false)
    }
  }

  const closeForgot = () => {
    setForgot(false)
    setResetSent('')
    setError('')
  }

  const backToCredentials = () => {
    setShowMfa(false)
    setMfaCode('')
    setError('')
  }

  const forgotView = forgot && !needsBootstrap

  return (
    <AuthShell
      screenLabel="Sign in"
      title={
        needsBootstrap
          ? 'Create your account'
          : forgotView
            ? 'Reset your password'
            : showMfa
              ? 'Two-factor authentication'
              : 'Sign in'
      }
      subtitle={
        needsBootstrap
          ? 'No account exists yet. Set up the first administrator.'
          : forgotView
            ? 'Enter the email on your account to request a reset link.'
            : showMfa
              ? 'Confirm your identity to finish signing in.'
              : 'Authenticate to access the operations console.'
      }
    >
            {error && (
              <div className={`auth-error${showMfa ? ' info' : ''}`} role="alert">
                <Icon name={showMfa ? 'info' : 'alert'} size={15} />
                <span>{error}</span>
              </div>
            )}

            {notice && !error && !forgotView && !showMfa && !needsBootstrap && (
              <div className="auth-error info" role="status">
                <Icon name="info" size={15} />
                <span>{notice}</span>
              </div>
            )}

            {resetSent && forgotView && (
              <div className="auth-error info" role="status">
                <Icon name="info" size={15} />
                <span>{resetSent}</span>
              </div>
            )}

            {forgotView ? (
              <form onSubmit={handleForgot} autoComplete="on" noValidate>
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
                          placeholder="you@company.com"
                          autoComplete="email"
                          autoFocus
                          disabled={loading}
                          value={resetEmail}
                          onChange={(e) => setResetEmail(e.target.value)}
                        />
                      </div>
                    </div>

                    <button
                      className="btn-signin"
                      type="submit"
                      disabled={loading || !resetEmail.trim()}
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
            ) : needsBootstrap ? (
              <form onSubmit={handleBootstrap} autoComplete="on" noValidate>
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
                      autoFocus
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
                      placeholder="At least 12 characters"
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
                  disabled={loading || !usernameOrEmail || !email || !password}
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
            ) : (
            <form onSubmit={handleSubmit} autoComplete="on" noValidate>
              {!showMfa ? (
                <>
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

                  <div className="row-between">
                    <label className="remember">
                      <input
                        type="checkbox"
                        checked={remember}
                        onChange={(e) => setRemember(e.target.checked)}
                      />
                      <span className="box">
                        <Icon name="check2" />
                      </span>
                      Keep me signed in
                    </label>
                    <button
                      type="button"
                      className="link"
                      onClick={() => {
                        setError('')
                        setForgot(true)
                      }}
                    >
                      Forgot password?
                    </button>
                  </div>
                </>
              ) : (
                <div className="field">
                  <label htmlFor="auth-mfa">Authentication code</label>
                  <div className="ctrl">
                    <Icon name="shield" className="lead" />
                    <input
                      id="auth-mfa"
                      name="mfa"
                      type="text"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      maxLength={6}
                      placeholder="000000"
                      ref={mfaInputRef}
                      disabled={loading}
                      value={mfaCode}
                      onChange={(e) => {
                        const v = e.target.value.replace(/\D/g, '')
                        if (v.length <= 6) setMfaCode(v)
                      }}
                    />
                  </div>
                  <div className="mfa-back">
                    <button type="button" className="link" onClick={backToCredentials}>
                      ← Back to sign in
                    </button>
                  </div>
                </div>
              )}

              <button
                className="btn-signin"
                type="submit"
                disabled={loading || (showMfa && mfaCode.length !== 6)}
              >
                {loading ? (
                  <span className="spin" aria-hidden="true" />
                ) : (
                  <>
                    {showMfa ? 'Verify & sign in' : 'Sign in'}
                    <Icon name="arrowR" />
                  </>
                )}
              </button>
            </form>
            )}
    </AuthShell>
  )
}
