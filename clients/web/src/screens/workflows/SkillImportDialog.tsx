import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Popup, TextInput } from '../../shared/ui'
import {
  skillsApi,
  type ApiSkillRepoPreview,
  type ApiSkillRepoScan,
  type ApiSkillRepoScanned,
  type ApiSkillUpload,
} from '../../services/skillsApi'

const errText = (e: unknown, fallback: string) => {
  const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = err?.response?.data?.detail
  return typeof detail === 'string' ? detail : err?.message || fallback
}

type Found = ApiSkillRepoPreview['skills'][number]
// A name already installed can't be installed again, but can be scanned.
const scannable = (s: Found) => !s.problem || !!s.installed
const REASONS_SHOWN = 5

const NO_VERDICT: Record<string, string> = {
  failed: 'Scan failed',
  unavailable: 'Scanner unavailable',
  disabled: 'Scanning is off',
  not_scanned: 'Not scanned',
}

function ScanRow({ s }: { s: ApiSkillRepoScanned }) {
  const verdict = s.status !== 'scanned' ? 'none' : s.held ? 'held' : 'safe'
  const mark = verdict === 'none' ? NO_VERDICT[s.status] ?? s.status : verdict === 'held' ? 'Held' : 'Safe'
  const more = s.reasons.length - REASONS_SHOWN
  return (
    <li className="vg-skill-result sk-scan-row" data-verdict={verdict}>
      <span className="vg-skill-result-head">
        <span className="vg-skill-result-mark">{mark}</span>
        <b className="min-w-0 break-words">{s.name}</b>
        {s.recommendation && <span className="sk-scan-rec">{s.recommendation.replace(/_/g, ' ')}{s.max_severity && s.max_severity !== 'NONE' ? ` · ${s.max_severity}` : ''}</span>}
      </span>
      {s.error && <span className="sk-scan-why">{s.error}</span>}
      {s.reasons.length > 0 && (
        <ul className="sk-scan-reasons">
          {s.reasons.slice(0, REASONS_SHOWN).map((r, i) => (
            <li key={`${r.rule_id}-${i}`}>
              <span className="sk-scan-why"><b>{r.severity}</b> {r.message}</span>
              <span className="vg-skill-missing">{r.rule_id}{r.file ? ` · ${r.file}` : ''}</span>
            </li>
          ))}
          {more > 0 && <li className="sk-scan-why">+{more} more{s.finding_id ? ' in the Finding' : ''}</li>}
        </ul>
      )}
      {s.finding_id && (
        <span className="sk-scan-why">
          <Link className="sk-scan-link" to={`/overview?alert=${encodeURIComponent(s.finding_id)}`}>Open Finding {s.finding_id}</Link>
          {s.finding_new ? '' : ' · raised by an earlier scan of the same content'}
        </span>
      )}
    </li>
  )
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
  const [busy, setBusy] = useState<'read' | 'install' | 'scan' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [scan, setScan] = useState<ApiSkillRepoScan | null>(null)

  const readRepo = () => {
    const text = source.trim()
    if (!text) return
    setBusy('read')
    setError(null)
    setRead(null)
    setScan(null)
    skillsApi
      .previewRepo(text)
      .then((preview) => {
        setRead({ source: text, preview })
        setPicked(new Set(preview.skills.filter((s) => !s.problem).map((s) => s.name)))
      })
      .catch((e) => setError(errText(e, 'Could not read the repo')))
      .finally(() => setBusy(null))
  }

  const found = read?.preview.skills ?? []
  const toInstall = found.filter((s) => !s.problem && picked.has(s.name)).map((s) => s.name)
  const toScan = found.filter((s) => scannable(s) && picked.has(s.name)).map((s) => s.name)

  const install = () => {
    if (!read || toInstall.length === 0) return
    setBusy('install')
    setError(null)
    skillsApi
      .installRepo(read.source, read.preview.commit, toInstall)
      .then((result) => onInstalled(result, read.preview.label))
      .catch((e) => {
        setError(errText(e, 'Could not install the skills'))
        setBusy(null)
      })
  }

  const scanPicked = () => {
    if (!read || toScan.length === 0) return
    setBusy('scan')
    setError(null)
    skillsApi
      .scanRepo(read.source, read.preview.commit, toScan)
      .then(setScan)
      .catch((e) => {
        const gaveUp = (e as { code?: string })?.code === 'ECONNABORTED'
        setError(
          gaveUp
            ? 'The scan is taking longer than the console waits. It keeps running: each skill it holds still lands as a Finding (source skill-scan) on the Findings screen.'
            : errText(e, 'Could not scan the skills'),
        )
      })
      .finally(() => setBusy(null))
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
        {read && scan && (
          <section className="sk-pick" aria-label="Scan results">
            <span className="sk-scan-legend">
              {scan.label} · commit {scan.commit.slice(0, 7)} · {scan.scan_mode === 'static' ? 'static scan' : `model scan (${scan.scan_mode.replace(/^llm:/, '')})`}
            </span>
            <ul className="sk-pick-list sk-scan-list">
              {scan.skills.map((s) => <ScanRow key={s.folder || s.name} s={s} />)}
            </ul>
            <span className="field-hint">Nothing was installed and no agent loads these skills. Each held skill is a Finding carrying the scan's reasons.</span>
          </section>
        )}
        {read && !scan && (
          <fieldset className="sk-pick">
            <legend>
              {where} · commit {read.preview.commit.slice(0, 7)} · {read.preview.skills.length}{' '}
              {read.preview.skills.length === 1 ? 'skill' : 'skills'} found
            </legend>
            <div className="sk-pick-list">
              {read.preview.skills.map((s) => (
                <label key={s.folder || s.name} className={scannable(s) ? undefined : 'is-off'}>
                  <input type="checkbox" checked={scannable(s) && picked.has(s.name)} disabled={!scannable(s) || busy !== null} onChange={() => toggle(s.name)} />
                  <span>
                    <b>{s.name}</b>
                    <span>{s.installed ? `Already installed: can be scanned, not installed again` : s.problem ? `Can't install: ${s.problem}` : s.description}</span>
                  </span>
                </label>
              ))}
            </div>
            <span className="field-hint">
              Install: each skill is scanned and held from agents until it clears, as an upload is. Scan without installing: each skill the scan holds becomes a Finding.
            </span>
          </fieldset>
        )}
        {error && <div role="alert" className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex flex-wrap justify-end gap-2.5 pt-1">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          {read && scan && (
            <button type="button" className="btn ghost" onClick={() => setScan(null)}>Back to the list</button>
          )}
          {read && !scan && (
            <>
              <button type="button" className="btn ghost" disabled={busy !== null || toScan.length === 0} onClick={scanPicked}>
                {busy === 'scan' ? 'Scanning…' : 'Scan without installing'}
              </button>
              <button type="button" className="btn primary" disabled={busy !== null || toInstall.length === 0} onClick={install}>
                {busy === 'install' ? 'Installing…' : `Install ${toInstall.length} ${toInstall.length === 1 ? 'skill' : 'skills'}`}
              </button>
            </>
          )}
        </div>
      </div>
    </Popup>
  )
}
