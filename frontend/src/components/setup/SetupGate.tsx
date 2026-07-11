import { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import useSetupStatus from '../../hooks/useSetupStatus'
import RedesignLoader from '../../redesign/shell/Loader'

interface Props {
  children: ReactNode
}

// First-access gate: blocks the app until a working LLM provider exists. The
// /setup route lives OUTSIDE this gate so it stays reachable when unconfigured
// (no redirect loop).
const SetupGate = ({ children }: Props) => {
  const { configured, loading } = useSetupStatus()

  if (loading) {
    return <RedesignLoader label="Checking setup…" />
  }

  if (!configured) {
    return <Navigate to="/setup" replace />
  }

  return <>{children}</>
}

export default SetupGate
