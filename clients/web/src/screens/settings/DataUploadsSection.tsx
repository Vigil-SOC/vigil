import { useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { NotMeasured } from '../../shared/NotMeasured'
import { SettingsCard } from '../../shared/ui'
import { useAlignOnSettle } from '../../shared/useAlignOnSettle'
import { DemoDataClear, HistoryImportCard, SampleAlertsCard, StreamsCard, UploadCard } from './DataIngestion'
import DetectionRulesPanel from './DetectionRulesPanel'
import LoglmCard from './LoglmCard'
import type { SectionProps } from './types'

const RETENTION_NOTE =
  'Vigil does not record how long uploaded data is kept. No schedule deletes uploaded findings or cases. ' +
  'The daemon’s cleanup cutoff (daemon_cleanup_retention_days, 90 days by default) only prunes the episodic memory read log.'

export default function DataUploadsSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const page = useRef<HTMLDivElement>(null)
  // Home's setup steps land on a card by tab
  const anchor = { detection: 'rule-sources', history: 'history-imports' }[searchParams.get('tab') ?? '']
  useAlignOnSettle(anchor ?? null, page)

  return (
    <div ref={page} className="data-page">
      <LoglmCard notify={notify} />
      <UploadCard notify={notify} />
      <div id="history-imports" className="data-anchor">
        <HistoryImportCard notify={notify} />
      </div>
      <SampleAlertsCard notify={notify} />
      <StreamsCard notify={notify} />
      <div id="rule-sources" className="data-anchor">
        <DetectionRulesPanel notify={notify} />
      </div>
      <DemoDataClear notify={notify} />
      <SettingsCard title="Retention" desc="How long Vigil keeps the data you bring in.">
        <NotMeasured tip={RETENTION_NOTE} />
      </SettingsCard>
    </div>
  )
}
