import { useEffect, useState } from 'react'
import { configApi, workflowApi } from '../../services/api'
import { InfoTip } from '../../shared/InfoTip'
import { Toggle } from '../../shared/ui'
import {
  ORCHESTRATOR_DEFAULTS,
  stripOrchestratorProfiles,
  type OrchestratorConfig,
} from '../settings/useSettings'
import { errorText } from './errorText'

interface WorkflowRow {
  id: string
  name: string
  description: string
  enabled: boolean
  canDisable: boolean
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
        }[]
        setRows(
          list.map((workflow) => ({
            id: workflow.id,
            name: workflow.name || workflow.id,
            description: workflow.description || '',
            enabled: workflow.enabled !== false,
            canDisable: workflow.can_disable !== false,
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
    return <p className="text-sm text-high">Could not read workflows.</p>
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
            <span className="toggle-row-label flex items-center gap-1.5 font-medium">
              {workflow.name}
              {!workflow.canDisable && (
                <InfoTip
                  label={`Why ${workflow.name} is always on`}
                  text="Where alerts land when nothing else fits"
                  align="start"
                />
              )}
            </span>
            {workflow.description && <span className="toggle-row-hint">{workflow.description}</span>}
            {rowErrors[workflow.id] && (
              <span className="text-xs text-high">{rowErrors[workflow.id]}</span>
            )}
          </div>
          <Toggle
            label={workflow.name}
            checked={workflow.canDisable ? workflow.enabled : true}
            disabled={!workflow.canDisable || saving.has(workflow.id)}
            onChange={(on) => toggleWorkflow(workflow.id, on)}
          />
        </div>
      ))}
    </div>
  )
}
