import { Fragment, useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, Popup, Select, SettingsCard, TextInput } from '../../shared/ui'
import { useAuth } from '../../contexts/AuthContext'
import api from '../../services/api'
import { HeldTokens } from './TokensCard'
import type { Role, User } from './useSettings'
import type { SectionProps } from './types'

const VIEWER = 'role-viewer'
// The server's rule: lower-case letters, digits, dot, dash and underscore.
const NAME_RE = /^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$/

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string })?.msg).join(', ')
  return fallback
}

const personLabel = (u: { username: string; full_name: string }) =>
  u.full_name && u.full_name !== u.username ? `${u.full_name} (${u.username})` : u.username

type Draft = { name: string; purpose: string; role_id: string; accountable_user_id: string }

/**
 * Accounts that integrations call Vigil as, with an API token and nothing else:
 * no password, no two-step sign-in. A person answers for each. Medic's own
 * gateway account is listed here too, managed by Medic and holding no tokens.
 */
export default function ServiceAccountsCard({ notify, onChange }: SectionProps & { onChange?: () => void }) {
  const { user: me, hasPermission } = useAuth()
  const canWrite = hasPermission('users.write')
  const [accounts, setAccounts] = useState<User[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [people, setPeople] = useState<User[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [reassigning, setReassigning] = useState<{ user: User; to: string } | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    const list = (kind: string, status: string) =>
      api.get<{ users: User[] }>('/users/', { params: { kind, status, limit: 1000 } }).then((r) => r.data.users)
    Promise.all([list('service_account', 'current'), canWrite ? list('person', 'active') : [], api.get('/users/roles/list')])
      .then(([a, p, r]) => {
        setAccounts(a)
        setPeople(p)
        setRoles(r.data.roles ?? [])
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [canWrite])
  useEffect(load, [load])

  const changed = () => {
    load()
    onChange?.()
  }

  const roleName = (id: string) => roles.find((r) => r.role_id === id)?.name ?? id
  const roleOptions = roles
    .filter((r) => r.kind !== 'legacy')
    .map((r) => ({ value: r.role_id, label: r.description ? `${r.name} — ${r.description}` : r.name }))
  const personOptions = people.map((p) => ({ value: p.user_id, label: personLabel(p) }))

  const openCreate = () => {
    setError('')
    setDraft({ name: '', purpose: '', role_id: VIEWER, accountable_user_id: me?.user_id ?? '' })
  }

  const create = async () => {
    if (!draft) return
    if (!NAME_RE.test(draft.name)) return setError('A name is 3–64 lower-case letters, digits, dots, dashes or underscores.')
    if (!draft.purpose.trim()) return setError('Say what it is for.')
    if (!draft.accountable_user_id) return setError('Choose the person who answers for it.')
    setSaving(true)
    setError('')
    try {
      await api.post('/users/service-accounts', { ...draft, purpose: draft.purpose.trim() })
      notify('ok', `Created ${draft.name}. Mint it a token to use it.`)
      setDraft(null)
      setOpen(null)
      changed()
    } catch (e) {
      setError(errText(e, 'Could not create the service account'))
    } finally {
      setSaving(false)
    }
  }

  const reassign = async () => {
    if (!reassigning) return
    setSaving(true)
    setError('')
    try {
      await api.put(`/users/${encodeURIComponent(reassigning.user.user_id)}/accountable`, {
        accountable_user_id: reassigning.to,
      })
      notify('ok', `${reassigning.user.username} has a new accountable person.`)
      setReassigning(null)
      changed()
    } catch (e) {
      setError(errText(e, 'Could not reassign'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard
      wide
      id="service-accounts"
      title="Service accounts"
      desc="Accounts an integration calls Vigil as, with an API token. They have no password and no two-step sign-in, and a person answers for each. Its tokens last a year."
      actions={
        <>
          <button className="btn ghost" onClick={load}>
            <Icon name="refresh" /> Refresh
          </button>
          {canWrite && (
            <button className="btn primary" onClick={openCreate}>
              <Icon name="plus" /> New service account
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
            title="Couldn’t load the service accounts"
            primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
          />
        ) : (
          <EmptyState compact loading icon="link" title="Loading service accounts…" />
        )
      ) : accounts.length === 0 ? (
        <EmptyState
          compact
          icon="link"
          title="No service accounts"
          body="Create one for an integration that reads from Vigil, such as a SIEM forwarding the audit trail."
        />
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
              {accounts.map((u) => (
                <Fragment key={u.user_id}>
                  <tr>
                    <td style={{ whiteSpace: 'normal' }}>
                      <div className="flex flex-col items-start gap-1">
                        <span>{u.username}</span>
                        {u.purpose && (
                          <span className="text-xs text-tx-3 line-clamp-2 break-words" title={u.purpose}>
                            {u.purpose}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className="chip">{roleName(u.role_id)}</span>
                    </td>
                    <td style={{ whiteSpace: 'normal' }}>
                      {u.managed_by_medic ? (
                        <span className="text-tx-3">Managed by Medic</span>
                      ) : (
                        <div className="flex flex-col items-start gap-1">
                          {u.accountable && <span>{personLabel(u.accountable)}</span>}
                          {u.accountable_gone && (
                            <span className="state-pill fair">
                              <span className="state-dot" aria-hidden="true" />
                              Accountable person gone: reassign
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className={`state-pill ${u.status === 'active' ? 'good' : ''}`}>
                        <span className="state-dot" aria-hidden="true" />
                        {u.status === 'active' ? 'Active' : 'Deactivated'}
                      </span>
                    </td>
                    <td className="col-act" style={{ textAlign: 'right' }}>
                      {canWrite && !u.managed_by_medic && (
                        <div className="inline-flex gap-1.5">
                          <button
                            className="btn ghost"
                            aria-expanded={open === u.user_id}
                            onClick={() => setOpen(open === u.user_id ? null : u.user_id)}
                          >
                            <Icon name="link" size={14} /> Tokens
                          </button>
                          <button
                            className="btn ghost"
                            onClick={() => {
                              setError('')
                              setReassigning({ user: u, to: '' })
                            }}
                          >
                            <Icon name="user" size={14} /> Reassign
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                  {open === u.user_id && (
                    <tr>
                      <td colSpan={5} style={{ whiteSpace: 'normal' }}>
                        <HeldTokens notify={notify} holder={u} onChange={changed} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Popup open={!!draft} onClose={() => !saving && setDraft(null)} title="New service account" width={480}>
        {draft && (
          <div className="flex flex-col gap-3.5">
            {error && (
              <div className="settings-banner err">
                <Icon name="alert" size={14} /> {error}
              </div>
            )}
            <Field label="Name" hint="The account’s sign-in name, such as “siem-forwarder”.">
              <TextInput value={draft.name} maxLength={64} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </Field>
            <Field label="Purpose" hint="What the integration does with it.">
              <TextInput
                value={draft.purpose}
                maxLength={500}
                onChange={(e) => setDraft({ ...draft, purpose: e.target.value })}
              />
            </Field>
            <Field label="Role" hint="Giving it audit.read needs audit.forward.">
              <Select value={draft.role_id} options={roleOptions} onSelect={(v) => setDraft({ ...draft, role_id: v })} />
            </Field>
            <Field label="Accountable person" hint="An active person who answers for it and is told about its tokens.">
              <Select
                value={draft.accountable_user_id}
                options={personOptions}
                searchable
                placeholder="Choose a person…"
                onSelect={(v) => setDraft({ ...draft, accountable_user_id: v })}
              />
            </Field>
            <div className="flex justify-end gap-2.5 mt-1">
              <button className="btn ghost" onClick={() => setDraft(null)} disabled={saving}>
                Cancel
              </button>
              <button className="btn primary" onClick={create} disabled={saving}>
                {saving ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        )}
      </Popup>

      <Popup
        open={!!reassigning}
        onClose={() => !saving && setReassigning(null)}
        title={`Reassign ${reassigning?.user.username ?? ''}`}
        width={440}
      >
        {reassigning && (
          <div className="flex flex-col gap-3.5">
            {error && (
              <div className="settings-banner err">
                <Icon name="alert" size={14} /> {error}
              </div>
            )}
            <Field label="Accountable person">
              <Select
                value={reassigning.to}
                options={personOptions.filter((p) => p.value !== reassigning.user.accountable?.user_id)}
                searchable
                placeholder="Choose a person…"
                onSelect={(v) => setReassigning({ ...reassigning, to: v })}
              />
            </Field>
            <div className="flex justify-end gap-2.5 mt-1">
              <button className="btn ghost" onClick={() => setReassigning(null)} disabled={saving}>
                Cancel
              </button>
              <button className="btn primary" onClick={reassign} disabled={saving || !reassigning.to}>
                {saving ? 'Saving…' : 'Reassign'}
              </button>
            </div>
          </div>
        )}
      </Popup>
    </SettingsCard>
  )
}
