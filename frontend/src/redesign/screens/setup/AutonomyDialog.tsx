// frontend/src/redesign/screens/setup/AutonomyDialog.tsx
//
// Onboarding step dialog — enables the autonomous orchestrator without leaving
// the setup screen. Talks straight to configApi (no dependency on the redesign
// settings sections). The orchestrator POST takes the *full* config, so we GET
// the current one, flip `enabled`, and round-trip it back to preserve the caps.
import { useEffect, useState } from 'react'
import { Popup } from '../../shared/ui'
import { configApi } from '../../../services/api'

type OrchestratorConfig = Parameters<typeof configApi.setOrchestrator>[0]

interface Props {
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

const AutonomyDialog = ({ onClose, onSaved, onError }: Props) => {
  const [config, setConfig] = useState<OrchestratorConfig | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    configApi
      .getOrchestrator()
      .then(({ data }) => alive && setConfig(data))
      .catch(() => alive && setConfig(null))
    return () => {
      alive = false
    }
  }, [])

  const enable = async () => {
    setSaving(true)
    try {
      const base = config ?? (await configApi.getOrchestrator()).data
      await configApi.setOrchestrator({ ...base, enabled: true })
      onSaved()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      onError(err?.response?.data?.detail || err?.message || 'Failed to enable autonomous mode')
      setSaving(false)
    }
  }

  const caps: [string, number | undefined][] = [
    ['Per investigation', config?.max_cost_per_investigation],
    ['Per hour', config?.max_total_hourly_cost],
    ['Per day', config?.max_total_daily_cost],
  ]

  return (
    <Popup
      open
      onClose={onClose}
      title="Enable autonomous mode"
      width={460}
      dismissOnBackdrop={false}
    >
      <div className="flex flex-col gap-4">
        <p className="text-sm text-tx-2">
          Autonomous mode runs triage and investigation around the clock as new alerts
          arrive — no one has to be watching. It stops at the cost caps below, which you
          can fine-tune anytime in Settings → Auto Investigate.
        </p>
        <div className="grid grid-cols-3 gap-2">
          {caps.map(([label, val]) => (
            <div
              key={label}
              className="rounded-lg border border-line-soft p-3 text-center"
            >
              <div className="text-tx text-base font-semibold">
                {typeof val === 'number' ? `$${val}` : '—'}
              </div>
              <div className="text-tx-3 text-xs mt-0.5">{label}</div>
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2.5 mt-1">
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="btn primary" onClick={enable} disabled={saving}>
            {saving ? 'Enabling…' : 'Enable autonomous mode'}
          </button>
        </div>
      </div>
    </Popup>
  )
}

export default AutonomyDialog
