import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { autonomyApi, configApi, workflowApi, type Ceiling } from '../../services/api'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { ConfirmDialog, Toggle } from '../../shared/ui'
import {
  notifyAutonomyChanged,
  ORCHESTRATOR_DEFAULTS,
  stripOrchestratorProfiles,
  type OrchestratorConfig,
} from '../settings/useSettings'
import { TRIGGER_LABEL } from '../workflows/WorkflowsScreen'
import { errorText } from '../../shared/errorText'
import ChoiceCard from './ChoiceCard'

interface WorkflowRow {
  id: string
  name: string
  description: string
  enabled: boolean
  canDisable: boolean
  alwaysOnReason: string | null
  /** when it runs; null when the API sent no triggers field */
  runs: string[] | null
}

type Phase = 'loading' | 'ready' | 'error'

export default function WorkflowsStep() {
  const [rows, setRows] = useState<WorkflowRow[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [investigates, setInvestigates] = useState<boolean | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [autoError, setAutoError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    workflowApi
      .listAll()
      .then((res) => {
        if (!live) return
        const list = (res.data?.workflows || []) as {
          id: string
          name?: string
          description?: string
          enabled?: boolean
          can_disable?: boolean
          always_on_reason?: string | null
          triggers?: string[]
        }[]
        setRows(
          list.map((workflow) => ({
            id: workflow.id,
            name: workflow.name || workflow.id,
            description: workflow.description || '',
            enabled: workflow.enabled !== false,
            canDisable: workflow.can_disable !== false,
            alwaysOnReason: workflow.always_on_reason ?? null,
            // absent shows nothing; an empty list is a workflow started by hand
            runs: workflow.triggers
              ? workflow.triggers.length
                ? workflow.triggers.map((t) => TRIGGER_LABEL[t] ?? t)
                : ['Started by hand']
              : null,
          })),
        )
        setPhase('ready')
      })
      .catch(() => {
        if (live) setPhase('error')
      })
    configApi
      .getOrchestrator()
      .then(({ data }) => {
        if (live) setInvestigates(Boolean((data as Partial<OrchestratorConfig>)?.enabled))
      })
      .catch(() => {
        if (live) setAutoError('Could not read the automatic investigation setting.')
      })
    return () => {
      live = false
    }
  }, [])

  const setSavingId = (id: string, on: boolean) =>
    setSaving((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })

  const patchRow = (id: string, enabled: boolean) =>
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, enabled } : row)))

  const toggleWorkflow = async (id: string, enabled: boolean) => {
    setRowErrors((prev) => ({ ...prev, [id]: '' }))
    setSavingId(id, true)
    patchRow(id, enabled)
    try {
      await workflowApi.setEnabled(id, enabled)
    } catch (err) {
      patchRow(id, !enabled)
      setRowErrors((prev) => ({ ...prev, [id]: errorText(err, 'Could not save this workflow.') }))
    } finally {
      setSavingId(id, false)
    }
  }

  const toggleInvestigate = async (enabled: boolean) => {
    setAutoError(null)
    setSavingId('', true)
    setInvestigates(enabled)
    try {
      // Merge into the config as it is now, not as it was when the step opened.
      const { data } = await configApi.getOrchestrator()
      const current = stripOrchestratorProfiles((data ?? {}) as OrchestratorConfig & { profiles?: unknown })
      await configApi.setOrchestrator({ ...ORCHESTRATOR_DEFAULTS, ...current, enabled })
    } catch (err) {
      setInvestigates(!enabled)
      setAutoError(errorText(err, 'Could not save the automatic investigation setting.'))
    } finally {
      setSavingId('', false)
    }
  }

  if (phase === 'loading') {
    return <p className="text-tx-3 text-sm">Loading workflows…</p>
  }
  if (phase === 'error') {
    // the Assist/Act choice reads its own API, so it stands when workflows fail
    return (
      <div>
        <p className="text-sm text-high">Could not read workflows.</p>
        <AutonomyChoice />
      </div>
    )
  }

  return (
    <div>
      <div className="toggle-row">
        <div className="toggle-row-text">
          <span className="toggle-row-label font-medium">Investigate new alerts automatically</span>
          <span className="toggle-row-hint">Off, workflows start only when someone asks.</span>
          {autoError && <span className="text-xs text-high">{autoError}</span>}
        </div>
        {investigates !== null && (
          <Toggle
            label="Investigate new alerts automatically"
            checked={investigates}
            disabled={saving.has('')}
            onChange={toggleInvestigate}
          />
        )}
      </div>
      {rows.length === 0 && <p className="text-tx-3 text-sm pt-2">No workflows yet.</p>}
      {rows.map((workflow) => (
        <div key={workflow.id} className="toggle-row">
          <div className="toggle-row-text">
            <span className="toggle-row-label font-medium">{workflow.name}</span>
            {workflow.description && <span className="toggle-row-hint">{workflow.description}</span>}
            {workflow.runs && (
              <span className="flex flex-wrap gap-1.5 pt-1">
                {workflow.runs.map((t) => (
                  <span key={t} className="su-chip ac">{t}</span>
                ))}
              </span>
            )}
            {rowErrors[workflow.id] && (
              <span className="text-xs text-high">{rowErrors[workflow.id]}</span>
            )}
          </div>
          {workflow.canDisable ? (
            <Toggle
              label={workflow.name}
              checked={workflow.enabled}
              disabled={saving.has(workflow.id)}
              onChange={(on) => toggleWorkflow(workflow.id, on)}
            />
          ) : (
            <span className="flex shrink-0 items-center gap-1.5 text-xs font-medium text-tx-2">
              <Icon name="lock" size={12} />
              Always on
              {workflow.alwaysOnReason && (
                <InfoTip label={`Why ${workflow.name} is always on`} text={workflow.alwaysOnReason} />
              )}
            </span>
          )}
        </div>
      ))}
      <AutonomyChoice />
    </div>
  )
}

/** Assist or Act: whether Vigil may make reversible changes without asking. */
function AutonomyChoice() {
  const { hasPermission } = useAuth()
  // raising to Act needs ai_autonomy.write; without it the choice is shown read-only
  const canWrite = hasPermission('ai_autonomy.write')
  // Assist is both act flags off; Act is either on. Act writes only act_reversible; act_irreversible is Settings' call
  const [ceiling, setCeiling] = useState<Ceiling | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [confirmAct, setConfirmAct] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    autonomyApi
      .get()
      .then(({ data }) => {
        if (!live) return
        setCeiling(data)
        setPhase('ready')
      })
      .catch(() => {
        if (live) setPhase('error')
      })
    return () => {
      live = false
    }
  }, [])

  const saveAct = async (act: boolean) => {
    setError(null)
    try {
      const { data } = await autonomyApi.update(act ? { act_reversible: true } : { act_reversible: false, act_irreversible: false })
      setCeiling(data)
      notifyAutonomyChanged()
    } catch (err) {
      setError(errorText(err, 'Could not save the response mode.'))
    }
  }

  const reversibleOn = Boolean(ceiling?.act_reversible)
  const irreversibleOn = Boolean(ceiling?.act_irreversible)
  const actOn = reversibleOn || irreversibleOn
  const locked = Boolean(ceiling?.environment_locked)

  const selectAssist = () => {
    if (actOn) saveAct(false)
  }

  const selectAct = () => {
    // A locked Act is sent anyway so the API's 409 explains why it was not saved
    if (locked) saveAct(true)
    // irreversible alone shows Act selected, but reversible still needs writing
    else if (!reversibleOn) setConfirmAct(true)
  }

  return (
    <div className="flex flex-col gap-3 pt-4">
      <h4 className="text-xs font-semibold text-tx-2">What Vigil may do without asking</h4>
      {phase === 'loading' && <p className="text-tx-3 text-sm">Loading the response mode…</p>}
      {phase === 'error' && <p className="text-sm text-high">Could not read the response mode.</p>}
      {phase === 'ready' && (
        <>
          {locked && (
            <div className="settings-banner info">
              <Icon name="info" size={14} />
              <span>The environment wins. Act cannot be saved.</span>
            </div>
          )}
          {!canWrite && (
            <div className="settings-banner info">
              <Icon name="lock" size={14} />
              <span>Your role cannot change this. It shows the current choice.</span>
            </div>
          )}
          {error && <p className="text-sm text-high">{error}</p>}
          <div className="su-choices two">
            <ChoiceCard
              title="Assist"
              body="Uses read-only tools on its own. Asks before anything that changes a system."
              selected={!actOn}
              disabled={!canWrite}
              onSelect={selectAssist}
            />
            <ChoiceCard
              title="Act"
              badge={<span className="su-chip ac">Recommended</span>}
              body="May make changes that can be undone, then tells you."
              selected={actOn}
              disabled={!canWrite}
              onSelect={selectAct}
            />
          </div>
          <p className="text-xs text-tx-3">
            {irreversibleOn
              ? 'Actions that cannot be undone also run on their own, as set in '
              : 'Actions that cannot be undone still ask first unless they are allowed in '}
            <Link className="text-accent-2 underline" to="/settings?section=autoinvestigate">
              Settings › Limits &amp; autonomy
            </Link>
            .
          </p>
        </>
      )}
      <ConfirmDialog
        open={confirmAct}
        title="Switch to Act?"
        body={`Act lets Vigil make changes that can be undone without asking first, then tells you what it did. ${
          irreversibleOn ? 'Changes that cannot be undone already run on their own.' : 'Changes that cannot be undone still ask.'
        }`}
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          setConfirmAct(false)
          saveAct(true)
        }}
        onClose={() => setConfirmAct(false)}
      />
    </div>
  )
}
