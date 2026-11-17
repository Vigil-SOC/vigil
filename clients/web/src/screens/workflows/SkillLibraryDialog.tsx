import { useEffect, useState } from 'react'
import { Popup } from '../../shared/ui'
import { skillsApi, type ApiSkillLibraryChange, type ApiSkillLibraryPreview } from '../../services/skillsApi'

const errText = (e: unknown, fallback: string) => {
  const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = err?.response?.data?.detail
  return typeof detail === 'string' ? detail : err?.message || fallback
}

const EDITED = 'You edited this here; updating replaces your edits'
const EDITED_GONE = 'Remove it here too. You edited this here; removing it deletes your edits'

// Update the skills installed from one repo: what changed there, what is new,
// what is gone, each a tick. A skill edited here starts unticked.
export function SkillLibraryDialog({
  name,
  label,
  onClose,
  onUpdated,
}: {
  name: string
  label: string
  onClose: () => void
  onUpdated: () => void
}) {
  const [preview, setPreview] = useState<ApiSkillLibraryPreview | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'read' | 'update' | null>('read')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    skillsApi
      .libraryPreview(name)
      .then((got) => {
        if (cancelled) return
        setPreview(got)
        const ticked = [...got.changed, ...got.gone].filter((c) => !c.edited && !c.problem)
        setPicked(new Set(ticked.map((c) => c.name)))
      })
      .catch((e) => { if (!cancelled) setError(errText(e, 'Could not read the repo')) })
      .finally(() => { if (!cancelled) setBusy(null) })
    return () => { cancelled = true }
  }, [name])

  const update = () => {
    if (!preview || picked.size === 0) return
    setBusy('update')
    setError(null)
    // The edited picks the operator was warned about; the server refuses any other.
    const edited = [...preview.changed, ...preview.gone].filter((c) => c.edited && picked.has(c.name)).map((c) => c.name)
    skillsApi
      .libraryUpdate(name, preview.latest, [...picked], edited)
      .then(onUpdated)
      .catch((e) => {
        setError(errText(e, 'Could not update the skills'))
        setBusy(null)
      })
  }

  const toggle = (skill: string) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(skill)) next.delete(skill)
      else next.add(skill)
      return next
    })

  const row = (c: ApiSkillLibraryChange, text: string) => (
    <label key={c.name} className={c.problem ? 'is-off' : undefined}>
      <input type="checkbox" checked={!c.problem && picked.has(c.name)} disabled={!!c.problem || busy !== null} onChange={() => toggle(c.name)} />
      <span>
        <b>{c.name}</b>
        <span>{c.problem ? `Can't take it: ${c.problem}` : text}</span>
      </span>
    </label>
  )

  const group = (title: string, items: ApiSkillLibraryChange[], text: (c: ApiSkillLibraryChange) => string) =>
    items.length > 0 && (
      <fieldset className="sk-pick">
        <legend>{title}</legend>
        {items.map((c) => row(c, text(c)))}
      </fieldset>
    )

  const empty = preview && preview.changed.length + preview.new.length + preview.gone.length === 0
  const from = preview?.commit.slice(0, 7)
  const to = preview?.latest.slice(0, 7)

  return (
    <Popup open onClose={onClose} title="Update library" width={560}>
      <div className="flex flex-col gap-4">
        {busy === 'read' && <p className="text-[13px] text-tx-3">Reading {label}…</p>}
        {preview && (
          <p className="text-[13px] text-tx-2 leading-[1.5]">
            <strong>{preview.label}</strong>
            {empty
              ? ` at ${to} matches what is installed. Nothing to update.`
              : from === to
                ? ` is at ${to}. Tick what to take; each one is scanned again before agents can use it.`
                : ` moved from ${from} to ${to}. Tick what to take; each one is scanned again before agents can use it.`}
          </p>
        )}
        {preview && !empty && (
          <div className="sk-pick-list">
            {group('Changed', preview.changed, (c) => (c.edited ? `${c.note}. ${EDITED}` : c.note))}
            {/* Before the unticked list, which can be long: what an update removes stays in view. */}
            {group('Gone from the repo', preview.gone, (c) => (c.edited ? EDITED_GONE : 'Remove it here too'))}
            {/* Every skill in the repo not installed here, not only ones added since. */}
            {group('In the repo, not installed', preview.new, (c) => c.note)}
          </div>
        )}
        {error && <div role="alert" className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button type="button" className="btn ghost" onClick={onClose}>{empty ? 'Close' : 'Cancel'}</button>
          {preview && !empty && (
            <button type="button" className="btn primary" disabled={busy !== null || picked.size === 0} onClick={update}>
              {busy === 'update' ? 'Updating…' : `Update ${picked.size} ${picked.size === 1 ? 'skill' : 'skills'}`}
            </button>
          )}
        </div>
      </div>
    </Popup>
  )
}
