import { useCallback, useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, NumberInput, TextInput, Toggle } from '../../shared/ui'
import { auditFeedApi, type AuditFeed } from '../../services/api'
import { relativeTime } from './integrationHealth'
import type { SectionProps } from './types'

const DEFAULT_PORT = 6514

type Draft = { host: string; port: string; ca: string; cert: string; key: string }
type TestResult = { ok: boolean; error: string | null } | 'running' | null

const detail = (e: unknown, fallback: string) => {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : fallback
}

const draftOf = (f: AuditFeed): Draft => ({ host: f.host ?? '', port: String(f.port ?? DEFAULT_PORT), ca: '', cert: '', key: '' })

/**
 * The audit trail's syslog feed: CEF over TLS to the SIEM. Off until an admin
 * turns it on. The trail is the backlog, so an outage resumes from where the
 * last delivered entry left off.
 */
export default function AuditFeedPanel({ notify }: SectionProps) {
  const [feed, setFeed] = useState<AuditFeed | null>(null)
  const [failed, setFailed] = useState(false)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<TestResult>(null)

  const load = useCallback(() => {
    auditFeedApi
      .get()
      .then((r) => {
        setFeed(r.data)
        setDraft((d) => d ?? draftOf(r.data))
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  if (!feed || !draft) {
    return failed ? (
      <EmptyState
        error
        icon="alert"
        title="Couldn’t load the audit feed"
        body="Its destination and health could not be read."
        primary={{ label: 'Retry', onClick: load, icon: 'refresh' }}
      />
    ) : (
      <EmptyState loading icon="link" title="Loading the audit feed…" />
    )
  }

  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setDraft({ ...draft, [k]: e.target.value })
  const port = Number(draft.port)
  const portOk = Number.isInteger(port) && port >= 1 && port <= 65535
  const dirty = draft.host.trim() !== (feed.host ?? '') || port !== (feed.port ?? DEFAULT_PORT) || !!(draft.ca || draft.cert || draft.key)

  const save = async (enabled: boolean) => {
    setBusy(true)
    try {
      const r = await auditFeedApi.save({
        enabled,
        host: draft.host.trim(),
        port,
        ca_pem: draft.ca,
        client_cert_pem: draft.cert,
        client_key_pem: draft.key,
      })
      setFeed(r.data)
      setDraft(draftOf(r.data))
      setTest(null)
      notify('ok', enabled === feed.enabled ? 'Audit feed saved.' : enabled ? 'Audit feed is on.' : 'Audit feed is off.')
    } catch (e) {
      notify('err', `Could not save the audit feed: ${detail(e, 'request failed')}`)
    } finally {
      setBusy(false)
    }
  }

  const sendTest = async () => {
    setTest('running')
    try {
      const r = await auditFeedApi.test()
      setTest(r.data)
    } catch (e) {
      setTest({ ok: false, error: detail(e, 'request failed') })
    }
  }

  const h = feed.health
  const tiles: [string, string, string | undefined, string][] = [
    [
      'Last delivered',
      h.delivered_at ? relativeTime(h.delivered_at) : 'Never',
      h.delivered_at ?? undefined,
      'var(--tx0)',
    ],
    ['Behind the trail', `${h.behind} ${h.behind === 1 ? 'entry' : 'entries'}`, `Delivered through #${h.delivered_seq} of #${h.tip_seq}`, h.behind && feed.enabled ? 'var(--fair)' : 'var(--tx0)'],
  ]
  const stored = (on: boolean) => (on ? 'Stored. A blank field keeps it.' : undefined)

  return (
    <div className="mcp-card">
      <div className="mcp-head">
        <h3>Audit trail to your SIEM</h3>
        <p>
          Every audit trail entry, sent in order as CEF over syslog TLS (RFC 5425). One Vigil replica sends; if the SIEM is
          unreachable it retries from the last entry delivered, so nothing is skipped and nothing waits on it.
        </p>
      </div>

      <div className="mcp-onoff">
        <span className="mcp-onoff-text">
          <b>{feed.enabled ? `Sending to ${feed.host}:${feed.port}` : 'Off'}</b>
          <span>{feed.enabled ? 'Turning it off stops sending; the trail keeps everything.' : 'Nothing leaves Vigil until you turn it on.'}</span>
        </span>
        <Toggle
          checked={feed.enabled}
          label="Send the audit trail"
          title={!feed.host ? 'Save a destination first' : dirty ? 'Save the destination first' : undefined}
          disabled={busy || !feed.host || dirty}
          onChange={(v) => void save(v)}
        />
      </div>

      {feed.enabled && h.failing && (
        <div className="mcp-callout" role="status">
          <span>
            <Icon name="alert" size={16} /> The SIEM is not taking entries
            {h.last_error_at && <> since {relativeTime(h.last_error_at)}</>}: {h.last_error}. Vigil keeps retrying.
          </span>
        </div>
      )}

      <div className="int-tiles feed-tiles">
        {tiles.map(([label, value, title, color]) => (
          <div key={label} className="int-tile" title={title}>
            <span className="int-tile-k">{label}</span>
            <span className="int-tile-v" style={{ color }}>{value}</span>
          </div>
        ))}
      </div>

      <div className="mcp-head">
        <h4>Destination</h4>
        <p>The syslog receiver’s host name must match its certificate.</p>
      </div>
      <div className="feed-dest">
        <Field label="Host">
          <TextInput value={draft.host} placeholder="siem.example.com" onChange={set('host')} />
        </Field>
        <Field label="Port" error={portOk ? null : 'A port from 1 to 65535'}>
          <NumberInput value={draft.port} min={1} max={65535} onChange={set('port')} />
        </Field>
      </div>
      <Field label="CA certificate (PEM)" hint={stored(feed.tls.ca) ?? 'Blank uses the system’s trusted authorities.'}>
        <textarea className="field-input feed-pem" rows={3} value={draft.ca} placeholder="-----BEGIN CERTIFICATE-----" onChange={set('ca')} />
      </Field>
      <div className="feed-dest">
        <Field label="Client certificate (PEM)" hint={stored(feed.tls.client_cert) ?? 'Only if the receiver asks for one.'}>
          <textarea className="field-input feed-pem" rows={3} value={draft.cert} placeholder="-----BEGIN CERTIFICATE-----" onChange={set('cert')} />
        </Field>
        <Field label="Client key (PEM)" hint={stored(feed.tls.client_key)}>
          <textarea className="field-input feed-pem" rows={3} value={draft.key} placeholder="-----BEGIN PRIVATE KEY-----" onChange={set('key')} />
        </Field>
      </div>
      <div className="flex gap-2">
        <button className="btn primary" disabled={busy || !dirty || !portOk} onClick={() => void save(feed.enabled)}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button className="btn" disabled={busy || dirty || !feed.host || test === 'running'} onClick={() => void sendTest()} title={dirty ? 'Save the destination first' : undefined}>
          <Icon name="send" /> {test === 'running' ? 'Sending…' : 'Send test'}
        </button>
      </div>

      {test && test !== 'running' && (
        <div className={`int-probe ${test.ok ? 'pass' : 'fail'}`} role="status">
          <span className="int-probe-ic"><Icon name={test.ok ? 'check2' : 'close'} size={15} /></span>
          <span className="int-probe-name">
            {feed.host}:{feed.port}
          </span>
          <span className="int-probe-detail" style={test.ok ? { color: 'var(--tx2)' } : undefined}>
            {test.ok ? 'Test message written over TLS' : test.error}
          </span>
        </div>
      )}

      <div className="int-sec">
        <span className="int-sec-h">Security controls</span>
        <span className="int-sec-row"><Icon name="check2" size={13} />TLS material is stored encrypted and never shown again; a blank field keeps it.</span>
        <span className="int-sec-row"><Icon name="check2" size={13} />Only a person with audit.forward can change this; an MCP credential, a service account or an agent cannot.</span>
        <span className="int-sec-row"><Icon name="check2" size={13} />Every change here, and every outage and recovery of the feed, is an entry on the audit trail.</span>
      </div>
    </div>
  )
}
