import { InfoTip } from '../../shared/InfoTip'
import type { MonitorMetric } from '../../services/api'

/** The ⓘ for a server-read number: its info line, and its limit when it has one. */
export function MetricTip({ label, metric, limit }: { label: string; metric: Pick<MonitorMetric, 'info' | 'unmeasured_reason'>; limit?: string }) {
  return <InfoTip label={`How ${label.toLowerCase()} is calculated`} source={metric.unmeasured_reason ?? undefined} calculation={metric.info} limit={limit} />
}
