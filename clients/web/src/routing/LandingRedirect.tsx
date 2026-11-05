import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { coldStartApi } from '../services/api'
import { landingScreen, landsOnStart } from '../shell/landing'
import Loader from './Loader'

const LATEST_RUN_WAIT_MS = 3000

// `/` has no screen of its own; send each person to where their role starts.
// Someone who can finish day one setup starts there until a baseline is live.
const LandingRedirect = () => {
  const { hasPermission } = useAuth()
  const mayStart = landsOnStart(hasPermission)
  const [needsStart, setNeedsStart] = useState<boolean | null>(mayStart ? null : false)

  useEffect(() => {
    if (!mayStart) return
    let live = true
    coldStartApi
      .latestRun(LATEST_RUN_WAIT_MS)
      .then(({ data }) => data.status !== 'live')
      // 404 is no run yet. Any other failure must not hold anyone back from the console.
      .catch((e) => e?.response?.status === 404)
      .then((next) => {
        if (live) setNeedsStart(next)
      })
    return () => {
      live = false
    }
  }, [mayStart])

  if (needsStart === null) return <Loader />
  return <Navigate to={`/${needsStart ? 'start' : landingScreen(hasPermission)}`} replace />
}

export default LandingRedirect
