import { Icon } from './icons'
import type { ConsoleScreenProps } from './types'

/** Said wherever work would otherwise look like it is moving: with AutoOps off, nothing picks alerts up. */
export function AutoOpsOffBanner({ goSettings }: { goSettings: ConsoleScreenProps['goSettings'] }) {
  return (
    <div className="autoops-off" role="status">
      <Icon name="alert" size={14} />
      <span>
        <b>AutoOps is off.</b> Alerts are triaged but no agent picks them up until it is on.
      </span>
      <button type="button" className="btn ghost" onClick={() => goSettings('autoinvestigate')}>
        Turn it on in Settings →
      </button>
    </div>
  )
}
