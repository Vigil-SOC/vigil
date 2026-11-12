import { useEffect, useState, type ReactNode } from 'react'
import { peopleApi } from '../services/api'
import type { Schema } from '../services/apiTypes'
import { Select } from './ui'

export type Person = Schema<'Person'>
export type OwnerState = 'current' | 'not_current' | null | undefined

/** The open-work refusal (#4263): the save left work open with a stale owner. */
export const OWNER_NOT_CURRENT = 'OWNER_NOT_CURRENT'
export const isOwnerNotCurrent = (e: unknown) =>
  (e as { response?: { data?: { code?: unknown } } })?.response?.data?.code === OWNER_NOT_CURRENT

/** The owner is not a current member of the case's team. The move dialog is the way through. */
export const OWNER_OFF_TEAM = 'OWNER_OFF_TEAM'
export const isOwnerOffTeam = (e: unknown) =>
  (e as { response?: { data?: { code?: unknown } } })?.response?.data?.code === OWNER_OFF_TEAM

export const NOT_CURRENT = 'not a current account'

/** A stored owner as stored, labelled when it isn't a current account. */
export function OwnerName({ name, state }: { name: string; state?: OwnerState }) {
  return (
    <span className="owner-name">
      {name}
      {state === 'not_current' && <span className="owner-stale"> · {NOT_CURRENT}</span>}
    </span>
  )
}

// One read shared by every picker open within a minute; a failure is retried by the next picker.
let cached: Promise<Person[]> | null = null
let cachedAt = 0
function loadPeople(): Promise<Person[]> {
  if (cached && Date.now() - cachedAt < 60_000) return cached
  cachedAt = Date.now()
  cached = peopleApi.list().then(
    (r) => r.data,
    (e) => {
      cached = null
      throw e
    },
  )
  return cached
}

/** The list, read only when ``enabled``: a read-only picker never asks for it. */
export function usePeople(enabled = true): { people: Person[]; failed: boolean; loaded: boolean } {
  const [people, setPeople] = useState<Person[]>([])
  const [failed, setFailed] = useState(false)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (!enabled) return
    let live = true
    loadPeople().then(
      (list) => {
        if (!live) return
        setPeople(list)
        setLoaded(true)
      },
      () => {
        if (!live) return
        setFailed(true)
        setLoaded(true)
      },
    )
    return () => {
      live = false
    }
  }, [enabled])
  return { people, failed, loaded }
}

export function onTeam(person: Person, teamId: string): boolean {
  return person.teams.some((team) => team.id === teamId)
}

const personLabel = (p: Person) => (p.display_name ? `${p.display_name} (${p.username})` : p.username)

/**
 * A person or Unassigned (''). A stored owner that isn't in the list shows as stored,
 * labelled when not current, and is never offered. If the list can't load, a typed
 * username or email goes to the server, which checks it.
 *
 * ``teamId`` offers that team's members. ``onOffTeam`` is how someone on another
 * team is chosen: they stay out of the field, and the caller opens the move.
 */
export function PersonPicker({
  value,
  state,
  onSelect,
  disabled,
  label,
  commitTyped,
  teamId,
  onOffTeam,
}: {
  value: string
  /** The stored owner's state, from the read. */
  state?: OwnerState
  onSelect: (value: string) => void
  disabled?: boolean
  /** For a picker with no visible label. */
  label?: string
  /** A picker that saves on select: a typed fallback value is sent on Enter or blur, not per key. */
  commitTyped?: boolean
  /** When set, the choices are this team's members. */
  teamId?: string
  /** Someone not on ``teamId``. Absent: those people are not offered. */
  onOffTeam?: (person: Person) => void
}) {
  const { people, failed } = usePeople(!disabled)
  const [typed, setTyped] = useState<string | null>(null)
  if (failed && !disabled) {
    const commit = () => {
      if (typed !== null && typed.trim() !== value) onSelect(typed.trim())
      setTyped(null)
    }
    return (
      <span className="person-pick-fallback">
        <input
          className="person-pick-input"
          aria-label={label}
          placeholder="Username or email, blank for Unassigned"
          value={commitTyped ? (typed ?? value) : value}
          onChange={(e) => (commitTyped ? setTyped(e.target.value) : onSelect(e.target.value))}
          onBlur={commitTyped ? commit : undefined}
          onKeyDown={commitTyped ? (e) => e.key === 'Enter' && commit() : undefined}
        />
        <span className="person-pick-note">
          Couldn’t load the people list.{state === 'not_current' && ` ${value} is ${NOT_CURRENT}.`}
        </span>
      </span>
    )
  }
  const listed = teamId ? people.filter((p) => onTeam(p, teamId) || (onOffTeam && p.teams.length > 0 && !onTeam(p, teamId))) : people
  const options = [
    { value: '', label: 'Unassigned' },
    ...listed.map((p) => ({
      value: p.username,
      label: teamId && !onTeam(p, teamId) ? `${personLabel(p)} · ${p.teams.map((t) => t.name).join(', ')}` : personLabel(p),
    })),
  ]
  const stored = value && !options.some((o) => o.value === value)
  const pick = (next: string) => {
    const person = teamId && onOffTeam ? listed.find((p) => p.username === next && !onTeam(p, teamId)) : undefined
    if (person && onOffTeam) onOffTeam(person)
    else onSelect(next)
  }
  return (
    <span className="person-pick">
      <Select
        label={label}
        value={value}
        options={options}
        onSelect={pick}
        searchable
        disabled={disabled}
        // a stored value outside the list shows on the trigger only, so it is never a choice
        placeholder={stored ? (state === 'not_current' ? `${value} · ${NOT_CURRENT}` : value) : 'Unassigned'}
      />
    </span>
  )
}

/**
 * Where a save was refused for a stale owner: the fix in the same step for whoever may
 * change that owner, otherwise who can. ``children`` is the picker and retry, when shown.
 */
export function OwnerFix({
  owner,
  canChange,
  onAssignToMe,
  children,
}: {
  owner: string
  canChange: boolean
  /** A member of the case's team can take it here. */
  onAssignToMe?: () => void
  children?: ReactNode
}) {
  return (
    <div role="alert" className="owner-fix">
      <p>
        {owner} is {NOT_CURRENT}.{' '}
        {canChange ? 'Pick a person or set Unassigned, then try again.' : 'Someone holding cases.assign must reassign it.'}
      </p>
      {onAssignToMe && (
        <button type="button" className="btn" onClick={onAssignToMe}>Assign to me</button>
      )}
      {canChange && children}
    </div>
  )
}
