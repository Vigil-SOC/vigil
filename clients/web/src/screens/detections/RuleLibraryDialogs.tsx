import { useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Popup, Select, TextInput } from '../../shared/ui'
import { errorText } from '../../shared/errorText'
import {
  detectionRulesApi,
  type RuleLibraryChange,
  type RuleLibraryFile,
  type RuleLibraryRead,
} from '../../services/api'
import { FORMAT_OPTIONS } from './ruleFormats'
import './ruleLibrary.css'


const rulesWord = (n: number) => `${n.toLocaleString()} ${n === 1 ? 'rule' : 'rules'}`
// A file can hold several rules (multi-document Sigma, correlations): count its keys.
const ruleCount = (files: RuleLibraryFile[]) => files.reduce((n, f) => n + f.keys.length, 0)
// The server's 409 when the repo moved between Read repo and the import.
const isMoved = (msg: string | null) => !!msg && msg.includes('since it was read')
const where = (r: RuleLibraryRead) => [r.repo, r.ref, r.path].filter(Boolean).join(' · ')
const TECHNIQUES_SHOWN = 4

// Language and techniques, or why the file can't be picked.
function ruleMeta(r: Pick<RuleLibraryFile, 'language' | 'techniques' | 'problem'>, verb: string) {
  if (r.problem) return `Can't ${verb}: ${r.problem}`
  const t = r.techniques ?? []
  const shown = t.slice(0, TECHNIQUES_SHOWN).join(', ') + (t.length > TECHNIQUES_SHOWN ? ` +${t.length - TECHNIQUES_SHOWN}` : '')
  return [r.language, shown || 'no techniques'].filter(Boolean).join(' · ')
}

const toggled = (prev: Set<string>, paths: string[], on: boolean) => {
  const next = new Set(prev)
  for (const p of paths) {
    if (on) next.add(p)
    else next.delete(p)
  }
  return next
}

function FolderRow({
  folder,
  rules,
  picked,
  disabled,
  onPick,
}: {
  folder: string
  rules: RuleLibraryFile[]
  picked: Set<string>
  disabled: boolean
  onPick: (paths: string[], on: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const pickable = rules.filter((r) => !r.problem).map((r) => r.path)
  const n = pickable.filter((p) => picked.has(p)).length
  const blocked = rules.length - pickable.length
  const name = folder || 'Top folder'
  return (
    <li className="rl-folder">
      <div className="rl-folder-head">
        <input
          type="checkbox"
          aria-label={`Pick every rule in ${name}`}
          checked={pickable.length > 0 && n === pickable.length}
          ref={(el) => { if (el) el.indeterminate = n > 0 && n < pickable.length }}
          disabled={disabled || pickable.length === 0}
          onChange={(e) => onPick(pickable, e.target.checked)}
        />
        <button type="button" className="rl-folder-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Icon name={open ? 'chevD' : 'chevR'} size={14} />
          {/* Long paths wrap at a slash */}
          <span className="rl-folder-name">{name.split('/').map((part, i) => <span key={i}>{i > 0 && <>/<wbr /></>}{part}</span>)}</span>
        </button>
        <span className="rl-count">
          {n} of {pickable.length}
          {blocked > 0 && ` · ${blocked} can't`}
        </span>
      </div>
      {open && (
        <ul className="rl-rules">
          {rules.map((r) => (
            <li key={r.path}>
              <label className={r.problem ? 'is-off' : undefined}>
                <input type="checkbox" checked={!r.problem && picked.has(r.path)} disabled={disabled || !!r.problem} onChange={(e) => onPick([r.path], e.target.checked)} />
                <span>
                  <b>{r.name}</b>
                  <span className="rl-path">{r.path.split('/').pop()}</span>
                  <span className={r.problem ? 'rl-why' : 'rl-meta'}>{ruleMeta(r, 'import')}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// Detections' Import rules: read a repo, pick its rules by folder, import them as a rule library.
export function RuleImportDialog({ onClose, onImported }: { onClose: () => void; onImported: (name: string, n: number) => void }) {
  const [repo, setRepo] = useState('')
  const [format, setFormat] = useState('auto')
  const [read, setRead] = useState<{ repo: string; format: string; got: RuleLibraryRead } | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'read' | 'import' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const readRepo = (text = repo.trim(), fmt = format) => {
    if (!text) return
    setBusy('read')
    setError(null)
    setRead(null)
    detectionRulesApi
      .readLibrary({ repo: text, format: fmt })
      .then(({ data }) => {
        setRead({ repo: text, format: fmt, got: data })
        setPicked(new Set(data.folders.flatMap((f) => f.rules.filter((r) => !r.problem).map((r) => r.path))))
      })
      .catch((e) => setError(errorText(e, 'Could not read the repo')))
      .finally(() => setBusy(null))
  }

  const pickedFiles = read ? read.got.folders.flatMap((f) => f.rules.filter((r) => !r.problem && picked.has(r.path))) : []
  const picks = pickedFiles.map((r) => r.path)
  const pickedRules = ruleCount(pickedFiles)

  const importRules = () => {
    if (!read) return
    setBusy('import')
    setError(null)
    detectionRulesApi
      .importLibrary({ repo: read.repo, format: read.format, commit: read.got.commit, picks })
      .then(({ data }) => onImported(data?.source?.name || read.got.repo, pickedRules))
      .catch((e) => {
        setError(errorText(e, 'Could not import the rules'))
        setBusy(null)
      })
  }

  const all = read ? read.got.folders.flatMap((f) => f.rules.filter((r) => !r.problem).map((r) => r.path)) : []

  return (
    <Popup open onClose={onClose} title="Import rules" width={640}>
      <div className="flex flex-col gap-4">
        <form
          className="field"
          onSubmit={(e) => {
            e.preventDefault()
            readRepo()
          }}
        >
          <span className="field-label">From a git repo</span>
          <div className="rl-read">
            <TextInput
              className="grow"
              value={repo}
              onChange={(e) => { setRepo(e.target.value); setRead(null) }}
              placeholder="owner/repo, or an https link"
              aria-label="Repo"
              autoComplete="off"
              spellCheck={false}
            />
            <div className="rl-format">
              <Select value={format} options={FORMAT_OPTIONS} label="Rule format" onSelect={(v) => { setFormat(v); setRead(null) }} disabled={busy !== null} />
            </div>
            <button type="submit" className="btn ghost shrink-0" disabled={busy !== null || !repo.trim()}>
              {busy === 'read' ? 'Reading…' : 'Read repo'}
            </button>
          </div>
          <span className="field-hint">Add @name for a branch or tag, and /path for one folder. Only hosts your admin allows are read.</span>
        </form>
        {read && (
          <fieldset className="sk-pick rl-pick">
            <legend>
              {where(read.got)} · commit {read.got.commit.slice(0, 7)} · {rulesWord(ruleCount(read.got.folders.flatMap((f) => f.rules)))} in {read.got.folders.length}{' '}
              {read.got.folders.length === 1 ? 'folder' : 'folders'}
            </legend>
            {read.got.folders.length > 0 && (
              <div className="rl-bulk">
                <button type="button" className="data-link" disabled={busy !== null} onClick={() => setPicked(new Set(all))}>Pick all</button>
                <button type="button" className="data-link" disabled={busy !== null} onClick={() => setPicked(new Set())}>Pick none</button>
              </div>
            )}
            <ul className="rl-folders">
              {read.got.folders.map((f) => (
                <FolderRow
                  key={f.folder}
                  folder={f.folder}
                  rules={f.rules}
                  picked={picked}
                  disabled={busy !== null}
                  onPick={(paths, on) => setPicked((prev) => toggled(prev, paths, on))}
                />
              ))}
            </ul>
            <span className="field-hint">
              {picks.length === 0
                ? 'Nothing picked: this adds an empty library, which can take rules later or be a deploy target.'
                : 'The picked rules land as a library; Update library in Settings takes later changes.'}
            </span>
          </fieldset>
        )}
        {error && (
          <div role="alert" className="rl-error">
            {error}
            {isMoved(error) && read && (
              <button type="button" className="data-link" disabled={busy !== null} onClick={() => readRepo(read.repo, read.format)}>Read again</button>
            )}
          </div>
        )}
        <div className="flex flex-wrap justify-end gap-2.5 pt-1">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          {read && (
            <button type="button" className="btn primary" disabled={busy !== null} onClick={importRules}>
              {busy === 'import' ? 'Importing…' : `Import ${rulesWord(pickedRules)}`}
            </button>
          )}
        </div>
      </div>
    </Popup>
  )
}

// Settings' Update library: what changed in the repo since the library was taken, each a tick.
export function RuleLibraryDialog({ id, name, onClose, onUpdated }: { id: string; name: string; onClose: () => void; onUpdated: (n: number) => void }) {
  const [got, setGot] = useState<RuleLibraryRead | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'read' | 'update' | null>('read')
  const [error, setError] = useState<string | null>(null)
  const [readKey, setReadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    setBusy('read')
    setError(null)
    setGot(null)
    detectionRulesApi
      .readLibrary({ source_id: id })
      .then(({ data }) => {
        if (cancelled) return
        setGot(data)
        // What changed and what is gone start ticked; what was never imported starts unticked.
        const lib = data.library
        setPicked(new Set([...(lib?.changed ?? []), ...(lib?.gone ?? [])].filter((c) => !c.problem).map((c) => c.path)))
      })
      .catch((e) => { if (!cancelled) setError(errorText(e, 'Could not read the library')) })
      .finally(() => { if (!cancelled) setBusy(null) })
    return () => { cancelled = true }
  }, [id, readKey])

  const lib = got?.library
  // adopted_version is on the repo's listing, not the diff entries: join by path.
  const files = new Map((got?.folders ?? []).flatMap((f) => f.rules.map((r) => [r.path, r] as const)))
  // A repo file that holds no rule (a README, a CI workflow) is not in the listing: counted, not listed.
  const fresh = (lib?.new ?? []).filter((c) => files.has(c.path))
  const noRule = (lib?.new.length ?? 0) - fresh.length
  const offered = lib ? [...lib.changed, ...lib.gone, ...fresh].filter((c) => !c.problem).map((c) => c.path) : []
  const picks = offered.filter((p) => picked.has(p))
  const empty = !!lib && lib.changed.length + lib.gone.length + fresh.length === 0

  const update = () => {
    if (!got || picks.length === 0) return
    setBusy('update')
    setError(null)
    detectionRulesApi
      .importLibrary({ source_id: id, commit: got.commit, picks })
      .then(() => onUpdated(picks.length))
      .catch((e) => {
        setError(errorText(e, 'Could not update the library'))
        setBusy(null)
      })
  }

  const row = (c: RuleLibraryChange, gone: boolean) => {
    const file = files.get(c.path)
    const text = gone
      ? 'Remove it here too'
      : c.problem
        ? `Can't take it: ${c.problem}`
        : [file && ruleMeta(file, 'take'), file?.adopted_version != null && `adopted at version ${file.adopted_version}`].filter(Boolean).join(' · ')
    return (
      <label key={c.path} className={c.problem ? 'is-off' : undefined}>
        <input type="checkbox" checked={!c.problem && picked.has(c.path)} disabled={!!c.problem || busy !== null} onChange={(e) => setPicked((prev) => toggled(prev, [c.path], e.target.checked))} />
        <span>
          <b>{c.name}</b>
          <span className="rl-path">{c.path}</span>
          {text && <span className={c.problem ? 'rl-why' : 'rl-meta'}>{text}</span>}
        </span>
      </label>
    )
  }

  const group = (title: string, items: RuleLibraryChange[], gone = false) =>
    items.length > 0 && (
      <fieldset className="sk-pick">
        <legend>{title} · {items.length.toLocaleString()}</legend>
        {items.map((c) => row(c, gone))}
      </fieldset>
    )

  const from = lib?.commit?.slice(0, 7)
  const to = got?.commit.slice(0, 7)

  return (
    <Popup open onClose={onClose} title="Update library" width={600}>
      <div className="flex flex-col gap-4">
        {busy === 'read' && <p className="text-[13px] text-tx-3">Reading {name}…</p>}
        {got && lib && (
          <p className="text-[13px] text-tx-2 leading-[1.5]">
            <strong>{lib.name || name}</strong>
            {(lib.name || name) !== where(got) && ` · ${where(got)}`}
            {empty
              ? ` at ${to} matches what is imported. Nothing to update.`
              : !from || from === to
                ? ` is at ${to}. Tick what to take.`
                : ` moved from ${from} to ${to}. Tick what to take.`}
          </p>
        )}
        {lib && !empty && (
          <div className="sk-pick-list">
            {group('Changed', lib.changed)}
            {group('Gone from the repo', lib.gone, true)}
            {/* Every rule file in the repo not imported here, not only ones added since. */}
            {group('In the repo, not imported', fresh)}
          </div>
        )}
        {lib && !empty && noRule > 0 && (
          <span className="field-hint">{noRule.toLocaleString()} other {noRule === 1 ? 'file in the repo holds' : 'files in the repo hold'} no rule.</span>
        )}
        {error && (
          <div role="alert" className="rl-error">
            {error}
            {isMoved(error) && (
              <button type="button" className="data-link" disabled={busy !== null} onClick={() => setReadKey((k) => k + 1)}>Read again</button>
            )}
          </div>
        )}
        <div className="flex justify-end gap-2.5 pt-1">
          <button type="button" className="btn ghost" onClick={onClose}>{empty ? 'Close' : 'Cancel'}</button>
          {lib && !empty && (
            <button type="button" className="btn primary" disabled={busy !== null || picks.length === 0} onClick={update}>
              {busy === 'update' ? 'Updating…' : `Take ${picks.length.toLocaleString()} ${picks.length === 1 ? 'change' : 'changes'}`}
            </button>
          )}
        </div>
      </div>
    </Popup>
  )
}
