import { useState } from 'react'
import { Popup, TextInput } from '../../shared/ui'
import { skillsApi, type ApiSkillRepoPreview, type ApiSkillUpload } from '../../services/skillsApi'

const errText = (e: unknown, fallback: string) => {
  const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = err?.response?.data?.detail
  return typeof detail === 'string' ? detail : err?.message || fallback
}

// The Skills tab's one way in for skills made elsewhere: a SKILL.md or zip from
// disk, or a git repo the server reads and lists for the operator to pick from.
export function SkillImportDialog({
  onClose,
  onChooseFile,
  onInstalled,
}: {
  onClose: () => void
  onChooseFile: () => void
  onInstalled: (result: ApiSkillUpload, label: string) => void
}) {
  const [source, setSource] = useState('')
  const [read, setRead] = useState<{ source: string; preview: ApiSkillRepoPreview } | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'read' | 'install' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const readRepo = () => {
    const text = source.trim()
    if (!text) return
    setBusy('read')
    setError(null)
    setRead(null)
    skillsApi
      .previewRepo(text)
      .then((preview) => {
        setRead({ source: text, preview })
        setPicked(new Set(preview.skills.filter((s) => !s.problem).map((s) => s.name)))
      })
      .catch((e) => setError(errText(e, 'Could not read the repo')))
      .finally(() => setBusy(null))
  }

  const install = () => {
    if (!read || picked.size === 0) return
    setBusy('install')
    setError(null)
    skillsApi
      .installRepo(read.source, read.preview.commit, [...picked])
      .then((result) => onInstalled(result, read.preview.label))
      .catch((e) => {
        setError(errText(e, 'Could not install the skills'))
        setBusy(null)
      })
  }

  const toggle = (name: string) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const where = read
    ? [read.preview.label, read.preview.ref, read.preview.path].filter(Boolean).join(' · ')
    : ''

  return (
    <Popup open onClose={onClose} title="Import skills" width={560}>
      <div className="flex flex-col gap-4">
        <div className="field">
          <span className="field-label">From a file</span>
          <div className="flex items-center gap-3">
            <button type="button" className="btn ghost" onClick={onChooseFile}>Choose SKILL.md or zip</button>
            <span className="field-hint">A zip can hold several skills.</span>
          </div>
        </div>
        <form
          className="field"
          onSubmit={(e) => {
            e.preventDefault()
            readRepo()
          }}
        >
          <span className="field-label">From a git repo</span>
          <div className="flex items-center gap-2">
            <TextInput
              className="grow"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="owner/repo, or an https link"
              aria-label="Repo"
              autoComplete="off"
              spellCheck={false}
            />
            <button type="submit" className="btn ghost shrink-0" disabled={busy !== null || !source.trim()}>
              {busy === 'read' ? 'Reading…' : 'Read repo'}
            </button>
          </div>
          <span className="field-hint">Add @name for a branch or tag. Only hosts your admin allows are read.</span>
        </form>
        {read && (
          <fieldset className="sk-pick">
            <legend>
              {where} · commit {read.preview.commit.slice(0, 7)} · {read.preview.skills.length}{' '}
              {read.preview.skills.length === 1 ? 'skill' : 'skills'} found
            </legend>
            <div className="sk-pick-list">
              {read.preview.skills.map((s) => (
                <label key={s.folder || s.name} className={s.problem ? 'is-off' : undefined}>
                  <input type="checkbox" checked={!s.problem && picked.has(s.name)} disabled={!!s.problem || busy !== null} onChange={() => toggle(s.name)} />
                  <span>
                    <b>{s.name}</b>
                    <span>{s.problem ? `Can't install: ${s.problem}` : s.description}</span>
                  </span>
                </label>
              ))}
            </div>
            <span className="field-hint">Each skill is scanned and held from agents until it clears, as an upload is.</span>
          </fieldset>
        )}
        {error && <div role="alert" className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          {read && (
            <button type="button" className="btn primary" disabled={busy !== null || picked.size === 0} onClick={install}>
              {busy === 'install' ? 'Installing…' : `Install ${picked.size} ${picked.size === 1 ? 'skill' : 'skills'}`}
            </button>
          )}
        </div>
      </div>
    </Popup>
  )
}
