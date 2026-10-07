import { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { readSetupDismissed } from '../screens/setup/setupDismissed'

interface Props {
  children: ReactNode
}

// /setup lives outside this gate, so opening it from the account menu does
// not bounce. An unset vigil.setupDismissed sends the operator through the
// pass once, whether or not a provider exists.
const SetupGate = ({ children }: Props) => {
  if (!readSetupDismissed()) {
    return <Navigate to="/setup" replace />
  }

  return <>{children}</>
}

export default SetupGate
