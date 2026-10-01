// Changes save automatically, and take ~60s to apply (runtime-config TTL).
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  Field,
  NumberInput,
  SettingsCard,
  TextInput,
  Toggle,
  ToggleRow,
} from '../../shared/ui'
import {
  ORCHESTRATOR_DEFAULTS,
  useForceManualApproval,
  useOrchestrator,
  type InvestigationProfileValues,
  type OrchestratorConfig,
} from './useSettings'
import type { SectionProps } from './types'
import { fmtCost } from '../../shared/cost'
import IntentReportCard from './IntentReportCard'

const LIMIT_FIELDS = [
  'max_cost_per_investigation',
  'max_iterations_per_agent',
  'max_runtime_per_investigation',
  'max_concurrent_agents',
  'max_total_hourly_cost',
] as const satisfies readonly (keyof InvestigationProfileValues)[]

type PendingSave = { kind: 'config'; next: OrchestratorConfig } | { kind: 'act' }

const raisesLimit = (prev: OrchestratorConfig, next: OrchestratorConfig) =>
  LIMIT_FIELDS.some((field) => next[field] > prev[field])

const matchesProfile = (cfg: OrchestratorConfig, values: InvestigationProfileValues) =>
  (Object.entries(values) as [keyof InvestigationProfileValues, number][]).every(
    ([k, v]) => cfg[k] === v,
  )

const pendingCopy = (pending: PendingSave): { title: string; body: string } => {
  switch (pending.kind) {
    case 'config':
      return {
        title: 'Raise investigation limits?',
        body: 'This increases a cost, runtime, or concurrency cap. Confirm to save.',
      }
    case 'act':
      return {
        title: 'Switch to Act?',
        body: 'Act stops forcing manual approval, so autonomous response can proceed on its own.',
      }
    default: {
      const _exhaustive: never = pending
      return _exhaustive
    }
  }
}

function errorText(err: unknown, fallback: string): string {
  if (typeof err === 'object' && err && 'response' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
  }
  return fallback
}

interface NumOpts {
  min?: number
  max?: number
  unit?: string
  hint?: string
  allowUnlimited?: boolean
}

export default function AutoInvestigateSection({ notify }: SectionProps) {
  const { config, setConfig, profiles, status, phase, save } = useOrchestrator()
  const approval = useForceManualApproval()
  const lastSaved = useRef<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [advanced, setAdvanced] = useState(false)
  const [intentRevision, setIntentRevision] = useState(0)
  const [pending, setPending] = useState<PendingSave | null>(null)

  useEffect(() => {
    if (phase === 'ready') lastSaved.current = config
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  if (phase === 'loading' || approval.phase === 'loading') {
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

  const commitConfig = (next: OrchestratorConfig) => {
    setConfig(next)
    if (raisesLimit(lastSaved.current, next)) {
      setPending({ kind: 'config', next })
      return
    }
    persist(next)
  }

  const applyAndSave = (patch: Partial<OrchestratorConfig>) => {
    commitConfig({ ...config, ...patch })
  }

  const persistIfChanged = () => {
    if (JSON.stringify(config) !== JSON.stringify(lastSaved.current)) commitConfig(config)
  }

  const saveApproval = async (enabled: boolean) => {
    try {
      await approval.save(enabled)
      notify('ok', 'Auto Investigate settings saved.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to save Auto Investigate settings.'))
    }
  }

  const selectAssist = () => {
    if (approval.enabled) return
    saveApproval(true)
  }

  const selectAct = () => {
    if (approval.environment_wins) {
      saveApproval(false)
      return
    }
    if (!approval.enabled) return
    setPending({ kind: 'act' })
  }

  const confirmPending = () => {
    if (!pending) return
    const current = pending
    setPending(null)
    switch (current.kind) {
      case 'config':
        persist(current.next)
        return
      case 'act':
        saveApproval(false)
        return
      default: {
        const _exhaustive: never = current
        return _exhaustive
      }
    }
  }

  const dismissPending = () => {
    if (pending?.kind === 'config') setConfig(lastSaved.current)
    setPending(null)
  }

  let activeProfile: string | 'custom' = 'custom'
  for (const [key, profile] of Object.entries(profiles)) {
    if (matchesProfile(config, profile.values)) {
      activeProfile = key
      break
    }
  }
  const assistOn = approval.enabled || approval.environment_wins
  const dialog = pending ? pendingCopy(pending) : null

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
        title="Response mode"
        desc="Assist forces a person to approve each response. Act does not. Act is the default."
      >
        {approval.phase === 'error' ? (
          <div className="settings-banner err">
            <Icon name="alert" size={14} />
            <span>Could not load the response mode. Reload to try again.</span>
          </div>
        ) : (
          <>
            {approval.environment_wins && (
              <div className="settings-banner info mb-3">
                <Icon name="info" size={14} />
                <span>The environment wins. Act cannot be saved.</span>
              </div>
            )}
            <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
              <button
                onClick={selectAssist}
                className={`card card-sq text-left p-3.5 transition-colors ${
                  assistOn ? 'border-accent-line bg-[var(--accent-dim)]' : 'hover:border-line'
                }`}
                style={assistOn ? { borderColor: 'var(--accent-line)' } : undefined}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-[13px] font-semibold text-tx">Assist</span>
                  {assistOn && <span className="chip sel">Active</span>}
                </div>
                <span className="text-xs text-tx-3">Force manual approval before a response runs.</span>
              </button>
              <button
                onClick={selectAct}
                className={`card card-sq text-left p-3.5 transition-colors ${
                  !assistOn ? 'border-accent-line bg-[var(--accent-dim)]' : 'hover:border-line'
                }`}
                style={!assistOn ? { borderColor: 'var(--accent-line)' } : undefined}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-[13px] font-semibold text-tx">Act</span>
                  <span className="chip">Recommended</span>
                  {!assistOn && <span className="chip sel">Active</span>}
                </div>
                <span className="text-xs text-tx-3">Let autonomous response proceed without forcing approval.</span>
              </button>
            </div>
          </>
        )}
      </SettingsCard>

      <SettingsCard
        title="Investigation profile"
        desc="Pick a profile to set agent concurrency, runtime, and cost limits in one click. Fine-tune any value under Advanced."
      >
        <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
          {Object.entries(profiles).map(([key, profile]) => {
            const selected = activeProfile === key
            return (
              <button
                key={key}
                onClick={() => applyAndSave(profile.values)}
                className={`card card-sq text-left p-3.5 transition-colors ${
                  selected ? 'border-accent-line bg-[var(--accent-dim)]' : 'hover:border-line'
                }`}
                style={selected ? { borderColor: 'var(--accent-line)' } : undefined}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-[13px] font-semibold text-tx">{profile.label}</span>
                  {profile.recommended && <span className="chip">Recommended</span>}
                  {selected && <span className="chip sel">Active</span>}
                </div>
              </button>
            )
          })}
        </div>
        {activeProfile === 'custom' && (
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

      <ConfirmDialog
        open={dialog != null}
        title={dialog?.title ?? ''}
        body={dialog?.body ?? ''}
        confirmLabel="Save"
        danger={false}
        onConfirm={confirmPending}
        onClose={dismissPending}
      />
    </>
  )
}
