// Changes save automatically, and take ~60s to apply (runtime-config TTL).
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { DurationPicker } from '../../shared/DurationPicker'
import { formatDuration } from '../../shared/duration'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { ScrubField } from '../../shared/ScrubField'
import { ConfirmDialog, Field, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import {
  useForceManualApproval,
  useOrchestrator,
  type InvestigationProfileValues,
  type OrchestratorBound,
  type OrchestratorConfig,
} from './useSettings'
import type { SectionProps } from './types'
import { fmtCost } from '../../shared/cost'
import IntentReportCard from './IntentReportCard'

// Raising any of these needs a confirm; lowering applies at once.
const LIMIT_FIELDS = [
  'max_cost_per_investigation',
  'max_iterations_per_agent',
  'max_runtime_per_investigation',
  'max_concurrent_agents',
  'max_total_hourly_cost',
  'stale_threshold',
] as const satisfies readonly (keyof OrchestratorConfig)[]

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
        title: 'Let tools act on their own?',
        body: 'Tools that change something will stop waiting for a person when the agent is confident enough.',
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

const ACT_TIP =
  'A tool that can be undone runs on its own when the agent’s confidence is at or above the response confidence threshold, and waits for a person below it. Ask first makes every one of them wait.'

const money = (v: number) => `$${v}`

interface LimitRow {
  field: keyof OrchestratorConfig & keyof InvestigationProfileValues
  title: string
  hint: string
  label: string
  prefix?: string
  unit: string
}

// The board's rows; bounds, step and defaults come from the server
const SCRUB_ROWS: LimitRow[] = [
  { field: 'max_cost_per_investigation', title: 'Budget per case', hint: 'What a new case may spend without asking', label: 'Budget', prefix: '$', unit: 'per case' },
  { field: 'max_iterations_per_agent', title: 'Steps per case', hint: 'Steps before the case stops and asks', label: 'Steps', unit: 'per case' },
]
const FLEET_ROWS: LimitRow[] = [
  { field: 'max_concurrent_agents', title: 'Cases running at once, whole fleet', hint: 'More at once is faster but costs more per hour', label: 'Fleet', unit: 'at once' },
  { field: 'max_total_hourly_cost', title: 'Spend per hour, whole fleet', hint: 'Intake pauses when the last hour reaches this', label: 'Hourly cap', prefix: '$', unit: 'per hour' },
]

export default function AutoInvestigateSection({ notify }: SectionProps) {
  const { config, setConfig, defaults, bounds, profiles, status, phase, reload, save } = useOrchestrator()
  const approval = useForceManualApproval()
  const lastSaved = useRef<OrchestratorConfig | null>(null)
  const configRef = useRef<OrchestratorConfig | null>(null)
  const idleTimer = useRef<ReturnType<typeof setTimeout>>()
  const [advanced, setAdvanced] = useState(false)
  const [intentRevision, setIntentRevision] = useState(0)
  const [pending, setPending] = useState<PendingSave | null>(null)

  configRef.current = config
  useEffect(() => {
    if (phase === 'ready') lastSaved.current = config
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])
  // leaving the page inside the debounce still saves the pending duration
  const commitRef = useRef<(cfg: OrchestratorConfig) => void>()
  useEffect(
    () => () => {
      if (idleTimer.current) {
        clearTimeout(idleTimer.current)
        commitRef.current?.(configRef.current!)
      }
    },
    [],
  )

  if (phase === 'loading' || approval.phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading limits and autonomy…</div>
  }
  if (phase === 'error' || !config) {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load the limits. Nothing has been changed.</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const bound = (field: keyof OrchestratorConfig): OrchestratorBound | undefined => bounds[field]
  const isOutside = (field: keyof OrchestratorConfig) => {
    const b = bound(field)
    const v = config[field] as number
    return b !== undefined && (v < b.min || v > b.max)
  }
  // A config saved before the bounds existed (0 used to mean "unlimited") loads as it is,
  // and is corrected into range the next time anything is saved.
  const outside = (Object.keys(bounds) as (keyof OrchestratorConfig)[]).filter(isOutside)
  const intoRange = (cfg: OrchestratorConfig): OrchestratorConfig => {
    const next = { ...cfg }
    for (const field of outside) {
      const b = bound(field)!
      Object.assign(next, { [field]: Math.min(Math.max(cfg[field] as number, b.min), b.max) })
    }
    return next
  }

  const persist = async (next: OrchestratorConfig) => {
    try {
      await save(next)
      lastSaved.current = next
      notify('ok', 'Limits saved.')
      setIntentRevision((n) => n + 1)
    } catch (err) {
      setConfig(lastSaved.current)
      notify('err', errorText(err, 'Failed to save limits.'))
    }
  }

  const commitConfig = (raw: OrchestratorConfig) => {
    clearTimeout(idleTimer.current) // this save already carries any pending duration
    idleTimer.current = undefined
    const next = intoRange(raw)
    setConfig(next)
    if (lastSaved.current && raisesLimit(lastSaved.current, next)) {
      setPending({ kind: 'config', next })
      return
    }
    persist(next)
  }

  commitRef.current = commitConfig

  const applyAndSave = (patch: Partial<OrchestratorConfig>) => commitConfig({ ...config, ...patch })

  // The picker reports every digit typed; wait for a pause before saving, so a loosening
  // does not open the confirm half-way through "12".
  const setDuration = (field: 'stale_threshold' | 'max_runtime_per_investigation', hours: number) => {
    const next = { ...configRef.current!, [field]: Math.round(hours * 3600) }
    configRef.current = next
    setConfig(next)
    clearTimeout(idleTimer.current)
    idleTimer.current = setTimeout(() => commitConfig(configRef.current!), 600)
  }

  const persistIfChanged = () => {
    if (JSON.stringify(config) !== JSON.stringify(lastSaved.current)) commitConfig(config)
  }

  const saveApproval = async (enabled: boolean) => {
    try {
      await approval.save(enabled)
      notify('ok', 'Limits saved.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to save limits.'))
    }
  }

  const selectAsk = () => {
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
    if (pending?.kind === 'config' && lastSaved.current) setConfig(lastSaved.current)
    setPending(null)
  }

  let activeProfile: string | 'custom' = 'custom'
  for (const [key, profile] of Object.entries(profiles)) {
    if (matchesProfile(config, profile.values)) {
      activeProfile = key
      break
    }
  }
  const askFirst = approval.enabled || approval.environment_wins
  const dialog = pending ? pendingCopy(pending) : null

  const tag = (field: keyof OrchestratorConfig) => {
    if (isOutside(field)) return { text: 'Outside the allowed range', fair: true }
    const d = defaults?.[field] as number | undefined
    if (d === undefined || d === config[field]) return { text: 'Default', fair: false }
    const was = field.includes('cost') ? money(d) : field.endsWith('threshold') ? formatDuration(d / 3600) : d
    return { text: `Changed · default ${was}`, fair: false }
  }

  const row = (title: string, hint: string, field: keyof OrchestratorConfig, control: ReactNode) => {
    const t = tag(field)
    return (
      <div className="lim-row" key={field}>
        <span className="lim-row-title">
          {title}
          <span>{hint}</span>
        </span>
        <span className={`lim-row-tag${t.fair ? ' fair' : ''}`}>{t.text}</span>
        <span className="lim-row-ctl">{control}</span>
      </div>
    )
  }

  const scrub = (r: LimitRow) => {
    const b = bound(r.field)
    if (!b) return null
    return (
      <ScrubField
        name={r.title}
        label={r.label}
        prefix={r.prefix}
        unit={r.unit}
        value={config[r.field]}
        min={b.min}
        max={b.max}
        step={b.step}
        onCommit={(v) => applyAndSave({ [r.field]: v })}
      />
    )
  }

  const idle = bound('stale_threshold')
  const runtime = bound('max_runtime_per_investigation')
  const loop = bound('loop_interval')

  return (
    <>
      <SettingsCard
        wide
        title="Automatic investigation"
        desc="The fastest way to cap cost: turn it off, run it dry, or pick a smaller profile."
      >
        {status && (
          <div className={`settings-banner ${status.enabled ? 'ok' : 'info'} mb-3`}>
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
        <div className="lim-switch">
          <div className="toggle-row-text">
            <span className="toggle-row-label">Investigate new alerts automatically</span>
            <span className="toggle-row-hint">
              When off, alerts still arrive and are grouped into cases, but no agent starts until someone asks.
            </span>
          </div>
          <Toggle
            label="Investigate new alerts automatically"
            checked={config.enabled}
            onChange={(v) => applyAndSave({ enabled: v })}
          />
        </div>
        <div className="lim-switch">
          <div className="toggle-row-text">
            <span className="toggle-row-label">Dry run</span>
            <span className="toggle-row-hint">Agents gather evidence but skip every change, even ones you would approve.</span>
          </div>
          <Toggle label="Dry run" checked={config.dry_run} onChange={(v) => applyAndSave({ dry_run: v })} />
        </div>

        <div className="lim-profiles">
          {Object.entries(profiles).map(([key, profile]) => {
            const v = profile.values
            return (
              <button
                key={key}
                type="button"
                aria-pressed={activeProfile === key}
                onClick={() => applyAndSave(profile.values)}
                className={`lim-profile${activeProfile === key ? ' on' : ''}`}
              >
                <span className="lim-profile-head">
                  {profile.label}
                  {profile.recommended && <span className="lim-profile-flag">Recommended</span>}
                </span>
                <span className="lim-profile-vals">
                  {v.max_concurrent_agents} agents at once · {money(v.max_cost_per_investigation)} per investigation ·{' '}
                  {money(v.max_total_hourly_cost)} per hour
                </span>
                <span className="lim-profile-vals muted">
                  {v.max_iterations_per_agent} steps · {formatDuration(v.max_runtime_per_investigation / 3600)} longest run
                </span>
              </button>
            )
          })}
        </div>
        {activeProfile === 'custom' && (
          <div className="settings-banner info mt-3">
            <Icon name="info" size={14} />
            <span>Custom limits in effect — your values don’t match any profile. Pick one above or adjust the limits below.</span>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        wide
        title="Default limits for new cases"
        desc="Existing cases keep theirs. Drag a value left or right, or click it to type. Tightening applies when you let go; loosening asks you to confirm."
      >
        {outside.length > 0 && (
          <div className="settings-banner err mb-2">
            <Icon name="alert" size={14} />
            <span>
              Saved {outside.map((f) => `${f.replace(/_/g, ' ')} (${config[f] as number})`).join(', ')} outside the
              allowed range. The next save moves {outside.length === 1 ? 'it' : 'them'} to the nearest allowed value.
            </span>
          </div>
        )}
        {SCRUB_ROWS.map((r) => row(r.title, r.hint, r.field, scrub(r)))}
        {idle &&
          row(
            'Idle cut-off',
            'Time without progress before an agent is stopped',
            'stale_threshold',
            <DurationPicker
              label="Idle cut-off"
              value={config.stale_threshold / 3600}
              minMinutes={idle.min / 60}
              maxMinutes={idle.max / 60}
              onChange={(h) => setDuration('stale_threshold', h)}
            />,
          )}
        {FLEET_ROWS.map((r) => row(r.title, r.hint, r.field, scrub(r)))}
      </SettingsCard>

      <SettingsCard
        wide
        title="What tools may do on their own"
        desc="Applies to new cases. Individual tools can be changed in Agents & workflows › Tool permissions."
      >
        {approval.phase === 'error' ? (
          <div className="settings-banner err">
            <Icon name="alert" size={14} />
            <span>Could not load the tool setting. Reload to try again.</span>
          </div>
        ) : (
          <>
            {approval.environment_wins && (
              <div className="settings-banner info mb-3">
                <Icon name="info" size={14} />
                <span>The environment wins. On their own cannot be saved.</span>
              </div>
            )}
            <div className="lim-classes">
              <div className="lim-class good">
                <h4>Read-only tools</h4>
                <span>Look things up</span>
                <span className="lim-class-note">Never need approval</span>
              </div>
              <div className="lim-class fair">
                <h4>
                  Tools that change something <InfoTip label="About tools that change something" text={ACT_TIP} align="start" />
                </h4>
                <span>Reversible, such as revoking a session</span>
                <div className="lim-seg" role="group" aria-label="Tools that change something">
                  <button type="button" aria-pressed={!askFirst} onClick={selectAct}>On their own</button>
                  <button type="button" aria-pressed={askFirst} onClick={selectAsk}>Ask first</button>
                </div>
              </div>
              <div className="lim-class poor">
                <h4>Tools that cannot be undone</h4>
                <span>Such as isolating a host</span>
                <span className="lim-class-note">
                  <Icon name="lock" size={13} /> Always a person. This cannot be changed.
                </span>
              </div>
            </div>
          </>
        )}
      </SettingsCard>

      <SettingsCard
        wide
        title="Advanced"
        desc="Timing, the longest a case may run, and where investigation files go."
        actions={
          <button className="btn ghost" aria-expanded={advanced} onClick={() => setAdvanced((a) => !a)}>
            <Icon name={advanced ? 'chevD' : 'chevR'} /> {advanced ? 'Hide' : 'Show'}
          </button>
        }
      >
        {advanced ? (
          <div className="flex flex-col gap-4">
            {row(
              'Longest a case may run',
              'A case is stopped after this long',
              'max_runtime_per_investigation',
              runtime && (
                <DurationPicker
                  label="Longest a case may run"
                  value={config.max_runtime_per_investigation / 3600}
                  minMinutes={runtime.min / 60}
                  maxMinutes={runtime.max / 60}
                  onChange={(h) => setDuration('max_runtime_per_investigation', h)}
                />
              ),
            )}
            {loop &&
              row(
                'Loop interval',
                'How often the orchestrator checks for work',
                'loop_interval',
                <ScrubField
                  name="Loop interval"
                  label="Every"
                  unit="seconds"
                  value={config.loop_interval}
                  min={loop.min}
                  max={loop.max}
                  step={loop.step}
                  onCommit={(v) => applyAndSave({ loop_interval: v })}
                />,
              )}
            <Field label="Working directory" hint="Base path for investigation files">
              <TextInput
                value={config.workdir_base}
                onChange={(e) => setConfig((prev) => (prev ? { ...prev, workdir_base: e.target.value } : prev))}
                onBlur={persistIfChanged}
              />
            </Field>
            <IntentReportCard reloadKey={intentRevision} />
          </div>
        ) : (
          <span className="text-xs text-tx-3">Hidden — click Show to see timing, storage and the intent report.</span>
        )}
      </SettingsCard>

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
