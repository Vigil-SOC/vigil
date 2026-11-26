// site/*.md is edited where it lives; this card only shows the revision the
// daemon applied, any refusal, and each item with its file, line and owner.
import { useEffect, useState, type ReactNode } from 'react'
import { configApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { SettingsCard } from '../../shared/ui'

type Site = Schema<'SiteContextResponse'>
type OwnerRef = Schema<'SiteOwnerRef'>

const STATE_COLOR: Record<string, string> = { ok: 'var(--good)', rejected: 'var(--poor)', degraded: 'var(--fair)' }

// The daemon writes applied_at as UTC with no offset.
const utcText = (at: string) => new Date(/[Zz]|[+-]\d\d:?\d\d$/.test(at) ? at : `${at}Z`).toLocaleString()

function Where({ file, line }: { file: string; line: number }) {
  return (
    <span className="mono text-xs text-tx-3">
      {file}:{line}
    </span>
  )
}

// The server resolved each owner; an unresolved one shows its reason as given.
function Owners({ owners }: { owners?: OwnerRef[] }) {
  if (!owners?.length) return <span className="text-tx-3">no owner declared</span>
  return (
    <>
      {owners.map((o, i) => (
        <span key={`${o.file}:${o.line}`}>
          {i > 0 && ', '}
          {o.resolved ? (
            <span className="text-tx">{o.resolved}</span>
          ) : (
            <span style={{ color: 'var(--fair)' }}>unassigned: {o.unassigned}</span>
          )}
        </span>
      ))}
    </>
  )
}

function Selectors({ selectors }: { selectors: string[] }) {
  return <span className="mono text-xs text-tx-2">{selectors.join(', ')}</span>
}

function Group({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <div className="mb-4">
      <h4 className="text-sm font-semibold text-tx mb-1.5">
        {title} <span className="text-tx-3 font-normal">{count}</span>
      </h4>
      {count ? (
        <ul className="list-none m-0 p-0 flex flex-col gap-2 [overflow-wrap:anywhere]">{children}</ul>
      ) : (
        <p className="text-xs text-tx-3">None declared.</p>
      )}
    </div>
  )
}

function Item({ head, at, children }: { head: ReactNode; at: { file: string; line: number }; children: ReactNode }) {
  return (
    <li className="text-sm text-tx-2 border-b border-line pb-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-2">
        {head}
        <Where {...at} />
      </div>
      <div className="flex flex-col gap-0.5 mt-0.5">{children}</div>
    </li>
  )
}

export default function SiteContextCard({ reloadKey = 0 }: { reloadKey?: number }) {
  const [site, setSite] = useState<Site | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    configApi
      .getSite()
      .then((res) => {
        if (cancelled) return
        setSite(res.data)
        setFailed(false)
      })
      .catch(() => {
        if (cancelled) return
        setSite(null)
        setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const chip = (state: string) => (
    <span className="chip" style={{ color: STATE_COLOR[state] ?? 'var(--tx2)' }}>
      {state}
    </span>
  )
  const line = (text: string, tone = 'text-tx-2', state?: string) => (
    <SettingsCard title="Declared site" wide>
      <p className={`text-sm ${tone} [overflow-wrap:anywhere]`}>
        {state && <>{chip(state)} </>}
        {text}
      </p>
    </SettingsCard>
  )

  if (failed) return line('Could not load the declared site.')
  if (!site) return line('Loading declared site…', 'text-tx-3')
  // No directory, or a directory with no site revision applied or refused yet.
  if (site.state === 'none') return line(site.reason)
  // A directory set but no revision applied or refused yet (e.g. unreadable on the first poll).
  if (!site.declared && !site.rejected) return line(site.reason, 'text-tx-2', site.state)

  const rejected = site.rejected as { revision?: string; file?: string; line?: number; reason?: string } | null
  const zones = site.zones ?? []
  const jewels = site.crown_jewels ?? []
  const owners = site.owners ?? []
  const blind = site.blind_spots ?? []
  const files = site.files ?? []

  return (
    <SettingsCard title="Declared site" wide>
      <div className="text-xs text-tx-3 mb-3 flex flex-col gap-1 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {chip(site.state)}
          <span>
            {site.declared ? (
              <>
                Revision <span className="mono">{site.revision}</span>
                {site.applied_at && <> applied {utcText(site.applied_at)}</>}
              </>
            ) : (
              'No revision applied yet'
            )}{' '}
            from <span className="mono">{site.directory}</span>. Change it there, not here.
          </span>
        </div>
        {site.reason !== rejected?.reason && <div>{site.reason}</div>}
      </div>
      {rejected && (
        <div
          className="mb-3 px-3 py-2 rounded text-sm [overflow-wrap:anywhere]"
          style={{ background: 'var(--poor-bg)', border: '1px solid var(--poor-ln)' }}
        >
          <div style={{ color: 'var(--poor)' }}>
            Revision <span className="mono">{rejected.revision ?? 'unknown'}</span> refused at{' '}
            <span className="mono">
              {rejected.file}:{rejected.line}
            </span>
          </div>
          <div className="text-tx-2">{rejected.reason}</div>
          <div className="text-xs text-tx-3 mt-1">
            {site.declared
              ? `Below is the last good revision, ${site.revision}, which still applies.`
              : 'No earlier revision applied, so nothing is declared.'}
          </div>
        </div>
      )}
      {site.declared && (
        <>
          <Group title="Zones" count={zones.length}>
            {zones.map((z) => (
              <Item key={`${z.file}:${z.line}`} head={<span className="text-tx font-medium">{z.name}</span>} at={z}>
                {z.description && <span>{z.description}</span>}
                <Selectors selectors={z.selectors} />
                <span>
                  Owner: <Owners owners={z.owners} />
                </span>
              </Item>
            ))}
          </Group>
          <Group title="Crown jewels" count={jewels.length}>
            {jewels.map((j) => (
              <Item key={`${j.file}:${j.line}`} head={<span className="text-tx font-medium">{j.name}</span>} at={j}>
                <span>
                  Mission: {j.mission}
                  {j.zone && <> · Zone: {j.zone}</>}
                </span>
                {j.why && <span>Why: {j.why}</span>}
                <Selectors selectors={j.selectors} />
                <span>
                  Owner: <Owners owners={j.owners} />
                </span>
              </Item>
            ))}
          </Group>
          <Group title="Owners" count={owners.length}>
            {owners.map((o) => (
              <Item key={`${o.file}:${o.line}`} head={<Owners owners={[o]} />} at={o}>
                {!o.resolved && <span className="text-xs text-tx-3">Declared team: {o.team}</span>}
                <Selectors selectors={o.selectors} />
              </Item>
            ))}
          </Group>
          <Group title="Blind spots" count={blind.length}>
            {blind.map((b) => (
              <Item key={`${b.file}:${b.line}`} head={<span className="text-tx">{b.what}</span>} at={b}>
                <Selectors selectors={b.selectors} />
              </Item>
            ))}
          </Group>
          {files.length > 0 && (
            <div className="flex flex-col gap-1.5">
              {files.map((f) => (
                <details key={f.file}>
                  <summary className="mono text-xs text-tx-2 cursor-pointer">{f.file}</summary>
                  <pre className="mono text-xs text-tx mt-2 px-3 py-2.5 rounded bg-bg-2 whitespace-pre-wrap [overflow-wrap:anywhere] max-h-80 overflow-y-auto">
                    {f.text}
                  </pre>
                </details>
              ))}
            </div>
          )}
        </>
      )}
    </SettingsCard>
  )
}
