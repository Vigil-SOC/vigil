import { ReactNode, useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { configApi } from '../services/api'
import { markSetupDismissed, readSetupDismissed } from '../screens/setup/setupDismissed'

interface Props {
  children: ReactNode
}

type Verdict = 'checking' | 'through' | 'setup'

// /setup lives outside this gate, so opening it from the account menu does
// not bounce. A browser that dismissed setup goes straight through. Otherwise
// the install's record decides: once an admin has finished the wizard, every
// browser goes through; until then (or if the record can't be read) the
// operator is sent through the pass once, whether or not a provider exists.
const SetupGate = ({ children }: Props) => {
  const [verdict, setVerdict] = useState<Verdict>(() => (readSetupDismissed() ? 'through' : 'checking'))

  useEffect(() => {
    if (verdict !== 'checking') return
    let live = true
    configApi
      .getSetupState()
      .then(({ data }) => {
        if (!live) return
        if (data.finished) markSetupDismissed()
        setVerdict(data.finished ? 'through' : 'setup')
      })
      .catch(() => live && setVerdict('setup'))
    return () => {
      live = false
    }
  }, [verdict])

  if (verdict === 'checking') return null
  if (verdict === 'setup') return <Navigate to="/setup" replace />
  return <>{children}</>
}

export default SetupGate
