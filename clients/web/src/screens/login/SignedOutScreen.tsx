import type { Location } from 'react-router-dom'
import { useNavigate } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { endReasonText } from '../../services/signInEnd'
import AuthLayout from './AuthLayout'

/** What a person sees when the server ended their sign-in: why, and the way back to the same address. */
export default function SignedOutScreen({ reason, from }: { reason: string | null; from: Location }) {
  const navigate = useNavigate()
  return (
    <AuthLayout screenLabel="Signed out" title="Signed out" subtitle={endReasonText(reason)}>
      <div className="flex flex-col gap-4">
        <p className="field-hint" style={{ margin: 0 }}>
          This browser’s saved console data was cleared. After you sign in you return to{' '}
          <code style={{ overflowWrap: 'anywhere' }}>{from.pathname + from.search + from.hash}</code>.
        </p>
        <button className="btn-signin" type="button" autoFocus onClick={() => navigate('/login', { state: { from } })}>
          Sign in
          <Icon name="arrowR" />
        </button>
      </div>
    </AuthLayout>
  )
}
