import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { invitesApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthLayout from './AuthLayout'
import { requestErrorMessage } from './requestErrorMessage'

// Landing page for an invite link, /accept-invite#token=…. Public: the link is
// the credential. It sits in the fragment so loading the page sends it to no
// server; it reaches the API in a header and the accept body. Opening the link
// uses nothing up: only creating the account does.
export default function AcceptInviteScreen() {
  const navigate = useNavigate()
  const token = new URLSearchParams(useLocation().hash.slice(1)).get('token') ?? ''

  const [phase, setPhase] = useState<'loading' | 'ready' | 'invalid'>(token ? 'loading' : 'invalid')
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(token ? '' : 'This invite link is missing its token. Open the full link you were sent.')

  useEffect(() => {
    if (!token) return
    let cancelled = false
    invitesApi
      .details(token)
      .then(({ data }) => {
        if (cancelled) return
        setEmail(data.email)
        setUsername(data.username)
        setPhase('ready')
      })
      .catch((err) => {
        if (cancelled) return
        setError(requestErrorMessage(err, 'This invite link can’t be used. Ask for a new invite.'))
        setPhase('invalid')
      })
    return () => {
      cancelled = true
    }
  }, [token])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (password !== confirm) {
      setError('The passwords don’t match.')
      return
    }
    setError('')
    setLoading(true)
    try {
      await invitesApi.accept({ token, username: username.trim(), password })
      navigate('/login', {
        replace: true,
        state: { notice: 'Your account is ready. Sign in with your new password.' },
      })
    } catch (err) {
      setError(requestErrorMessage(err, 'Could not create your account. Please try again.'))
      setLoading(false)
    }
  }

  return (
    <AuthLayout
      screenLabel="Accept invite"
      title={phase === 'invalid' ? 'Invite link can’t be used' : 'Set up your account'}
      subtitle={
        phase === 'invalid'
          ? 'Ask the person who invited you for a new invite.'
          : 'Choose a username and password. You sign in with either your username or your email.'
      }
      message={{ text: error }}
    >
      {phase === 'loading' && (
        <div className="mfa-back">
          <span className="spin" aria-hidden="true" /> Checking your invite…
        </div>
      )}
      {phase === 'ready' && (
        <form onSubmit={handleSubmit} autoComplete="on" noValidate>
          <div className="field">
            <label htmlFor="inv-email">Email</label>
            <div className="ctrl">
              <Icon name="send" className="lead" />
              <input id="inv-email" name="email" type="email" autoComplete="email" value={email} readOnly disabled />
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
              />
            </div>
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
      )}
      {phase === 'invalid' && (
        <div className="mfa-back">
          <button type="button" className="link" onClick={() => navigate('/login')}>
            ← Back to sign in
          </button>
        </div>
      )}
    </AuthLayout>
  )
}
