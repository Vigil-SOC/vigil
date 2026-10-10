import { useEffect, useRef, useState } from 'react'
import { DurationPicker } from '../../shared/DurationPicker'
import { formatDuration } from '../../shared/duration'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { PageHead } from '../../shared/PageHead'
import { SeverityMark } from '../../shared/SeverityMark'
import {
  ConfirmDialog,
  Field,
  Select,
  TextInput,
  ToggleRow,
} from '../../shared/ui'
import { useSlaPolicies, type SlaPolicy } from './useSlaPolicies'
import type { SectionProps } from './types'

const PRIORITIES = [
  { value: 'critical', label: 'Critical' },
  { value: 'high', label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low', label: 'Low' },
]
const severityRank = (p: SlaPolicy) => {
  const i = PRIORITIES.findIndex((o) => o.value === p.priority_level)
  return i === -1 ? PRIORITIES.length : i
}
const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

export const SLA_DESC =
  'How fast a case must get a first response and be resolved, by severity. New cases use the default policy for their severity.'

const HOURS_OPTIONS = [
  { value: 'around', label: 'Around the clock' },
  { value: 'business', label: 'Business hours (Mon to Fri, 09:00 to 17:00)' },
]

interface FormState {
  name: string
  description: string
  priority_level: string
  response_time_hours: number
  resolution_time_hours: number
  business_hours_only: boolean
  is_active: boolean
  is_default: boolean
}
// New policies count around the clock, as the old form did, rather than taking
// the server's business-hours default: an SLA clock that stops overnight is a
// choice to make, not one to inherit.
const EMPTY: FormState = {
  name: '',
  description: '',
  priority_level: 'high',
  response_time_hours: 4,
  resolution_time_hours: 24,
  business_hours_only: false,
  is_active: true,
  is_default: false,
}

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string })?.msg || JSON.stringify(d)).join(', ')
  if (detail && typeof detail === 'object') return (detail as { msg?: string }).msg || JSON.stringify(detail)
  return (e as { message?: string })?.message || fallback
}

const slugify = (s: string) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')

const warnAt = (p: SlaPolicy) =>
  p.notification_thresholds?.length ? p.notification_thresholds.map((t) => `${t}%`).join(', ') : '—'

const WARN_TIP = 'Stored, not acted on yet: nothing warns at these points today.'

export default function SlaPoliciesSection({ notify }: SectionProps) {
  const { policies: fetched, usage, phase, error, reload, create, update, remove } = useSlaPolicies()
  // most severe first, as on the board; the API orders by last edit
  const policies = [...fetched].sort((a, b) => severityRank(a) - severityRank(b) || a.name.localeCompare(b.name))

  // 'new' is the create form; a policy is the one being edited; null is closed
  const [editing, setEditing] = useState<SlaPolicy | 'new' | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<SlaPolicy | null>(null)
  const [deleting, setDeleting] = useState(false)
  const panel = useRef<HTMLElement>(null)

  useEffect(() => {
    if (editing) panel.current?.scrollIntoView?.({ block: 'nearest' })
  }, [editing])

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading SLA policies…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load SLA policies: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const openCreate = () => {
    setEditing('new')
    setForm(EMPTY)
    setFormError('')
  }

  const openEdit = (p: SlaPolicy) => {
    setEditing(p)
    setForm({
      name: p.name,
      description: p.description || '',
      priority_level: String(p.priority_level),
      response_time_hours: p.response_time_hours,
      resolution_time_hours: p.resolution_time_hours,
      business_hours_only: !!p.business_hours_only,
      is_active: p.is_active !== false,
      is_default: !!p.is_default,
    })
    setFormError('')
  }

  const validate = (): string | null => {
    if (!form.name.trim()) return 'Name is required'
    // the server refuses equality too (response >= resolution)
    if (form.resolution_time_hours <= form.response_time_hours) return 'Resolve within must be longer than respond within'
    return null
  }

  const handleSave = async () => {
    const v = validate()
    if (v) { setFormError(v); return }
    setSaving(true)
    setFormError('')
    try {
      const common = {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        response_time_hours: form.response_time_hours,
        resolution_time_hours: form.resolution_time_hours,
        business_hours_only: form.business_hours_only,
        is_active: form.is_active,
        is_default: form.is_default,
      }
      if (editing && editing !== 'new') {
        await update(editing.policy_id, common)
        notify('ok', `Updated ${form.name}.`)
      } else {
        await create({ ...common, policy_id: slugify(form.name) || `policy-${Date.now()}`, priority_level: form.priority_level })
        notify('ok', `Created ${form.name}.`)
      }
      setEditing(null)
    } catch (e) {
      setFormError(errText(e, 'Failed to save policy'))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await remove(confirmDelete.policy_id)
      notify('ok', `Deleted ${confirmDelete.name}.`)
      if (editing && editing !== 'new' && editing.policy_id === confirmDelete.policy_id) setEditing(null)
      setConfirmDelete(null)
    } catch (e) {
      notify('err', errText(e, 'Failed to delete policy'))
    } finally {
      setDeleting(false)
    }
  }

  const met = (p: SlaPolicy) => {
    const u = usage[p.policy_id]
    if (u === undefined) return <span className="muted">…</span>
    // the server reports 0% for no cases, so the empty month is read off the count
    if (u === null) return <NotMeasured tip="Couldn’t read this policy’s cases." />
    if (u.total_cases === 0) return <NotMeasured />
    return <>{Math.round(u.compliance_rate)}%</>
  }

  const isNew = editing === 'new'

  return (
    <>
      <PageHead
        title="SLA policies"
        description={SLA_DESC}
        actions={
          <>
            <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
            <button className="btn primary" onClick={openCreate}><Icon name="plus" /> New policy</button>
          </>
        }
      />
      <section className="card card-sq settings-card wide" aria-label="Policies">
        <div className="card-b table-wrap">
          <table className="tbl sla-tbl">
            <thead>
              <tr>
                <th>Policy</th>
                <th>Severity</th>
                <th>Respond within</th>
                <th>Resolve within</th>
                <th>Hours that count</th>
                <th>Warn at <InfoTip label="About Warn at" text={WARN_TIP} align="start" /></th>
                <th>
                  Met this month
                  <InfoTip
                    label="How Met is calculated"
                    calculation="Cases created since the 1st of this month, UTC, that did not breach this policy, over all cases created in that time."
                  />
                </th>
                <th aria-label="Edit" />
              </tr>
            </thead>
            <tbody>
              {policies.length === 0 && (
                <tr><td colSpan={8} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>No SLA policies yet.</td></tr>
              )}
              {policies.map((p) => {
                const n = usage[p.policy_id]?.total_cases
                return (
                  <tr key={p.policy_id}>
                    <td>
                      <div className="sla-name" title={p.name}>
                        {p.name}
                        {p.is_default && <span className="sla-flag">Default</span>}
                        {p.is_active === false && <span className="sla-flag">Inactive</span>}
                      </div>
                      {n !== undefined && n !== null && (
                        <div className="text-xs text-tx-3 mt-0.5">{n === 1 ? '1 case' : `${n} cases`} this month</div>
                      )}
                    </td>
                    <td>
                      <SeverityMark level={String(p.priority_level)} />
                    </td>
                    <td>{formatDuration(p.response_time_hours)}</td>
                    <td>{formatDuration(p.resolution_time_hours)}</td>
                    <td className="muted">{p.business_hours_only ? 'Business hours' : 'Around the clock'}</td>
                    <td className="muted">{warnAt(p)}</td>
                    <td>{met(p)}</td>
                    <td style={{ textAlign: 'right' }}>
                      <button className="btn ghost" aria-label={`Edit ${p.name}`} onClick={() => openEdit(p)}>Edit</button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>

      {editing && (
        <section ref={panel} className="card card-sq settings-card wide sla-edit" aria-label="Edit policy">
          <div className="card-h">
            <div className="settings-card-head">
              <h3>{isNew ? 'New policy' : `Editing: ${capitalise(form.priority_level)} severity`}</h3>
            </div>
          </div>
          <div className="card-b flex flex-col gap-4">
            {formError && <div className="settings-banner err"><Icon name="alert" size={14} /> {formError}</div>}
            <div className="sla-grid">
              <div className="field">
                <span className="field-label">Respond within</span>
                <DurationPicker label="Respond within" value={form.response_time_hours} onChange={(v) => setForm({ ...form, response_time_hours: v })} />
              </div>
              <div className="field">
                <span className="field-label">Resolve within</span>
                <DurationPicker label="Resolve within" value={form.resolution_time_hours} onChange={(v) => setForm({ ...form, resolution_time_hours: v })} />
              </div>
              <div className="field">
                <span className="field-label">Hours that count</span>
                <Select
                  value={form.business_hours_only ? 'business' : 'around'}
                  options={HOURS_OPTIONS}
                  onSelect={(v) => setForm({ ...form, business_hours_only: v === 'business' })}
                />
              </div>
              <div className="field">
                <span className="field-label">Warn at <InfoTip label="About Warn at" text={WARN_TIP} align="start" /></span>
                <span className="sla-readonly">{editing !== 'new' ? warnAt(editing) : 'Server default'}</span>
              </div>
            </div>
            <div className="sla-grid sla-grid-2">
              <Field label="Name"><TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
              <Field label="Description"><TextInput value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
              {isNew && (
                <Field label="Severity">
                  <Select value={form.priority_level} options={PRIORITIES} onSelect={(v) => setForm({ ...form, priority_level: v })} />
                </Field>
              )}
            </div>
            <div className="sla-grid sla-grid-2">
              <ToggleRow label="Active" hint="An inactive policy is not given to new cases." checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} />
              <ToggleRow label="Default for this severity" hint="New cases at this severity use this policy." checked={form.is_default} onChange={(v) => setForm({ ...form, is_default: v })} />
            </div>
            <div className="field">
              <span className="field-label">
                Escalation steps <span className="sla-later">Later</span>
                <InfoTip label="About escalation steps" text="Escalation rules are stored but nothing acts on them yet, so they cannot be edited here." align="start" />
              </span>
              <div className="sla-escalation" aria-disabled="true">No steps. Escalation is not available yet.</div>
            </div>
            <div className="sla-actions">
              {!isNew && (
                <button className="btn danger" onClick={() => setConfirmDelete(editing)} disabled={saving}><Icon name="trash" size={14} /> Delete</button>
              )}
              <span className="grow" />
              <button className="btn ghost" onClick={() => setEditing(null)} disabled={saving}>Cancel</button>
              <button className="btn primary" onClick={handleSave} disabled={saving}>{saving ? 'Saving…' : isNew ? 'Create' : 'Save'}</button>
            </div>
          </div>
        </section>
      )}

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete SLA policy?"
        body={
          <div className="flex flex-col gap-3">
            <span>Permanently delete {confirmDelete?.name ?? 'this policy'}? This cannot be undone.</span>
            <span className="text-sm opacity-70">
              A policy that cases still reference cannot be deleted. Deactivate it
              instead: no new case will take it, and the cases that used it keep
              their SLA history.
            </span>
          </div>
        }
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={handleDelete}
        onClose={() => setConfirmDelete(null)}
      />
    </>
  )
}
