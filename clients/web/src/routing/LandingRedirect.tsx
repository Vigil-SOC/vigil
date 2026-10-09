import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { landingScreen } from '../shell/landing'

// `/` has no screen of its own; send each person to where their role starts,
// keeping router state (the setup hand-off's startTour) across the redirect
const LandingRedirect = () => {
  const { hasPermission } = useAuth()
  const { state } = useLocation()
  return <Navigate to={`/${landingScreen(hasPermission)}`} replace state={state} />
}

export default LandingRedirect
