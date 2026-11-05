import { useRef, useState, type ReactNode } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { InfoTip } from '../../shared/InfoTip'
import { HoldButton } from '../../shared/HoldButton'
import { ConfirmDialog, EmptyState, Popup } from '../../shared/ui'
import { utcDayClock } from '../../shared/utc'
import type { PackFacts, PackState } from './medicTypes'
import { safeText } from './safeText'
import { importPack, packProblem, revertPack, switchToBundled, usePack } from './useMedic'
import './medic.css'

const MAX_PACK = 4 * 1024 * 1024 // MAX_PACK_BYTES: larger files are refused before upload
const POLL_TRIES = 6
const POLL_MS = 3_000

// One line per check Medic names in `pack_code` (the contract's closed set); Medic sends no text of its own.
const REASON: Record<string, string> = {
  'S-DIGEST': "The file's sha256 is not the one you typed.",
  'P-WITHDRAWN': 'This version was withdrawn on this site.',
  'P-LINEAGE': "This pack has another id or channel than the active pack.",
  'P-DOWNGRADE': 'This version is not newer than the active one.',
  'P-NO-TARGET': 'There is no pack to switch to.',
  'P-NO-ACTIVE': 'There is no active pack.',
  'P-BUNDLED-ACTIVE': 'The bundled pack is already active.',
  'P-SIZE': 'The file is larger than 4 MB.',
  'P-JSON': "The file isn't valid JSON.",
  'P-SCHEMA': "The file isn't a medic.pack/v1 pack.",
  'P-FILES': "The pack's file list is wrong.",
  'P-HASH': "A file's content doesn't match its manifest hash.",
  'P-DATES': "The pack's dates are wrong.",
  'P-EXPIRED': 'The pack has expired.',
  'P-VIGIL': "The pack's Vigil range is wrong.",
  'P-RULE': 'A rule in the pack fails its checks.',
  'P-RULE-DUP': 'A rule id appears twice.',
  'P-PATH': "A path doesn't match its rule or runbook.",
  'P-ENGINE': "The pack needs another Medic engine.",
  'P-SUPPRESSION': "The suppression list fails its checks.",
  'P-CATALOG': 'The template catalog fails its checks.',
  'P-RUNBOOK': 'A runbook fails its checks.',
  'P-COUNTS': "The manifest's counts don't match the files.",
}
const PROBLEM: Record<string, string> = {
  medic_off: 'Medic is off.',
  medic_unavailable: "Medic isn't answering. Try again in a moment.",
  busy: 'Medic was too busy to take it. Try again in a moment.',
  store_not_recording: "Medic isn't recording, so it took nothing.",
  payload_too_large: 'The file is larger than 4 MB.',
  unsupported_media_type: 'The file must be JSON.',
  invalid_parameter: "The sha256 isn't 64 hex digits.",
  pack_unavailable: 'Medic has no pack lifecycle on this install.',
  not_found: "Medic can't take pack changes yet.",
}

const label = (p: { id: string; version: string; channel: string }) => `${safeText(p.id)} ${safeText(p.version)} (${safeText(p.channel)})`
const when = (iso: string) => utcDayClock(iso) ?? safeText(iso)
const isWithdrawn = (s: PackState, p: PackFacts) => s.withdrawn.some((w) => w.id === p.pack.id && w.channel === p.pack.channel && w.version === p.pack.version)

/** UX-25: the expiry line, amber at 30 days or fewer, red once Medic says it is stale. */
function Expiry({ p }: { p: PackFacts }) {
  if (p.stale) return <span className="mdc-expiry poor">Expired, still running</span>
  const d = Math.max(0, Math.ceil((Date.parse(p.pack.expires_at) - Date.now()) / 864e5))
  return <span className={`mdc-expiry${d <= 30 ? ' fair' : ''}`}>expires in {d} d</span>
}

function Facts({ title, p, state, note }: { title: string; p: PackFacts | null; state: PackState; note?: ReactNode }) {
  return (
    <section className="mdc-card" aria-label={title}>
      <h3>{title}</h3>
      {!p ? <p className="mdc-quiet">None.</p> : (
        <dl className="mdc-facts">
          <dt>Pack</dt><dd>{safeText(p.pack.id)} {safeText(p.pack.version)} · {safeText(p.pack.channel)}{isWithdrawn(state, p) && <span className="mdc-expiry poor"> · Withdrawn on this site</span>}</dd>
          <dt>Source</dt><dd>{p.source === 'bundled' ? 'bundled' : `imported by ${safeText(p.imported_by)} on ${when(p.activated_at)}`}</dd>
          <dt>Created</dt><dd>{when(p.pack.created_at)}</dd>
          <dt>Expires</dt><dd><Expiry p={p} /></dd>
          <dt>Vigil range</dt><dd>{safeText(p.vigil.min)} to {safeText(p.vigil.max_exclusive)}{!p.in_vigil_range && <span className="mdc-expiry poor"> · this Vigil is outside it</span>}</dd>
          <dt>sha256</dt><dd className="mdc-mono">{safeText(p.sha256)}</dd>
        </dl>
      )}
      {note}
    </section>
  )
}

/** The file's own identity, read for the confirm step only; Medic checks the file itself. */
async function peek(file: File): Promise<{ id: string; version: string; channel: string } | null> {
  try {
    const p = JSON.parse(await file.text())?.manifest?.pack
    return p && typeof p.id === 'string' ? { id: p.id, version: String(p.version), channel: String(p.channel) } : null
  } catch { return null }
}

type Action = 'import' | 'override' | 'revert' | 'switch' | null

/** UX-25…27: the active and previous packs, what the suppression list held back, and the admin's pack actions. */
export function PackTab({ running, reloadStatus }: { running: boolean; reloadStatus: () => void }) {
  const { hasPermission } = useAuth()
  const admin = hasPermission('medic.admin')
  const { pack, phase, unavailable, reload, set } = usePack(running)
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [sha, setSha] = useState('')
  const [incoming, setIncoming] = useState<{ id: string; version: string; channel: string } | null>(null)
  const [formError, setFormError] = useState('')
  const [action, setAction] = useState<Action>(null)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<{ code: string; reason: string } | null>(null)
  const [done, setDone] = useState('')

  if (!running) {
    return <div className="mdc-body"><EmptyState error icon="alert" title="Medic isn't answering" body="The content pack is read from Medic, so it shows again once Medic answers." /></div>
  }
  if (unavailable) {
    return <div className="mdc-body"><EmptyState icon="info" title="This Medic has no content pack to manage" body="It is running the source pack (dev mode), or its pack store could not be opened." /></div>
  }
  if (!pack) {
    return <div className="mdc-body">{phase === 'error'
      ? <EmptyState error icon="alert" title="Couldn't load the content pack" primary={{ label: 'Retry', onClick: () => { reload() }, icon: 'refresh' }} />
      : <EmptyState loading icon="clock" title="Loading the content pack…" />}</div>
  }

  const { active, previous, bundled } = pack
  const canSwitch = !!active && active.in_vigil_range === false && !!bundled

  const choose = async (f: File | null) => {
    setDone(''); setRefusal(null); setFile(null); setIncoming(null)
    if (!f) { setFormError(''); return }
    if (f.size > MAX_PACK) { setFormError(`That file is ${(f.size / 2 ** 20).toFixed(1)} MB; a pack is at most 4 MB. Nothing was sent.`); if (fileRef.current) fileRef.current.value = ''; return }
    setFormError('')
    setFile(f)
    setIncoming(await peek(f))
  }

  const review = () => {
    setDone(''); setRefusal(null)
    if (!/^[0-9a-fA-F]{64}$/.test(sha.trim())) { setFormError('Type the 64 hex digits of the sha256 you expect the file to have.'); return }
    setFormError('')
    setAction('import')
  }

  // after a write: show Medic's answer, then re-read until the pack facts and the status block show that version
  const landed = async (next: PackState, text: string) => {
    set(next)
    setDone(text)
    for (let n = 0; n < POLL_TRIES; n++) {
      const got = await reload()
      reloadStatus()
      if (got && got.active?.sha256 === next.active?.sha256) return
      await new Promise((r) => window.setTimeout(r, POLL_MS))
    }
  }

  const run = async (kind: Exclude<Action, null>) => {
    setBusy(true); setRefusal(null); setDone('')
    try {
      let next: PackState
      if (kind === 'import' || kind === 'override') next = await importPack(file!, sha.trim().toLowerCase(), kind === 'override')
      else next = kind === 'revert' ? await revertPack() : await switchToBundled()
      setAction(null)
      if (kind === 'import' || kind === 'override') { setFile(null); setSha(''); setIncoming(null); if (fileRef.current) fileRef.current.value = '' }
      await landed(next, `Medic now runs ${next.active ? label(next.active.pack) : 'no pack'}.`)
    } catch (e) {
      const { status, code, packCode } = packProblem(e)
      setAction(null)
      // a withdrawn version, or another id or channel, may be imported once the admin confirms the override
      if (kind === 'import' && status === 409 && (packCode === 'P-WITHDRAWN' || packCode === 'P-LINEAGE')) {
        setRefusal({ code: packCode, reason: REASON[packCode] })
        setAction('override')
      } else if (packCode) setRefusal({ code: packCode, reason: REASON[packCode] ?? 'Medic refused it.' })
      else setRefusal({ code: code || 'network', reason: PROBLEM[code] ?? "Couldn't reach Medic. Check the pack on this page before trying again." })
    } finally { setBusy(false) }
  }

  const both = (from: PackFacts | null, to: PackFacts | null, extra?: boolean) => (
    <span className="mdc-confirm">
      <span>Now: {from ? label(from.pack) : 'none'}</span>
      <span>Becomes: {to ? label(to.pack) : 'none'}{to && <> · {to.stale ? 'expired' : 'not expired'} · {to.in_vigil_range ? "inside Vigil's range" : "outside Vigil's range"}</>}</span>
      {extra && <span>The version you leave is withdrawn on this site; importing it again needs a second confirm.</span>}
    </span>
  )

  return (
    <div className="mdc-body">
      {done && <div className="mdc-strip good" role="status">{done}</div>}
      {refusal && (
        <div className="mdc-strip poor" role="alert">
          Refused: <span className="mdc-mono">{safeText(refusal.code)}</span> · {safeText(refusal.reason)} The active pack stays.
        </div>
      )}

      <div className="mdc-packs">
        <Facts title="Active pack" p={active} state={pack} />
        <Facts title="Previous pack (last known good)" p={previous} state={pack} />
      </div>

      {admin && (
        <section className="mdc-card" aria-label="Pack actions">
          <h3>Change the pack<InfoTip label="About pack changes" source="Medic's own pack store; the change is logged there." calculation="A change takes effect at Medic's next cycle (up to about 20 s). Nothing needs a restart." limit="Importing needs the sha256 you expect from outside the file; Vigil does not work it out for you." /></h3>
          <div className="mdc-import">
            <label className="mdc-quiet" htmlFor="mdc-pack-file">Pack file (.json, up to 4 MB)</label>
            <input id="mdc-pack-file" ref={fileRef} type="file" accept=".json,application/json" onChange={(e) => choose(e.target.files?.[0] ?? null)} />
            <label className="mdc-quiet" htmlFor="mdc-pack-sha">Expected sha256</label>
            <input id="mdc-pack-sha" className="field-input mdc-mono" value={sha} spellCheck={false} autoComplete="off" maxLength={64} placeholder="64 hex digits" onChange={(e) => setSha(e.target.value)} />
            {formError && <p className="mdc-save-failed" role="alert">{formError}</p>}
            <div className="mdc-actions">
              <button type="button" className="btn primary" disabled={!file || busy} onClick={review}>Import pack…</button>
              {previous && <button type="button" className="btn" disabled={busy} onClick={() => { setDone(''); setRefusal(null); setAction('revert') }}>Revert to previous pack…</button>}
              {canSwitch && <button type="button" className="btn" disabled={busy} onClick={() => { setDone(''); setRefusal(null); setAction('switch') }}>Switch to bundled pack…</button>}
            </div>
          </div>
        </section>
      )}

      <section className="mdc-card" aria-label="Withdrawn on this site">
        <h3>Withdrawn on this site</h3>
        {pack.withdrawn.length === 0 ? <p className="mdc-quiet">No pack version has been withdrawn here.</p>
          : <ul className="mdc-list">{pack.withdrawn.map((w) => <li key={`${w.id}${w.channel}${w.version}`} className="mdc-mono">{label(w)}</li>)}</ul>}
      </section>

      <section className="mdc-card" aria-label="Suppressed incidents">
        <h3>Suppressed in the last 7 days<InfoTip label="About suppression counts" source="Medic's decision log." calculation="Each suppressed incident is counted once, under the rule that suppressed it." limit="Refreshed about once a minute and after every pack change." /></h3>
        {pack.suppression_7d.length === 0 ? <p className="mdc-quiet">No incidents were suppressed.</p>
          : <ul className="mdc-list">{pack.suppression_7d.map((s) => <li key={s.rule}>{s.suppressed} incident{s.suppressed === 1 ? '' : 's'} suppressed by rule <span className="mdc-mono">{safeText(s.rule)}</span> (last 7 d)</li>)}</ul>}
      </section>

      <ConfirmDialog
        open={action === 'import'} danger={false} busy={busy} confirmLabel="Import pack"
        title="Import this pack?"
        body={<>
          <span className="mdc-confirm">
            <span>New: {incoming ? label(incoming) : `${safeText(file?.name)} (its id and version are not shown; Medic checks the file)`}</span>
            <span>Active: {active ? label(active.pack) : 'none'}</span>
            <span>Expected sha256: <span className="mdc-mono">{safeText(sha.trim().toLowerCase())}</span></span>
            <span>Medic takes it at its next cycle, which can take up to 20 s.</span>
          </span>
        </>}
        onConfirm={() => run('import')} onClose={() => !busy && setAction(null)}
      />
      <ConfirmDialog
        open={action === 'revert'} busy={busy} confirmLabel="Revert"
        title="Revert to the previous pack?" body={both(active, previous, true)}
        onConfirm={() => run('revert')} onClose={() => !busy && setAction(null)}
      />
      <ConfirmDialog
        open={action === 'switch'} busy={busy} confirmLabel="Switch to bundled"
        title="Switch to the bundled pack?" body={both(active, bundled, true)}
        onConfirm={() => run('switch')} onClose={() => !busy && setAction(null)}
      />
      <Popup open={action === 'override'} onClose={() => !busy && setAction(null)} title="Import anyway?" width={440}>
        <p className="text-sm text-tx-2 leading-relaxed">
          Medic refused it with <span className="mdc-mono">{safeText(refusal?.code)}</span>: {safeText(refusal?.reason)} Importing anyway is a logged override of that refusal.
        </p>
        <div className="flex justify-end gap-2.5 mt-5">
          <button className="btn ghost" onClick={() => setAction(null)} disabled={busy}>Cancel</button>
          <HoldButton label="Hold to override" disabled={busy} onConfirm={() => run('override')} />
        </div>
      </Popup>
    </div>
  )
}
