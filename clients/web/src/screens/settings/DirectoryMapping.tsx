import { useCallback, useEffect, useState } from 'react'
import {
  directorySyncApi,
  teamsApi,
  type DirectoryMapping as Mapping,
  type MappingEffect,
  type TeamLine,
} from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, Select } from '../../shared/ui'
import type { Role } from './useSettings'
import type { SectionProps } from './types'

type RoleLine = { group: string; role_id: string }

const same = (a: { group_roles: RoleLine[]; team_lines: TeamLine[] }, b: Mapping | null) =>
  !!b &&
  JSON.stringify(a.group_roles) === JSON.stringify(b.group_roles) &&
  JSON.stringify(a.team_lines) === JSON.stringify(b.team_lines)

/** Group → role and group → team, saved apart from the provider settings.
 * One step-up, then the counts, then the apply. */
export default function DirectoryMapping({
  canSync,
  roles,
  settingsVersion,
  notify,
  onApplied,
}: {
  canSync: boolean
  roles: Role[]
  settingsVersion: number
  notify: SectionProps['notify']
  onApplied: () => void
}) {
  const [loaded, setLoaded] = useState<Mapping | null>(null)
  const [roleLines, setRoleLines] = useState<RoleLine[]>([])
  const [teamLines, setTeamLines] = useState<TeamLine[]>([])
  const [teams, setTeams] = useState<{ id: string; name: string; is_default: boolean }[]>([])
  const [failed, setFailed] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [effect, setEffect] = useState<MappingEffect | null>(null)

  const load = useCallback(() => {
    directorySyncApi
      .mapping()
      .then((res) => {
        setLoaded(res.data)
        setRoleLines(res.data.group_roles.map((g) => ({ ...g })))
        setTeamLines(res.data.team_lines.map((t) => ({ ...t })))
        setFailed(null)
      })
      .catch((e) => setFailed(errorText(e, 'Could not load the group mapping.')))
    teamsApi
      .list()
      .then((res) =>
        setTeams(res.data.filter((t) => !t.archived).map((t) => ({ id: t.id, name: t.name, is_default: t.is_default }))),
      )
      .catch(() => setTeams([]))
  }, [])
  useEffect(load, [load, settingsVersion])

  if (failed) {
    return (
      <div className="settings-banner err" role="alert">
        <Icon name="alert" size={14} />
        <span>{failed}</span>
        <button className="btn ghost" onClick={load}>Retry</button>
      </div>
    )
  }
  if (!loaded) return <div className="text-sm text-tx-3">Loading groups…</div>

  const groups = loaded.groups
  const dirty = !same({ group_roles: roleLines, team_lines: teamLines }, loaded)
  const ready =
    roleLines.every((g) => g.group && g.role_id) && teamLines.every((t) => t.group && t.team_id)
  const roleOptions = (current: string) => {
    const opts = roles.map((r) => ({ value: r.role_id, label: r.name }))
    return current && !opts.some((o) => o.value === current) ? [...opts, { value: current, label: current }] : opts
  }
  const groupOptions = (current: string) => {
    const counts = new Map<string, number>()
    for (const g of groups) counts.set(g.display_name, (counts.get(g.display_name) ?? 0) + 1)
    const opts = groups.map((g) => ({
      value: g.external_id,
      label: counts.get(g.display_name)! > 1 ? `${g.display_name} (${g.external_id})` : g.display_name,
    }))
    return current && !opts.some((o) => o.value === current)
      ? [...opts, { value: current, label: `${current} (no group)` }]
      : opts
  }
  const teamOptions = (current: string) => {
    const opts = teams
      .filter((t) => !t.is_default || t.id === current)
      .map((t) => ({ value: t.id, label: t.is_default ? `${t.name} (everyone joins it)` : t.name }))
    return current && !opts.some((o) => o.value === current) ? [...opts, { value: current, label: current }] : opts
  }
  const body = {
    group_roles: roleLines.map((g) => ({ group: g.group.trim(), role_id: g.role_id })),
    team_lines: teamLines.map((t) => ({ group: t.group.trim(), team_id: t.team_id })),
  }
  const firstGroup = groups[0]?.external_id ?? ''
  const firstRole = roles[0]?.role_id ?? loaded.group_roles[0]?.role_id ?? 'role-viewer'
  const firstTeam = teams.find((t) => !t.is_default)?.id ?? ''

  const preview = async () => {
    setBusy(true)
    setProblem(null)
    try {
      const res = await directorySyncApi.saveMapping(body, loaded.version, true)
      setEffect(res.data)
    } catch (e) {
      setProblem(errorText(e, 'The mapping was not saved.'))
    } finally {
      setBusy(false)
    }
  }
  const apply = async () => {
    setBusy(true)
    setProblem(null)
    try {
      await directorySyncApi.saveMapping(body, loaded.version, false)
      setEffect(null)
      notify('ok', 'Group mapping saved.')
      onApplied()
    } catch (e) {
      setEffect(null)
      setProblem(errorText(e, 'The mapping was not saved.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-3.5 min-w-0">
      {problem && (
        <div className="settings-banner err" role="alert">
          <Icon name="alert" size={14} />
          <span>{problem}</span>
        </div>
      )}
      {!canSync && (
        <p className="text-xs text-tx-3">Saving a mapping needs roles.manage, users.write and teams.manage.</p>
      )}
      {groups.length === 0 && <p className="text-xs text-tx-3">No groups have been pushed yet.</p>}
      <div className="sso-sub">
        <h4>Roles</h4>
        <p>The first match wins. A managed account whose groups match nothing takes the default role.</p>
      </div>
      {roleLines.length > 0 && (
        <ol className="sso-map">
          {roleLines.map((g, i) => (
            <li key={i} className="sso-map-sync">
              <span className="sso-map-order">
                <button type="button" className="btn ghost icon" aria-label="Move up" disabled={busy || !canSync || i === 0} onClick={() => setRoleLines((rows) => { const next = [...rows]; [next[i], next[i - 1]] = [next[i - 1], next[i]]; return next })}>
                  <Icon name="arrowUp" size={13} />
                </button>
                <button type="button" className="btn ghost icon" aria-label="Move down" disabled={busy || !canSync || i === roleLines.length - 1} onClick={() => setRoleLines((rows) => { const next = [...rows]; [next[i], next[i + 1]] = [next[i + 1], next[i]]; return next })}>
                  <Icon name="arrowDn" size={13} />
                </button>
              </span>
              <span className="sso-map-n">{i + 1}</span>
              <Select label={`Group ${i + 1}`} value={g.group} options={groupOptions(g.group)} disabled={busy || !canSync} onSelect={(v) => setRoleLines((rows) => rows.map((row, j) => (j === i ? { ...row, group: v } : row)))} />
              <Icon name="arrowR" size={14} />
              <Select label={`Role for group ${i + 1}`} value={g.role_id} options={roleOptions(g.role_id)} disabled={busy || !canSync} onSelect={(v) => setRoleLines((rows) => rows.map((row, j) => (j === i ? { ...row, role_id: v } : row)))} />
              <button type="button" className="btn ghost icon" aria-label={`Remove role line ${i + 1}`} disabled={busy || !canSync} onClick={() => setRoleLines((rows) => rows.filter((_, j) => j !== i))}>
                <Icon name="close" size={13} />
              </button>
            </li>
          ))}
        </ol>
      )}
      <span>
        <button type="button" className="btn ghost" disabled={busy || !canSync || !firstGroup} onClick={() => setRoleLines((rows) => [...rows, { group: firstGroup, role_id: firstRole }])}>
          <Icon name="plus" size={14} /> Add role
        </button>
      </span>
      <div className="sso-sub">
        <h4>Teams</h4>
        <p>A person in any of these groups joins that team. The default team is not listed: everyone joins it.</p>
      </div>
      {teamLines.length > 0 && (
        <ul className="sso-map">
          {teamLines.map((t, i) => (
            <li key={i} className="sso-map-sync">
              <Select label={`Group for team ${i + 1}`} value={t.group} options={groupOptions(t.group)} disabled={busy || !canSync} onSelect={(v) => setTeamLines((rows) => rows.map((row, j) => (j === i ? { ...row, group: v } : row)))} />
              <Icon name="arrowR" size={14} />
              <Select label={`Team ${i + 1}`} value={t.team_id} options={teamOptions(t.team_id)} disabled={busy || !canSync} onSelect={(v) => setTeamLines((rows) => rows.map((row, j) => (j === i ? { ...row, team_id: v } : row)))} />
              <button type="button" className="btn ghost icon" aria-label={`Remove team line ${i + 1}`} disabled={busy || !canSync} onClick={() => setTeamLines((rows) => rows.filter((_, j) => j !== i))}>
                <Icon name="close" size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <span className="sso-line">
        <button type="button" className="btn ghost" disabled={busy || !canSync || !firstGroup || !firstTeam} onClick={() => setTeamLines((rows) => [...rows, { group: firstGroup, team_id: firstTeam }])}>
          <Icon name="plus" size={14} /> Add team
        </button>
        <span className="grow" />
        <button type="button" className="btn primary" disabled={busy || !canSync || !dirty || !ready} onClick={() => void preview()}>
          {busy ? 'Checking…' : 'Save mapping'}
        </button>
      </span>
      <ConfirmDialog
        open={!!effect}
        title="Apply this mapping?"
        danger={false}
        busy={busy}
        confirmLabel="Apply"
        onClose={() => !busy && setEffect(null)}
        onConfirm={() => void apply()}
        width={480}
        body={
          effect && (
            <ul className="m-0 pl-4">
              <li>{effect.roles_rise} roles will rise</li>
              <li>{effect.roles_fall} roles will fall</li>
              <li>{effect.roles_default} will move to the default role</li>
              <li>{effect.memberships_added} memberships will be added</li>
              <li>{effect.memberships_removed} memberships will be removed</li>
            </ul>
          )
        }
      />
    </div>
  )
}
