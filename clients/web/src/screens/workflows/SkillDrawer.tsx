import { useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Popup } from '../../shared/ui'
import { skillsApi, type ApiSkillDetail } from '../../services/skillsApi'
import type { Skill } from '../../data/appData'

const PATH_UNSET = 'The skills path is unset. Set VIGIL_SKILLS_PATH to a directory before saving.'

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || 'Something went wrong'
}

/** `name` null opens a blank editor that builds a new skill; `existingNames` are the skills already loaded. */
export function SkillDrawer({
  name,
  existingNames,
  onClose,
  onSaved,
}: {
  name: string | null
  existingNames: string[]
  onClose: () => void
  onSaved: () => void
}) {
  const creating = name === null
  const [detail, setDetail] = useState<ApiSkillDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [skillName, setSkillName] = useState(name ?? '')
  const [description, setDescription] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rootUnset, setRootUnset] = useState(false)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    if (name === null) return
    let cancelled = false
    skillsApi
      .get(name)
      .then((loaded) => {
        if (cancelled) return
        setDetail(loaded)
        setSkillName(loaded.name)
        setDescription(loaded.description)
        setBody(loaded.body)
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errMsg(e))
      })
    return () => {
      cancelled = true
    }
  }, [name])

  // Create mode has no skill of its own to read the flag from; any loaded skill carries it.
  useEffect(() => {
    if (name !== null || existingNames.length === 0) return
    let cancelled = false
    skillsApi
      .get(existingNames[0])
      .then((loaded) => { if (!cancelled) setRootUnset(!loaded.operator_root_set) })
      .catch(() => undefined)
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name])

  const pathUnset = rootUnset || (detail !== null && !detail.operator_root_set)
  const nameLocked = detail !== null && !detail.bundled
  const needsNewName = detail?.bundled === true && skillName.trim() === detail.name
  // POST /api/skills overwrites an operator skill of the same name, so a taken name is refused here.
  const nameTaken = !nameLocked && !needsNewName && existingNames.includes(skillName.trim())
  const ready = creating || detail !== null
  const saveDisabled = busy || !ready || pathUnset || needsNewName || nameTaken || !skillName.trim() || !description.trim()

  const save = () => {
    if (saveDisabled) return
    setBusy(true)
    setError(null)
    skillsApi
      .save({
        name: nameLocked && detail ? detail.name : skillName.trim(),
        description: description.trim(),
        body,
      })
      .then(onSaved)
      .catch((e) => {
        setError(errMsg(e))
        setBusy(false)
      })
  }

  return (
    <div className="vg-drawer-scrim" onMouseDown={onClose}>
      <aside
        className="vg-case-drawer"
        role="dialog"
        aria-label={creating ? 'Build a skill' : `Edit ${name}`}
        style={{ width: 480, maxWidth: '100%' }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="vg-drawer-tools">
          <button type="button" className="btn ghost" onClick={onClose}>Close</button>
        </div>
        <div className="flex flex-col gap-3.5 p-4 overflow-auto">
          <h3 className="text-base">{creating ? 'Build a skill' : `Edit · ${name}`}</h3>
          {loadError && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{loadError}</div>}
          {!ready && !loadError && <p className="text-[13px] text-tx-3">Loading skill…</p>}
          {ready && (
            <>
              {pathUnset && <p className="text-[13px] text-tx-2 leading-[1.5]">{PATH_UNSET}</p>}
              {detail?.bundled && (
                <p className="text-[13px] text-tx-2 leading-[1.5]">
                  Saving writes a new skill under the operator root and leaves the bundled directory unchanged.
                </p>
              )}
              <label className="flex flex-col gap-1.5">
                <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Name</span>
                <input
                  className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line font-mono"
                  value={skillName}
                  maxLength={64}
                  disabled={nameLocked}
                  placeholder={creating ? 'lowercase-with-hyphens' : undefined}
                  autoFocus={creating}
                  onChange={(e) => setSkillName(e.target.value)}
                />
                {nameTaken && <span className="text-[11px]" style={{ color: 'var(--crit)' }}>A skill named {skillName.trim()} already exists. Choose another name.</span>}
                {needsNewName && (
                  <span className="text-[11px] text-tx-3">Choose a new name. A bundled skill cannot be overwritten.</span>
                )}
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Description</span>
                <textarea
                  className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line resize-y"
                  rows={3}
                  maxLength={1024}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Body</span>
                <textarea
                  className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line resize-y font-mono"
                  rows={12}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                />
              </label>
              {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
              <div className="flex justify-end gap-2.5 pt-1">
                <button className="btn ghost" onClick={onClose}>Cancel</button>
                <button className="btn primary" disabled={saveDisabled} style={{ opacity: saveDisabled ? 0.5 : 1 }} onClick={save}>
                  {busy ? 'Saving…' : 'Save'}
                </button>
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

export function SkillDeleteModal({
  skill,
  onClose,
  onDeleted,
}: {
  skill: Skill
  onClose: () => void
  onDeleted: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const del = () => {
    setBusy(true)
    setError(null)
    skillsApi
      .delete(skill.name)
      .then(onDeleted)
      .catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  return (
    <Popup open onClose={onClose} title="Delete skill" width={460}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5]">Delete <strong>{skill.name}</strong>? This removes the skill directory from the operator root.</p>
        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={busy} style={{ opacity: busy ? 0.5 : 1 }} onClick={del}><Icon name="trash" /> {busy ? 'Deleting…' : 'Delete'}</button>
        </div>
      </div>
    </Popup>
  )
}
