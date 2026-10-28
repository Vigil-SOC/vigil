import { useEffect, useState } from 'react'
import { orchestratorApi } from '../services/api'

const POLL_MS = 15_000

/** True once the orchestrator reports itself off; null until known or when the read fails. */
export function useAutoOpsOff(): boolean | null {
  const [off, setOff] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    const read = () =>
      Promise.resolve()
        .then(() => orchestratorApi.getStatus())
        .then((res) => live && setOff(res?.data?.enabled === false))
        .catch(() => live && setOff(null))
    read()
    const id = setInterval(read, POLL_MS)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [])
  return off
}
