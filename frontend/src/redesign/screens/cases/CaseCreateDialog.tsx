import { useEffect, useMemo, useState } from 'react'
import { casesApi } from '../../../services/api'
import { Popup, Select } from '../../shared/ui'
import { inputCls } from './CaseSections'

export interface InitialCaseFinding {
  id: string
  severity?: string
}

interface CaseCreateDialogProps {
  open: boolean
  initialFindings?: InitialCaseFinding[]
  onClose: () => void
  onCreated: (caseId: string) => void
}

const PRIORITY_RANK: Record<string, number> = {
  low: 0,
  medium: 1,
  high: 2,
  critical: 3,
}

function dedupeInitialFindings(findings: InitialCaseFinding[]): InitialCaseFinding[] {
  const byId = new Map<string, InitialCaseFinding>()
  for (const finding of findings) {
    const id = finding.id.trim()
    if (!id) continue
    const severity = finding.severity?.toLowerCase()
    const current = byId.get(id)
    if (!current || (PRIORITY_RANK[severity || ''] ?? -1) > (PRIORITY_RANK[current.severity?.toLowerCase() || ''] ?? -1)) {
      byId.set(id, { id, severity })
    }
  }
  return [...byId.values()]
}

function caseDefaults(findings: InitialCaseFinding[]): { title: string; priority: string } {
  if (findings.length === 0) return { title: '', priority: 'medium' }
  const title = findings.length === 1
    ? `Investigation for ${findings[0].id}`
    : `Investigation for ${findings.length} findings`
  const priorities = findings
    .map((finding) => finding.severity?.toLowerCase() || '')
    .filter((severity) => severity in PRIORITY_RANK)
  const priority = priorities.length
    ? priorities.reduce((highest, severity) => PRIORITY_RANK[severity] > PRIORITY_RANK[highest] ? severity : highest)
    : 'medium'
  return { title, priority }
}

function createError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || 'Failed to create case'
}

export default function CaseCreateDialog({
  open,
  initialFindings = [],
  onClose,
  onCreated,
}: CaseCreateDialogProps) {
  const findings = useMemo(() => dedupeInitialFindings(initialFindings), [initialFindings])
  const defaults = caseDefaults(findings)
  const findingKey = findings.map((finding) => `${finding.id}:${finding.severity || ''}`).join('|')
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState('medium')
  const [status, setStatus] = useState('open')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setTitle(defaults.title)
    setPriority(defaults.priority)
    setStatus('open')
    setDescription('')
    setBusy(false)
    setError('')
    // findingKey captures the normalized IDs and severities that drive defaults.
  }, [open, findingKey, defaults.title, defaults.priority])

  const close = () => {
    if (!busy) onClose()
  }

  const submit = async () => {
    if (!title.trim()) {
      setError('Title is required.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const response = await casesApi.create({
        title: title.trim(),
        description: description.trim() || undefined,
        finding_ids: findings.map((finding) => finding.id),
        priority,
        status,
      })
      const caseId = response.data?.case_id
      if (typeof caseId !== 'string' || !caseId) {
        throw new Error('The case was created without an identifier')
      }
      onCreated(caseId)
      onClose()
    } catch (cause) {
      setError(createError(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={close} title={findings.length ? 'Create case from findings' : 'New case'} width={520}>
      <div className="flex flex-col gap-3.5">
        {findings.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold uppercase tracking-wide text-tx-3">Linked findings</span>
            <div className="fp-chips" aria-label="Linked findings">
              {findings.map((finding) => <span className="chip mono" key={finding.id}>{finding.id}</span>)}
            </div>
            <span className="text-xs text-tx-3">These findings will be linked when the case is created.</span>
          </div>
        )}
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Title</span>
          <input className={inputCls} placeholder="Case title" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Priority</span>
            <Select
              value={priority}
              onSelect={setPriority}
              options={[
                { value: 'critical', label: 'Critical' },
                { value: 'high', label: 'High' },
                { value: 'medium', label: 'Medium' },
                { value: 'low', label: 'Low' },
              ]}
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Status</span>
            <Select
              value={status}
              onSelect={setStatus}
              options={[
                { value: 'open', label: 'Open' },
                { value: 'investigating', label: 'Investigating' },
                { value: 'closed', label: 'Closed' },
              ]}
            />
          </label>
        </div>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Description</span>
          <textarea className={inputCls} rows={4} placeholder="Optional description" value={description} onChange={(event) => setDescription(event.target.value)} style={{ resize: 'vertical' }} />
        </label>
        {error && <div role="alert" className="text-[13px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={close} disabled={busy}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Creating…' : 'Create case'}</button>
        </div>
      </div>
    </Popup>
  )
}
