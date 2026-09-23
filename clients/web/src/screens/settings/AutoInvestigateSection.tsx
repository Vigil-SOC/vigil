// Changes save automatically, and take ~60s to apply (runtime-config TTL).
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  Field,
  NumberInput,
  SettingsCard,
  TextInput,
  Toggle,
  ToggleRow,
} from '../../shared/ui'
import {
  ORCHESTRATOR_DEFAULTS,
  useOrchestrator,
  type OrchestratorConfig,
} from './useSettings'
import type { SectionProps } from './types'
import { fmtCost } from '../../shared/cost'
import IntentReportCard from './IntentReportCard'

type PresetKey = 'conservative' | 'balanced' | 'aggressive'
type PresetValues = Pick<
  OrchestratorConfig,
  | 'max_concurrent_agents'
  | 'max_iterations_per_agent'
  | 'max_runtime_per_investigation'
  | 'max_cost_per_investigation'
  | 'max_total_hourly_cost'
>

const PRESETS = {
  conservative: {
    label: 'Conservative',
    summary: 'Minimal spend · 2 agents · tight limits',
    values: {
      max_concurrent_agents: 2,
      max_iterations_per_agent: 25,
      max_runtime_per_investigation: 1800,
      max_cost_per_investigation: 1.0,
      max_total_hourly_cost: 5.0,
    },
  },
  balanced: {
    label: 'Balanced',
    summary: 'Recommended · 3 agents · $20/hr',
    values: {
      max_concurrent_agents: 3,
      max_iterations_per_agent: 50,
      max_runtime_per_investigation: 3600,
      max_cost_per_investigation: 5.0,
      max_total_hourly_cost: 20.0,
    },
  },
  aggressive: {
    label: 'Aggressive',
    summary: 'Broad coverage · 5 agents · $60/hr',
    values: {
      max_concurrent_agents: 5,
      max_iterations_per_agent: 100,
      max_runtime_per_investigation: 7200,
      max_cost_per_investigation: 15.0,
      max_total_hourly_cost: 60.0,
    },
  },
} satisfies Record<PresetKey, { label: string; summary: string; values: PresetValues }>

const matchesPreset = (cfg: OrchestratorConfig, key: PresetKey) =>
  (Object.entries(PRESETS[key].values) as [keyof PresetValues, number][]).every(
    ([k, v]) => cfg[k] === v,
  )

const detectActivePreset = (cfg: OrchestratorConfig): PresetKey | 'custom' => {
  for (const key of Object.keys(PRESETS) as PresetKey[]) if (matchesPreset(cfg, key)) return key
  return 'custom'
}

interface NumOpts {
  min?: number
  max?: number
  unit?: string
  hint?: string
  allowUnlimited?: boolean
}

export default function AutoInvestigateSection({ notify }: SectionProps) {
  const { config, setConfig, status, phase, save } = useOrchestrator()
  const lastSaved = useRef<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [advanced, setAdvanced] = useState(false)
  const [intentRevision, setIntentRevision] = useState(0)

  useEffect(() => {
    if (phase === 'ready') lastSaved.current = config
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading Auto Investigate config…</div>
  }

  const persist = async (next: OrchestratorConfig) => {
    try {
      await save(next)
      lastSaved.current = next
      notify('ok', 'Auto Investigate settings saved.')
      setIntentRevision((n) => n + 1)
    } catch {
      notify('err', 'Failed to save Auto Investigate settings.')
    }
  }

  const applyAndSave = (patch: Partial<OrchestratorConfig>) => {
    const next = { ...config, ...patch }
    setConfig(next)
    persist(next)
  }

  const persistIfChanged = () => {
    if (JSON.stringify(config) !== JSON.stringify(lastSaved.current)) persist(config)
  }

  const activePreset = detectActivePreset(config)

  const numField = (label: string, field: keyof OrchestratorConfig, opts: NumOpts = {}) => {
    const unlimited = Boolean(opts.allowUnlimited) && (config[field] as number) === 0
    return (
      <Field label={opts.unit ? `${label} (${opts.unit})` : label} hint={opts.hint}>
        <NumberInput
          value={unlimited ? '' : (config[field] as number)}
          placeholder={unlimited ? 'Unlimited' : undefined}
          disabled={unlimited}
          min={opts.min}
          max={opts.max}
          onChange={(e) => {
            let v = Number(e.target.value)
            if (opts.min !== undefined && v < opts.min) v = opts.min
            if (opts.max !== undefined && v > opts.max) v = opts.max
            setConfig((prev) => ({ ...prev, [field]: v }))
          }}
          onBlur={persistIfChanged}
        />
        {opts.allowUnlimited && (
          <span className="flex items-center gap-2 text-xs text-tx-3 mt-0.5">
            <Toggle
              checked={unlimited}
              onChange={(on) =>
                applyAndSave({ [field]: on ? 0 : (ORCHESTRATOR_DEFAULTS[field] as number) })
              }
            />
            Unlimited
          </span>
        )}
      </Field>
    )
  }

  return (
    <>
      <SettingsCard
        title="Auto Investigate"
        desc="Runtime toggles for the autonomous investigation orchestrator. Changes save automatically and take effect across backend / daemon / llm-worker within ~60 seconds."
      >
        {status && (
          <div className={`settings-banner ${status.enabled ? 'ok' : 'info'} mb-4`}>
            <Icon name="info" size={14} />
            <span>
              Orchestrator is <strong>{status.enabled ? 'ENABLED' : 'DISABLED'}</strong>
              {status.active_agents !== undefined && ` · ${status.active_agents} active agent(s)`}
              {status.total_investigations !== undefined &&
                ` · ${status.total_investigations} investigation(s)`}
              {status.cost?.total_cost_usd !== undefined &&
                ` · Total cost: ${fmtCost(status.cost.total_cost_usd)}`}
            </span>
          </div>
        )}

        <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-1">
          Master controls
        </h4>
        <ToggleRow
          label="Enable autonomous investigations"
          checked={config.enabled}
          onChange={(v) => applyAndSave({ enabled: v })}
        />
        <ToggleRow
          label="Dry run mode"
          hint="Agents gather data but skip write actions."
          checked={config.dry_run}
          onChange={(v) => applyAndSave({ dry_run: v })}
        />
      </SettingsCard>

      <SettingsCard
        title="Investigation profile"
        desc="Pick a profile to set agent concurrency, runtime, and cost limits in one click. Fine-tune any value under Advanced."
      >
        <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
          {(Object.keys(PRESETS) as PresetKey[]).map((key) => {
            const p = PRESETS[key]
            const selected = activePreset === key
            return (
              <button
                key={key}
                onClick={() => applyAndSave(p.values)}
                className={`card card-sq text-left p-3.5 transition-colors ${
                  selected ? 'border-accent-line bg-[var(--accent-dim)]' : 'hover:border-line'
                }`}
                style={selected ? { borderColor: 'var(--accent-line)' } : undefined}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-[13px] font-semibold text-tx">{p.label}</span>
                  {selected && <span className="chip sel">Active</span>}
                </div>
                <span className="text-xs text-tx-3">{p.summary}</span>
              </button>
            )
          })}
        </div>
        {activePreset === 'custom' && (
          <div className="settings-banner info mt-3">
            <Icon name="info" size={14} />
            <span>
              Custom limits in effect — your values don’t match any profile. Pick one above or expand
              Advanced to review.
            </span>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="Advanced"
        desc="Fine-tune limits, timing, and storage."
        actions={
          <button className="btn ghost" onClick={() => setAdvanced((a) => !a)}>
            <Icon name={advanced ? 'chevD' : 'chevR'} /> {advanced ? 'Hide' : 'Show'}
          </button>
        }
      >
        {advanced ? (
          <div className="flex flex-col gap-5">
            <div>
              <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2">
                Agent limits
              </h4>
              <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                {numField('Max concurrent agents', 'max_concurrent_agents', {
                  min: 1, max: 10, hint: '1–10 simultaneous agents', allowUnlimited: true,
                })}
                {numField('Max iterations per agent', 'max_iterations_per_agent', {
                  min: 1, max: 500, hint: 'Claude calls per investigation', allowUnlimited: true,
                })}
                {numField('Max runtime', 'max_runtime_per_investigation', {
                  min: 60, max: 86400, unit: 's',
                  hint: `${Math.round(config.max_runtime_per_investigation / 60)} minutes`,
                  allowUnlimited: true,
                })}
              </div>
            </div>

            <div>
              <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2">
                Cost guardrails
              </h4>
              <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                {numField('Per investigation limit', 'max_cost_per_investigation', {
                  min: 0.5, max: 100, unit: '$', hint: 'Max spend per investigation', allowUnlimited: true,
                })}
                {numField('Hourly cost limit', 'max_total_hourly_cost', {
                  min: 1, max: 500, unit: '$', hint: 'Pause intake if exceeded', allowUnlimited: true,
                })}
              </div>
            </div>

            <div>
              <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2">
                Timing
              </h4>
              <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                {numField('Loop interval', 'loop_interval', { min: 10, max: 600, unit: 's', hint: 'Orchestrator check interval' })}
                {numField('Stale threshold', 'stale_threshold', { min: 60, max: 3600, unit: 's', hint: 'Kill idle agents after this' })}
              </div>
            </div>

            <div>
              <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2">
                Storage
              </h4>
              <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                <Field label="Working directory" hint="Base path for investigation files">
                  <TextInput
                    value={config.workdir_base}
                    onChange={(e) => setConfig((prev) => ({ ...prev, workdir_base: e.target.value }))}
                    onBlur={persistIfChanged}
                  />
                </Field>
              </div>
            </div>

            <div>
              <button className="btn ghost" onClick={() => applyAndSave(ORCHESTRATOR_DEFAULTS)}>
                <Icon name="refresh" /> Reset to defaults
              </button>
            </div>
          </div>
        ) : (
          <span className="text-xs text-tx-3">Hidden — click Show to fine-tune limits.</span>
        )}
      </SettingsCard>

      <IntentReportCard reloadKey={intentRevision} />
    </>
  )
}
