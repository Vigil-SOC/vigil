// sources/*.md is edited in Git; this card shows each declared remote MCP
// source, its acceptance and connection state, takes its credentials, and lets
// a person with integrations.install accept the pending ones (#5304).
import { useEffect, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { configApi, type DeclaredSource, type DeclaredSources, type IntegrationHealthSource } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { LevelBadge } from '../../shared/LevelBadge'
import { ConfirmDialog, PasswordInput, SettingsCard } from '../../shared/ui'
import { healthLevel, healthPhrase } from './integrationHealth'
import type { SectionProps } from './types'

const TITLE = 'Declared sources'
const DESC = 'Remote MCP servers declared in sources/ of the declared directory. Edited in Git, not here.'
const STATE_COLOR: Record<string, string> = { ok: 'var(--good)', rejected: 'var(--poor)', degraded: 'var(--fair)' }
const MOVE_NOTE = 'A URL change, once accepted, deletes the stored credentials and asks for them again.'

/** The daemon's reason without the revision and place the card already shows. */
const rejectedWhy = (r: NonNullable<DeclaredSources['rejected']>) =>
  r.reason.replace(/^sources revision \S+ rejected: /, '').replace(`${r.file}:${r.line ?? ''}: `, '')

/** What a pending source alters, old against new. */
function Change({ pending }: { pending: NonNullable<DeclaredSource['pending']> }) {
  if (pending.kind === 'new') return <div className="src-flag">New source: nothing connects until a person accepts it.</div>
  return (
    <div className="src-flag">
      <div>Changed since accepted: {pending.changes.join(', ')}.</div>
      {pending.changes.includes('url') && (
        <div>
          URL <span className="mono">{pending.url_from ?? '—'}</span> → <span className="mono">{pending.url_to}</span>
          {pending.asks_credentials_again && ' · credentials are asked for again'}
        </div>
      )}
      {!!pending.headers_changed?.length && (
        <div>
          Headers changed: <span className="mono">{pending.headers_changed.join(', ')}</span>
        </div>
      )}
      {!!pending.credentials_added?.length && (
        <div>
          Credentials added: <span className="mono">{pending.credentials_added.join(', ')}</span>
        </div>
      )}
      {!!pending.credentials_removed?.length && (
        <div>
          Credentials removed: <span className="mono">{pending.credentials_removed.join(', ')}</span>
        </div>
      )}
    </div>
  )
}

/** The source's Integration Health row, as the Connected table shows it. */
function Connection({ row, failed }: { row?: IntegrationHealthSource; failed: boolean }) {
  if (failed) return <span className="text-tx-3">Could not read</span>
  if (!row) return <span className="text-tx-3">—</span>
  const level = healthLevel(row.state)
  const phrase = healthPhrase(row)
  return (
    <span className="flex flex-wrap items-center gap-2">
      {level && <LevelBadge level={level} variant="pill" />}
      <span className={level === 'poor' ? 'text-tx-2' : 'text-tx-3'}>{phrase}</span>
    </span>
  )
}

/** Credentials of an accepted source: set or not, and where from; never a value. */
function Credentials({ source, canWrite, saving, onSave }: {
  source: DeclaredSource
  /** integrations.write: POST /api/config/integrations answers 403 without it */
  canWrite: boolean
  saving: boolean
  onSave: (values: Record<string, string>) => Promise<boolean>
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const editable = canWrite && source.status === 'accepted' ? source.credentials.filter((c) => c.from !== 'environment') : []
  const entered = Object.fromEntries(Object.entries(values).filter(([, v]) => v))
  if (!source.credentials.length) return <div className="src-meta">No credentials declared.</div>
  return (
    <div className="flex flex-col gap-2">
      <ul className="src-creds">
        {source.credentials.map((c) => (
          <li key={c.field}>
            <span className="mono">{c.field}</span>{' '}
            <span className="text-tx-3">
              (<span className="mono">{c.key}</span>) ·{' '}
              {c.from === 'environment' ? 'set in the environment' : c.from === 'stored' ? 'set' : 'not set'}
            </span>
          </li>
        ))}
      </ul>
      {canWrite && source.status === 'pending' && source.credentials.some((c) => c.from !== 'environment') && (
        <div className="src-meta">Credentials are entered here once it is accepted.</div>
      )}
      {editable.length > 0 && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void onSave(entered).then((ok) => ok && setValues({}))
          }}
        >
          <div className="src-fields">
            {editable.map((c) => (
              <label key={c.field} className="field">
                <span className="field-label">{c.field}</span>
                <PasswordInput
                  autoComplete="off"
                  placeholder={c.set ? 'Set · type to replace' : 'Not set'}
                  value={values[c.field] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [c.field]: e.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2.5">
            <button className="btn" type="submit" disabled={saving || !Object.keys(entered).length}>
              {saving ? 'Saving…' : 'Save credentials'}
            </button>
            <span className="src-meta">{MOVE_NOTE}</span>
          </div>
        </form>
      )}
    </div>
  )
}

export default function DeclaredSourcesCard({
  notify,
  health,
  healthFailed,
  saveCredentials,
  onChanged,
}: {
  notify: SectionProps['notify']
  /** the section's one GET /api/integration-health read */
  health: IntegrationHealthSource[]
  healthFailed: boolean
  /** POST /api/config/integrations for source-<name> */
  saveCredentials: (integrationId: string, values: Record<string, string>) => Promise<void>
  /** after an accept or a save: the section re-reads servers and health */
  onChanged: () => void
}) {
  const [view, setView] = useState<DeclaredSources | null>(null)
  const [failed, setFailed] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const { hasPermission } = useAuth()
  const canAccept = hasPermission('integrations.install')
  const canWrite = hasPermission('integrations.write')

  useEffect(() => {
    let cancelled = false
    configApi
      .getSources()
      .then((res) => {
        if (cancelled) return
        setView(res.data)
        setFailed(false)
      })
      .catch(() => !cancelled && setFailed(true))
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  if (failed || !view) {
    return (
      <SettingsCard title={TITLE} desc={DESC} wide>
        <p className="text-sm text-tx-2">{failed ? 'Could not load declared sources.' : 'Loading declared sources…'}</p>
      </SettingsCard>
    )
  }

  const byId = new Map(health.map((h) => [h.source_id, h]))
  const pending = view.sources.filter((s) => s.status === 'pending')

  const accept = () => {
    if (!view.revision || !view.hash) return
    setBusy(true)
    configApi
      .acceptSources({ revision: view.revision, hash: view.hash, sources: pending.map((s) => s.name) })
      .then((res) => {
        setView(res.data)
        notify('ok', `Accepted ${pending.map((s) => s.name).join(', ')} at revision ${view.revision}.`)
        onChanged()
      })
      .catch((e) => {
        const moved = (e as { response?: { status?: number } })?.response?.status === 409
        notify('err', moved
          ? `Nothing was accepted: ${errorText(e, 'the declared sources changed')}. The card now shows the current revision.`
          : errorText(e, 'The pending sources were not accepted.'))
        // re-read, never retry: the person reviews what is there now
        if (moved) setReloadKey((k) => k + 1)
      })
      .finally(() => {
        setBusy(false)
        setAsking(false)
      })
  }

  const save = async (source: DeclaredSource, values: Record<string, string>) => {
    setSaving(source.name)
    try {
      await saveCredentials(source.integration_id, values)
      // the server drops a source's fields once it is pending again, and still answers 200
      const { data } = await configApi.getSources()
      setView(data)
      onChanged()
      if (data.sources.find((x) => x.name === source.name)?.status !== 'accepted') {
        notify('err', `${source.name}: not saved. A new revision made it pending; accept it, then enter its credentials.`)
        return false
      }
      notify('ok', `${source.name}: credentials saved. It connects on its next attempt.`)
      return true
    } catch (e) {
      notify('err', errorText(e, `${source.name}: credentials were not saved.`))
      return false
    } finally {
      setSaving(null)
    }
  }

  if (!view.declared) {
    return (
      <SettingsCard title={TITLE} desc={DESC} wide>
        {/* off (VIGIL_INTENT_DIR unset), or set and waiting for the daemon's first apply */}
        <p className="text-sm text-tx-2">
          {view.reason.includes('VIGIL_INTENT_DIR is not set') ? 'Sources are off. ' : ''}
          {view.reason}.
        </p>
      </SettingsCard>
    )
  }

  return (
    <SettingsCard title={TITLE} desc={DESC} wide>
      <div className="text-xs text-tx-3 mb-3 flex flex-col gap-1 [overflow-wrap:anywhere]">
        <div>
          <span className="mono">{view.directory}</span> at revision <span className="mono">{view.revision ?? 'none'}</span>
          {' · '}
          <span style={{ color: STATE_COLOR[view.state] ?? 'var(--tx2)' }}>{view.state === 'ok' ? 'applied' : view.state}</span>
        </div>
        {view.state === 'degraded' && <div style={{ color: 'var(--fair)' }}>{view.reason}</div>}
      </div>
      {view.rejected && (
        <div className="src-rejected" role="status">
          <div>
            Revision <span className="mono">{view.rejected.revision}</span> was rejected; the last good revision still applies.
          </div>
          <div>
            <span className="mono">
              {view.rejected.file}
              {view.rejected.line != null && `:${view.rejected.line}`}
            </span>{' '}
            — {rejectedWhy(view.rejected)}
          </div>
        </div>
      )}
      {pending.length > 0 && (
        <div className="flex flex-wrap items-center gap-2.5 mb-3">
          <span className="text-sm text-tx-2">
            {pending.length === 1 ? 'A source waits' : `${pending.length} sources wait`} for a person:{' '}
            <span className="mono">{pending.map((s) => s.name).join(', ')}</span>
          </span>
          {canAccept && (
            <button className="btn" onClick={() => setAsking(true)}>
              Accept
            </button>
          )}
        </div>
      )}
      {view.sources.length === 0 ? (
        <p className="text-sm text-tx-3">No source is declared in {view.directory}/sources.</p>
      ) : (
        <div className="src-stack">
          {view.sources.map((s) => (
            <div key={s.name} className="src-row">
              <div className="flex flex-wrap items-center gap-2">
                <span className="mono text-tx">{s.name}</span>
                <span className="chip" style={{ color: s.status === 'accepted' ? 'var(--good)' : 'var(--fair)' }}>
                  {s.status === 'accepted' ? 'Accepted' : 'Pending'}
                </span>
                <span className="src-meta mono">{s.file}</span>
              </div>
              <dl className="src-facts">
                <dt>URL</dt>
                <dd className="mono">{s.url}</dd>
                <dt>Headers</dt>
                <dd className="mono">{s.headers.length ? s.headers.join(', ') : '—'}</dd>
                <dt>Connection</dt>
                <dd>
                  <Connection row={byId.get(s.integration_id)} failed={healthFailed} />
                </dd>
                {s.accepted && (
                  <>
                    <dt>Accepted</dt>
                    <dd>
                      by {s.accepted.by_name} at revision <span className="mono">{s.accepted.revision}</span>
                      {s.status === 'pending' && ' (an earlier launch)'}
                    </dd>
                  </>
                )}
                <dt>Credentials</dt>
                <dd>
                  <Credentials source={s} canWrite={canWrite} saving={saving === s.name} onSave={(v) => save(s, v)} />
                </dd>
              </dl>
              {s.pending && <Change pending={s.pending} />}
            </div>
          ))}
        </div>
      )}
      <ConfirmDialog
        open={asking}
        title={`Accept revision ${view.revision ?? ''}?`}
        width={520}
        body={
          <>
            <p className="mb-2">These sources connect once accepted:</p>
            <ul className="text-xs mb-2 [overflow-wrap:anywhere]">
              {pending.map((s) => (
                <li key={s.name} className="mb-1">
                  <span className="mono">{s.name}</span>:{' '}
                  {s.pending?.kind === 'new' ? (
                    <>new, <span className="mono">{s.url}</span></>
                  ) : (
                    <>changes {s.pending?.changes.join(', ')}</>
                  )}
                  {s.pending?.asks_credentials_again && ' · its stored credentials are deleted'}
                </li>
              ))}
            </ul>
            <p className="mb-2">Each new tool still asks before it is first used.</p>
            <p>Your name and the revision go on the audit trail.</p>
          </>
        }
        confirmLabel="Accept"
        busy={busy}
        onConfirm={accept}
        onClose={() => setAsking(false)}
      />
    </SettingsCard>
  )
}
