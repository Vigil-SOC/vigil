import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import '../../shared/kit.css'
import {
  ConfirmDialog,
  Field,
  Popup,
  Select,
  SettingsCard,
  TextInput,
} from '../../shared/ui'
import { useAuth } from '../../contexts/AuthContext'
import { signInsApi } from '../../services/api'
import ServiceAccountsCard from './ServiceAccountsCard'
import TokensCard from './TokensCard'
import UserSignInsDialog from './UserSignInsDialog'
import {
  useUsers,
  type AccountFilter,
  type AccountStatus,
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

// Everyone manages their own tokens, so they do not wait on, or hide behind, the user list.
export default function UsersSection({ notify }: SectionProps) {
  const { hasPermission } = useAuth()
  // a service account made or changed below shows in Team & access too
  const [changes, setChanges] = useState(0)
  return (
    <>
      <PeopleAndRoles notify={notify} changes={changes} />
      {hasPermission('users.read') && (
        <ServiceAccountsCard notify={notify} onChange={() => setChanges((n) => n + 1)} />
      )}
      <TokensCard notify={notify} canManage={hasPermission('users.write')} changes={changes} />
    </>
  )
}

function PeopleAndRoles({ notify, changes }: SectionProps & { changes: number }) {
  const { user: me, hasPermission, logout } = useAuth()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [typed, setTyped] = useState('')
  const [statusFilter, setStatusFilter] = useState<AccountFilter>('current')
  const [page, setPage] = useState(0)
  // the server searches; wait for a pause in typing before asking it
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(typed)
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [typed])
  const {
    users,
    total,
    roles,
    rolesMeta,
    phase,
    error,
    reload,
    createUser,
    updateUser,
    deleteUser,
    changeStatus,
    createRole,
    updateRole,
    deleteRole,
  } = useUsers({ search, status: statusFilter, skip: page * PAGE, limit: PAGE })
  useEffect(() => {
    if (changes) reload()
  }, [changes, reload])
  // an act can empty the last page: step back to the last one with rows
  useEffect(() => {
    if (phase === 'ready' && page > 0 && page * PAGE >= total) setPage(Math.max(0, Math.ceil(total / PAGE) - 1))
  }, [phase, page, total])

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [form, setForm] = useState<UserPayload>(EMPTY_FORM)
  const [dialogError, setDialogError] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<User | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [signInsOf, setSignInsOf] = useState<User | null>(null)
  const [everyoneOpen, setEveryoneOpen] = useState(false)
  const [everyoneReason, setEveryoneReason] = useState('')
  const [signingOut, setSigningOut] = useState(false)
  const [unlockOf, setUnlockOf] = useState<User | null>(null)
  const [confirmedBy, setConfirmedBy] = useState('')
  const [unlocking, setUnlocking] = useState(false)
  const [statusAct, setStatusAct] = useState<{ user: User; act: StatusAct } | null>(null)
  const [acting, setActing] = useState(false)

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

  // The sign-ins of an account whose role grants nothing the caller lacks. The server
  // checks the same rule; this only keeps the button off rows it would refuse.
  const known = new Set(rolesMeta.permissions)
  const covers = (roleId: string) => {
    const r = roles.find((x) => x.role_id === roleId)
    return !r || granted(r.permissions).every((k) => !known.has(k) || hasPermission(k))
  }
  const canSignOutEveryone = canWrite && users.filter((u) => u.is_active).every((u) => covers(u.role_id))

  const handleSignOutEveryone = async () => {
    setSigningOut(true)
    try {
      const { data } = await signInsApi.endEveryone(everyoneReason.trim())
      setEveryoneOpen(false)
      setEveryoneReason('')
      if (data.signed_out) {
        await logout()
        navigate('/login')
      } else {
        notify('ok', `Signed out ${data.ended} sign-in${data.ended === 1 ? '' : 's'}.`)
      }
    } catch (e) {
      notify('err', errText(e, 'Failed to sign everyone out'))
    } finally {
      setSigningOut(false)
    }
  }

  const handleUnlock = async () => {
    if (!unlockOf || !confirmedBy) return
    setUnlocking(true)
    try {
      await signInsApi.unlock(unlockOf.user_id, confirmedBy)
      notify('ok', `Unlocked ${unlockOf.username}.`)
      setUnlockOf(null)
      setConfirmedBy('')
      reload()
    } catch (e) {
      notify('err', errText(e, 'Failed to unlock'))
    } finally {
      setUnlocking(false)
    }
  }

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
      // here would drift from a deployment's session policy.
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

  const handleStatusAct = async () => {
    if (!statusAct) return
    const { user: u, act } = statusAct
    setActing(true)
    try {
      const data = await changeStatus(u.user_id, act)
      notify(
        'ok',
        act === 'deactivate'
          ? `Deactivated ${u.username}. Their sign-ins ended and their tokens were revoked.`
          : act === 'reactivate'
            ? `Reactivated ${u.username}.${data.new_tokens_required ? ' New tokens must be minted.' : ''}`
            : `Restored ${u.username}. The account is deactivated until you reactivate it.`,
      )
      setStatusAct(null)
    } catch (e) {
      notify('err', errText(e, `Failed to ${act} ${u.username}`))
    } finally {
      setActing(false)
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
        title="Team & access"
        desc="Who can sign in, with which role, and the status of each account."
        actions={
          <>
            <button className="btn ghost" onClick={reload}>
              <Icon name="refresh" /> Refresh
            </button>
            {canSignOutEveryone && (
              <button className="btn ghost" onClick={() => setEveryoneOpen(true)}>
                <Icon name="logout" /> Sign everyone out
              </button>
            )}
            {canWrite && (
              <button className="btn primary" onClick={openCreate}>
                <Icon name="plus" /> Add User
              </button>
            )}
          </>
        }
      >
        <div className="flex items-center gap-2.5 flex-wrap mb-3">
          <div className="search">
            <Icon name="search" />
            <input
              aria-label="Search accounts"
              placeholder="Search accounts…"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
          </div>
          <div style={{ width: 220 }}>
            <Select
              value={statusFilter}
              options={STATUS_FILTERS}
              onSelect={(v) => {
                setStatusFilter(v as AccountFilter)
                setPage(0)
              }}
            />
          </div>
        </div>
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ minWidth: 'var(--col-main-min)' }}>
                  Username
                  <span className="col-low-note">Full name, MFA and last login are hidden while Ask Vigil is open</span>
                </th>
                <th className="col-low">Full Name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th className="col-low">MFA</th>
                <th className="col-low">Last Login</th>
                <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr>
                  <td colSpan={8} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                    {search.trim() || statusFilter !== 'current' ? 'No accounts match.' : 'No users found.'}
                  </td>
                </tr>
              )}
              {users.map((u) => (
                <tr key={u.user_id}>
                  <td>{u.username}</td>
                  <td className="col-low" style={{ whiteSpace: 'normal' }}>
                    <span className="line-clamp-2 break-words" title={u.full_name}>
                      {u.full_name}
                    </span>
                  </td>
                  {/* an erased account's email is an opaque placeholder */}
                  <td className="muted">{u.status === 'erased' ? '—' : u.email}</td>
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
                    <span className={`state-pill ${STATUS_TONE[u.status]}`}>
                      <span className="state-dot" aria-hidden="true" />
                      {STATUS_LABEL[u.status]}
                    </span>
                    {u.status === 'locked' && (
                      <div className="text-xs text-tx-3 mt-1" title={lockedFrom(u)}>
                        <Icon name="lock" size={11} /> {u.locked_until ? 'Until ' + new Date(u.locked_until).toLocaleString() : 'Until unlocked'}
                      </div>
                    )}
                    {u.status === 'active' && u.deactivating_at && (
                      <div className="text-xs text-tx-3 mt-1" title={`No sign-in or personal MCP token use; deactivates on ${new Date(u.deactivating_at).toLocaleString()}.`}>
                        <Icon name="clock" size={11} /> Deactivating soon
                      </div>
                    )}
                  </td>
                  <td className="col-low">
                    <span className={`status ${u.mfa_enabled ? 'closed' : 'open'}`}>
                      {u.mfa_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                  <td className="muted col-low">
                    {u.last_login ? new Date(u.last_login).toLocaleDateString() : 'Never'}
                  </td>
                  <td className="col-act" style={{ textAlign: 'right' }}>
                    <div className="inline-flex gap-1.5">
                      {canWrite && live(u) && covers(u.role_id) && u.user_id !== me?.user_id && (
                        <button className="btn ghost icon" title="Sign-ins" onClick={() => setSignInsOf(u)}>
                          <Icon name="clock" size={15} />
                        </button>
                      )}
                      {canWrite && u.status === 'locked' && covers(u.role_id) && u.user_id !== me?.user_id && (
                        <button className="btn ghost icon" title="Unlock" onClick={() => setUnlockOf(u)}>
                          <Icon name="lock" size={15} />
                        </button>
                      )}
                      {canWrite && (live(u) || u.status === 'deactivated') && (
                        <button className="btn ghost icon" title="Edit" onClick={() => openEdit(u)}>
                          <Icon name="edit" size={15} />
                        </button>
                      )}
                      {canWrite && live(u) && covers(u.role_id) && u.user_id !== me?.user_id && (
                        <button className="btn ghost icon" title="Deactivate" onClick={() => setStatusAct({ user: u, act: 'deactivate' })}>
                          <Icon name="pause" size={15} />
                        </button>
                      )}
                      {canWrite && u.status === 'deactivated' && covers(u.role_id) && (
                        <button className="btn ghost icon" title="Reactivate" onClick={() => setStatusAct({ user: u, act: 'reactivate' })}>
                          <Icon name="play" size={15} />
                        </button>
                      )}
                      {canDelete && u.status === 'deleted' && covers(u.role_id) && (
                        <button className="btn ghost icon" title="Restore" onClick={() => setStatusAct({ user: u, act: 'restore' })}>
                          <Icon name="refresh" size={15} />
                        </button>
                      )}
                      {canDelete && (live(u) || u.status === 'deactivated') && (
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
        {total > PAGE && (
          <div className="flex items-center justify-end gap-2.5 mt-3 text-xs text-tx-3">
            <span className="font-mono">
              {page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of {total}
            </span>
            <button className="btn ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Previous
            </button>
            <button className="btn ghost" disabled={(page + 1) * PAGE >= total} onClick={() => setPage(page + 1)}>
              Next
            </button>
          </div>
        )}

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
              <Field label="Password" hint="A few unrelated words make a strong one.">
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

        {signInsOf && (
          <UserSignInsDialog user={signInsOf} onClose={() => setSignInsOf(null)} notify={notify} />
        )}

        <ConfirmDialog
          open={everyoneOpen}
          title="Sign everyone out?"
          body={
            <div className="flex flex-col gap-3.5">
              <span>
                This ends every open sign-in on this install, yours too, and everyone is told. People sign in again to
                carry on.
              </span>
              <Field label="Reason" hint="Kept in the audit trail.">
                <TextInput
                  maxLength={500}
                  value={everyoneReason}
                  disabled={signingOut}
                  onChange={(e) => setEveryoneReason(e.target.value)}
                />
              </Field>
            </div>
          }
          confirmLabel="Sign everyone out"
          busy={signingOut}
          confirmDisabled={!everyoneReason.trim()}
          onConfirm={handleSignOutEveryone}
          onClose={() => !signingOut && setEveryoneOpen(false)}
        />

        <ConfirmDialog
          open={!!unlockOf}
          title="Unlock account?"
          danger={false}
          body={
            <div className="flex flex-col gap-3.5">
              <span>
                {unlockOf?.username} {unlockOf?.locked_at ? lockedFrom(unlockOf) : 'is locked.'} Unlocking lets them
                sign in again and starts their failure count over. Confirm it is them first.
              </span>
              <Field label="How you confirmed it is them" hint="Kept in the audit trail.">
                <Select
                  value={confirmedBy}
                  options={CONFIRMED_BY}
                  placeholder="Choose…"
                  disabled={unlocking}
                  onSelect={setConfirmedBy}
                />
              </Field>
            </div>
          }
          confirmLabel="Unlock"
          busy={unlocking}
          confirmDisabled={!confirmedBy}
          onConfirm={handleUnlock}
          onClose={() => !unlocking && setUnlockOf(null)}
        />

        <ConfirmDialog
          open={!!statusAct}
          title={statusAct ? `${ACT_LABEL[statusAct.act]} ${statusAct.user.username}?` : ''}
          danger={statusAct?.act === 'deactivate'}
          body={
            statusAct && (
              <div className="flex flex-col gap-2.5">
                <span>{ACT_BODY[statusAct.act]}</span>
                {statusAct.act === 'deactivate' && <OpenCases user={statusAct.user} />}
              </div>
            )
          }
          confirmLabel={statusAct ? ACT_LABEL[statusAct.act] : ''}
          busy={acting}
          onConfirm={handleStatusAct}
          onClose={() => !acting && setStatusAct(null)}
        />

        <ConfirmDialog
          open={!!confirmDelete}
          title="Delete user?"
          body={
            confirmDelete && (
              <div className="flex flex-col gap-2.5">
                <span>
                  Delete {confirmDelete.username}? Their sign-ins end and their tokens are revoked. The account is kept
                  for the record, and someone with permission to delete accounts can restore it, deactivated.
                </span>
                <OpenCases user={confirmDelete} />
              </div>
            )
          }
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

const PAGE = 50

type StatusAct = 'deactivate' | 'reactivate' | 'restore'

const ACT_LABEL: Record<StatusAct, string> = {
  deactivate: 'Deactivate',
  reactivate: 'Reactivate',
  restore: 'Restore',
}

const ACT_BODY: Record<StatusAct, string> = {
  deactivate: 'Their sign-ins end and every token they hold is revoked. Reactivating later keeps their password and two-step sign-in, but not the tokens.',
  reactivate: 'They can sign in again with their password and two-step sign-in, and are told. Their old tokens stay revoked: new tokens must be minted.',
  restore: 'The account comes back deactivated, with its password and two-step sign-in, and they are told. Its tokens stay revoked. Reactivate it to let them sign in.',
}

const STATUS_LABEL: Record<AccountStatus, string> = {
  active: 'Active',
  locked: 'Locked',
  deactivated: 'Deactivated',
  deleted: 'Deleted',
  erased: 'Erased',
}

// state-pill tones: only Active and Locked carry a level
const STATUS_TONE: Record<AccountStatus, string> = {
  active: 'good',
  locked: 'fair',
  deactivated: '',
  deleted: '',
  erased: '',
}

const STATUS_FILTERS = [
  { value: 'current', label: 'Current accounts' },
  { value: 'active', label: 'Active' },
  { value: 'locked', label: 'Locked' },
  { value: 'deactivated', label: 'Deactivated' },
  { value: 'deleted', label: 'Deleted' },
  { value: 'erased', label: 'Erased' },
  { value: 'all', label: 'All, with deleted and erased' },
]

// can sign in, or could but for a lock
const live = (u: User) => u.status === 'active' || u.status === 'locked'

/** What the account owns that someone has to pick up, linked to Cases filtered to it. */
function OpenCases({ user }: { user: User }) {
  if (!user.open_cases) return <span className="text-tx-3">They own no open cases.</span>
  const n = user.open_cases
  return (
    <span>
      They own{' '}
      <Link className="underline" to={`/cases?owner=${encodeURIComponent(user.username)}`}>
        {n} open case{n === 1 ? '' : 's'}
      </Link>
      . Reassign {n === 1 ? 'it' : 'them'}.
    </span>
  )
}

const CONFIRMED_BY = [
  { value: 'in_person', label: 'In person' },
  { value: 'video_call', label: 'On a video call' },
  { value: 'phone_call_back', label: 'By calling them back on a known number' },
  { value: 'other_verified_channel', label: 'Another verified channel' },
]

// when and where the lock was set, as a sentence
function lockedFrom(u: User): string {
  const at = u.locked_at ? ` on ${new Date(u.locked_at).toLocaleString()}` : ''
  return `was locked${at}${u.locked_from ? ` from ${u.locked_from}` : ''}.`
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
              <th style={{ minWidth: 'var(--col-main-min)' }}>
                Role
                <span className="col-low-note">Description is hidden while Ask Vigil is open</span>
              </th>
              <th className="col-low">Description</th>
              <th>Permissions</th>
              <th>Accounts</th>
              <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
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
                <td className="muted col-low">
                  <span className="line-clamp-2 break-words" title={r.description}>
                    {r.description}
                  </span>
                </td>
                <td className="muted font-mono">{granted(r.permissions).length}</td>
                <td className="muted font-mono">{r.holders}</td>
                <td className="col-act" style={{ textAlign: 'right' }}>
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
