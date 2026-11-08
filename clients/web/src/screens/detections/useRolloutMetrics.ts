import { useEffect, useState } from 'react'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi, type DetectionMetrics } from '../../services/api'

/** The rollout metrics, read once per visit to the page. */
export function useRolloutMetrics() {
  const [data, setData] = useState<DetectionMetrics | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    detectionRulesApi.metrics()
      .then((res) => { if (live) setData(res.data) })
      .catch((e) => { if (live) setError(errorText(e, 'The rollout metrics did not load.')) })
    return () => { live = false }
  }, [])
  return { data, error }
}
