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
  // The file shown read-only in place of the steps; null is the editable SKILL.md.
  const [openFile, setOpenFile] = useState<string | null>(null)
  const [file, setFile] = useState<{ content?: string; error?: string }>({})

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

  useEffect(() => {
    if (name === null || openFile === null) return
    let cancelled = false
    setFile({})
    skillsApi
      .file(name, openFile)
      .then((r) => { if (!cancelled) setFile({ content: r.content }) })
      .catch((e) => { if (!cancelled) setFile({ error: errMsg(e) }) })
    return () => { cancelled = true }
  }, [name, openFile])

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
        // A built-in is never written to: its folder is copied under the new name.
        // A custom skill sends the version it opened so a stale save is refused.
        ...(detail?.bundled ? { source: detail.name } : detail ? { version: detail.version } : {}),
      })
      .then(onSaved)
      .catch((e) => {
        setError(errMsg(e))
        setBusy(false)
      })
  }

  const origin = creating || !detail?.bundled ? 'Custom' : 'Built in'
  const subtitle = detail ? `${origin} · version ${detail.version}` : origin
  const err = (text: string) => <span className="text-[12px]" style={{ color: 'var(--crit)' }}>{text}</span>

  return (
    <div className="vg-skill-scrim" onMouseDown={onClose}>
      <aside
        className="vg-skill-drawer"
        role="dialog"
        aria-label={creating ? 'Build a skill' : `Edit ${name}`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <span className="flex flex-col gap-[3px] grow min-w-0">
            <span className="text-[20px] font-bold leading-[1.25] tracking-[-0.2px] text-tx break-words">
              {creating ? 'Build a skill' : `Skill · ${name}`}
            </span>
            {ready && <span className="vg-skill-hint">{subtitle}</span>}
          </span>
          <button type="button" className="vg-skill-close" aria-label="Close" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
            </svg>
          </button>
        </div>
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
            <div className="vg-skill-field">
              <label htmlFor="vg-s-name" className="vg-skill-label">Name</label>
              <input
                id="vg-s-name"
                className="vg-skill-input mono"
                value={skillName}
                maxLength={64}
                disabled={nameLocked}
                autoComplete="off"
                placeholder={creating ? 'lowercase-with-hyphens' : undefined}
                autoFocus={creating}
                onChange={(e) => setSkillName(e.target.value)}
              />
              <span className="vg-skill-hint">Lower case and hyphens, 64 characters at most</span>
              {nameTaken && err(`A skill named ${skillName.trim()} already exists. Choose another name.`)}
              {needsNewName && <span className="vg-skill-hint">Choose a new name. A bundled skill cannot be overwritten.</span>}
            </div>
            <div className="vg-skill-field">
              <label htmlFor="vg-s-desc" className="vg-skill-label">When to use it</label>
              <textarea
                id="vg-s-desc"
                className="vg-skill-input"
                rows={3}
                maxLength={1024}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
              <span className="vg-skill-hint">Agents read this to decide whether the skill applies (1,024 characters at most)</span>
            </div>
            {openFile === null ? (
              <div className="vg-skill-field">
                <label htmlFor="vg-s-body" className="vg-skill-label">Steps (SKILL.md)</label>
                <textarea
                  id="vg-s-body"
                  className="vg-skill-input mono"
                  rows={9}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                />
              </div>
            ) : (
              <div className="vg-skill-field">
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="vg-s-file" className="vg-skill-label mono">
                    <span className="break-all">{openFile}</span>{' '}
                    <span className="whitespace-nowrap">· read-only</span>
                  </label>
                  <button type="button" className="vg-skill-btn" onClick={() => setOpenFile(null)}>Back to steps</button>
                </div>
                {file.error && err(file.error)}
                {!file.error && file.content === undefined && <span className="vg-skill-hint">Loading file…</span>}
                {file.content !== undefined && (
                  <textarea id="vg-s-file" className="vg-skill-input mono" rows={9} readOnly value={file.content} />
                )}
              </div>
            )}
            {detail && detail.files.length > 0 && (
              <div className="vg-skill-field">
                <span className="vg-skill-files-title">Files in this skill</span>
                {detail.files.map((f) => (
                  <button
                    key={f.path}
                    type="button"
                    className="vg-skill-file"
                    aria-current={(openFile ?? 'SKILL.md') === f.path}
                    onClick={() => setOpenFile(f.path === 'SKILL.md' ? null : f.path)}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--tx2)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M5 4h10l4 4v12H5zM15 4v4h4M8.5 12h7M8.5 15.5h5" />
                    </svg>
                    <span className="min-w-0 break-all">{f.path}</span>
                  </button>
                ))}
              </div>
            )}
            {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
            <div className="vg-skill-foot">
              <button type="button" className="vg-skill-btn" onClick={onClose}>Cancel</button>
              <button type="button" className="vg-skill-btn primary" disabled={saveDisabled} onClick={save}>
                {busy ? 'Saving…' : creating ? 'Save' : 'Save new version'}
              </button>
            </div>
          </>
        )}
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
