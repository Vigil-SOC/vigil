/* ============================================================
   Settings · Users — user CRUD + role assignment + active toggle.
   Mirrors UserManagementTab.tsx. Uses the raw axios client (no
   named users API object) and AuthContext for permission gating.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  Field,
  Popup,
  Select,
  SettingsCard,
  TextInput,
  Toggle,
} from '../../shared/ui'
import { useAuth } from '../../../contexts/AuthContext'
import { useUsers, type User, type UserPayload } from './useSettings'
import type { SectionProps } from './types'

const EMPTY_FORM: UserPayload = {
  username: '',
  email: '',
  password: '',
  full_name: '',
  role_id: '',
}

// Pragmatic RFC 5322-ish check: a local part, an @, a domain with a dot.
// Mirrors what the backend's Pydantic EmailStr will accept, so we fail
// fast inline instead of round-tripping to a 422.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d as { msg?: string })?.msg || JSON.stringify(d))
      .join(', ')
  }
  if (detail && typeof detail === 'object') {
    return (detail as { msg?: string }).msg || JSON.stringify(detail)
  }
  return (e as { message?: string })?.message || fallback
}

export default function UsersSection({ notify }: SectionProps) {
  const { hasPermission } = useAuth()
  const { users, roles, phase, error, reload, createUser, updateUser, deleteUser } = useUsers()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [form, setForm] = useState<UserPayload>(EMPTY_FORM)
  const [dialogError, setDialogError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<User | null>(null)
  const [deleting, setDeleting] = useState(false)

  const canWrite = hasPermission('users.write')
  const canDelete = hasPermission('users.delete')

  if (!hasPermission('users.read')) {
    return (
      <div className="settings-banner err">
        <Icon name="alert" size={14} /> You don’t have permission to view user management.
      </div>
    )
  }
  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading users…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load users: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const roleName = (id: string) => roles.find((r) => r.role_id === id)?.name || id
  const roleOptions = roles.map((r) => ({
    value: r.role_id,
    label: r.description ? `${r.name} — ${r.description}` : r.name,
  }))

  const openCreate = () => {
    if (roles.length === 0) {
      notify('err', 'Roles not loaded yet — try Refresh.')
      return
    }
    setEditing(null)
    setForm({ ...EMPTY_FORM, role_id: roles[0].role_id })
    setDialogError('')
    setDialogOpen(true)
  }

  const openEdit = (u: User) => {
    setEditing(u)
    setForm({
      username: u.username,
      email: u.email,
      password: '',
      full_name: u.full_name,
      role_id: u.role_id,
    })
    setDialogError('')
    setDialogOpen(true)
  }

  const validate = (): string | null => {
    if (!editing) {
      if (!form.username.trim()) return 'Username is required'
      if (!form.password || form.password.length < 8) return 'Password must be at least 8 characters'
    }
    if (!form.full_name.trim()) return 'Full name is required'
    if (!form.email.trim()) return 'Email is required'
    if (!EMAIL_RE.test(form.email.trim())) return 'Enter a valid email address'
    if (!form.role_id) return 'Role is required'
    return null
  }

  const handleSave = async () => {
    const v = validate()
    if (v) {
      setDialogError(v)
      return
    }
    setSaving(true)
    setDialogError('')
    try {
      const email = form.email.trim()
      if (editing) {
        await updateUser(editing.user_id, {
          full_name: form.full_name,
          email,
          role_id: form.role_id,
        })
        notify('ok', `Updated ${form.username || editing.username}.`)
      } else {
        await createUser({ ...form, email })
        notify('ok', `Created ${form.username}.`)
      }
      setDialogOpen(false)
    } catch (e) {
      setDialogError(errText(e, 'Failed to save user'))
    } finally {
      setSaving(false)
    }
  }

  const handleToggleActive = async (u: User) => {
    try {
      await updateUser(u.user_id, { is_active: !u.is_active })
    } catch (e) {
      notify('err', errText(e, 'Failed to update user'))
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await deleteUser(confirmDelete.user_id)
      notify('ok', `Deleted ${confirmDelete.username}.`)
      setConfirmDelete(null)
    } catch (e) {
      notify('err', errText(e, 'Failed to delete user'))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <SettingsCard
      title="User Management"
      desc="Manage system users and their roles."
      actions={
        <>
          <button className="btn ghost" onClick={reload}>
            <Icon name="refresh" /> Refresh
          </button>
          {canWrite && (
            <button className="btn primary" onClick={openCreate}>
              <Icon name="plus" /> Add User
            </button>
          )}
        </>
      }
    >
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Username</th>
              <th>Full Name</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>MFA</th>
              <th>Last Login</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 && (
              <tr>
                <td colSpan={8} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                  No users found.
                </td>
              </tr>
            )}
            {users.map((u) => (
              <tr key={u.user_id}>
                <td>{u.username}</td>
                <td>{u.full_name}</td>
                <td className="muted">{u.email}</td>
                <td>
                  <span className="chip">{roleName(u.role_id)}</span>
                </td>
                <td>
                  <div className="flex items-center gap-2">
                    <Toggle
                      checked={u.is_active}
                      disabled={!canWrite}
                      onChange={() => handleToggleActive(u)}
                    />
                    <span className="text-xs text-tx-3">{u.is_active ? 'Active' : 'Inactive'}</span>
                  </div>
                </td>
                <td>
                  <span className={`status ${u.mfa_enabled ? 'closed' : 'open'}`}>
                    {u.mfa_enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </td>
                <td className="muted">
                  {u.last_login ? new Date(u.last_login).toLocaleDateString() : 'Never'}
                </td>
                <td style={{ textAlign: 'right' }}>
                  <div className="inline-flex gap-1.5">
                    {canWrite && (
                      <button className="btn ghost icon" title="Edit" onClick={() => openEdit(u)}>
                        <Icon name="edit" size={15} />
                      </button>
                    )}
                    {canDelete && (
                      <button
                        className="btn ghost icon"
                        title="Delete"
                        onClick={() => setConfirmDelete(u)}
                      >
                        <Icon name="trash" size={15} />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Popup
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title={editing ? 'Edit User' : 'Create New User'}
        width={460}
      >
        <div className="flex flex-col gap-3.5">
          {dialogError && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {dialogError}
            </div>
          )}
          <Field label="Username">
            <TextInput
              value={form.username}
              disabled={!!editing}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
            />
          </Field>
          <Field label="Full Name">
            <TextInput
              value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })}
            />
          </Field>
          <Field label="Email">
            <TextInput
              type="email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
            />
          </Field>
          {!editing && (
            <Field label="Password" hint="Minimum 8 characters.">
              <TextInput
                type="password"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
            </Field>
          )}
          <Field label="Role">
            <Select
              value={form.role_id}
              options={roleOptions}
              placeholder={roles.length ? 'Select a role…' : 'Loading roles…'}
              onSelect={(v) => setForm({ ...form, role_id: v })}
            />
          </Field>
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setDialogOpen(false)} disabled={saving}>
              Cancel
            </button>
            <button className="btn primary" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : editing ? 'Update' : 'Create'}
            </button>
          </div>
        </div>
      </Popup>

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete user?"
        body={`Permanently delete ${confirmDelete?.username ?? 'this user'}? This cannot be undone.`}
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={handleDelete}
        onClose={() => setConfirmDelete(null)}
      />
    </SettingsCard>
  )
}
