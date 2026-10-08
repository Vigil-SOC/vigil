import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { landingScreen } from '../shell/landing'

// `/` has no screen of its own; send each person to where their role starts
const LandingRedirect = () => {
  const { hasPermission } = useAuth()
  return <Navigate to={`/${landingScreen(hasPermission)}`} replace />
}

export default LandingRedirect
