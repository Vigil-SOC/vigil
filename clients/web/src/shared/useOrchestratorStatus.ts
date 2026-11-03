import { useEffect, useState } from 'react'
import { orchestratorApi, type OrchestratorStatus } from '../services/api'

const POLL_MS = 15_000

/** The orchestrator's own read; null until known or when the read fails. */
export function useOrchestratorStatus(): OrchestratorStatus | null {
  const [status, setStatus] = useState<OrchestratorStatus | null>(null)
  useEffect(() => {
    let live = true
    const read = () =>
      Promise.resolve()
        .then(() => orchestratorApi.getStatus())
        .then((res) => live && setStatus(res?.data ?? null))
        .catch(() => live && setStatus(null))
    read()
    const id = setInterval(read, POLL_MS)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [])
  return status
}
