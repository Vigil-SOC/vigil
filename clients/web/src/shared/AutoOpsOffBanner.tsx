import { Link } from 'react-router-dom'
import { Icon } from './icons'
import { InfoTip } from './InfoTip'
import { fmtCost } from './cost'
import { useOrchestratorStatus } from './useOrchestratorStatus'
import type { ConsoleScreenProps } from './types'
import type { OrchestratorStatus } from '../services/api'

type GoSettings = ConsoleScreenProps['goSettings']

function LimitsLink({ goSettings }: { goSettings: GoSettings }) {
  return (
    <button type="button" className="btn ghost" onClick={() => goSettings('autoinvestigate')}>
      Open Limits &amp; autonomy →
    </button>
  )
}

/** Said wherever work would otherwise look like it is moving: with AutoOps off, nothing picks alerts up. */
export function AutoOpsOffBanner({ goSettings }: { goSettings: GoSettings }) {
  return (
    <div className="autoops-off" role="status">
      <Icon name="alert" size={14} />
      <span>
        <b>AutoOps is off.</b> Alerts are triaged but no agent picks them up until it is on.
      </span>
      <LimitsLink goSettings={goSettings} />
    </div>
  )
}

function HourlyPauseBanner({ cost, goSettings }: { cost: OrchestratorStatus['cost']; goSettings: GoSettings }) {
  const spent = cost.hourly_cost_usd ?? 0
  const limit = cost.hourly_limit_usd ?? 0
  return (
    <div className="autoops-off" role="status">
      <Icon name="alert" size={14} />
      <span>
        <b>Agents are paused at the hourly cost limit.</b>{' '}
        {spent >= limit
          ? `${fmtCost(spent)} of ${fmtCost(limit)} spent in the last hour. Alerts stay in the queue until older spend leaves the hour.`
          : 'Spend in the last hour could not be read, so it counts as over the limit. Alerts stay in the queue.'}{' '}
        <InfoTip
          label="How the hourly cost limit is worked out"
          source="The recorded cost of each run, and the limit saved in Limits & autonomy."
          calculation="The sum over runs active in the last hour. A run waiting on a person stops counting an hour after it parked."
        />
      </span>
      <LimitsLink goSettings={goSettings} />
    </div>
  )
}

// DESIGN §2: "4 min", "7 h 5 min"
function fmtAge(seconds: number): string {
  const minutes = Math.max(0, Math.round(seconds / 60))
  if (minutes < 60) return `${minutes} min`
  const rest = minutes % 60
  return rest ? `${Math.floor(minutes / 60)} h ${rest} min` : `${minutes / 60} h`
}

function ParkedBanner({ status }: { status: OrchestratorStatus }) {
  const { waiting_approval: approvals, paused, oldest_parked_age_seconds: age } = status
  const total = approvals + paused
  const parts = [approvals > 0 && `${approvals} on an approval`, paused > 0 && `${paused} paused`].filter(Boolean).join(', ')
  return (
    <div className="autoops-off" role="status">
      <Icon name="clock" size={14} />
      <span>
        <b>{total} run{total === 1 ? '' : 's'} waiting on a person.</b> {parts}
        {age !== null && `, the oldest for ${fmtAge(age)}`}.{' '}
        <InfoTip
          label="How runs waiting on a person are counted"
          source="Runs whose status is waiting approval or paused."
          calculation="A count of each, and the time since the oldest one's last activity."
        />
      </span>
      <Link className="btn ghost no-underline" to="/cases">Open cases →</Link>
    </div>
  )
}

/** Every reason alerts are not moving, on one surface. A failed read claims nothing. */
export function IntakeBanners({ autoOpsOff, goSettings }: { autoOpsOff: boolean | null; goSettings: GoSettings }) {
  const status = useOrchestratorStatus()
  return (
    <>
      {autoOpsOff && <AutoOpsOffBanner goSettings={goSettings} />}
      {!autoOpsOff && status?.cost?.hourly_paused && <HourlyPauseBanner cost={status.cost} goSettings={goSettings} />}
      {status && status.waiting_approval + status.paused > 0 && <ParkedBanner status={status} />}
    </>
  )
}
