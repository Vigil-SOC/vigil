// Changes save automatically, and take ~60s to apply (runtime-config TTL).
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { DurationPicker } from '../../shared/DurationPicker'
import { formatDuration } from '../../shared/duration'
import { Icon } from '../../shared/icons'
import { ScrubField } from '../../shared/ScrubField'
import { ConfirmDialog, Field, NumberInput, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import { NotMeasured } from '../../shared/NotMeasured'
import { utcDayClock } from '../../shared/utc'
import { agreementText, dropWhy, falseCloseText, modelsInOrder, workflowTier, type WorkflowTier } from '../../shared/autonomy'
import {
  matchesProfile,
  useCeiling,
  useEmulation,
  useInvestigateWorkflows,
  useOrchestrator,
  useUnverifiedTools,
  type InvestigateWorkflow,
  type InvestigationProfileValues,
  type OrchestratorBound,
  type OrchestratorConfig,
} from './useSettings'
import type { SectionProps } from './types'
import IntentReportCard from './IntentReportCard'
import SiteContextCard from './SiteContextCard'
import RolloutLimitsCard from './RolloutLimitsCard'
import EmulatorsBlock from './EmulatorsBlock'
import {
  aiConfigApi,
  approvalsApi,
  configApi,
  detectionRulesApi,
  type Ceiling,
  type InvestigationReplayResponse,
  type EffectClass,
  type OverlayOnlyRule,
  type ToolClass,
} from '../../services/api'
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
  | { kind: 'workflow'; workflow: InvestigateWorkflow }

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
type HeldRow = Ceiling['held'][number]
const CAP_MAX = 1000

/** The ceiling's hourly release cap. A lower saves for anyone who can lower; a raise needs
 * ai_autonomy.write and no stop, and says so before it is tried. */
function ReleaseCap({
  ceil,
  canRaise,
  canLower,
  save,
  notify,
}: {
  ceil: Ceiling
  canRaise: boolean
  canLower: boolean
  save: (patch: { released_per_hour: number }) => Promise<boolean>
  notify: SectionProps['notify']
}) {
  const saved = ceil.released_per_hour
  const [draft, setDraft] = useState(String(saved))
  // Every read of the ceiling, a refused save's refresh included, resets the field.
  useEffect(() => setDraft(String(saved)), [ceil, saved])
  // Why a raise would be refused, said before anyone tries.
  const raiseRefusal = !canRaise
    ? 'Your role can lower this. Raising it needs permission to loosen autonomy.'
    : ceil.stopped
      ? `${STOPPED_LOOSEN}: a higher number is refused; a lower one saves.`
      : null
  const commit = () => {
    const n = Number(draft)
    if (draft.trim() === '' || !Number.isInteger(n) || n < 0 || n > CAP_MAX) {
      setDraft(String(saved))
      notify('err', `Enter a whole number from 0 to ${CAP_MAX}.`)
      return
    }
    if (n === saved) return
    if (n > saved && raiseRefusal) {
      setDraft(String(saved))
      notify('err', raiseRefusal)
      return
    }
    // A refused save whose refresh also failed leaves ceil as it was: put the saved value back.
    void save({ released_per_hour: n }).then((ok) => ok || setDraft(String(saved)))
  }
  return (
    // Wraps so a narrow screen puts the field under the text, not beside it.
    <div className="lim-switch mt-3 flex-wrap">
      <div className="toggle-row-text">
        <label className="toggle-row-label" htmlFor="lim-released-per-hour">
          Actions released per hour
        </label>
        <span className="toggle-row-hint" id="lim-released-per-hour-hint">
          The most proposals from runs no person started that the ceiling carries out in any 60 minutes. Past it, each
          waits for a person and its case says why. 0 releases none.
        </span>
        {!canRaise && canLower && <span className="toggle-row-hint">{raiseRefusal}</span>}
        {canRaise && raiseRefusal && (
          <span className="lim-class-note">
            <Icon name="alert" size={13} className="shrink-0" /> {raiseRefusal}
          </span>
        )}
      </div>
      {canLower ? (
        <span className="flex items-center gap-2 shrink-0">
          <NumberInput
            id="lim-released-per-hour"
            aria-describedby="lim-released-per-hour-hint"
            style={{ width: 96 }}
            min={0}
            max={raiseRefusal ? saved : CAP_MAX}
            step={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => e.key === 'Enter' && commit()}
          />
          <span className="text-xs text-tx-3">per hour</span>
        </span>
      ) : (
        <span className="text-sm text-tx-2">{saved} per hour</span>
      )}
    </div>
  )
}

/** The ceiling stores a naive UTC stamp; without an offset a browser reads it as local. */
function utcStamp(iso: string | null | undefined): string | null {
  if (!iso) return null
  return utcDayClock(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`)
}

function stoppedBanner(ceil: Ceiling): string {
  const who = ceil.stopped_by_label || ceil.stopped_by
  const when = utcStamp(ceil.stopped_at)
  const by = who ? ` by ${who}${when ? ` ${when}` : ''}` : ''
  return (
    `Stopped${by}. Unattended writes, proposals, outside actions and Vigil’s own detection rules ` +
    'are off until someone resumes; runs no person started can’t reach restricted cases, and other reads continue.'
  )
}

type WorkflowChoice = 'fleet' | 'act' | 'assist'
/** A workflow INTENT.md manages: its declared entry and where it is declared. */
type ManagedFlow = { declared: { tier: 'act' | 'assist'; floor?: number }; file: string; line: number; revision: string }
const WORKFLOW_CHOICES: { to: WorkflowChoice; label: string }[] = [
  { to: 'fleet', label: 'Follows fleet' },
  { to: 'act', label: 'Act' },
  { to: 'assist', label: 'Assist' },
]
const pct = (rate: number) => `${Math.round(rate * 100)}%`

/** The row's last change: who raised it and on what agreement, or Vigil taking it back. */
function lastChange(entry: WorkflowTier | undefined): string {
  if (!entry) return ''
  // the ceiling stores naive UTC stamps; without an offset a browser reads them as local time
  const at = utcDayClock(/(Z|[+-]\d\d:?\d\d)$/.test(entry.at) ? entry.at : `${entry.at}Z`) ?? ''
  const rate = entry.evidence?.agreement_rate
  if (entry.by === 'system' && entry.from_model) {
    // A model-change drop: the stored reason ends with why, in brackets; it names no floor shortfall.
    const why = dropWhy(entry.reason)
    return `Lowered by Vigil ${at} on a model change: ${entry.from_model} → ${entry.model ?? 'unknown model'}${why ? ` · ${why}` : ''}`
  }
  // The INTENT.md applier also writes as system: Act at a raised floor, or Assist with no record.
  if (entry.by === 'system' && entry.tier === 'act') return `Floor set to ${entry.floor}% by INTENT.md ${at}`
  if (entry.by === 'system' && !entry.evidence) return `Set to Assist by INTENT.md ${at}`
  if (entry.by === 'system') {
    // rounded down: the server drops below the exact floor, so 79.5% must not read as 80%
    const agree = rate == null ? 'Not measured' : `${Math.floor(rate * 100)}%`
    // The stored evidence names the drop's false-close sample; a null rate was not measured.
    const close = falseCloseText(entry.evidence) ?? 'false close not measured'
    return `Lowered by Vigil ${at}: ${agree} under the ${entry.floor}% floor · ${close}`
  }
  const who = entry.by_label || entry.by
  if (entry.tier === 'assist') return `Set to Assist by ${who} ${at}`
  const over = entry.evidence ? entry.evidence.agreed + entry.evidence.disagreed : 0
  return `Raised by ${who} ${at} at ${rate == null ? 'Not measured' : `${pct(rate)} over ${over}`}`
}
const TOOLS_SHOWN = 8

/** The latest replay's line for one workflow, marked as a replay with its model and start date. */
function replayLine(replay: InvestigationReplayResponse | null, id: string): string | null {
  const part = replay?.status === 'succeeded' ? replay.by_workflow?.[id] : undefined
  if (!replay || !part) return null
  const agree = agreementText({ agreed: part.agreed, disagreed: part.disagreed, agreement_rate: part.agreement_rate ?? null })
  // falseCloseText reads answered / wilson_*; the replay names them replayed / false_close_wilson_*
  const close = falseCloseText({
    false_close_rate: part.false_close_rate ?? null,
    answered: part.replayed,
    wilson_low: part.false_close_wilson_low ?? null,
    wilson_high: part.false_close_wilson_high ?? null,
  })?.replace(/^30 days · /, '')
  const when = utcStamp(replay.started_at)
  const figures = [agree?.text ?? 'agreement not measured', close ?? 'false close not measured'].join(' · ')
  return `Replay of ${replay.model_id}${when ? `, ${when}` : ''} · ${figures}`
}

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
        body: 'No tool acts on its own, and unattended writes, proposals, outside actions and Vigil’s own detection rules are off until you resume; runs no person started can’t reach restricted cases, and other reads continue. Automatic investigation turns off and investigations in progress are marked failed.',
      }
    case 'workflow': {
      const shown = agreementText(pending.workflow.agreement)
      const close = falseCloseText(pending.workflow.agreement) ?? 'false close not measured'
      return {
        title: `Let ${pending.workflow.name} act on its own?`,
        body: `Its tools that change something, such as revoking a session, will stop waiting for a person. Last 30 days: ${
          shown ? shown.text : 'not measured, no closure has been scored yet'
        }; ${close}. Below the floor, Vigil puts it back on Assist.`,
      }
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
  { field: 'max_cost_per_investigation', title: 'Budget per case', hint: "Checked before each model call, so a case can end above it by the cost of the call it was making (retries included). Calls that couldn't be priced (up to 3), calls whose usage was estimated, and requests billed but not recorded aren't fully counted. The allowance starts again when a run resumes. A case that reaches it ends; it doesn't ask.", label: 'Budget', prefix: '$', unit: 'per case' },
  { field: 'max_iterations_per_agent', title: 'Steps per case', hint: 'Steps before the case stops and asks', label: 'Steps', unit: 'per case' },
]
const FLEET_ROWS: LimitRow[] = [
  { field: 'max_concurrent_agents', title: 'Cases running at once, whole fleet', hint: 'More at once is faster but costs more per hour', label: 'Fleet', unit: 'at once' },
  { field: 'max_total_hourly_cost', title: 'Spend per hour, whole fleet', hint: 'Intake pauses when the last hour reaches this', label: 'Hourly cap', prefix: '$', unit: 'per hour' },
]

export default function AutoInvestigateSection({ notify }: SectionProps) {
  const { config, setConfig, defaults, bounds, profiles, phase, reload, save } = useOrchestrator()
  const ceiling = useCeiling()
  const investigate = useInvestigateWorkflows()
  // The latest replay, shown per workflow apart from live agreement; none or unreadable shows nothing.
  const [replay, setReplay] = useState<InvestigationReplayResponse | null>(null)
  useEffect(() => {
    aiConfigApi.getInvestigationReplay().then((r) => setReplay(r.data), () => setReplay(null))
  }, [])
  const [floor, setFloor] = useState('')
  const unverified = useUnverifiedTools()
  const emulation = useEmulation()
  const [allTools, setAllTools] = useState(false)
  const { hasPermission } = useAuth()
  // write raises, resumes and switches four-eyes; stop stops and lowers
  const canRaise = hasPermission('ai_autonomy.write')
  const canLower = canRaise || hasPermission('ai_autonomy.stop')
  const canDecide = hasPermission('ai_decisions.approve')
  const canReadSite = hasPermission('platform.read')
  const [rejectFor, setRejectFor] = useState<HeldRow | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [heldBusy, setHeldBusy] = useState<string | null>(null)
  const lastSaved = useRef<OrchestratorConfig | null>(null)
  const configRef = useRef<OrchestratorConfig | null>(null)
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const [advanced, setAdvanced] = useState(false)
  const [intentRevision, setIntentRevision] = useState(0)
  const [pending, setPending] = useState<PendingSave | null>(null)
  // Live rules only Vigil runs: Stop halts their detection, so the Stop prompt names them
  const [overlayOnly, setOverlayOnly] = useState<OverlayOnlyRule[]>([])
  const [deploying, setDeploying] = useState<number[] | 'all' | null>(null)
  const stopping = pending?.kind === 'stop'
  // The declared directory's act.hold_crown_jewels, when it manages that key: the API refuses a turn-off below it.
  const [holdManaged, setHoldManaged] = useState<{ declared: unknown; file?: string | null; line?: number | null } | null>(null)
  // Workflows the declared directory manages, by id: the API refuses a raise past the declared entry.
  const [flowManaged, setFlowManaged] = useState<Record<string, ManagedFlow>>({})
  useEffect(() => {
    let cancelled = false
    configApi
      .getIntent()
      .then((r) => {
        const row = r.data.rows?.find((x) => x.key === 'act.hold_crown_jewels' && x.managed)
        const flows: Record<string, ManagedFlow> = {}
        for (const x of r.data.rows ?? []) {
          if (x.managed && x.key.startsWith('workflows.'))
            flows[x.key.slice('workflows.'.length)] = {
              declared: x.declared as ManagedFlow['declared'],
              file: x.file ?? 'INTENT.md',
              line: x.line ?? 1,
              revision: x.revision ?? 'unknown',
            }
        }
        if (cancelled) return
        setHoldManaged(row ?? null)
        setFlowManaged(flows)
      })
      .catch(() => {
        if (cancelled) return
        setHoldManaged(null) // unread: the API still refuses
        setFlowManaged({})
      })
    return () => {
      cancelled = true
    }
  }, [intentRevision])
  // Opens Advanced, then scrolls once the site card has rendered.
  const [toSite, setToSite] = useState(0)
  const showSite = () => {
    setAdvanced(true)
    setToSite((n) => n + 1)
  }
  useEffect(() => {
    const el = toSite ? document.getElementById('declared-site') : null
    if (!el?.parentElement) return
    const go = () => el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    go()
    // The cards above it load after it opens; follow it while they settle.
    const ro = new ResizeObserver(go)
    ro.observe(el.parentElement)
    const stop = setTimeout(() => ro.disconnect(), 1500)
    return () => {
      clearTimeout(stop)
      ro.disconnect()
    }
  }, [toSite])
  const readOverlayOnly = () =>
    detectionRulesApi
      .getOverlayOnly()
      .then((r) => setOverlayOnly(r.data.rules))
      .catch(() => {}) // unread: the prompt says nothing extra
  useEffect(() => {
    setOverlayOnly([])
    if (stopping) void readOverlayOnly()
  }, [stopping])
  // Opens the deploy pull request now, as the rollout sweep would: the rule joins the SIEM once it merges.
  const deployOverlay = (ids?: number[]) => {
    setDeploying(ids ?? 'all')
    detectionRulesApi
      .deployOverlayOnly(ids)
      .then((r) => {
        const { opened, failed, skipped } = r.data
        if (opened.length) notify('ok', `${opened.length === 1 ? 'A deploy pull request is' : `${opened.length} deploy pull requests are`} open; each rule joins your SIEM when its PR merges.`)
        if (failed.length) notify('err', `${failed.length === 1 ? 'A deploy pull request' : `${failed.length} deploy pull requests`} could not be opened: ${failed[0].reason}`)
        if (!opened.length && !failed.length && skipped.length) notify('info', `Nothing opened: ${skipped[0].reason}.`)
        return readOverlayOnly()
      })
      .catch((e) => notify('err', errorText(e, 'The deploy pull request was not opened.')))
      .finally(() => setDeploying(null))
  }

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
      setIntentRevision((n) => n + 1) // a narrowing shows as drift
      return true
    } catch (err) {
      notify('err', errorText(err, 'Failed to save limits.'))
      return false
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

  const setWorkflow = async (w: InvestigateWorkflow, to: WorkflowChoice, floorPct?: number) => {
    try {
      await ceiling.setWorkflow(w.id, { tier: to === 'fleet' ? null : to, floor: floorPct ?? null })
      notify('ok', `${w.name} ${to === 'fleet' ? 'follows the fleet' : to === 'act' ? 'acts on its own' : 'asks first'}.`)
      setIntentRevision((n) => n + 1) // a managed workflow's narrowing shows as drift
    } catch (err) {
      notify('err', errorText(err, 'Failed to save the workflow’s autonomy.'))
    }
  }

  const floorValue = Number(floor)
  // A managed Act workflow may not be raised below its declared floor.
  const floorMin = (pending?.kind === 'workflow' && flowManaged[pending.workflow.id]?.declared.floor) || 1
  const floorValid = /^\d+$/.test(floor) && floorValue >= floorMin && floorValue <= 100

  const confirmPending = () => {
    if (!pending) return
    if (pending.kind === 'workflow' && !floorValid) return
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
      case 'workflow':
        setWorkflow(current.workflow, 'act', floorValue)
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
        readOnly={!canRaise}
        onCommit={(v) => applyAndSave({ [r.field]: v })}
      />
    )
  }

  // the environment lock disables raising; lowering a stored flag still saves.
  // Without ai_autonomy.write there is no raise control: stop holders can only lower.
  const seg = (label: string, on: boolean, stored: boolean, raise: () => void, lower: () => void) => {
    const raiseStopped = Boolean(ceil?.stopped) && !on
    return (
      <>
        {canRaise ? (
          <div className="lim-seg" role="group" aria-label={label}>
            <button
              type="button"
              aria-pressed={on}
              disabled={locked || raiseStopped}
              title={raiseStopped ? STOPPED_LOOSEN : undefined}
              onClick={on ? undefined : raise}
            >
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
        {raiseStopped && canRaise && (
          <span className="lim-class-note">
            <Icon name="alert" size={13} /> {STOPPED_LOOSEN}
          </span>
        )}
      </>
    )
  }

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
          {emulation.data?.tools.includes(t.name) && <span className="lim-emu-mark">Emulator</span>}{' '}
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

  // Act is always a raise; so is following a fleet on Act (either flag) from Assist. The server decides, this only picks the gate.
  const workflowRow = (w: InvestigateWorkflow, stopped: boolean) => {
    const entry = workflowTier(ceil, w.id)
    const current: WorkflowChoice = entry?.tier ?? 'fleet'
    const fleetActs = Boolean(ceil?.act_reversible || ceil?.act_irreversible)
    const raises = (to: WorkflowChoice) => to === 'act' || (to === 'fleet' && current === 'assist' && fleetActs)
    const managed = flowManaged[w.id]
    const where = managed && `${managed.file}:${managed.line} @ ${managed.revision}`
    // As the server judges it: past an Assist declaration, Act or a fleet that acts is a raise.
    const pastDeclared = (to: WorkflowChoice) =>
      managed?.declared.tier === 'assist' && (to === 'act' || (to === 'fleet' && fleetActs))
    const choices = WORKFLOW_CHOICES.filter((c) => c.to === current || (raises(c.to) ? canRaise : canLower))
    const shown = agreementText(w.agreement)
    const close = falseCloseText(w.agreement)
    const change = lastChange(entry)
    const unattributed = w.agreement?.unattributed ?? 0
    const replayed = replayLine(replay, w.id)
    return (
      <div className={`lim-row lim-wf${choices.length < 2 ? ' lim-wf-static' : ''}`} key={w.id}>
        <span className="lim-row-title">
          {w.name}
          <span>
            {shown ? shown.text : <NotMeasured />}
            {' · '}
            {close ?? <NotMeasured label="False close" />}
          </span>
          {/* each model's own record, the tier's model first; the blended line above is the workflow's */}
          {entry?.model && !w.agreement?.by_model.some((m) => m.model === entry.model) && (
            <span className="lim-wf-model">
              <b>{entry.model}</b> · no live record in 30 days
            </span>
          )}
          {modelsInOrder(w.agreement, entry?.model).map((m) => (
            <span className="lim-wf-model" key={m.model}>
              <b>{m.model}</b>
              {' · '}
              {agreementText(m)?.text ?? 'agreement not measured'}
              {' · '}
              {falseCloseText(m)?.replace(/^30 days · /, '') ?? 'false close not measured'}
            </span>
          ))}
          {unattributed > 0 && (
            <span className="lim-wf-model">
              {unattributed} {unattributed === 1 ? 'close' : 'closes'} not attributed to one model
            </span>
          )}
          {replayed && <span className="lim-wf-model">{replayed}</span>}
        </span>
        <span className="lim-wf-tag">
          <span>
            {entry?.floor ? `Floor ${entry.floor}%` : 'No floor'}
            {entry?.model && ` · on ${entry.model}`}
          </span>
          {change && <span title={change}>{change}</span>}
          {managed && (
            <span title={`Declared ${managed.declared.tier === 'act' ? `Act · floor ${managed.declared.floor}` : 'Assist'}`}>
              Managed by {where}
            </span>
          )}
        </span>
        <span className="lim-row-ctl">
          {choices.length < 2 ? (
            // nothing else this person may pick: the current setting, as text
            <span className="text-sm text-tx-2">{WORKFLOW_CHOICES.find((c) => c.to === current)?.label}</span>
          ) : (
            <div className="lim-seg" role="group" aria-label={`Autonomy of ${w.name}`}>
              {choices.map((c) => {
                const declaredPast = c.to !== current && pastDeclared(c.to)
                const blocked = declaredPast || (c.to !== current && raises(c.to) && (stopped || (c.to === 'act' && locked)))
                return (
                  <button
                    key={c.to}
                    type="button"
                    aria-pressed={c.to === current}
                    disabled={blocked}
                    title={
                      declaredPast
                        ? `Managed by ${where}, declared Assist. Change it in the declared directory.`
                        : blocked
                          ? stopped
                            ? STOPPED_LOOSEN
                            : 'Locked by the environment'
                          : undefined
                    }
                    onClick={() => {
                      if (c.to === current) return
                      if (c.to === 'act') {
                        // A managed Act workflow is restored at its declared floor.
                        setFloor(managed?.declared.floor ? String(managed.declared.floor) : '')
                        setPending({ kind: 'workflow', workflow: w })
                      } else setWorkflow(w, c.to)
                    }}
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

  const workflowList = (stopped: boolean) => (
    <div className="lim-tools mt-3">
      <div className="lim-tools-head">
        <h4>Each investigation workflow</h4>
        <span>
          Act lets one workflow’s tools that change something act on their own, whatever the fleet does. Vigil puts it
          back on Assist when its agreement with people falls below the floor.
        </span>
        {!canLower && <span>Your role can see these but not change them.</span>}
        {canLower && !canRaise && <span>Your role can put a workflow on Assist. Letting one act needs permission to loosen autonomy.</span>}
      </div>
      {investigate.phase === 'loading' && <span className="text-xs text-tx-3">Loading workflows…</span>}
      {investigate.phase === 'error' && (
        <span className="text-xs text-tx-3">Could not load the workflows. Reload to try again.</span>
      )}
      {investigate.phase === 'ready' && investigate.workflows?.length === 0 && (
        <span className="text-xs text-tx-3">No investigation workflow is installed.</span>
      )}
      {investigate.workflows?.map((w) => workflowRow(w, stopped))}
    </div>
  )

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

  const releaseOne = async (actionId: string) => {
    setHeldBusy(actionId)
    try {
      await ceiling.releaseHeld(actionId)
      notify('ok', 'Released. It runs on the next pass, once autonomy is not stopped.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to release the action.'))
    } finally {
      setHeldBusy(null)
    }
  }

  const releaseAll = async () => {
    setHeldBusy('all')
    try {
      await ceiling.releaseAllHeld()
      notify('ok', 'Released. Each one runs on the next pass, once autonomy is not stopped.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to release the held actions.'))
    } finally {
      setHeldBusy(null)
    }
  }

  const rejectHeld = async () => {
    if (!rejectFor || !rejectReason.trim()) return
    const row = rejectFor
    setHeldBusy(row.action_id)
    try {
      await approvalsApi.reject(row.action_id, rejectReason.trim())
      setRejectFor(null)
      setRejectReason('')
      await ceiling.refresh()
      notify('ok', 'Rejected.')
    } catch (err) {
      notify('err', errorText(err, 'Failed to reject the action.'))
    } finally {
      setHeldBusy(null)
    }
  }

  const heldList = (view: NonNullable<typeof ceil>) => {
    const rows = view.held ?? []
    if (rows.length === 0) return null
    const releasable = rows.filter((row) => !row.restricted)
    return (
      <div className="lim-tools mt-3">
        <div className="lim-tools-head">
          <h4>
            Actions the stop is holding <span className="lim-tools-count">{rows.length}</span>
          </h4>
          <span>
            Approved outside actions stay held after a resume until someone releases them. The next pass runs a
            released one.
          </span>
          {!canDecide && releasable.length > 0 && (
            <span>Releasing or rejecting one needs permission to approve a decision.</span>
          )}
        </div>
        {rows.map((row) => (
          <div className="lim-row lim-held" key={row.action_id}>
            <span className="lim-row-title">
              <span className="lim-tool-name">{row.title}</span>
              {!row.restricted && (
                <span>
                  {row.action_type}
                  {row.target ? ` · ${row.target}` : ''}
                  {row.approved_by ? ` · approved by ${row.approved_by}` : ''}
                </span>
              )}
            </span>
            <span className="lim-row-tag">{row.restricted ? 'Restricted' : 'Held'}</span>
            <span className="lim-row-ctl lim-held-ctl">
              {canDecide && !row.restricted && (
                <>
                  <button
                    type="button"
                    className="btn"
                    disabled={heldBusy != null}
                    onClick={() => releaseOne(row.action_id)}
                  >
                    {heldBusy === row.action_id ? 'Releasing…' : 'Release'}
                  </button>
                  <button
                    type="button"
                    className="btn danger"
                    disabled={heldBusy != null}
                    onClick={() => {
                      setRejectReason('')
                      setRejectFor(row)
                    }}
                  >
                    Reject
                  </button>
                </>
              )}
            </span>
          </div>
        ))}
        {canDecide && releasable.length > 1 && (
          <button type="button" className="btn ghost mt-2" disabled={heldBusy != null} onClick={releaseAll}>
            {heldBusy === 'all' ? 'Releasing…' : 'Release all'}
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
          {canRaise ? (
            <Toggle
              label="Investigate new alerts automatically"
              checked={config.enabled}
              onChange={(v) => applyAndSave({ enabled: v })}
            />
          ) : (
            <span className="text-sm text-tx-2">{config.enabled ? 'On' : 'Off'}</span>
          )}
        </div>
        <div className="lim-switch">
          <div className="toggle-row-text">
            <span className="toggle-row-label">Dry run</span>
            <span className="toggle-row-hint">Agents gather evidence but skip every change, even ones you would approve.</span>
          </div>
          {canRaise ? (
            <Toggle label="Dry run" checked={config.dry_run} onChange={(v) => applyAndSave({ dry_run: v })} />
          ) : (
            <span className="text-sm text-tx-2">{config.dry_run ? 'On' : 'Off'}</span>
          )}
        </div>

        <div className="lim-profiles">
          {Object.entries(profiles).map(([key, profile]) => {
            const v = profile.values
            return (
              <button
                key={key}
                type="button"
                aria-pressed={activeProfile === key}
                disabled={!canRaise}
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
        desc="Existing cases keep theirs. Click a value to type a new one. Tightening applies at once; loosening asks you to confirm."
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
              readOnly={!canRaise}
            />,
          )}
        {FLEET_ROWS.map((r) => row(r.title, r.hint, r.field, scrub(r)))}
      </SettingsCard>

      <RolloutLimitsCard notify={notify} canWrite={canRaise} />

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
              <div className="settings-banner err lim-stopped mb-3">
                <Icon name="alert" size={14} />
                <span>
                  {stoppedBanner(ceil)}
                </span>
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
            <ReleaseCap ceil={ceil} canRaise={canRaise} canLower={canLower} save={saveCeiling} notify={notify} />
            <div className="lim-switch">
              <div className="toggle-row-text">
                <span className="toggle-row-label">Four-eyes approval</span>
                <span className="toggle-row-hint">
                  Nobody approves an action they proposed, or one proposed by a run that acted for them.
                </span>
                {ceil.stopped && ceil.four_eyes && canRaise && (
                  <span className="lim-class-note">
                    <Icon name="alert" size={13} /> {STOPPED_LOOSEN}
                  </span>
                )}
              </div>
              {canRaise ? (
                <Toggle
                  label="Four-eyes approval"
                  checked={ceil.four_eyes}
                  disabled={ceil.stopped && ceil.four_eyes}
                  title={ceil.stopped && ceil.four_eyes ? STOPPED_LOOSEN : undefined}
                  onChange={(v) => saveCeiling({ four_eyes: v })}
                />
              ) : (
                <span className="text-sm text-tx-2">{ceil.four_eyes ? 'On' : 'Off'}</span>
              )}
            </div>
            {(() => {
              const on = ceil.hold_crown_jewels
              const n = ceil.crown_jewels
              const managedOn = holdManaged?.declared === true
              // Why a turn-off would be refused, said before anyone tries.
              const refusal = !on
                ? null
                : managedOn
                  ? `Managed by ${holdManaged?.file ?? 'INTENT.md'}:${holdManaged?.line ?? 1}, declared on. Change it in the declared directory.`
                  : ceil.stopped
                    ? STOPPED_LOOSEN
                    : null
              return (
                <div className="lim-switch">
                  <div className="toggle-row-text">
                    <span className="toggle-row-label">Hold crown jewels</span>
                    <span className="toggle-row-hint">
                      A model’s close, exception, dismissal or outside call on a declared crown jewel waits for a person.
                    </span>
                    <span className="toggle-row-hint">
                      {n === 0 ? 'No crown jewels declared.' : n === 1 ? '1 crown jewel declared.' : `${n} crown jewels declared.`}{' '}
                      {canReadSite && (
                        <button type="button" className="stop-link" onClick={showSite}>
                          View the declared site
                        </button>
                      )}
                    </span>
                    {refusal && canRaise && (
                      <span className="lim-class-note">
                        <Icon name={managedOn ? 'lock' : 'alert'} size={13} className="shrink-0" /> {refusal}
                      </span>
                    )}
                  </div>
                  {canRaise ? (
                    <Toggle
                      label="Hold crown jewels"
                      checked={on}
                      disabled={refusal != null}
                      title={refusal ?? undefined}
                      onChange={(v) => saveCeiling({ hold_crown_jewels: v })}
                    />
                  ) : canLower && !on ? (
                    <Toggle label="Hold crown jewels" checked={false} onChange={() => saveCeiling({ hold_crown_jewels: true })} />
                  ) : (
                    <span className="text-sm text-tx-2">{on ? 'On' : 'Off'}</span>
                  )}
                </div>
              )
            })()}
            {heldList(ceil)}
            {workflowList(ceil.stopped)}
            {toolList(ceil.stopped)}
            <EmulatorsBlock
              emulation={emulation}
              stopped={ceil.stopped}
              canRaise={canRaise}
              canLower={canLower}
              notify={notify}
            />
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
                  readOnly={!canRaise}
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
                  readOnly={!canRaise}
                  onCommit={(v) => applyAndSave({ loop_interval: v })}
                />,
              )}
            <Field label="Working directory" hint="Base path for investigation files">
              <TextInput
                value={config.workdir_base}
                onChange={(e) => setConfig((prev) => (prev ? { ...prev, workdir_base: e.target.value } : prev))}
                readOnly={!canRaise}
                onBlur={persistIfChanged}
              />
            </Field>
            <IntentReportCard
              reloadKey={intentRevision}
              notify={notify}
              onAccepted={() => {
                void ceiling.refresh()
                reload()
                setIntentRevision((n) => n + 1)
              }}
              workflowNames={Object.fromEntries((investigate.workflows ?? []).map((w) => [w.id, w.name]))}
            />
            {canReadSite && (
              <div id="declared-site">
                <SiteContextCard reloadKey={intentRevision} />
              </div>
            )}
          </div>
        ) : (
          <span className="text-xs text-tx-3">Hidden — click Show to see timing, storage, the intent report and the declared site.</span>
        )}
      </SettingsCard>

      <ConfirmDialog
        open={dialog != null}
        title={dialog?.title ?? ''}
        body={
          pending?.kind === 'workflow' ? (
            <>
              {dialog?.body}
              <label className="lim-floor">
                Floor
                <NumberInput
                  autoFocus
                  min={1}
                  max={100}
                  step={1}
                  value={floor}
                  placeholder={String(agreementText(pending.workflow.agreement)?.pct ?? '')}
                  onChange={(e) => setFloor(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && confirmPending()}
                />
                % agree, required
              </label>
              {flowManaged[pending.workflow.id]?.declared.floor && (
                <p className="text-xs text-tx-3 mt-2">
                  Managed by INTENT.md: at least {flowManaged[pending.workflow.id].declared.floor}%, the declared floor.
                </p>
              )}
            </>
          ) : stopping && overlayOnly.length > 0 ? (
            <StopOverlayOnly rules={overlayOnly} deploying={deploying} onDeploy={deployOverlay} />
          ) : (
            dialog?.body ?? ''
          )
        }
        confirmDisabled={pending?.kind === 'workflow' && !floorValid}
        confirmLabel={
          pending?.kind === 'stop'
            ? overlayOnly.length
              ? `Stop · ${overlayOnly.length === 1 ? '1 rule goes' : `${overlayOnly.length} rules go`} dark`
              : 'Stop'
            : pending?.kind === 'tool'
              ? 'Confirm'
              : 'Save'
        }
        width={stopping && overlayOnly.length > 0 ? 680 : undefined}
        danger={pending?.kind === 'stop'}
        onConfirm={confirmPending}
        onClose={dismissPending}
      />
      <ConfirmDialog
        open={rejectFor != null}
        title="Reject this action?"
        body={
          <Field label="Why it is rejected">
            <TextInput
              autoFocus
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && rejectReason.trim() && rejectHeld()}
            />
          </Field>
        }
        confirmLabel="Reject"
        confirmDisabled={!rejectReason.trim()}
        busy={heldBusy != null && heldBusy === rejectFor?.action_id}
        onConfirm={rejectHeld}
        onClose={() => {
          if (heldBusy == null) {
            setRejectFor(null)
            setRejectReason('')
          }
        }}
      />
    </>
  )
}

const SIEM_NAME: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk' }

/** Stop's prompt when live rules run only in Vigil: what Stop turns off, then those rules and a way to put each in the SIEM. */
function StopOverlayOnly({
  rules,
  deploying,
  onDeploy,
}: {
  rules: OverlayOnlyRule[]
  deploying: number[] | 'all' | null
  onDeploy: (ids?: number[]) => void
}) {
  const n = rules.length
  // A rule whose PR is open, or whose file is already in the repo, has nothing left to add; a failed one is retried.
  const done = (r: OverlayOnlyRule) => !!(r.deploy?.pr_url || r.deploy?.in_repo)
  const deployable = rules.filter((r) => r.deploy_target && !done(r))
  const busy = (id: number) => deploying === 'all' || (Array.isArray(deploying) && deploying.includes(id))
  return (
    <>
      <ul className="stop-lines">
        <li>No tool acts on its own; reads continue.</li>
        <li>Unattended writes, proposals and outside actions are off until you resume.</li>
        <li>Automatic investigation turns off; investigations in progress are marked failed.</li>
      </ul>
      <div className="stop-dark-head">
        <div>
          <b>{n === 1 ? '1 live rule goes dark' : `${n} live rules go dark`}</b>
          <span>Vigil’s own detection rules are off while stopped. These have no copy in your SIEM.</span>
        </div>
        {deployable.length > 0 && (
          <button type="button" className="btn stop-add-all" disabled={deploying != null} onClick={() => onDeploy(deployable.length === n ? undefined : deployable.map((r) => r.id))}>
            {deploying === 'all' ? 'Opening…' : deployable.length === 1 ? 'Add it to SIEM' : `Add all ${deployable.length} to SIEM`}
          </button>
        )}
      </div>
      <div className="stop-dark-table" role="table" aria-label="Live rules only Vigil runs">
        <div className="stop-dark-row stop-dark-th" role="row">
          <span role="columnheader">Rule</span>
          <span role="columnheader">SIEM</span>
          <span role="columnheader">Keep it detecting</span>
        </div>
        <div className="stop-dark-body">
          {rules.map((r) => (
            <div key={r.id} className="stop-dark-row" role="row">
              <span role="cell" className="stop-dark-name" title={r.name}>{r.name}</span>
              <span role="cell"><span className="stop-siem">{SIEM_NAME[r.language] ?? r.language}</span></span>
              <span role="cell" className="stop-dark-act">
                {r.deploy?.pr_url ? (
                  <a className="stop-link" href={r.deploy.pr_url} target="_blank" rel="noreferrer">PR opened</a>
                ) : r.deploy?.in_repo ? (
                  <span className="text-tx-3" title={r.deploy.in_repo}>Already in the repo</span>
                ) : r.target_refused ? (
                  <Link className="stop-link" to="/settings?section=data" title={r.target_refused}>Target isn’t GitHub</Link>
                ) : r.deploy_target && r.deploy?.error ? (
                  <span className="stop-failed">
                    <span className="stop-why" title={`${r.deploy.reason ?? ''}\n\n${r.deploy.error}`.trim()}>{(r.deploy.reason ?? r.deploy.error).split('. ')[0].replace(/\.$/, '')}</span>
                    <button type="button" className="stop-link" disabled={deploying != null} onClick={() => onDeploy([r.id])}>
                      {busy(r.id) ? 'Opening…' : 'Retry'}
                    </button>
                  </span>
                ) : r.deploy_target ? (
                  <button type="button" className="stop-link" disabled={deploying != null} onClick={() => onDeploy([r.id])}>
                    {busy(r.id) ? 'Opening…' : 'Add to SIEM'}
                  </button>
                ) : (
                  <Link className="stop-link" to="/settings?section=data">Set a deploy target</Link>
                )}
              </span>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
