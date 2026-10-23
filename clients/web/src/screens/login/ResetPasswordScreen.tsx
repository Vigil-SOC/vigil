import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthLayout from './AuthLayout'
import { requestErrorMessage } from './requestErrorMessage'

// Landing page for the emailed /reset-password?token=… link. Public: the
// signed token is the credential. AuthProvider's /auth/me GET on mount seeds
// the csrf_token cookie the confirm POST needs in a fresh browser.
export default function ResetPasswordScreen() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''

  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(
    token ? '' : 'This reset link is missing its token. Request a new link.',
  )

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await authApi.confirmPasswordReset({ token, new_password: password })
      navigate('/login', {
        replace: true,
        state: { notice: 'Password updated. Sign in with your new password.' },
      })
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not reset your password. Please try again.'))
      setLoading(false)
    }
  }

  const requestNewLink = () => navigate('/login', { state: { forgot: true } })

  return (
    <AuthLayout
      screenLabel="Reset password"
      title={token ? 'Choose a new password' : 'Reset link incomplete'}
      subtitle={
        token
          ? 'This signs out every existing session.'
          : 'Open the full link from your reset email, or request another.'
      }
      message={{ text: error }}
    >
      {token ? (
        <form onSubmit={handleSubmit} autoComplete="on" noValidate>
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
                Set new password
                <Icon name="arrowR" />
              </>
            )}
          </button>

          <div className="mfa-back">
            <button type="button" className="link" onClick={requestNewLink}>
              Request a new link
            </button>
          </div>
        </form>
      ) : (
        <>
          <button className="btn-signin" type="button" onClick={requestNewLink}>
            Request a new link
            <Icon name="arrowR" />
          </button>
          <div className="mfa-back">
            <button type="button" className="link" onClick={() => navigate('/login')}>
              ← Back to sign in
            </button>
          </div>
        </>
      )}
    </AuthLayout>
  )
}
