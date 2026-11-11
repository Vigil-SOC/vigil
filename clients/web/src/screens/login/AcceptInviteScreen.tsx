import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { invitesApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthLayout from './AuthLayout'
import { requestErrorMessage } from './requestErrorMessage'

// Landing page for an invite link, /accept-invite?token=… Public: the link is the
// credential. Opening it only looks the invite up; the account is made, and the
// link used up, when the form is submitted. AuthProvider's /auth/me GET on mount
// seeds the csrf_token cookie the accept POST needs in a fresh browser.
export default function AcceptInviteScreen() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''

  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [checking, setChecking] = useState(!!token)
  // a link that can't be used: the page says so, and only offers sign-in
  const [dead, setDead] = useState(token ? '' : 'This invite link is missing its token. Ask for a new invite.')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!token) return
    invitesApi
      .check(token)
      .then(({ data }) => {
        setEmail(data.email)
        setUsername(data.username)
      })
      .catch((err) => setDead(requestErrorMessage(err, 'Could not open this invite. Please try again.')))
      .finally(() => setChecking(false))
  }, [token])

  const mismatch = confirm !== '' && confirm !== password

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (password !== confirm) {
      setError('The two passwords differ.')
      return
    }
    setLoading(true)
    try {
      await invitesApi.accept({ token, username: username.trim(), password })
      navigate('/login', {
        replace: true,
        state: { notice: 'Your account is ready. Sign in with your new password.' },
      })
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not set up your account. Please try again.'))
      setLoading(false)
    }
  }

  const ready = !checking && !dead
  return (
    <AuthLayout
      screenLabel="Accept invite"
      title={dead ? 'Invite link unavailable' : 'Set up your account'}
      subtitle={
        dead
          ? 'Ask the person who invited you to send a new one.'
          : 'Choose a username and a password. Your email is fixed by the invite.'
      }
      message={{ text: dead || error }}
    >
      {ready ? (
        <form onSubmit={handleSubmit} autoComplete="on" noValidate>
          <div className="field">
            <label htmlFor="inv-email">Email</label>
            <div className="ctrl">
              <Icon name="send" className="lead" />
              <input id="inv-email" name="email" type="email" value={email} readOnly disabled />
            </div>
          </div>

          <div className="field">
            <label htmlFor="inv-user">Username</label>
            <div className="ctrl">
              <Icon name="user" className="lead" />
              <input
                id="inv-user"
                name="username"
                type="text"
                autoComplete="username"
                autoFocus
                spellCheck={false}
                disabled={loading}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="inv-pass">Password</label>
            <div className="ctrl">
              <Icon name="lock" className="lead" />
              <input
                id="inv-pass"
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

          <div className="field">
            <label htmlFor="inv-confirm">Confirm password</label>
            <div className="ctrl">
              <Icon name="lock" className="lead" />
              <input
                id="inv-confirm"
                name="confirm-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="new-password"
                disabled={loading}
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                aria-invalid={mismatch}
              />
            </div>
            {mismatch && <p className="field-hint">The two passwords differ.</p>}
          </div>

          <button
            className="btn-signin"
            type="submit"
            disabled={loading || !username.trim() || !password || !confirm}
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
        !checking && (
          <button className="btn-signin" type="button" onClick={() => navigate('/login')}>
            Go to sign in
            <Icon name="arrowR" />
          </button>
        )
      )}
    </AuthLayout>
  )
}
