import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { NotMeasured } from '../../shared/NotMeasured'
import { SettingsCard } from '../../shared/ui'
import { DemoDataClear, StreamsCard, UploadCard } from './DataIngestion'
import DetectionRulesPanel from './DetectionRulesPanel'
import LoglmCard from './LoglmCard'
import type { SectionProps } from './types'

const RETENTION_NOTE =
  'Vigil does not record how long uploaded data is kept. No schedule deletes uploaded findings or cases. ' +
  'The daemon’s cleanup cutoff (daemon_cleanup_retention_days, 90 days by default) only prunes the episodic memory read log.'

// Cards above the rule sources settle after load; keep the anchor in view until the reader scrolls.
const SETTLE_MS = 2000

export default function DataUploadsSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const wantsRules = searchParams.get('tab') === 'detection'
  const page = useRef<HTMLDivElement>(null)
  const rules = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!wantsRules || !rules.current) return
    const align = () => rules.current?.scrollIntoView?.({ block: 'start' })
    align()
    if (typeof ResizeObserver === 'undefined' || !page.current) return
    const watch = new ResizeObserver(align)
    watch.observe(page.current)
    const stop = () => watch.disconnect()
    const timer = window.setTimeout(stop, SETTLE_MS)
    const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'] as const
    events.forEach((e) => window.addEventListener(e, stop, { once: true, passive: true }))
    return () => {
      stop()
      window.clearTimeout(timer)
      events.forEach((e) => window.removeEventListener(e, stop))
    }
  }, [wantsRules])

  return (
    <div ref={page} className="data-page">
      <LoglmCard notify={notify} />
      <UploadCard notify={notify} />
      <StreamsCard notify={notify} />
      <div ref={rules} id="rule-sources" className="data-anchor">
        <DetectionRulesPanel notify={notify} />
      </div>
      <DemoDataClear notify={notify} />
      <SettingsCard title="Retention" desc="How long Vigil keeps the data you bring in.">
        <NotMeasured tip={RETENTION_NOTE} />
      </SettingsCard>
    </div>
  )
}
