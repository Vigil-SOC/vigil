import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { Icon } from '../../shared/icons'
import '../../shared/kit.css'
import { ConfirmDialog, Field, Popup, SettingsCard, TextInput } from '../../shared/ui'
import api, { teamsApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import type { User } from './useSettings'
import type { SectionProps } from './types'

type Team = Schema<'TeamOut'>
type Detail = Schema<'TeamDetail'>
type Member = Schema<'TeamMemberOut'>
type Naming = { mode: 'create' } | { mode: 'rename'; team: Team }

function errText(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string })?.msg || JSON.stringify(d)).join(', ')
  return (e as { message?: string })?.message || fallback
}

const personName = (p: { display_name?: string | null; username: string; full_name?: string }) => {
  const name = (p.display_name ?? p.full_name ?? '').trim()
  return name || p.username
}

/** People matching a search, excluding anyone already on the team. */
function usePersonSearch(query: string, excludeKey: string, enabled: boolean) {
  const [hits, setHits] = useState<User[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const q = query.trim()
    if (!enabled || !q) {
      setHits([])
      setSearching(false)
      setError('')
      return
    }
    let live = true
    setHits([])
    setSearching(true)
    setError('')
    const t = setTimeout(() => {
      const exclude = new Set(excludeKey ? excludeKey.split('\n') : [])
      api
        .get<{ users?: User[] }>('/users/', {
          params: { service_account: false, search: q, limit: 20, status: 'current' },
        })
        .then(({ data }) => {
          if (!live) return
          setHits(
            (data.users ?? []).filter((u) => u.principal_kind !== 'service_account' && !exclude.has(u.user_id)),
          )
          setError('')
        })
        .catch((e) => {
          if (!live) return
          setHits([])
          setError(errText(e, 'Couldn’t search people'))
        })
        .finally(() => {
          if (live) setSearching(false)
        })
    }, 300)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [query, excludeKey, enabled])
  return { hits, searching, error }
}

/** Teams on Team & access: list and open them, and change them with teams.manage. */
export default function TeamsCard({ notify }: SectionProps) {
  const { hasPermission } = useAuth()
  const canManage = hasPermission('teams.manage')
  const [teams, setTeams] = useState<Team[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [opened, setOpened] = useState<Team | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [openError, setOpenError] = useState('')
  const [warning, setWarning] = useState<string | null>(null)
  const [naming, setNaming] = useState<Naming | null>(null)
  const [name, setName] = useState('')
  const [nameError, setNameError] = useState('')
  const [archiving, setArchiving] = useState<Team | null>(null)
  const [archiveError, setArchiveError] = useState('')
  const [removing, setRemoving] = useState<Member | null>(null)
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [addingId, setAddingId] = useState<string | null>(null)
  const openGen = useRef(0)

  const reload = useCallback(() => {
    teamsApi
      .list()
      .then(({ data }) => {
        setTeams(data)
        setLoadError('')
      })
      .catch((e) => setLoadError(errText(e, 'Couldn’t load teams')))
  }, [])
  useEffect(reload, [reload])

  const apply = (team: Team) => {
    setOpened((cur) => (cur && cur.id === team.id ? team : cur))
    setDetail((cur) => (cur && cur.id === team.id ? { ...cur, ...team, members: cur.members } : cur))
    setTeams((cur) => cur?.map((t) => (t.id === team.id ? team : t)) ?? cur)
  }

  const open = (team: Team) => {
    const gen = ++openGen.current
    setOpened(team)
    setDetail(null)
    setOpenError('')
    setWarning(null)
    setQuery('')
    teamsApi.get(team.id).then(
      ({ data }) => {
        if (openGen.current === gen) setDetail(data)
      },
      (e) => {
        if (openGen.current === gen) setOpenError(errText(e, 'Couldn’t open this team'))
      },
    )
  }

  const closeOpen = () => {
    if (busy) return
    setOpened(null)
    setDetail(null)
    setWarning(null)
    setQuery('')
  }

  const startCreate = () => {
    setName('')
    setNameError('')
    setNaming({ mode: 'create' })
  }

  const startRename = () => {
    if (!opened) return
    setName(opened.name)
    setNameError('')
    setNaming({ mode: 'rename', team: opened })
  }

  const saveName = async () => {
    if (!naming) return
    const next = name.trim()
    if (!next) return setNameError('A team needs a name.')
    setBusy(true)
    setNameError('')
    try {
      const { data } = naming.mode === 'create' ? await teamsApi.create(next) : await teamsApi.rename(naming.team.id, next)
      setNaming(null)
      apply(data)
      if (naming.mode === 'create') open(data)
      notify('ok', naming.mode === 'create' ? `Created ${data.name}.` : `Renamed to ${data.name}.`)
      reload()
    } catch (e) {
      setNameError(errText(e, naming.mode === 'create' ? 'Couldn’t create the team' : 'Couldn’t rename the team'))
    } finally {
      setBusy(false)
    }
  }

  const archive = async () => {
    if (!archiving) return
    setBusy(true)
    setArchiveError('')
    try {
      const { data } = await teamsApi.archive(archiving.id)
      apply(data)
      setArchiving(null)
      notify('ok', `Archived ${data.name}.`)
      reload()
    } catch (e) {
      setArchiveError(errText(e, 'Couldn’t archive the team'))
    } finally {
      setBusy(false)
    }
  }

  const unarchive = async () => {
    if (!opened) return
    setBusy(true)
    setOpenError('')
    try {
      const { data } = await teamsApi.unarchive(opened.id)
      apply(data)
      notify('ok', `Brought ${data.name} back.`)
      reload()
    } catch (e) {
      setOpenError(errText(e, 'Couldn’t bring the team back'))
    } finally {
      setBusy(false)
    }
  }

  const add = async (user: User) => {
    if (!opened) return
    const gen = openGen.current
    setAddingId(user.user_id)
    setOpenError('')
    setWarning(null)
    try {
      const { data } = await teamsApi.addMember(opened.id, user.user_id)
      if (openGen.current !== gen) return
      setDetail(data)
      setQuery('')
      notify('ok', `Added ${personName(user)} to ${data.name}.`)
    } catch (e) {
      if (openGen.current === gen) setOpenError(errText(e, 'Couldn’t add this person'))
    } finally {
      setAddingId(null)
    }
  }

  const remove = async () => {
    if (!opened || !removing) return
    const gen = openGen.current
    const teamId = opened.id
    setBusy(true)
    setOpenError('')
    const who = personName(removing)
    const teamName = opened.name
    try {
      const { data } = await teamsApi.removeMember(teamId, removing.user_id)
      setRemoving(null)
      if (openGen.current !== gen) return
      // The server's warning, as returned: a last team, an empty team with unowned cases, or both.
      setWarning(data.warning ?? null)
      if (!data.warning) notify('ok', `Removed ${who} from ${teamName}.`)
    } catch (e) {
      if (openGen.current === gen) setOpenError(errText(e, 'Couldn’t remove this person'))
      return
    } finally {
      setBusy(false)
    }
    try {
      const { data: fresh } = await teamsApi.get(teamId)
      if (openGen.current === gen) setDetail(fresh)
    } catch (e) {
      if (openGen.current === gen) setOpenError(errText(e, 'Couldn’t refresh members'))
    }
  }

  const excludeKey = (detail?.members ?? []).map((m) => m.user_id).join('\n')
  const searchOn = !!opened && canManage && !!detail && !detail.archived
  const { hits, searching, error: searchError } = usePersonSearch(query, excludeKey, searchOn)

  return (
    <>
      <SettingsCard
        wide
        title="Teams"
        desc="People grouped for the work they share. A new account joins the default team unless an invite names others."
        actions={
          <>
            <button className="btn ghost" onClick={reload}>
              <Icon name="refresh" /> Refresh
            </button>
            {canManage && (
              <button className="btn primary" onClick={startCreate}>
                <Icon name="plus" /> New team
              </button>
            )}
          </>
        }
      >
        {loadError && (
          <div className="settings-banner err mb-3" role="alert">
            <Icon name="alert" size={14} /> {loadError}
          </div>
        )}
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ minWidth: 'var(--col-main-min)' }}>Team</th>
                <th className="col-act" style={{ textAlign: 'right' }}>
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {teams === null && !loadError && (
                <tr>
                  <td colSpan={2} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                    Loading teams…
                  </td>
                </tr>
              )}
              {teams !== null && teams.length === 0 && (
                <tr>
                  <td colSpan={2} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>
                    No teams.
                  </td>
                </tr>
              )}
              {teams?.map((team) => (
                <tr key={team.id}>
                  <td className="cell-wrap">
                    <div className="cell-clamp">{team.name}</div>
                    {(team.is_default || team.archived) && (
                      <div className="flex flex-wrap items-center gap-1.5 mt-1">
                        {team.is_default && <span className="chip">Default</span>}
                        {team.archived && (
                          <span className="state-pill">
                            <span className="state-dot" aria-hidden="true" />
                            Archived
                          </span>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="col-act" style={{ textAlign: 'right' }}>
                    <button className="btn ghost" onClick={() => open(team)} aria-label={`Open ${team.name}`}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SettingsCard>

      <Popup open={!!opened} onClose={closeOpen} title={detail?.name ?? opened?.name ?? 'Team'} width={520}>
        {opened && (
          <div className="flex flex-col gap-3.5">
            {(opened.is_default || opened.archived) && (
              <div className="flex flex-wrap items-center gap-1.5">
                {opened.is_default && <span className="chip">Default</span>}
                {opened.archived && (
                  <span className="state-pill">
                    <span className="state-dot" aria-hidden="true" />
                    Archived
                  </span>
                )}
              </div>
            )}
            {openError && (
              <div className="settings-banner err" role="alert">
                <Icon name="alert" size={14} /> <span className="min-w-0">{openError}</span>
              </div>
            )}
            {warning && (
              <div className="settings-banner warn" role="status">
                <Icon name="alert" size={14} /> <span className="min-w-0">{warning}</span>
              </div>
            )}
            {detail === null && !openError && <div className="text-sm text-tx-3">Loading members…</div>}
            {detail && (
              <>
                {detail.members.length === 0 ? (
                  <div className="text-sm text-tx-3">No members.</div>
                ) : (
                  <ul className="flex flex-col m-0 p-0 list-none" style={{ borderTop: '1px solid var(--ln0)' }}>
                    {detail.members.map((m) => (
                      <li
                        key={m.user_id}
                        className="flex items-center gap-2 py-2"
                        style={{ borderBottom: '1px solid var(--ln0)' }}
                      >
                        <div className="min-w-0 flex-1">
                          <div className="break-words">{personName(m)}</div>
                          {personName(m) !== m.username && <div className="muted text-xs break-all">{m.username}</div>}
                        </div>
                        {canManage && (
                          <button
                            className="btn ghost shrink-0"
                            onClick={() => setRemoving(m)}
                            aria-label={`Remove ${personName(m)} from ${detail.name}`}
                          >
                            Remove
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {searchOn && (
                  <div className="flex flex-col gap-2">
                    <div className="search" style={{ minWidth: 0, width: '100%' }}>
                      <Icon name="search" />
                      <input
                        aria-label="Search people to add"
                        placeholder="Search people to add…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                    </div>
                    {searchError && (
                      <div className="settings-banner err" role="alert">
                        <Icon name="alert" size={14} /> <span className="min-w-0">{searchError}</span>
                      </div>
                    )}
                    {query.trim() && !searchError && (searching || hits.length === 0) && (
                      <div className="text-xs text-tx-3" role="status">
                        {searching ? 'Searching…' : 'No one matches.'}
                      </div>
                    )}
                    {hits.length > 0 && (
                      <div className="flex flex-col" style={{ maxHeight: 180, overflowY: 'auto' }}>
                        {hits.map((u) => (
                          <button
                            key={u.user_id}
                            className="btn ghost"
                            style={{ justifyContent: 'flex-start', height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}
                            disabled={addingId !== null}
                            onClick={() => add(u)}
                          >
                            <span className="min-w-0 break-words">
                              {addingId === u.user_id ? 'Adding…' : personName(u)}
                              {personName(u) !== u.username && <span className="text-tx-3"> · {u.username}</span>}
                              {u.status !== 'active' && <span className="text-tx-3"> · {u.status}</span>}
                            </span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
            {canManage && (
              <div className="flex flex-wrap justify-end gap-2">
                <button className="btn ghost" onClick={startRename} disabled={busy}>
                  <Icon name="edit" size={14} /> Rename
                </button>
                {!opened.is_default && !opened.archived && (
                  <button
                    className="btn ghost"
                    disabled={busy}
                    onClick={() => {
                      setArchiveError('')
                      setArchiving(opened)
                    }}
                  >
                    Archive
                  </button>
                )}
                {opened.archived && (
                  <button className="btn ghost" onClick={unarchive} disabled={busy}>
                    {busy ? 'Working…' : 'Unarchive'}
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </Popup>

      <Popup
        open={!!naming}
        onClose={() => !busy && setNaming(null)}
        title={naming?.mode === 'rename' ? 'Rename team' : 'New team'}
        width={460}
      >
        <form
          className="flex flex-col gap-3.5"
          onSubmit={(e) => {
            e.preventDefault()
            void saveName()
          }}
        >
          {nameError && (
            <div className="settings-banner err" role="alert">
              <Icon name="alert" size={14} /> <span className="min-w-0">{nameError}</span>
            </div>
          )}
          <Field label="Name">
            <TextInput
              value={name}
              maxLength={100}
              disabled={busy}
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <div className="flex flex-wrap justify-end gap-2.5">
            <button type="button" className="btn ghost" onClick={() => setNaming(null)} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? 'Saving…' : naming?.mode === 'rename' ? 'Rename' : 'Create'}
            </button>
          </div>
        </form>
      </Popup>

      <ConfirmDialog
        open={!!archiving}
        title={archiving ? `Archive ${archiving.name}?` : ''}
        body={
          <div className="flex flex-col gap-3">
            <span>It stays in this list. It accepts no one until you bring it back.</span>
            {archiveError && (
              <div className="settings-banner err" role="alert">
                <Icon name="alert" size={14} /> <span className="min-w-0">{archiveError}</span>
              </div>
            )}
          </div>
        }
        confirmLabel="Archive"
        busy={busy}
        onConfirm={archive}
        onClose={() => !busy && setArchiving(null)}
      />

      <ConfirmDialog
        open={!!removing}
        title={removing ? `Remove ${personName(removing)}?` : ''}
        body={
          opened && removing
            ? `${personName(removing)} leaves ${opened.name}.`
            : ''
        }
        confirmLabel="Remove"
        busy={busy}
        onConfirm={remove}
        onClose={() => !busy && setRemoving(null)}
      />
    </>
  )
}
