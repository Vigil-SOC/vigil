/* ============================================================
   Settings · SLA Policies — policy CRUD + set-default-per-priority.
   Production has no UI for slaPoliciesApi; this surfaces it in the
   redesign's settings style, mirroring UsersSection. REDESIGN_GAPS §8.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  Field,
  NumberInput,
  Popup,
  Select,
  SettingsCard,
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
const PRIORITY_COLOR: Record<string, string> = {
  critical: 'var(--crit)',
  high: 'var(--high)',
  medium: 'var(--med)',
  low: 'var(--ok)',
}

interface FormState {
  name: string
  description: string
  priority_level: string
  response_time_hours: string
  resolution_time_hours: string
  business_hours_only: boolean
  notification_thresholds: string
  is_active: boolean
  is_default: boolean
}
const EMPTY: FormState = {
  name: '',
  description: '',
  priority_level: 'high',
  response_time_hours: '4',
  resolution_time_hours: '24',
  business_hours_only: false,
  notification_thresholds: '50, 75, 90',
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

const parseThresholds = (s: string): number[] =>
  s.split(',').map((t) => parseInt(t.trim(), 10)).filter((n) => Number.isFinite(n) && n > 0 && n <= 100)

export default function SlaPoliciesSection({ notify }: SectionProps) {
  const { policies, phase, error, reload, create, update, remove, setDefault } = useSlaPolicies()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<SlaPolicy | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [dialogError, setDialogError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<SlaPolicy | null>(null)
  const [forceDelete, setForceDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

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
    setEditing(null)
    setForm(EMPTY)
    setDialogError('')
    setDialogOpen(true)
  }

  const openEdit = (p: SlaPolicy) => {
    setEditing(p)
    setForm({
      name: p.name,
      description: p.description || '',
      priority_level: String(p.priority_level),
      response_time_hours: String(p.response_time_hours ?? ''),
      resolution_time_hours: String(p.resolution_time_hours ?? ''),
      business_hours_only: !!p.business_hours_only,
      notification_thresholds: (p.notification_thresholds || []).join(', '),
      is_active: p.is_active !== false,
      is_default: !!p.is_default,
    })
    setDialogError('')
    setDialogOpen(true)
  }

  const validate = (): string | null => {
    if (!form.name.trim()) return 'Name is required'
    const resp = Number(form.response_time_hours)
    const res = Number(form.resolution_time_hours)
    if (!Number.isFinite(resp) || resp <= 0) return 'Response time must be a positive number of hours'
    if (!Number.isFinite(res) || res <= 0) return 'Resolution time must be a positive number of hours'
    if (res < resp) return 'Resolution time should be ≥ response time'
    return null
  }

  const handleSave = async () => {
    const v = validate()
    if (v) { setDialogError(v); return }
    setSaving(true)
    setDialogError('')
    try {
      const common = {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        response_time_hours: Number(form.response_time_hours),
        resolution_time_hours: Number(form.resolution_time_hours),
        business_hours_only: form.business_hours_only,
        notification_thresholds: parseThresholds(form.notification_thresholds),
        is_active: form.is_active,
        is_default: form.is_default,
      }
      if (editing) {
        await update(editing.policy_id, common)
        notify('ok', `Updated ${form.name}.`)
      } else {
        await create({ ...common, policy_id: slugify(form.name) || `policy-${Date.now()}`, priority_level: form.priority_level })
        notify('ok', `Created ${form.name}.`)
      }
      setDialogOpen(false)
    } catch (e) {
      setDialogError(errText(e, 'Failed to save policy'))
    } finally {
      setSaving(false)
    }
  }

  const handleSetDefault = async (p: SlaPolicy) => {
    try {
      await setDefault(p.policy_id)
      notify('ok', `${p.name} is now the default for ${p.priority_level}.`)
    } catch (e) {
      notify('err', errText(e, 'Failed to set default'))
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await remove(confirmDelete.policy_id, forceDelete)
      notify('ok', `Deleted ${confirmDelete.name}.`)
      setConfirmDelete(null)
      setForceDelete(false)
    } catch (e) {
      notify('err', errText(e, 'Failed to delete policy'))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <SettingsCard
      title="SLA Policies"
      desc="Define response/resolution targets per priority and pick the default policy for each level."
      actions={
        <>
          <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
          <button className="btn primary" onClick={openCreate}><Icon name="plus" /> New Policy</button>
        </>
      }
    >
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Name</th>
              <th>Priority</th>
              <th>Response</th>
              <th>Resolution</th>
              <th>Default</th>
              <th>Status</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {policies.length === 0 && (
              <tr><td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>No SLA policies yet.</td></tr>
            )}
            {policies.map((p) => (
              <tr key={p.policy_id}>
                <td>
                  {p.name}
                  {p.description && <div className="text-xs text-tx-3 mt-0.5">{p.description}</div>}
                </td>
                <td><span className="tag" style={{ color: PRIORITY_COLOR[String(p.priority_level)] || 'var(--tx-2)' }}>{p.priority_level}</span></td>
                <td className="muted">{p.response_time_hours}h</td>
                <td className="muted">{p.resolution_time_hours}h</td>
                <td>{p.is_default ? <span className="status closed"><Icon name="check2" size={12} /> Default</span> : <span className="muted">—</span>}</td>
                <td><span className={`status ${p.is_active !== false ? 'closed' : 'open'}`}>{p.is_active !== false ? 'Active' : 'Inactive'}</span></td>
                <td style={{ textAlign: 'right' }}>
                  <div className="inline-flex gap-1.5">
                    {!p.is_default && (
                      <button className="btn ghost icon" title="Set as default for its priority" onClick={() => handleSetDefault(p)}><Icon name="bolt" size={15} /></button>
                    )}
                    <button className="btn ghost icon" title="Edit" onClick={() => openEdit(p)}><Icon name="edit" size={15} /></button>
                    <button className="btn ghost icon" title="Delete" onClick={() => { setForceDelete(false); setConfirmDelete(p) }}><Icon name="trash" size={15} /></button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Popup open={dialogOpen} onClose={() => setDialogOpen(false)} title={editing ? 'Edit SLA Policy' : 'New SLA Policy'} width={480}>
        <div className="flex flex-col gap-3.5">
          {dialogError && <div className="settings-banner err"><Icon name="alert" size={14} /> {dialogError}</div>}
          <Field label="Name"><TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Description"><TextInput value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
          <Field label="Priority" hint={editing ? 'Priority level is fixed after creation.' : undefined}>
            {editing ? (
              <span className="tag" style={{ color: PRIORITY_COLOR[form.priority_level] || 'var(--tx-2)' }}>{form.priority_level}</span>
            ) : (
              <Select value={form.priority_level} options={PRIORITIES} onSelect={(v) => setForm({ ...form, priority_level: v })} />
            )}
          </Field>
          <div className="grid grid-cols-2 gap-3.5">
            <Field label="Response (hours)"><NumberInput min={1} value={form.response_time_hours} onChange={(e) => setForm({ ...form, response_time_hours: e.target.value })} /></Field>
            <Field label="Resolution (hours)"><NumberInput min={1} value={form.resolution_time_hours} onChange={(e) => setForm({ ...form, resolution_time_hours: e.target.value })} /></Field>
          </div>
          <Field label="Notification thresholds (%)" hint="Comma-separated SLA-elapsed percentages that trigger a warning, e.g. 50, 75, 90.">
            <TextInput value={form.notification_thresholds} onChange={(e) => setForm({ ...form, notification_thresholds: e.target.value })} />
          </Field>
          <ToggleRow label="Business hours only" hint="Pause SLA timers outside business hours." checked={form.business_hours_only} onChange={(v) => setForm({ ...form, business_hours_only: v })} />
          <ToggleRow label="Active" checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} />
          <ToggleRow label="Default for this priority" hint="New cases at this priority use this policy." checked={form.is_default} onChange={(v) => setForm({ ...form, is_default: v })} />
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setDialogOpen(false)} disabled={saving}>Cancel</button>
            <button className="btn primary" onClick={handleSave} disabled={saving}>{saving ? 'Saving…' : editing ? 'Update' : 'Create'}</button>
          </div>
        </div>
      </Popup>

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete SLA policy?"
        body={
          <div className="flex flex-col gap-3">
            <span>Permanently delete {confirmDelete?.name ?? 'this policy'}? This cannot be undone.</span>
            <ToggleRow
              label="Force delete"
              hint="Delete even if cases currently reference this policy."
              checked={forceDelete}
              onChange={setForceDelete}
            />
          </div>
        }
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={handleDelete}
        onClose={() => { setConfirmDelete(null); setForceDelete(false) }}
      />
    </SettingsCard>
  )
}
