import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import AuthLayout from './AuthLayout'
import { requestErrorMessage } from './requestErrorMessage'

// Landing page for /confirm-email?token=… Public: the link is the credential.
// Opening it uses nothing up. The address is confirmed when the form is
// submitted. AuthProvider's /auth/me GET on mount seeds the csrf_token cookie
// the confirm POST needs in a fresh browser.
export default function ConfirmEmailScreen() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''

  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState(
    token ? '' : 'This confirmation link is missing its token. Ask for a new one.',
  )

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await authApi.confirmEmail(token)
      setDone(true)
    } catch (err: any) {
      setError(requestErrorMessage(err, 'Could not confirm this email address. Please try again.'))
      setLoading(false)
    }
  }

  const title = done
    ? 'Email address confirmed'
    : token
      ? 'Confirm your email address'
      : 'Confirmation link incomplete'
  const subtitle = done
    ? 'You can sign in with this address.'
    : token
      ? 'This link was sent because the address on the account changed. It works once, for 24 hours.'
      : 'Open the full link from the confirmation email, or ask for another.'

  return (
    <AuthLayout
      screenLabel="Confirm email"
      title={title}
      subtitle={subtitle}
      message={done ? undefined : { text: error }}
    >
      {done ? (
        <button className="btn-signin" type="button" onClick={() => navigate('/login')}>
          Back to sign in
          <Icon name="arrowR" />
        </button>
      ) : token ? (
        <form onSubmit={handleSubmit} noValidate>
          <button className="btn-signin" type="submit" disabled={loading}>
            {loading ? (
              <span className="spin" aria-hidden="true" />
            ) : (
              <>
                Confirm this address
                <Icon name="arrowR" />
              </>
            )}
          </button>
          <div className="mfa-back">
            <button type="button" className="link" onClick={() => navigate('/login')}>
              ← Back to sign in
            </button>
          </div>
        </form>
      ) : (
        <button className="btn-signin" type="button" onClick={() => navigate('/login')}>
          Back to sign in
          <Icon name="arrowR" />
        </button>
      )}
    </AuthLayout>
  )
}
