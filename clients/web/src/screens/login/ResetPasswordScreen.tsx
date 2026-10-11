import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { bootstrapApi, passwordResetApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthShell from './AuthShell'
import { requestErrorMessage } from './requestErrorMessage'

export default function ResetPasswordScreen() {
  const navigate = useNavigate()
  const token = useSearchParams()[0].get('token') ?? ''

  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // A fresh session has no csrf_token cookie; any GET seeds it, and the
  // confirm POST is rejected without it.
  const csrfSeed = useRef<Promise<unknown>>(Promise.resolve())
  useEffect(() => {
    csrfSeed.current = bootstrapApi.status().catch(() => undefined)
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await csrfSeed.current
      await passwordResetApi.confirm({ token, new_password: password })
      navigate('/login', {
        replace: true,
        state: { notice: 'Your password has been reset. Sign in with your new password.' },
      })
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not reset your password. Please try again.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthShell
      screenLabel="Reset password"
      title={token ? 'Choose a new password' : 'Reset link incomplete'}
      subtitle={
        token
          ? 'Set a new password for your account.'
          : 'This link is missing its reset token.'
      }
    >
      {!token ? (
        <>
          <div className="auth-error" role="alert">
            <Icon name="alert" size={15} />
            <span>Open the full link from your reset email, or request a new one.</span>
          </div>
          <Link className="link" to="/login">
            ← Back to sign in
          </Link>
        </>
      ) : (
        <form onSubmit={handleSubmit} autoComplete="on" noValidate>
          {error && (
            <div className="auth-error" role="alert">
              <Icon name="alert" size={15} />
              <span>{error}</span>
            </div>
          )}

          <div className="field">
            <label htmlFor="reset-pass">New password</label>
            <div className="ctrl">
              <Icon name="lock" className="lead" />
              <input
                id="reset-pass"
                name="new-password"
                type={showPassword ? 'text' : 'password'}
                placeholder="At least 12 characters"
                autoComplete="new-password"
                autoFocus
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

          <button className="btn-signin" type="submit" disabled={loading || !password}>
            {loading ? (
              <span className="spin" aria-hidden="true" />
            ) : (
              <>
                Reset password
                <Icon name="arrowR" />
              </>
            )}
          </button>
          <div className="mfa-back">
            <Link className="link" to="/login">
              ← Back to sign in
            </Link>
          </div>
        </form>
      )}
    </AuthShell>
  )
}
