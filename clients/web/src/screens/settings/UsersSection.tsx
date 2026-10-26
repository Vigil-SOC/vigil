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
import { useAuth } from '../../contexts/AuthContext'
import {
  useUsers,
  type Role,
  type RolePayload,
  type RolesMeta,
  type User,
  type UserPayload,
} from './useSettings'
import type { SectionProps } from './types'

const EMPTY_FORM: UserPayload = {
  username: '',
  email: '',
  password: '',
  full_name: '',
  role_id: '',
}

// mirrors what the backend's Pydantic EmailStr accepts, so this fails fast
// inline instead of round-tripping to a 422
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
  const {
    users,
    roles,
    rolesMeta,
    phase,
    error,
    reload,
    createUser,
    updateUser,
    deleteUser,
    createRole,
    updateRole,
    deleteRole,
  } = useUsers()

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

  const roleName = (id: string) => {
    const r = roles.find((x) => x.role_id === id)
    return r ? roleLabel(r) : id
  }
  const roleOptions = roles.map((r) => ({
    value: r.role_id,
    label: r.description ? `${roleLabel(r)} — ${r.description}` : roleLabel(r),
  }))
  // A kept legacy role is never the default for a new account.
  const defaultRoleId =
    roles.find((r) => r.role_id === 'role-viewer')?.role_id ??
    roles.find((r) => r.kind !== 'legacy')?.role_id ??
    roles[0]?.role_id
  const editingMedic = !!editing?.managed_by_medic

  const openCreate = () => {
    if (roles.length === 0) {
      notify('err', 'Roles not loaded yet — try Refresh.')
      return
    }
    setEditing(null)
    setForm({ ...EMPTY_FORM, role_id: defaultRoleId })
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
      // Length and strength are the server's policy. A copy of those numbers
      // here would drift from a deployment's AUTH_MIN_PASSWORD_LENGTH.
      if (!form.password) return 'Enter a password.'
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
          // Medic's account is not reassigned; the server refuses it too.
          ...(editingMedic ? {} : { role_id: form.role_id }),
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
    <>
      <SettingsCard
        wide
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
                  <td style={{ whiteSpace: 'normal' }}>
                    <span className="line-clamp-2 break-words" title={u.full_name}>
                      {u.full_name}
                    </span>
                  </td>
                  <td className="muted">{u.email}</td>
                  <td>
                    <div className="flex flex-col items-start gap-1">
                      <span className="chip">{roleName(u.role_id)}</span>
                      {u.principal_kind === 'service_account' && (
                        <span className="text-xs text-tx-3 whitespace-nowrap">
                          {u.managed_by_medic ? 'Managed by Medic' : 'Service account'}
                        </span>
                      )}
                    </div>
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
              <Field label="Password" hint="At least 12 characters. A few unrelated words make a strong one.">
                <TextInput
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </Field>
            )}
            <Field
              label="Role"
              hint={
                editingMedic
                  ? 'Managed by Medic. Its gateway account stays Viewer.'
                  : editing?.no_effect.length
                    ? `${editing.no_effect.join(', ')}: stored on the role, no effect for a service account.`
                    : undefined
              }
            >
              <Select
                value={form.role_id}
                options={roleOptions}
                disabled={editingMedic}
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

      <RolesCard
        roles={roles}
        meta={rolesMeta}
        canManage={hasPermission('roles.manage')}
        notify={notify}
        createRole={createRole}
        updateRole={updateRole}
        deleteRole={deleteRole}
      />
    </>
  )
}

function roleLabel(r: Role): string {
  return r.kind === 'legacy' ? `${r.name} (legacy)` : r.name
}

const granted = (p: Record<string, boolean>) => Object.keys(p).filter((k) => p[k])

// "findings.read" groups under "findings"
function byArea(keys: string[]): [string, string[]][] {
  const areas = new Map<string, string[]>()
  for (const k of keys) {
    const area = k.split('.')[0]
    areas.set(area, [...(areas.get(area) ?? []), k])
  }
  return [...areas.entries()]
}

type RoleDialog =
  | { mode: 'create' }
  | { mode: 'edit'; role: Role }
  | { mode: 'view'; role: Role }

const EMPTY_ROLE: RolePayload = { name: '', description: '', permissions: {} }

function RolesCard({
  roles,
  meta,
  canManage,
  notify,
  createRole,
  updateRole,
  deleteRole,
}: {
  roles: Role[]
  meta: RolesMeta
  canManage: boolean
  notify: SectionProps['notify']
  createRole: (p: RolePayload) => Promise<unknown>
  updateRole: (id: string, p: RolePayload) => Promise<unknown>
  deleteRole: (id: string) => Promise<unknown>
}) {
  const [dialog, setDialog] = useState<RoleDialog | null>(null)
  const [form, setForm] = useState<RolePayload>(EMPTY_ROLE)
  const [startFrom, setStartFrom] = useState('')
  const [dialogError, setDialogError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<Role | null>(null)
  const [deleting, setDeleting] = useState(false)

  const known = new Set(meta.permissions)
  const onlyKnown = (p: Record<string, boolean>) =>
    Object.fromEntries(Object.entries(p).filter(([k]) => known.has(k)))

  const startOptions = [
    { value: '', label: 'Blank' },
    ...meta.presets.map((p) => ({ value: `preset:${p.key}`, label: `${p.name} (preset)` })),
    ...roles.map((r) => ({ value: `role:${r.role_id}`, label: `Copy of ${roleLabel(r)}` })),
  ]

  const open = (next: RoleDialog, values: RolePayload, from = '') => {
    setDialog(next)
    setForm(values)
    setStartFrom(from)
    setDialogError('')
  }
  const openCreate = () => open({ mode: 'create' }, EMPTY_ROLE)
  const openCopy = (r: Role) =>
    open(
      { mode: 'create' },
      { name: `${r.name} copy`, description: r.description, permissions: onlyKnown(r.permissions) },
      `role:${r.role_id}`,
    )
  const openRole = (r: Role) =>
    open(
      canManage && r.kind !== 'builtin' ? { mode: 'edit', role: r } : { mode: 'view', role: r },
      { name: r.name, description: r.description, permissions: { ...r.permissions } },
    )

  const pickStart = (v: string) => {
    setStartFrom(v)
    const [kind, id] = [v.slice(0, v.indexOf(':')), v.slice(v.indexOf(':') + 1)]
    const preset = kind === 'preset' ? meta.presets.find((p) => p.key === id) : undefined
    const role = kind === 'role' ? roles.find((r) => r.role_id === id) : undefined
    if (preset) setForm({ name: preset.name, description: preset.description, permissions: { ...preset.permissions } })
    else if (role) setForm({ name: `${role.name} copy`, description: role.description, permissions: onlyKnown(role.permissions) })
    else setForm(EMPTY_ROLE)
  }

  const toggle = (k: string) =>
    setForm((f) => ({ ...f, permissions: { ...f.permissions, [k]: !f.permissions[k] } }))

  const handleSave = async () => {
    if (!dialog || dialog.mode === 'view') return
    if (!form.name.trim()) {
      setDialogError('A role needs a name.')
      return
    }
    setSaving(true)
    setDialogError('')
    const payload = { ...form, name: form.name.trim(), permissions: onlyKnown(form.permissions) }
    try {
      if (dialog.mode === 'edit') {
        await updateRole(dialog.role.role_id, payload)
        notify('ok', `Updated ${payload.name}.`)
      } else {
        await createRole(payload)
        notify('ok', `Created ${payload.name}.`)
      }
      setDialog(null)
    } catch (e) {
      setDialogError(errText(e, 'Failed to save role'))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await deleteRole(confirmDelete.role_id)
      notify('ok', `Deleted ${confirmDelete.name}.`)
      setConfirmDelete(null)
    } catch (e) {
      notify('err', errText(e, 'Failed to delete role'))
      setConfirmDelete(null)
    } finally {
      setDeleting(false)
    }
  }

  const viewing = dialog?.mode === 'view' ? dialog.role : null
  const platformEquivalent = new Set(meta.platform_equivalent)

  return (
    <SettingsCard
      wide
      title="Roles"
      desc="Built-in roles and the custom roles copied from them or from a preset."
      actions={
        canManage && (
          <button className="btn primary" onClick={openCreate}>
            <Icon name="plus" /> New Role
          </button>
        )
      }
    >
      {meta.nobody_can_manage_roles && (
        <div className="settings-banner err mb-3">
          <Icon name="alert" size={14} /> Nobody on this install can manage roles. On the host, run{' '}
          <code className="font-mono">python -m core.auth.recover_admin &lt;username&gt;</code>
        </div>
      )}
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Role</th>
              <th>Description</th>
              <th>Permissions</th>
              <th>Accounts</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {roles.map((r) => (
              <tr key={r.role_id}>
                <td>
                  <div className="flex items-center gap-1.5 whitespace-nowrap">
                    <span>{roleLabel(r)}</span>
                    {r.kind === 'builtin' && <span className="chip">Built-in</span>}
                    {r.kind === 'legacy' && <span className="chip sel">Review</span>}
                    {r.modified && (
                      <span className="chip sel" title={Object.keys(r.diff ?? {}).join(', ')}>
                        Modified on this install
                      </span>
                    )}
                  </div>
                </td>
                <td className="muted">
                  <span className="line-clamp-2 break-words" title={r.description}>
                    {r.description}
                  </span>
                </td>
                <td className="muted font-mono">{granted(r.permissions).length}</td>
                <td className="muted font-mono">{r.holders}</td>
                <td style={{ textAlign: 'right' }}>
                  <div className="inline-flex gap-1.5">
                    <button
                      className="btn ghost icon"
                      title={canManage && r.kind !== 'builtin' ? 'Edit' : 'View'}
                      onClick={() => openRole(r)}
                    >
                      <Icon name={canManage && r.kind !== 'builtin' ? 'edit' : 'eye'} size={15} />
                    </button>
                    {canManage && (
                      <button className="btn ghost icon" title="Copy" onClick={() => openCopy(r)}>
                        <Icon name="copy" size={15} />
                      </button>
                    )}
                    {canManage && r.kind !== 'builtin' && (
                      <button
                        className="btn ghost icon"
                        title={r.holders ? `${r.holders} account(s) hold this role` : 'Delete'}
                        disabled={r.holders > 0}
                        onClick={() => setConfirmDelete(r)}
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
        open={!!dialog}
        onClose={() => setDialog(null)}
        title={
          dialog?.mode === 'create' ? 'New Role' : dialog?.mode === 'edit' ? 'Edit Role' : viewing ? roleLabel(viewing) : ''
        }
        width={640}
      >
        <div className="flex flex-col gap-3.5">
          {dialogError && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {dialogError}
            </div>
          )}
          {viewing?.kind === 'builtin' && (
            <div className="settings-banner info">
              <Icon name="info" size={14} /> Built-in roles can’t be changed. Copy it to create a custom role.
            </div>
          )}
          {viewing?.modified && (
            <div className="text-xs text-tx-3">
              Modified on this install:{' '}
              {Object.entries(viewing.diff ?? {})
                .map(([k, d]) => `${k} ${String(d.catalog)} → ${String(d.role)}`)
                .join(', ')}
            </div>
          )}
          {dialog?.mode === 'create' && (
            <Field label="Start from…">
              <Select value={startFrom} options={startOptions} onSelect={pickStart} searchable />
            </Field>
          )}
          {!viewing && (
            <>
              <Field label="Name">
                <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </Field>
              <Field label="Description">
                <TextInput
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </Field>
            </>
          )}
          <div>
            <div className="field-label mb-1.5">Permissions</div>
            <div
              className="grid grid-cols-2 gap-x-4 gap-y-2"
              style={{ maxHeight: 300, overflowY: 'auto', border: '1px solid var(--line)', borderRadius: 6, padding: 10 }}
            >
              {byArea(meta.permissions).map(([area, keys]) => (
                <div key={area} className="flex flex-col">
                  {keys.map((k) => (
                    <label
                      key={k}
                      className="flex items-center gap-2 py-0.5 text-xs"
                      style={{ cursor: viewing ? 'default' : 'pointer' }}
                    >
                      <input
                        type="checkbox"
                        checked={!!form.permissions[k]}
                        disabled={!!viewing}
                        onChange={() => toggle(k)}
                      />
                      <span className="font-mono">{k}</span>
                      {platformEquivalent.has(k) && <span className="text-tx-3 whitespace-nowrap">platform-equivalent</span>}
                    </label>
                  ))}
                </div>
              ))}
            </div>
          </div>
          {viewing && viewing.unrecognised.length > 0 && (
            <div className="text-xs text-tx-3">
              Not recognised; no effect: <span className="font-mono">{viewing.unrecognised.join(', ')}</span>
            </div>
          )}
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setDialog(null)} disabled={saving}>
              {viewing ? 'Close' : 'Cancel'}
            </button>
            {viewing && canManage && (
              <button className="btn primary" onClick={() => openCopy(viewing)}>
                Copy
              </button>
            )}
            {!viewing && (
              <button className="btn primary" onClick={handleSave} disabled={saving}>
                {saving ? 'Saving…' : dialog?.mode === 'edit' ? 'Update' : 'Create'}
              </button>
            )}
          </div>
        </div>
      </Popup>

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete role?"
        body={`Delete ${confirmDelete?.name ?? 'this role'}? Nobody holds it.`}
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={handleDelete}
        onClose={() => setConfirmDelete(null)}
      />
    </SettingsCard>
  )
}
