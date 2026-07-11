/* ============================================================
   Login / auth screen — split brand + form layout, ported from
   the design's "SOC Login.html" handoff into the redesign token
   system so it themes (dark/light) and accents identically to the
   console. Wired to the real auth flow (useAuth().login), mirroring
   pages/Login.tsx — including the MFA step — and lands in the
   redesign console on success.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../../styles.css'
import { useAuth } from '../../../contexts/AuthContext'
import { Icon } from '../../shared/icons'
import { VigilLogo } from '../../shared/VigilLogo'
import { accentVars } from '../../shell/accent'
import { RedesignThemeProvider, useSocTheme } from '../../shell/theme'

export default function LoginScreen() {
  // the auth screen lives outside the console shell, so it brings its own
  // theme provider (mode + accent) — same source of truth the console reads.
  return (
    <RedesignThemeProvider>
      <LoginInner />
    </RedesignThemeProvider>
  )
}

function LoginInner() {
  const navigate = useNavigate()
  const { login } = useAuth()
  const { mode, setMode, accent } = useSocTheme()

  const [usernameOrEmail, setUsernameOrEmail] = useState('')
  const [password, setPassword] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [remember, setRemember] = useState(true)
  const [showMfa, setShowMfa] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const mfaInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (showMfa) setTimeout(() => mfaInputRef.current?.focus(), 80)
  }, [showMfa])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(usernameOrEmail, password, showMfa ? mfaCode : undefined)
      // land in the SOC console (the primary surface)
      navigate('/dashboard')
    } catch (err: any) {
      if (err?.message === 'MFA_REQUIRED') {
        setShowMfa(true)
        setMfaCode('')
        setError('Enter your 2FA code to continue.')
      } else {
        setError(err?.response?.data?.detail || 'Sign in failed. Check your credentials.')
      }
    } finally {
      setLoading(false)
    }
  }

  const backToCredentials = () => {
    setShowMfa(false)
    setMfaCode('')
    setError('')
  }

  return (
    <div
      className="soc-console auth-root"
      data-theme={mode}
      style={accentVars(accent.a, accent.b)}
    >
      <div className="auth" data-screen-label="Sign in">
        <button
          className="auth-theme"
          type="button"
          onClick={() => setMode(mode === 'dark' ? 'light' : 'dark')}
          aria-label={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          title={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          <Icon name={mode === 'dark' ? 'sun' : 'moon'} size={17} />
        </button>

        {/* ---------- brand panel ---------- */}
        <section className="brand">
          <div className="brand-top">
            <VigilLogo className="auth-logo" />
          </div>

          <div className="brand-body">
            <h1>Security operations, under one calm pane of glass.</h1>
            <p>
              Triage findings, correlate signals across your estate, and let Vigil
              surface what actually needs an analyst — before it becomes an incident.
            </p>
          </div>
        </section>

        {/* ---------- form panel ---------- */}
        <section className="auth-pane">
          <div className="form-wrap">
            <header>
              <h2>{showMfa ? 'Two-factor authentication' : 'Sign in'}</h2>
              <p>
                {showMfa
                  ? 'Confirm your identity to finish signing in.'
                  : 'Authenticate to access the operations console.'}
              </p>
            </header>

            {error && (
              <div className={`auth-error${showMfa ? ' info' : ''}`} role="alert">
                <Icon name={showMfa ? 'info' : 'alert'} size={15} />
                <span>{error}</span>
              </div>
            )}

            <form onSubmit={handleSubmit} autoComplete="on" noValidate>
              {!showMfa ? (
                <>
                  <div className="field">
                    <label htmlFor="auth-user">Username or email</label>
                    <div className="ctrl">
                      <Icon name="name" className="lead" />
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
                    <a
                      className="link"
                      href="#"
                      onClick={(e) => e.preventDefault()}
                    >
                      Forgot password?
                    </a>
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
          </div>
        </section>
      </div>
    </div>
  )
}
