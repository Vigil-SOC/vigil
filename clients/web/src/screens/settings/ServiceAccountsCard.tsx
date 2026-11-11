import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, Popup, Select, SettingsCard, TextInput } from '../../shared/ui'
import api from '../../services/api'
import TokensCard from './TokensCard'
import type { Role, User } from './useSettings'
import type { SectionProps } from './types'

const NAME_RE = /^[a-z0-9][a-z0-9._-]{2,63}$/

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string })?.msg || JSON.stringify(d)).join(', ')
  return fallback
}

const roleLabel = (r: Role) => (r.kind === 'legacy' ? `${r.name} (legacy)` : r.name)

const EMPTY = { name: '', purpose: '', role_id: '', accountable: '' }

/**
 * Token-only accounts for integrations: no password, no sign-in. Each answers to
 * an accountable person. Medic's is listed but managed by Medic. Deactivate,
 * change the role or delete one under Team & access.
 */
export default function ServiceAccountsCard({ notify, canWrite }: SectionProps & { canWrite: boolean }) {
  const [accounts, setAccounts] = useState<User[] | null>(null)
  const [people, setPeople] = useState<User[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [failed, setFailed] = useState(false)
  const [form, setForm] = useState(EMPTY)
  const [creating, setCreating] = useState(false)
  const [reassign, setReassign] = useState<User | null>(null)
  const [person, setPerson] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [tokensOf, setTokensOf] = useState<User | null>(null)

  const load = useCallback(() => {
    Promise.all([
      api.get('/users/', { params: { service_account: true, limit: 1000 } }),
      api.get('/users/', { params: { service_account: false, is_active: true, limit: 1000 } }),
      api.get('/users/roles/list'),
    ])
      .then(([a, p, r]) => {
        setAccounts(a.data.users)
        setPeople(p.data.users)
        setRoles(r.data.roles)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [])
  useEffect(load, [load])

  const roleName = (id: string) => {
    const r = roles.find((x) => x.role_id === id)
    return r ? roleLabel(r) : id
  }
  const roleOptions = roles.map((r) => ({ value: r.role_id, label: roleLabel(r) }))
  const personOptions = people.map((p) => ({
    value: p.user_id,
    label: p.full_name && p.full_name !== p.username ? `${p.full_name} (${p.username})` : p.username,
  }))

  const openCreate = () => {
    const viewer = roles.find((r) => r.role_id === 'role-viewer') ?? roles.find((r) => r.kind !== 'legacy')
    setForm({ ...EMPTY, role_id: viewer?.role_id ?? '' })
    setError('')
    setCreating(true)
  }

  const create = async () => {
    if (!NAME_RE.test(form.name)) return setError('A name is 3 to 64 lowercase letters, digits, dots, dashes or underscores.')
    if (!form.purpose.trim()) return setError('Say what it is for.')
    if (!form.role_id) return setError('Pick a role.')
    if (!form.accountable) return setError('Pick the person accountable for it.')
    setSaving(true)
    setError('')
    try {
      await api.post('/users/service-accounts', { ...form, purpose: form.purpose.trim() })
      notify('ok', `Created ${form.name}. Mint it a token to put it to work.`)
      setCreating(false)
      load()
    } catch (e) {
      setError(errText(e, 'Could not create the service account'))
    } finally {
      setSaving(false)
    }
  }

  const doReassign = async () => {
    if (!reassign || !person) return
    setSaving(true)
    setError('')
    try {
      await api.put(`/users/service-accounts/${encodeURIComponent(reassign.user_id)}/accountable`, { accountable: person })
      notify('ok', `${reassign.username} is now the responsibility of the person you chose.`)
      setReassign(null)
      load()
    } catch (e) {
      setError(errText(e, 'Could not reassign'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <SettingsCard
        wide
        id="service-accounts"
        title="Service accounts"
        desc="Accounts for integrations. They have no password and no sign-in: a token is how one acts, with what its role allows. Each answers to an accountable person."
        actions={
          <>
            <button className="btn ghost" onClick={load}>
              <Icon name="refresh" /> Refresh
            </button>
            {canWrite && (
              <button className="btn primary" disabled={!roles.length} onClick={openCreate}>
                <Icon name="plus" /> Add service account
              </button>
            )}
          </>
        }
      >
        {accounts === null ? (
          failed ? (
            <EmptyState
              compact
              error
              icon="alert"
              title="Couldn’t load service accounts"
              primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
            />
          ) : (
            <EmptyState compact loading icon="link" title="Loading service accounts…" />
          )
        ) : accounts.length === 0 ? (
          <EmptyState compact icon="link" title="No service accounts" body="Add one to give an integration its own access." />
        ) : (
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th style={{ minWidth: 'var(--col-main-min)' }}>Name</th>
                  <th>Role</th>
                  <th>Accountable person</th>
                  <th>Status</th>
                  <th className="col-act" style={{ textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((a) => (
                  <tr key={a.user_id}>
                    <td style={{ whiteSpace: 'normal' }}>
                      <div className="flex flex-col items-start gap-1">
                        <span className="break-all">{a.username}</span>
                        {a.purpose && (
                          <span className="text-xs text-tx-3 line-clamp-2 break-words" title={a.purpose}>
                            {a.purpose}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className="chip">{roleName(a.role_id)}</span>
                    </td>
                    <td style={{ whiteSpace: 'normal' }}>
                      {a.managed_by_medic ? (
                        <span className="text-xs text-tx-3">Managed by Medic</span>
                      ) : a.accountable_gone ? (
                        <span className="text-xs" style={{ color: 'var(--poor)' }}>
                          <Icon name="alert" size={11} /> Accountable person gone: reassign
                        </span>
                      ) : (
                        <span className="break-words">{a.accountable?.full_name || a.accountable?.username}</span>
                      )}
                    </td>
                    <td>
                      <span className={`state-pill ${a.status === 'active' ? 'good' : ''}`}>
                        <span className="state-dot" aria-hidden="true" />
                        {a.status === 'active' ? 'Active' : a.status[0].toUpperCase() + a.status.slice(1)}
                      </span>
                    </td>
                    <td className="col-act" style={{ textAlign: 'right' }}>
                      {!a.managed_by_medic && canWrite && a.status === 'active' && (
                        <div className="inline-flex gap-1.5">
                          <button className="btn ghost" onClick={() => setTokensOf(tokensOf?.user_id === a.user_id ? null : a)}>
                            <Icon name="link" size={14} /> Tokens
                          </button>
                          <button
                            className="btn ghost"
                            onClick={() => {
                              setPerson('')
                              setError('')
                              setReassign(a)
                            }}
                          >
                            <Icon name="edit" size={14} /> Reassign
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Popup open={creating} onClose={() => !saving && setCreating(false)} title="Add service account" width={460}>
          <div className="flex flex-col gap-3.5">
            {error && (
              <div className="settings-banner err">
                <Icon name="alert" size={14} /> {error}
              </div>
            )}
            <Field label="Name" hint="Its sign-in name. Lowercase; it cannot be changed.">
              <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
            <Field label="Purpose">
              <TextInput
                value={form.purpose}
                maxLength={500}
                placeholder="What it does and for whom"
                onChange={(e) => setForm({ ...form, purpose: e.target.value })}
              />
            </Field>
            <Field label="Role" hint="A role holding audit.read needs you to hold audit.forward.">
              <Select
                value={form.role_id}
                options={roleOptions}
                onSelect={(v) => setForm({ ...form, role_id: v })}
              />
            </Field>
            <Field label="Accountable person" hint="An active person who answers for this account.">
              <Select
                value={form.accountable}
                options={personOptions}
                searchable
                placeholder="Choose a person…"
                onSelect={(v) => setForm({ ...form, accountable: v })}
              />
            </Field>
            <div className="flex justify-end gap-2.5 mt-1">
              <button className="btn ghost" onClick={() => setCreating(false)} disabled={saving}>
                Cancel
              </button>
              <button className="btn primary" onClick={create} disabled={saving}>
                {saving ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        </Popup>

        <Popup open={!!reassign} onClose={() => !saving && setReassign(null)} title={`Reassign ${reassign?.username ?? ''}`} width={460}>
          <div className="flex flex-col gap-3.5">
            {error && (
              <div className="settings-banner err">
                <Icon name="alert" size={14} /> {error}
              </div>
            )}
            <Field label="Accountable person" hint="They are told. The change is kept in the audit trail.">
              <Select value={person} options={personOptions} searchable placeholder="Choose a person…" onSelect={setPerson} />
            </Field>
            <div className="flex justify-end gap-2.5 mt-1">
              <button className="btn ghost" onClick={() => setReassign(null)} disabled={saving}>
                Cancel
              </button>
              <button className="btn primary" onClick={doReassign} disabled={saving || !person}>
                {saving ? 'Saving…' : 'Reassign'}
              </button>
            </div>
          </div>
        </Popup>
      </SettingsCard>

      {tokensOf && <TokensCard key={tokensOf.user_id} notify={notify} canManage account={{ user_id: tokensOf.user_id, username: tokensOf.username }} />}
    </>
  )
}
