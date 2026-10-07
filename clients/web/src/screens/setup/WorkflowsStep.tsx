import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { configApi, workflowApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { ConfirmDialog, SettingsCard, Toggle } from '../../shared/ui'
import { triggerLabels } from '../workflows/triggers'
import {
  ORCHESTRATOR_DEFAULTS,
  stripOrchestratorProfiles,
  type OrchestratorConfig,
} from '../settings/useSettings'
import ChoiceCard from './ChoiceCard'
import { errorText } from './errorText'

interface WorkflowRow {
  id: string
  name: string
  description: string
  enabled: boolean
  canDisable: boolean
  triggers: string[]
}

interface Approval {
  enabled: boolean
  environment_wins: boolean
}

type Phase = 'loading' | 'ready' | 'error'

export default function WorkflowsStep() {
  const [rows, setRows] = useState<WorkflowRow[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [investigates, setInvestigates] = useState<boolean | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [autoError, setAutoError] = useState<string | null>(null)
  const [approval, setApproval] = useState<Approval>({ enabled: false, environment_wins: false })
  const [approvalPhase, setApprovalPhase] = useState<Phase>('loading')
  const [confirmAct, setConfirmAct] = useState(false)
  const [approvalError, setApprovalError] = useState<string | null>(null)

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
          triggers?: string[]
        }[]
        setRows(
          list.map((workflow) => ({
            id: workflow.id,
            name: workflow.name || workflow.id,
            description: workflow.description || '',
            enabled: workflow.enabled !== false,
            canDisable: workflow.can_disable !== false,
            triggers: triggerLabels(workflow.triggers),
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
    configApi
      .getForceManualApproval()
      .then(({ data }) => {
        if (!live) return
        const row = data as Partial<Approval>
        setApproval({
          enabled: Boolean(row.enabled),
          environment_wins: Boolean(row.environment_wins),
        })
        setApprovalPhase('ready')
      })
      .catch(() => {
        if (live) setApprovalPhase('error')
      })
    return () => {
      live = false
    }
  }, [])

  const saveApproval = async (enabled: boolean) => {
    setApprovalError(null)
    try {
      const { data } = await configApi.setForceManualApproval(enabled)
      const row = data as Partial<Approval>
      setApproval({
        enabled: Boolean(row.enabled),
        environment_wins: Boolean(row.environment_wins),
      })
    } catch (err) {
      setApprovalError(errorText(err, 'Could not save the response mode.'))
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
    setConfirmAct(true)
  }

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

  const actOn = approvalPhase === 'ready' && !approval.enabled

  return (
    <div className="flex flex-col gap-4">
      <SettingsCard title="How much may agents do without asking?" desc="Applies to every workflow.">
        {approvalPhase === 'loading' && <p className="text-tx-3 text-sm">Loading response mode…</p>}
        {approvalPhase === 'error' && <p className="text-sm text-high">Could not read the response mode.</p>}
        {approvalPhase === 'ready' && (
          <div className="flex flex-col gap-3">
            {approval.environment_wins && (
              <div className="settings-banner info">
                <Icon name="info" size={14} />
                <span>The environment wins. Act cannot be saved.</span>
              </div>
            )}
            {approvalError && <p className="text-sm text-high">{approvalError}</p>}
            <div className="su-choices two">
              <ChoiceCard
                title="Assist · asks before changes"
                body="Agents use read-only tools on their own, and ask you before anything that changes a system."
                selected={!actOn}
                onSelect={selectAssist}
              />
              <ChoiceCard
                title="Act · reversible changes on its own"
                body="Agents may make changes that can be undone, such as ending a session, then tell you. Anything that cannot be undone still asks."
                badge={<span className="su-chip ac">Recommended</span>}
                selected={actOn}
                onSelect={selectAct}
              />
            </div>
            <p className="su-note su-lockline">
              <Icon name="lock" size={12} />
              Actions that cannot be undone, such as isolating a host, always need a person. This cannot be changed.
            </p>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="Workflows"
        desc="Built in. Edit them, or build your own, in Agents & workflows."
        actions={
          <Link className="btn" to="/workflows">
            <Icon name="play" size={12} /> See how an investigation runs
          </Link>
        }
      >
        {phase === 'loading' && <p className="text-tx-3 text-sm">Loading workflows…</p>}
        {phase === 'error' && <p className="text-sm text-high">Could not read workflows.</p>}
        {phase === 'ready' && rows.length === 0 && <p className="text-tx-3 text-sm">No workflows yet.</p>}
        {phase === 'ready' && rows.length > 0 && (
          <div role="table" aria-label="Workflows" className="su-wf">
            <div role="row" className="su-wf-row su-wf-head">
              <span role="columnheader">Workflow</span>
              <span role="columnheader">When it runs</span>
              <span role="columnheader" className="su-wf-end" />
            </div>
            {rows.map((workflow) => (
              <div key={workflow.id} role="row" className="su-wf-row">
                <span role="cell" className="su-wf-name">
                  <span className="su-wf-t" title={workflow.description || undefined}>
                    {workflow.name}
                  </span>
                  <code className="su-wf-id">{workflow.id}</code>
                  {rowErrors[workflow.id] && (
                    <span className="text-xs text-high">{rowErrors[workflow.id]}</span>
                  )}
                </span>
                <span role="cell" className="su-wf-when">
                  {workflow.triggers.join(' · ')}
                </span>
                <span role="cell" className="su-wf-end">
                  {workflow.canDisable ? (
                    <Toggle
                      label={workflow.name}
                      checked={workflow.enabled}
                      disabled={saving.has(workflow.id)}
                      onChange={(on) => toggleWorkflow(workflow.id, on)}
                    />
                  ) : (
                    <span className="su-wf-locked">
                      <InfoTip
                        label={`Why ${workflow.name} is always on`}
                        text="Where alerts land when nothing else fits"
                      />
                      <Icon name="lock" size={12} />
                      Always on
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </SettingsCard>

      {phase === 'ready' && (
        <div className="su-auto">
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
      )}

      <ConfirmDialog
        open={confirmAct}
        title="Switch to Act?"
        body="Act stops forcing manual approval, so autonomous response can proceed on its own."
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          setConfirmAct(false)
          saveApproval(false)
        }}
        onClose={() => setConfirmAct(false)}
      />
    </div>
  )
}
