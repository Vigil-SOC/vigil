import { useCallback, useEffect, useState } from 'react'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi, type DetectionMetrics } from '../../services/api'

/** The rollout metrics, read once per visit to the page; ``retry`` reads them again.
 * ``error`` is the reason the read failed, as the server or browser said it. */
export function useRolloutMetrics() {
  const [data, setData] = useState<DetectionMetrics | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let live = true
    setError(null)
    detectionRulesApi.metrics()
      .then((res) => { if (live) setData(res.data) })
      .catch((e) => { if (live) setError(errorText(e, 'no reason given')) })
    return () => { live = false }
  }, [attempt])
  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { data, error, retry }
}
