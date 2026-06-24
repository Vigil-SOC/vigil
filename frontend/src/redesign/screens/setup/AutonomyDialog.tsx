// frontend/src/redesign/screens/setup/AutonomyDialog.tsx
//
// Setup step panel — enables the autonomous orchestrator. The orchestrator POST
// takes the full config, so we GET it, flip `enabled`, and round-trip to keep the caps.
import { useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Banner, useSaveAction } from '../../shared/formKit'
import { budgetsApi, configApi } from '../../../services/api'

type OrchestratorConfig = Parameters<typeof configApi.setOrchestrator>[0]

interface Props {
  onClose: () => void
  onSaved: () => void
  // Jump to the cost-guardrails step. Autonomy runs investigations around the
  // clock, so we require an account-level spend cap before switching it on.
  onConfigureBudget: () => void
}

const AutonomyDialog = ({ onClose, onSaved, onConfigureBudget }: Props) => {
  const [config, setConfig] = useState<OrchestratorConfig | null>(null)
  // null = still loading; gate the enable button until we know.
  const [hasCap, setHasCap] = useState<boolean | null>(null)
  const { saving, error, run } = useSaveAction({ onSaved })

  useEffect(() => {
    let alive = true
    configApi
      .getOrchestrator()
      .then(({ data }) => alive && setConfig(data))
      .catch(() => alive && setConfig(null))
    budgetsApi
      .get()
      .then(({ data }) => alive && setHasCap(!!data?.default_vk?.trim()))
      .catch(() => alive && setHasCap(false))
    return () => {
      alive = false
    }
  }, [])

  const enable = () =>
    run(async () => {
      const base = config ?? (await configApi.getOrchestrator()).data
      await configApi.setOrchestrator({ ...base, enabled: true })
    }, 'Failed to enable autonomous mode')

  const caps: [string, number | undefined][] = [
    ['Per investigation', config?.max_cost_per_investigation],
    ['Per hour', config?.max_total_hourly_cost],
    ['Per day', config?.max_total_daily_cost],
  ]

  return (
    <div className="flex flex-col gap-4">
      {error && <Banner kind="err">{error}</Banner>}
      <p className="text-sm text-tx-2">
        Autonomous mode runs triage and investigation around the clock as new alerts
        arrive — no one has to be watching. It stops at the cost caps below, which you
        can fine-tune anytime in Settings → Auto Investigate.
      </p>
      <div className="grid grid-cols-3 gap-2">
        {caps.map(([label, val]) => (
          <div key={label} className="rounded-lg border border-line-soft p-3 text-center">
            <div className="text-tx text-base font-semibold">
              {typeof val === 'number' ? `$${val}` : '—'}
            </div>
            <div className="text-tx-3 text-xs mt-0.5">{label}</div>
          </div>
        ))}
      </div>
      {hasCap === false && (
        <div className="flex items-start gap-2 rounded-lg border border-line-soft bg-bg-1 p-3 text-xs text-tx-2">
          <Icon name="alert" size={14} className="text-high shrink-0 mt-0.5" />
          <span>
            Set a spend cap first — autonomous mode runs around the clock, and the per-run caps
            above don&apos;t limit your total bill.
          </span>
        </div>
      )}
      <div className="flex justify-end gap-2.5 mt-1">
        <button className="btn ghost" onClick={onClose} disabled={saving}>
          Cancel
        </button>
        {hasCap === false ? (
          <button className="btn primary" onClick={onConfigureBudget}>
            Set cost guardrails first
            <Icon name="arrowR" size={14} />
          </button>
        ) : (
          <button className="btn primary" onClick={enable} disabled={saving || hasCap === null}>
            {saving ? 'Enabling…' : 'Enable autonomous mode'}
          </button>
        )}
      </div>
    </div>
  )
}

export default AutonomyDialog
