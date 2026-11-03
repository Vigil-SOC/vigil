import { useEffect, useState } from 'react'
import { configApi } from '../services/api'

const POLL_MS = 15_000

/** True once automatic investigation is switched off; null until known or when the read fails. */
export function useAutoOpsOff(): boolean | null {
  const [off, setOff] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    const read = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      Promise.resolve()
        .then(() => configApi.getOrchestrator())
        .then((res) => live && setOff(res?.data?.enabled === false))
        .catch(() => live && setOff(null))
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') read()
    }
    read()
    const id = setInterval(read, POLL_MS)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      live = false
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])
  return off
}
