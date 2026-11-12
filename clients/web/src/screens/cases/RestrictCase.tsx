import { useEffect, useState } from 'react'
import { casesApi, teamsApi } from '../../services/api'
import type { CaseRecord } from '../../data/data'
import { useAuth } from '../../contexts/AuthContext'
import { Icon } from '../../shared/icons'
import { usePeople, type Person } from '../../shared/people'
import { Popup } from '../../shared/ui'
import { errorDetail } from './caseFormat'
import { inputCls } from './CaseSections'

/** What restricting a case means, word for word (epic #4352, behaviour 20 with decision C). */
const RESTRICT_HELP = [
  'Only the people and teams listed can open this case. Its alerts stay visible wherever alerts are shown, and others can see that an alert or indicator belongs to a restricted case, but not which case or what’s in it.',
  'Auditors see who changed this case and when, not its contents.',
  'Vigil’s agents, including automation your admins configure, can read it to investigate; what Vigil records from that stays with this case.',
  'People who can install integrations or configure Vigil’s agents can reach it through what they configure.',
  'A team kept in step with your identity provider includes whoever is added to that group there.',
  'New cases start open; restrict a case to limit who can see it.',
]

export function RestrictHelp() {
  return (
    <ul className="restrict-help">
      {RESTRICT_HELP.map((line) => <li key={line}>{line}</li>)}
    </ul>
  )
}

interface NamedTeam {
  id: string
  name: string
  is_default: boolean
}

// One teams read shared by every caller within a minute, like the people list.
let teamsCache: Promise<NamedTeam[]> | null = null
let teamsAt = 0
function loadTeams(): Promise<NamedTeam[]> {
  if (teamsCache && Date.now() - teamsAt < 60_000) return teamsCache
  teamsAt = Date.now()
  teamsCache = teamsApi.list().then(
    (r) => r.data.filter((team) => !team.archived),
    (e) => {
      teamsCache = null
      throw e
    },
  )
  return teamsCache
}

/** The teams a reader may name: the teams list, for users.read holders only; null without it. */
function useNamedTeams(): NamedTeam[] | null {
  const { hasPermission } = useAuth()
  const canRead = hasPermission('users.read')
  const [teams, setTeams] = useState<NamedTeam[]>([])
  useEffect(() => {
    if (!canRead) return
    let live = true
    loadTeams().then((list) => live && setTeams(list), () => live && setTeams([]))
    return () => {
      live = false
    }
  }, [canRead])
  return canRead ? teams : null
}

const personName = (p: Person) => p.display_name || p.username

/**
 * Reader ids as names. A person id not in the directory shows as stored. Team names come from
 * the teams list, else the viewer's own teams; any other team is counted, not named.
 */
export function useReaderNames() {
  const { user } = useAuth()
  const { people, loaded } = usePeople()
  const teams = useNamedTeams()
  const me = people.find((p) => p.username === user?.username)
  const byId = new Map(people.map((p) => [p.principal_id, p]))
  const person = (id: string) => {
    const p = byId.get(id)
    if (!p) return id
    return p === me ? `${personName(p)} (you)` : personName(p)
  }
  const teamName = (id: string) => teams?.find((t) => t.id === id)?.name ?? me?.teams.find((t) => t.id === id)?.name
  const names = (readers: string[], readerTeams: string[]): string[] => {
    const named = readerTeams.flatMap((id) => teamName(id) ?? [])
    const unnamed = readerTeams.length - named.length
    const count = unnamed ? [`${unnamed} ${named.length ? 'other ' : ''}team${unnamed === 1 ? '' : 's'}`] : []
    return [...readers.map(person), ...named, ...count]
  }
  return { people, loaded, me, teams, names, person }
}

/** "Restricted" and who reads it. ``limit`` shows that many names and "+N". */
export function RestrictedMark({ names, limit }: { names: string[]; limit?: number }) {
  const shown = limit ? names.slice(0, limit) : names
  const more = names.length - shown.length
  return (
    <span className="restricted-mark" title={`Restricted to ${names.join(', ')}`}>
      <span className="restricted-chip">
        <Icon name="lock" size={11} />
        Restricted
      </span>
      {names.length > 0 && (
        <span className="restricted-readers">
          {shown.join(', ')}
          {more > 0 && ` +${more}`}
        </span>
      )}
    </span>
  )
}

const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id])

/** Tick people and teams. Ticked ids missing from the directory stay listed, as stored. */
export function ReaderPicker({
  readers,
  readerTeams,
  onReaders,
  onTeams,
}: {
  readers: string[]
  readerTeams: string[]
  onReaders: (next: string[]) => void
  onTeams: (next: string[]) => void
}) {
  const { people, teams, me, person, names, loaded } = useReaderNames()
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const known = new Set(people.map((p) => p.principal_id))
  const rows = [
    ...readers.filter((id) => !known.has(id)).map((id) => ({ id, label: id, sub: 'not in the people list' })),
    ...people.map((p) => ({
      id: p.principal_id,
      label: person(p.principal_id),
      sub: p.display_name && p.display_name !== p.username ? p.username : '',
    })),
  ].filter((row) => !q || `${row.label} ${row.sub}`.toLowerCase().includes(q))
  // Without users.read, teams already named can't be changed here: they show by name or count.
  const fixedTeams = teams === null && readerTeams.length ? names([], readerTeams) : []
  return (
    <div className="reader-pick">
      <div className="reader-pick-group">
        <span className="reader-pick-head">People</span>
        {people.length > 8 && (
          <input className={inputCls} placeholder="Find a person" aria-label="Find a person" value={query} onChange={(e) => setQuery(e.target.value)} />
        )}
        <div className="reader-pick-list" role="group" aria-label="People who can read this case">
          {!loaded && <span className="reader-pick-note">Loading people…</span>}
          {loaded && rows.length === 0 && <span className="reader-pick-note">No one matches.</span>}
          {rows.map((row) => (
            <label key={row.id} className="reader-pick-row">
              <input type="checkbox" checked={readers.includes(row.id)} onChange={() => onReaders(toggle(readers, row.id))} />
              <span className="reader-pick-name">{row.label}</span>
              {row.sub && <span className="reader-pick-sub">{row.sub}</span>}
            </label>
          ))}
        </div>
      </div>
      {teams !== null && (
        <div className="reader-pick-group">
          <span className="reader-pick-head">Teams</span>
          <div className="reader-pick-list" role="group" aria-label="Teams whose members can read this case">
            {teams.length === 0 && <span className="reader-pick-note">No teams.</span>}
            {teams.map((team) => (
              <label key={team.id} className="reader-pick-row">
                <input type="checkbox" checked={readerTeams.includes(team.id)} onChange={() => onTeams(toggle(readerTeams, team.id))} />
                <span className="reader-pick-name">{team.name}</span>
                {me?.teams.some((t) => t.id === team.id) && <span className="reader-pick-sub">your team</span>}
              </label>
            ))}
          </div>
        </div>
      )}
      {fixedTeams.length > 0 && (
        <p className="reader-pick-note">Also readable by members of {fixedTeams.join(', ')}. Naming teams needs users.read.</p>
      )}
    </div>
  )
}

/**
 * Restrict an open case, change a restricted one's readers, or lift it. Opening pre-ticks the
 * owner, the task assignees and you; the API's refusals show as returned.
 */
export function RestrictCaseDialog({ c, onClose, onChanged }: { c: CaseRecord; onClose: () => void; onChanged: () => void }) {
  const { people, loaded, me } = useReaderNames()
  // The lists as they stood when the dialog opened: the change sent is the ticks against these,
  // so a refresh of the case underneath never removes a name nobody unticked.
  const [base] = useState(() => ({ restricted: c.restricted, readers: c.readers, readerTeams: c.readerTeams }))
  const [readers, setReaders] = useState<string[] | null>(base.restricted ? base.readers : null)
  const [readerTeams, setReaderTeams] = useState<string[]>(base.restricted ? base.readerTeams : [])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // An open case's defaults, resolved through the directory once it and the tasks are read.
  useEffect(() => {
    if (base.restricted || readers !== null || !loaded) return
    let live = true
    const pick = (assignees: (string | null | undefined)[]) => {
      const names = [c.stored.assignee, ...assignees, me?.username]
      const ids = names.flatMap((name) => people.find((p) => p.username === name)?.principal_id ?? [])
      if (live) setReaders([...new Set(ids)])
    }
    casesApi.getTasks(c.id).then((r) => pick(r.data.tasks.map((task) => task.assignee)), () => pick([]))
    return () => {
      live = false
    }
  }, [c, base, readers, loaded, people, me])

  const run = async (act: () => Promise<unknown>) => {
    setBusy(true)
    setErr('')
    try {
      await act()
      onChanged()
      onClose()
    } catch (e) {
      setErr(errorDetail(e, 'Couldn’t change who can read this case'))
      setBusy(false)
    }
  }

  const save = () => {
    if (!readers || busy) return
    if (!base.restricted) return run(() => casesApi.restrict(c.id, { readers, reader_teams: readerTeams }))
    const change = {
      add: readers.filter((id) => !base.readers.includes(id)),
      remove: base.readers.filter((id) => !readers.includes(id)),
      add_teams: readerTeams.filter((id) => !base.readerTeams.includes(id)),
      remove_teams: base.readerTeams.filter((id) => !readerTeams.includes(id)),
    }
    if (!Object.values(change).some((list) => list.length)) return onClose()
    return run(() => casesApi.changeReaders(c.id, change))
  }

  return (
    <Popup open onClose={onClose} title={base.restricted ? 'Who can read this case' : 'Restrict case'} width={560}>
      <div className="flex flex-col gap-3.5">
        <RestrictHelp />
        {readers === null ? (
          <p className="reader-pick-note">Loading people…</p>
        ) : (
          <ReaderPicker readers={readers} readerTeams={readerTeams} onReaders={setReaders} onTeams={setReaderTeams} />
        )}
        {err && <div role="alert" className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex flex-wrap justify-end gap-2.5">
          {base.restricted && (
            <button type="button" className="btn" style={{ marginRight: 'auto' }} onClick={() => void run(() => casesApi.liftRestriction(c.id))} disabled={busy}>
              Lift restriction
            </button>
          )}
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn primary" onClick={() => void save()} disabled={busy || readers === null}>
            {busy ? 'Saving…' : base.restricted ? 'Save readers' : 'Restrict case'}
          </button>
        </div>
      </div>
    </Popup>
  )
}
