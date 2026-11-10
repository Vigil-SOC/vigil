import { useCallback, useEffect, useRef, useState } from 'react'
import { breachedListApi, type BreachedListImport, type BreachedListStatus } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { utcDayClock } from '../../shared/utc'
import { SettingsCard } from '../../shared/ui'
import type { SectionProps } from './types'

const count = (n: number) => n.toLocaleString('en-US')

/** Settings › Sessions: the breached-password list. Shows its date and size and imports a newer
 * file offline (POST /api/session-policy/breached-list). An import only adds entries. */
export default function BreachedListCard({ notify }: SectionProps) {
  const [status, setStatus] = useState<BreachedListStatus | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<BreachedListImport | null>(null)
  const file = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setFailed(null)
    breachedListApi.get().then((res) => setStatus(res.data)).catch((e) => setFailed(errorText(e, 'Could not load the breached-password list.')))
  }, [])
  useEffect(load, [load])

  const choose = async (picked?: File) => {
    if (!picked) return
    setBusy(true)
    try {
      const res = await breachedListApi.import(picked)
      setResult(res.data)
      load()
      notify('ok', `Imported ${picked.name}: ${count(res.data.added)} added.`)
    } catch (e) {
      notify('err', errorText(e, 'The file was not imported.'))
    } finally {
      setBusy(false)
      if (file.current) file.current.value = ''
    }
  }

  const refreshed = status && utcDayClock(status.refreshed_at)
  return (
    <SettingsCard
      title="Breached passwords"
      desc="A password on this list is refused when it is set and at each sign-in. Vigil looks nothing up online: import a newer file here instead."
    >
      {failed && (
        <div className="settings-banner err mb-3" role="alert">
          <Icon name="alert" size={14} />
          <span>{failed}</span>
        </div>
      )}
      {status?.stale && (
        <div className="settings-banner err mb-3" role="alert">
          <Icon name="alert" size={14} />
          <span>The list is over 90 days old{refreshed ? ` (last refreshed ${refreshed} UTC)` : ''}. Import a newer one.</span>
        </div>
      )}
      <div className="lim-row">
        <span className="lim-row-title">
          Breached-password list
          <span>
            A plain-text file, one entry per line. Entries under 12 characters, email-shaped ones and blanks are dropped; the rest are
            added in lowercase, and an import never removes an entry. Any import counts as a refresh and is recorded on the audit trail with
            its counts and SHA-256. The file, and the licence it comes with, are the operator’s choice.
          </span>
        </span>
        <span className="lim-row-tag">{status ? `Checked ${status.shipped_date ?? 'on an unknown date'}` : 'Loading…'}</span>
        <span className="lim-row-ctl flex items-center gap-2">
          <input ref={file} type="file" hidden accept=".txt,text/plain" aria-label="Breached-password file" onChange={(e) => choose(e.target.files?.[0])} />
          <button className="btn ghost" disabled={busy || !status} onClick={() => file.current?.click()}>
            <Icon name="upload" size={14} /> {busy ? 'Importing…' : 'Import a file'}
          </button>
        </span>
      </div>
      {status && (
        <p className="text-xs text-tx-3 mt-3">
          {count(status.shipped_count)} shipped · {count(status.imported_count)} imported
          {status.last_import_at ? ` · last import ${utcDayClock(status.last_import_at)} UTC` : ''}
        </p>
      )}
      {result && (
        <p className="text-xs text-tx-3 mt-1" role="status">
          Last import: {count(result.read)} lines read, {count(result.added)} added, {count(result.already_listed)} already listed, {count(result.skipped)} skipped.
        </p>
      )}
    </SettingsCard>
  )
}
