import { Link } from 'react-router-dom'
import { InfoTip } from './InfoTip'
import { STOP_CAUSES, type StopCause } from './stopCause'

export function StopAction({ cause, className }: { cause: StopCause; className: string }) {
  const { label, to } = STOP_CAUSES[cause].action
  return <Link className={`${className} no-underline`} to={to}>{label}</Link>
}

/** The sentence and its action on one line, with the provider's own words behind the ⓘ. */
export function StopLine({ cause, raw }: { cause: StopCause; raw?: string | null }) {
  return (
    <div className="flex items-center gap-2 min-w-0 pt-2 text-[12.5px]" aria-label="Why the run stopped">
      <span className="min-w-0 text-tx-2">{STOP_CAUSES[cause].sentence}</span>
      {raw && <InfoTip label="What the run reported" text={raw} align="start" />}
      <StopAction cause={cause} className="btn ghost shrink-0" />
    </div>
  )
}
