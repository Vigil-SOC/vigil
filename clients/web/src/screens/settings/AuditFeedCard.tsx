import { useEffect, useState } from 'react'
import { auditFeedApi, type AuditFeed } from '../../services/api'
import { ToggleRow } from '../../shared/ui'
import type { SectionProps } from './types'

function utcClock(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mm} UTC`
}

function detail(e: unknown): string {
  const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const body = err.response?.data?.detail
  return typeof body === 'string' ? body : err.message || 'request failed'
}

/** Syslog feed on Settings › Integrations. Hidden when the caller may not forward. */
export default function AuditFeedCard({ notify }: SectionProps) {
  const [feed, setFeed] = useState<AuditFeed | null>(null)
  const [hidden, setHidden] = useState(false)
  const [host, setHost] = useState('')
  const [port, setPort] = useState('6514')
  const [enabled, setEnabled] = useState(false)
  const [ca, setCa] = useState('')
  const [cert, setCert] = useState('')
  const [keyPem, setKeyPem] = useState('')
  const [clearClient, setClearClient] = useState(false)
  const [busy, setBusy] = useState<'save' | 'test' | null>(null)

  const load = async () => {
    try {
      const { data } = await auditFeedApi.get()
      setFeed(data)
      setHost(data.host || '')
      setPort(data.port ? String(data.port) : '6514')
      setEnabled(data.enabled)
      setHidden(false)
    } catch (e) {
      const status = (e as { response?: { status?: number } }).response?.status
      if (status === 403 || status === 503) setHidden(true)
      else setFeed(null)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  if (hidden || !feed) return null

  const behind =
    feed.behind === 0 ? 'Caught up' : `${feed.behind} ${feed.behind === 1 ? 'entry' : 'entries'} behind`

  const save = async () => {
    const parsed = port.trim() === '' ? null : Number(port)
    if (parsed !== null && (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535)) {
      notify('err', 'Port must be between 1 and 65535.')
      return
    }
    setBusy('save')
    try {
      const { data } = await auditFeedApi.save({
        enabled,
        host: host.trim(),
        port: parsed,
        ca_pem: ca.trim() ? ca : null,
        client_cert_pem: clearClient ? '' : cert.trim() ? cert : null,
        client_key_pem: clearClient ? '' : keyPem.trim() ? keyPem : null,
      })
      setFeed(data)
      setCa('')
      setCert('')
      setKeyPem('')
      setClearClient(false)
      notify('ok', enabled ? 'Audit feed saved and on.' : 'Audit feed saved. It stays off until you turn it on.')
    } catch (e) {
      notify('err', `Could not save the audit feed: ${detail(e)}.`)
    } finally {
      setBusy(null)
    }
  }

  const test = async () => {
    setBusy('test')
    try {
      await auditFeedApi.test()
      notify('ok', 'Test event sent.')
    } catch (e) {
      notify('err', `Test event was not sent: ${detail(e)}.`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="audit-feed" aria-label="Audit feed">
      <div>
        <h3>Audit feed</h3>
        <p className="field-hint">CEF over TLS to a SIEM. Off until you turn it on. Sending never blocks an action.</p>
      </div>
      <div className="audit-feed-dest">
        <label className="field">
          <span className="field-label">Host</span>
          <input className="field-input" value={host} onChange={(e) => setHost(e.target.value)} autoComplete="off" spellCheck={false} />
        </label>
        <label className="field">
          <span className="field-label">Port</span>
          <input className="field-input" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} />
        </label>
      </div>
      <label className="field">
        <span className="field-label">CA certificate</span>
        <textarea
          className="field-input"
          rows={3}
          value={ca}
          spellCheck={false}
          placeholder={feed.ca_set ? 'A certificate is saved. Paste a new one to replace it.' : 'PEM'}
          onChange={(e) => setCa(e.target.value)}
        />
      </label>
      <label className="field">
        <span className="field-label">Client certificate</span>
        <textarea
          className="field-input"
          rows={3}
          value={cert}
          spellCheck={false}
          placeholder={feed.client_cert_set && !clearClient ? 'A certificate is saved. Paste a new one to replace it.' : 'Optional PEM'}
          onChange={(e) => {
            setCert(e.target.value)
            setClearClient(false)
          }}
        />
      </label>
      <label className="field">
        <span className="field-label">Client key</span>
        <textarea
          className="field-input"
          rows={3}
          value={keyPem}
          spellCheck={false}
          placeholder={feed.client_cert_set && !clearClient ? 'A key is saved. Paste a new one to replace it.' : 'Optional PEM'}
          onChange={(e) => {
            setKeyPem(e.target.value)
            setClearClient(false)
          }}
        />
      </label>
      {feed.client_cert_set && (
        <button type="button" className="btn ghost" onClick={() => setClearClient((v) => !v)}>
          {clearClient ? 'Keep client certificate' : 'Remove client certificate'}
        </button>
      )}
      <ToggleRow
        label="Send the trail"
        hint="One replica sends, from the cursor, in batches of 100."
        checked={enabled}
        disabled={busy !== null}
        onChange={setEnabled}
      />
      <div className="audit-feed-actions">
        <button type="button" className="btn primary" disabled={busy !== null} onClick={() => void save()}>
          {busy === 'save' ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className="btn ghost" disabled={busy !== null} onClick={() => void test()}>
          {busy === 'test' ? 'Sending…' : 'Send test'}
        </button>
      </div>
      <p className="audit-feed-health">
        <span>Last delivered <b title={feed.last_delivered_at ?? 'Not delivered yet'}>{utcClock(feed.last_delivered_at)}</b></span>
        <span><b>{behind}</b></span>
      </p>
      {feed.last_error && <p className="audit-feed-error" title={feed.last_error}>{feed.last_error}</p>}
    </section>
  )
}
