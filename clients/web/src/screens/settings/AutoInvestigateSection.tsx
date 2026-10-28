// Changes save automatically, and take ~60s to apply (runtime-config TTL).
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { DurationPicker } from '../../shared/DurationPicker'
import { formatDuration } from '../../shared/duration'
import { Icon } from '../../shared/icons'
import { ScrubField } from '../../shared/ScrubField'
import { ConfirmDialog, Field, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import {
  matchesProfile,
  useCeiling,
  useOrchestrator,
  useUnverifiedTools,
  type InvestigationProfileValues,
  type OrchestratorBound,
  type OrchestratorConfig,
} from './useSettings'
import type { SectionProps } from './types'
import IntentReportCard from './IntentReportCard'
import type { EffectClass, ToolClass } from '../../services/api'
import { errorText } from '../../shared/errorText'

// Raising any of these needs a confirm; lowering applies at once. Settings also
// guards stale_threshold, which Setup's profile picker never changes.
const LIMIT_FIELDS = [
  'max_cost_per_investigation',
  'max_iterations_per_agent',
  'max_runtime_per_investigation',
  'max_concurrent_agents',
  'max_total_hourly_cost',
  'stale_threshold',
] as const satisfies readonly (keyof OrchestratorConfig)[]

const raisesLimit = (prev: OrchestratorConfig, next: OrchestratorConfig) =>
  LIMIT_FIELDS.some((field) => next[field] > prev[field])

type PendingSave =
  | { kind: 'config'; next: OrchestratorConfig }
  | { kind: 'act' }
  | { kind: 'irreversible' }
  | { kind: 'stop' }
  | { kind: 'tool'; name: string; to: EffectClass }

// Tighter last, as in core/response/ceiling.py; an unclassified tool loosens to anything
const CLASS_ORDER: EffectClass[] = ['read', 'read_unverified', 'reversible', 'irreversible']
const loosens = (before: ToolClass['effect_class'], after: EffectClass) =>
  before == null || CLASS_ORDER.indexOf(after) < CLASS_ORDER.indexOf(before)
const CHOICES: { to: Exclude<EffectClass, 'read_unverified'>; label: string }[] = [
  { to: 'read', label: 'Read' },
  { to: 'reversible', label: 'Reversible' },
  { to: 'irreversible', label: 'Cannot be undone' },
]
const CLASS_PHRASE: Record<EffectClass, string> = {
  read: 'a read',
  read_unverified: 'an unconfirmed read',
  reversible: 'reversible',
  irreversible: 'impossible to undo',
}
// Outside tools are flattened as server_tool; server names use hyphens, never underscores
const splitTool = (name: string) => {
  const at = name.indexOf('_')
  return at > 0 ? { server: name.slice(0, at), tool: name.slice(at + 1) } : { server: '', tool: name }
}
const STOPPED_LOOSEN = 'Stopped: resume first'
const TOOLS_SHOWN = 8

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
        body: 'Tools that change something, such as revoking a session, will stop waiting for a person.',
      }
    case 'irreversible':
      return {
        title: 'Let tools that cannot be undone act on their own?',
        body: 'Tools such as isolating a host will stop waiting for a person. Tools that change something will act on their own too.',
      }
    case 'stop':
      return {
        title: 'Stop autonomy?',
        body: 'No tool acts on its own, and unattended writes, proposals and outside actions are off until you resume; reads continue. Automatic investigation turns off and investigations in progress are marked failed.',
      }
    case 'tool':
      return pending.to === 'read'
        ? {
            title: `Confirm ${pending.name} as a read?`,
            body: 'Runs no person started call it without asking. Confirm only if it never changes anything.',
          }
        : {
            title: `Class ${pending.name} as ${CLASS_PHRASE[pending.to]}?`,
            body: 'Runs no person started may then call it whenever tools of that kind may act on their own.',
          }
    default: {
      const _exhaustive: never = pending
      return _exhaustive
    }
  }
}

const money = (v: number) => `$${v}`

// One-phrase summaries from the board; Balanced has none (its claim is not checkable).
const PROFILE_SUMMARY: Partial<Record<string, string>> = {
  conservative: 'Least spend',
  aggressive: 'Widest coverage',
}

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
  const { config, setConfig, defaults, bounds, profiles, phase, reload, save } = useOrchestrator()
  const ceiling = useCeiling()
  const unverified = useUnverifiedTools()
  const [allTools, setAllTools] = useState(false)
  const { hasPermission } = useAuth()
  // write raises, resumes and switches four-eyes; stop stops and lowers
  const canRaise = hasPermission('ai_autonomy.write')
  const canLower = canRaise || hasPermission('ai_autonomy.stop')
  const lastSaved = useRef<OrchestratorConfig | null>(null)
  const configRef = useRef<OrchestratorConfig | null>(null)
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const [advanced, setAdvanced] = useState(false)
  const [intentRevision, setIntentRevision] = useState(0)
  const [pending, setPending] = useState<PendingSave | null>(null)

  configRef.current = config
  useEffect(() => {
    if (phase === 'ready') lastSaved.current = config
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])
  // leaving the page inside the debounce still saves the pending duration
  const commitRef = useRef<((cfg: OrchestratorConfig) => void) | undefined>(undefined)
  useEffect(
    () => () => {
      if (idleTimer.current) {
        clearTimeout(idleTimer.current)
        commitRef.current?.(configRef.current!)
      }
    },
    [],
  )

  if (phase === 'loading' || ceiling.phase === 'loading') {
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

  const saveCeiling = async (patch: Parameters<typeof ceiling.save>[0]) => {
    try {
      await ceiling.save(patch)
      notify('ok', 'Limits saved.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to save limits.'))
    }
  }

  const stopAll = async () => {
    try {
      await ceiling.stop()
      notify('ok', 'Stopped. Automatic investigation is off.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to stop.'))
    }
    reload() // kill turned automatic investigation off
  }

  const resume = async () => {
    try {
      await ceiling.resume()
      notify('ok', 'Resumed. Automatic investigation stays off until you turn it on.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to resume.'))
    }
  }

  const classifyTool = async (name: string, to: EffectClass) => {
    try {
      await unverified.classify(name, to)
      notify('ok', `${name} is now ${CLASS_PHRASE[to]}.`)
    } catch (err) {
      notify('err', errorText(err, 'Failed to save the tool’s class.'))
      ceiling.refresh() // a 409 means someone stopped meanwhile
    }
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
        saveCeiling({ act_reversible: true })
        return
      case 'irreversible':
        saveCeiling({ act_reversible: true, act_irreversible: true })
        return
      case 'stop':
        stopAll()
        return
      case 'tool':
        classifyTool(current.name, current.to)
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
  const ceil = ceiling.ceiling
  const locked = Boolean(ceil?.environment_locked)
  // stored flags drive the controls; the lock shows them as Ask first
  const reversibleOn = Boolean(ceil?.act_reversible) && !locked
  const irreversibleOn = Boolean(ceil?.act_irreversible) && !locked
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

  // the environment lock disables raising; lowering a stored flag still saves.
  // Without ai_autonomy.write there is no raise control: stop holders can only lower.
  const seg = (label: string, on: boolean, stored: boolean, raise: () => void, lower: () => void) => (
    <>
      {canRaise ? (
        <div className="lim-seg" role="group" aria-label={label}>
          <button type="button" aria-pressed={on} disabled={locked} onClick={on ? undefined : raise}>
            {locked && <Icon name="lock" size={12} />} On their own
          </button>
          <button type="button" aria-pressed={!on} onClick={stored ? lower : undefined}>
            Ask first
          </button>
        </div>
      ) : (
        <span className="lim-class-note">
          {on ? 'On their own.' : 'Ask first.'}
          {canLower && stored && (
            <button type="button" className="btn ghost" onClick={lower}>
              Switch to Ask first
            </button>
          )}
        </span>
      )}
      {locked && (
        <span className="lim-class-note">
          <Icon name="lock" size={13} /> Locked by the environment
        </span>
      )}
    </>
  )

  // write may loosen (confirm first, refused while stopped); stop may only tighten
  const toolRow = (t: ToolClass, stopped: boolean) => {
    const { server, tool } = splitTool(t.name)
    const choices = CHOICES.filter((c) => canRaise || (canLower && !loosens(t.effect_class, c.to)))
    return (
      <div className="lim-row" key={t.name}>
        <span className="lim-row-title">
          <span className="lim-tool-name" title={t.name}>{tool}</span>
          <span title={server}>{server || 'Unknown server'}</span>
        </span>
        <span className={`lim-row-tag${t.effect_class ? '' : ' fair'}`}>
          {t.effect_class ? 'Runs as a read, unconfirmed' : 'Unclassified, waits for a person'}
        </span>
        <span className="lim-row-ctl">
          {choices.length > 0 && (
            <div className="lim-seg" role="group" aria-label={`Class of ${t.name}`}>
              {choices.map((c) => {
                const loosen = loosens(t.effect_class, c.to)
                const blocked = loosen && stopped
                return (
                  <button
                    key={c.to}
                    type="button"
                    aria-pressed={false}
                    disabled={blocked}
                    title={blocked ? STOPPED_LOOSEN : undefined}
                    onClick={() =>
                      loosen ? setPending({ kind: 'tool', name: t.name, to: c.to }) : classifyTool(t.name, c.to)
                    }
                  >
                    {c.label}
                  </button>
                )
              })}
            </div>
          )}
        </span>
      </div>
    )
  }

  const toolList = (stopped: boolean) => {
    const tools = (unverified.phase === 'ready' && unverified.tools) || []
    const shown = allTools ? tools : tools.slice(0, TOOLS_SHOWN)
    return (
      <div className="lim-tools mt-3">
        <div className="lim-tools-head">
          <h4>
            Tools nobody has confirmed{' '}
            {unverified.phase === 'ready' && <span className="lim-tools-count">{tools.length}</span>}
          </h4>
          <span>
            Each outside tool’s class decides when it may act on its own. These were guessed to be reads, or have
            no class yet.
          </span>
          {!canLower && tools.length > 0 && <span>Your role can see these but not change them.</span>}
          {canLower && !canRaise && tools.length > 0 && (
            <span>Your role can mark a tool reversible or cannot be undone. Confirming a read, or classing a new tool, needs permission to loosen autonomy.</span>
          )}
          {stopped && canRaise && tools.length > 0 && (
            <span className="lim-class-note">
              <Icon name="alert" size={13} /> {STOPPED_LOOSEN}
            </span>
          )}
        </div>
        {unverified.phase === 'loading' && <span className="text-xs text-tx-3">Loading tools…</span>}
        {unverified.phase === 'error' && (
          <span className="text-xs text-tx-3">Could not load the tools. Reload to try again.</span>
        )}
        {unverified.phase === 'ready' && tools.length === 0 && (
          <span className="text-xs text-tx-3">Every outside tool has a confirmed class.</span>
        )}
        {shown.map((t) => toolRow(t, stopped))}
        {tools.length > TOOLS_SHOWN && (
          <button type="button" className="btn ghost mt-2" onClick={() => setAllTools((a) => !a)}>
            {allTools ? 'Show fewer' : `Show all ${tools.length}`}
          </button>
        )}
      </div>
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
        <div className="lim-switch">
          <div className="toggle-row-text">
            <span className="toggle-row-label">Investigate new alerts automatically</span>
            <span className="toggle-row-hint">
              When off, alerts still arrive and wait in the Triage queue; no case opens and no agent starts. Alerts waiting over 4 hours expire when it is turned back on.
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
                {PROFILE_SUMMARY[key] && <span className="lim-profile-sum">{PROFILE_SUMMARY[key]}</span>}
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
        desc="Applies at once, to cases already running. Each workflow’s own tool permissions are shown in Agents & workflows."
        actions={
          ceil &&
          (ceil.stopped
            ? canRaise && (
                <button className="btn" onClick={resume}>
                  <Icon name="play" /> Resume
                </button>
              )
            : canLower && (
                <button className="btn danger" onClick={() => setPending({ kind: 'stop' })}>
                  <Icon name="stop" /> Stop
                </button>
              ))
        }
      >
        {!ceil ? (
          <div className="settings-banner err">
            <Icon name="alert" size={14} />
            <span>Could not load the autonomy ceiling. Reload to try again.</span>
          </div>
        ) : (
          <>
            {ceil.stopped && (
              <div className="settings-banner err mb-3">
                <Icon name="alert" size={14} />
                <span>Stopped. Unattended writes, proposals and outside actions are off until someone resumes; reads continue. The choices below apply after that.</span>
              </div>
            )}
            {locked && (
              <div className="settings-banner info mb-3">
                <Icon name="lock" size={14} />
                <span>The environment wins. Tools ask first here, and On their own cannot be saved.</span>
              </div>
            )}
            <div className="lim-classes">
              <div className="lim-class good">
                <h4>Read, write and propose</h4>
                <span>Look things up, note findings, suggest a change</span>
                <span className="lim-class-note">
                  {ceil.read && ceil.write && ceil.propose
                    ? 'On. Never need approval.'
                    : `Off while stopped: ${[!ceil.read && 'read', !ceil.write && 'write', !ceil.propose && 'propose']
                        .filter(Boolean)
                        .join(', ')}.`}
                </span>
              </div>
              <div className="lim-class fair">
                <h4>Tools that change something</h4>
                <span>Reversible, such as revoking a session</span>
                {seg(
                  'Tools that change something',
                  reversibleOn,
                  Boolean(ceil.act_reversible || ceil.act_irreversible),
                  () => setPending({ kind: 'act' }),
                  // Assist: both act flags off
                  () => saveCeiling({ act_reversible: false, act_irreversible: false }),
                )}
              </div>
              <div className="lim-class poor">
                <h4>Tools that cannot be undone</h4>
                <span>Such as isolating a host</span>
                {seg(
                  'Tools that cannot be undone',
                  irreversibleOn,
                  ceil.act_irreversible,
                  () => setPending({ kind: 'irreversible' }),
                  () => saveCeiling({ act_irreversible: false }),
                )}
              </div>
            </div>
            <div className="lim-switch mt-3">
              <div className="toggle-row-text">
                <span className="toggle-row-label">Four-eyes approval</span>
                <span className="toggle-row-hint">
                  Nobody approves an action they proposed, or one proposed by a run that acted for them.
                </span>
              </div>
              {canRaise ? (
                <Toggle
                  label="Four-eyes approval"
                  checked={ceil.four_eyes}
                  onChange={(v) => saveCeiling({ four_eyes: v })}
                />
              ) : (
                <span className="text-sm text-tx-2">{ceil.four_eyes ? 'On' : 'Off'}</span>
              )}
            </div>
            {toolList(ceil.stopped)}
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
        confirmLabel={pending?.kind === 'stop' ? 'Stop' : pending?.kind === 'tool' ? 'Confirm' : 'Save'}
        danger={pending?.kind === 'stop'}
        onConfirm={confirmPending}
        onClose={dismissPending}
      />
    </>
  )
}
