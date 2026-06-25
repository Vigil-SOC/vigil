/* ============================================================
   Settings · Integrations · Data Ingestion — first-party ingest
   sources: S3 (config + browse/ingest + local upload), Kafka
   (consumer config + live stats), Darktrace (webhook receiver).
   Mirrors the S3 dialog, KafkaTab, and Darktrace dialog from the
   legacy Integrations tab.
   ============================================================ */
import { useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  Field,
  NumberInput,
  PasswordInput,
  Select,
  SettingsCard,
  TextInput,
  ToggleRow,
} from '../../shared/ui'
import { ingestionApi } from '../../../services/api'
import {
  useDarktrace,
  useKafka,
  useS3,
  type KafkaConfig,
} from './useSettings'
import type { SectionProps } from './types'

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export default function DataIngestionPanel({ notify }: SectionProps) {
  return (
    <div className="flex flex-col gap-4" style={{ maxWidth: 920 }}>
      <S3Panel notify={notify} />
      <KafkaPanel notify={notify} />
      <DarktracePanel notify={notify} />
    </div>
  )
}

/* ---------------- S3 ---------------- */
const AUTH_OPTIONS = [
  { value: 'credentials', label: 'Manual credentials' },
  { value: 'profile', label: 'AWS profile (SSO)' },
]

function S3Panel({ notify }: SectionProps) {
  const { config, setConfig, phase, error, reload, save } = useS3()
  const [saving, setSaving] = useState(false)
  const [confirmSave, setConfirmSave] = useState(false)

  // browse + ingest
  const [prefix, setPrefix] = useState('')
  const [files, setFiles] = useState<{ key: string; size: number; last_modified: string }[]>([])
  const [browsing, setBrowsing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [ingesting, setIngesting] = useState(false)
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [results, setResults] = useState<{ key: string; success: boolean; message: string }[]>([])

  // upload
  const fileRef = useRef<HTMLInputElement>(null)
  const [upload, setUpload] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)

  if (phase === 'loading') return <SettingsCard title="S3 Storage" desc=""><div className="text-sm text-tx-3 py-6 text-center">Loading…</div></SettingsCard>
  if (phase === 'error') {
    return (
      <SettingsCard title="S3 Storage" desc="">
        <div className="py-6 text-center flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load S3 config: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      </SettingsCard>
    )
  }

  const onSave = async () => {
    if (!config.bucket_name.trim()) { notify('err', 'Bucket name is required.'); return }
    setConfirmSave(true)
  }
  const doSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'S3 configuration saved.')
      setConfirmSave(false)
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save S3 config.')
    } finally {
      setSaving(false)
    }
  }

  const browse = async () => {
    setBrowsing(true)
    setResults([])
    try {
      const res = await ingestionApi.listS3Files(prefix)
      setFiles(res.data.files || [])
      setLoaded(true)
      setSelected(new Set())
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to list S3 files.')
    } finally {
      setBrowsing(false)
    }
  }

  const toggleFile = (key: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  const toggleAll = () =>
    setSelected((prev) => (prev.size === files.length ? new Set() : new Set(files.map((f) => f.key))))

  const ingestSelected = async () => {
    const keys = Array.from(selected)
    if (!keys.length) return
    setIngesting(true)
    setProgress({ done: 0, total: keys.length })
    const out: { key: string; success: boolean; message: string }[] = []
    for (const key of keys) {
      try {
        const res = await ingestionApi.ingestS3File(key)
        out.push({ key, success: res.data.success, message: res.data.message })
      } catch (e) {
        out.push({ key, success: false, message: (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Ingestion failed' })
      }
      setProgress((p) => ({ ...p, done: p.done + 1 }))
    }
    setResults(out)
    setIngesting(false)
    setSelected(new Set())
    const ok = out.filter((r) => r.success).length
    notify(ok === keys.length ? 'ok' : 'err', `Ingested ${ok}/${keys.length} file(s).`)
  }

  const doUpload = async () => {
    if (!upload) return
    setUploading(true)
    try {
      const res = await ingestionApi.uploadFile(upload)
      notify(res.data.success ? 'ok' : 'err', res.data.message || 'Upload complete.')
      if (res.data.success) setUpload(null)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Upload failed.')
    } finally {
      setUploading(false)
    }
  }

  return (
    <SettingsCard
      title="S3 Storage"
      desc="AWS S3 bucket for parquet ingest, browsing, and local file uploads."
      actions={
        <button className="btn primary" onClick={onSave} disabled={saving}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
        </button>
      }
    >
      <div className="flex flex-col gap-3.5 max-w-[560px]">
        <Field label="Bucket Name" hint="Tip: paste a full s3:// URI to auto-populate the prefix.">
          <TextInput
            value={config.bucket_name}
            placeholder="my-bucket"
            onChange={(e) => {
              const val = e.target.value
              if (val.startsWith('s3://')) {
                const [bucket, ...rest] = val.slice(5).split('/')
                const path = rest.join('/')
                setConfig({ ...config, bucket_name: bucket, parquet_prefix: path ? (path.endsWith('/') ? path : path + '/') : config.parquet_prefix })
              } else {
                setConfig({ ...config, bucket_name: val })
              }
            }}
          />
        </Field>
        <Field label="Region">
          <TextInput value={config.region} onChange={(e) => setConfig({ ...config, region: e.target.value })} />
        </Field>
        <Field label="Authentication">
          <Select value={config.auth_method} options={AUTH_OPTIONS} onSelect={(v) => setConfig({ ...config, auth_method: v })} />
        </Field>
        {config.auth_method === 'profile' ? (
          <Field label="AWS Profile Name" hint="Name in ~/.aws/config. Run `aws sso login --profile <name>` first.">
            <TextInput value={config.aws_profile} placeholder="e.g. my-sso-profile" onChange={(e) => setConfig({ ...config, aws_profile: e.target.value })} />
          </Field>
        ) : (
          <>
            <Field label="Access Key ID">
              <TextInput value={config.access_key_id} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, access_key_id: e.target.value })} />
            </Field>
            <Field label="Secret Access Key">
              <PasswordInput value={config.secret_access_key} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, secret_access_key: e.target.value })} />
            </Field>
            <Field label="Session Token (optional)" hint="Required for temporary AWS STS credentials (keys starting with ASIA).">
              <PasswordInput value={config.session_token} placeholder={config.configured ? '(saved — leave blank to keep)' : ''} onChange={(e) => setConfig({ ...config, session_token: e.target.value })} />
            </Field>
          </>
        )}
        <Field label="Default Path / Prefix" hint="S3 key prefix used as the default when browsing files.">
          <TextInput value={config.parquet_prefix} placeholder="e.g. lake/v1/embeddings/" onChange={(e) => setConfig({ ...config, parquet_prefix: e.target.value })} />
        </Field>
      </div>

      {/* Browse & ingest */}
      <div className="mt-5 pt-5 border-t border-line-soft">
        <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2.5">Browse &amp; Ingest</h4>
        <div className="flex gap-2 items-end max-w-[560px]">
          <div className="flex-1">
            <Field label="Prefix">
              <TextInput value={prefix} placeholder={config.parquet_prefix || 'lake/v1/'} onChange={(e) => setPrefix(e.target.value)} />
            </Field>
          </div>
          <button className="btn ghost" onClick={browse} disabled={browsing}>
            <Icon name="search" /> {browsing ? 'Browsing…' : 'Browse'}
          </button>
        </div>

        {loaded && (
          <div className="mt-3">
            {files.length === 0 ? (
              <div className="text-sm text-tx-3 py-3">No files found under that prefix.</div>
            ) : (
              <>
                <div className="table-wrap">
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th style={{ width: 36 }}>
                          <input type="checkbox" checked={selected.size === files.length && files.length > 0} onChange={toggleAll} />
                        </th>
                        <th>Key</th><th>Size</th><th>Last Modified</th>
                      </tr>
                    </thead>
                    <tbody>
                      {files.map((f) => (
                        <tr key={f.key}>
                          <td><input type="checkbox" checked={selected.has(f.key)} onChange={() => toggleFile(f.key)} /></td>
                          <td className="font-mono text-xs">{f.key}</td>
                          <td className="muted">{formatFileSize(f.size)}</td>
                          <td className="muted">{f.last_modified ? new Date(f.last_modified).toLocaleString() : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center gap-3 mt-2.5">
                  <button className="btn primary" disabled={selected.size === 0 || ingesting} onClick={ingestSelected}>
                    <Icon name="upload" /> {ingesting ? `Ingesting ${progress.done}/${progress.total}…` : `Ingest selected (${selected.size})`}
                  </button>
                </div>
              </>
            )}
            {results.length > 0 && (
              <div className="flex flex-col gap-1.5 mt-3">
                {results.map((r) => (
                  <div key={r.key} className={`settings-banner ${r.success ? 'ok' : 'err'}`}>
                    <Icon name={r.success ? 'check2' : 'alert'} size={13} />
                    <span className="text-xs"><span className="font-mono">{r.key}</span> — {r.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Upload local file */}
      <div className="mt-5 pt-5 border-t border-line-soft">
        <h4 className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3 mb-2.5">Upload Local File</h4>
        <div className="flex items-center gap-2.5 flex-wrap">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.parquet,.json,.jsonl"
            className="hidden"
            onChange={(e) => setUpload(e.target.files?.[0] || null)}
          />
          <button className="btn ghost" onClick={() => fileRef.current?.click()}>
            <Icon name="paperclip" /> {upload ? upload.name : 'Choose File'}
          </button>
          {upload && <span className="text-xs text-tx-3">{formatFileSize(upload.size)}</span>}
          <button className="btn primary" disabled={!upload || uploading} onClick={doUpload}>
            <Icon name="upload" /> {uploading ? 'Uploading…' : 'Upload'}
          </button>
        </div>
        <p className="text-xs text-tx-3 mt-1.5">Accepts CSV, Parquet, JSON, or JSONL finding exports.</p>
      </div>

      <ConfirmDialog
        open={confirmSave}
        danger={false}
        title="Save S3 Configuration"
        body="Save these S3 settings? Credentials are stored in the encrypted secrets store."
        confirmLabel="Save"
        busy={saving}
        onConfirm={doSave}
        onClose={() => setConfirmSave(false)}
      />
    </SettingsCard>
  )
}

/* ---------------- Kafka ---------------- */
const OFFSET_RESETS = [
  { value: 'latest', label: 'latest' },
  { value: 'earliest', label: 'earliest' },
]
const SECURITY_PROTOCOLS = ['PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL'].map((p) => ({ value: p, label: p }))
const SASL_MECHANISMS = [
  { value: '', label: '(none)' },
  { value: 'PLAIN', label: 'PLAIN' },
  { value: 'SCRAM-SHA-256', label: 'SCRAM-SHA-256' },
  { value: 'SCRAM-SHA-512', label: 'SCRAM-SHA-512' },
]

function KafkaPanel({ notify }: SectionProps) {
  const { config, setConfig, stats, daemonReachable, phase, save } = useKafka()
  const [saving, setSaving] = useState(false)
  const [topicInput, setTopicInput] = useState('')

  if (phase === 'loading') return <SettingsCard title="Kafka Ingestion" desc=""><div className="text-sm text-tx-3 py-6 text-center">Loading…</div></SettingsCard>

  const set = (patch: Partial<KafkaConfig>) => setConfig({ ...config, ...patch })

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'Kafka settings saved.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to save Kafka settings.')
    } finally {
      setSaving(false)
    }
  }

  const addTopic = () => {
    const t = topicInput.trim()
    if (!t || config.topics.includes(t)) { setTopicInput(''); return }
    set({ topics: [...config.topics, t] })
    setTopicInput('')
  }

  const statusKind = stats?.connected ? 'ok' : config.enabled ? 'info' : 'info'
  const statusText = stats?.connected ? 'CONNECTED' : config.enabled ? 'ENABLED (not yet connected)' : 'DISABLED'

  return (
    <SettingsCard
      title="Kafka Ingestion"
      desc="Stream JSON-encoded finding objects from Kafka topics. SASL password and SSL CA path must be set via env vars (KAFKA_SASL_PASSWORD, KAFKA_SSL_CA_LOCATION)."
      actions={
        <button className="btn primary" onClick={handleSave} disabled={saving}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
        </button>
      }
    >
      {!daemonReachable && (
        <div className="settings-banner info mb-3"><Icon name="info" size={14} /> Daemon health endpoint unreachable — live stats unavailable. Changes apply once the daemon reads the updated config.</div>
      )}
      <div className={`settings-banner ${statusKind} mb-4`}>
        <Icon name="info" size={14} />
        <span>
          Consumer is <strong>{statusText}</strong>
          {stats && ` — ${stats.messages_consumed} consumed, ${stats.messages_enqueued} enqueued, ${stats.duplicates_skipped} dupes skipped`}
          {stats?.last_error && ` · last error: ${stats.last_error}`}
        </span>
      </div>

      <ToggleRow
        label="Enable Kafka consumer"
        hint="The daemon re-reads this flag every few seconds — no restart needed."
        checked={config.enabled}
        onChange={(v) => set({ enabled: v })}
      />

      <div className="settings-grid-2 mt-4">
        <Field label="Bootstrap servers" hint="Comma-separated host:port list">
          <TextInput value={config.bootstrap_servers} onChange={(e) => set({ bootstrap_servers: e.target.value })} />
        </Field>
        <Field label="Consumer group">
          <TextInput value={config.consumer_group} onChange={(e) => set({ consumer_group: e.target.value })} />
        </Field>
        <Field label="Auto offset reset">
          <Select value={config.auto_offset_reset} options={OFFSET_RESETS} onSelect={(v) => set({ auto_offset_reset: v })} />
        </Field>
        <Field label="Security protocol">
          <Select value={config.security_protocol} options={SECURITY_PROTOCOLS} onSelect={(v) => set({ security_protocol: v })} />
        </Field>
      </div>

      <div className="mt-4">
        <span className="text-[13px] text-tx-2">Topics</span>
        <div className="flex gap-2 items-center mt-2 max-w-[420px]">
          <TextInput
            value={topicInput}
            placeholder="Add topic"
            onChange={(e) => setTopicInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTopic() } }}
          />
          <button className="btn ghost" onClick={addTopic}><Icon name="plus" /> Add</button>
        </div>
        <div className="flex gap-2 flex-wrap mt-2">
          {config.topics.length === 0 ? (
            <span className="text-xs text-tx-3">No topics — the consumer won’t start until you add at least one.</span>
          ) : (
            config.topics.map((t) => (
              <span key={t} className="chip">
                {t}
                <button className="dd clear" aria-label={`Remove ${t}`} onClick={() => set({ topics: config.topics.filter((x) => x !== t) })}>
                  <Icon name="close" size={11} />
                </button>
              </span>
            ))
          )}
        </div>
      </div>

      <div className="settings-grid-2 mt-4">
        <Field label="SASL mechanism">
          <Select value={config.sasl_mechanism ?? ''} options={SASL_MECHANISMS} onSelect={(v) => set({ sasl_mechanism: v || null })} />
        </Field>
        <Field label="SASL username">
          <TextInput value={config.sasl_username ?? ''} onChange={(e) => set({ sasl_username: e.target.value || null })} />
        </Field>
        <Field label="Max poll records">
          <NumberInput value={config.max_poll_records} onChange={(e) => set({ max_poll_records: Number(e.target.value) })} />
        </Field>
        <Field label="Session timeout (ms)">
          <NumberInput value={config.session_timeout_ms} onChange={(e) => set({ session_timeout_ms: Number(e.target.value) })} />
        </Field>
      </div>
    </SettingsCard>
  )
}

/* ---------------- Darktrace ---------------- */
function DarktracePanel({ notify }: SectionProps) {
  const { config, setConfig, phase, save } = useDarktrace()
  const [saving, setSaving] = useState(false)

  if (phase === 'loading') return <SettingsCard title="Darktrace" desc=""><div className="text-sm text-tx-3 py-6 text-center">Loading…</div></SettingsCard>

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(config)
      notify('ok', 'Darktrace config saved.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to save Darktrace config.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard
      title="Darktrace"
      desc="Webhook receiver for Darktrace Model Breach, AI Analyst, and System Status alerts."
      actions={
        <button className="btn primary" onClick={handleSave} disabled={saving}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
        </button>
      }
    >
      <ToggleRow
        label="Enable webhook receiver"
        checked={config.enabled}
        onChange={(v) => setConfig({ ...config, enabled: v })}
      />
      <div className="flex flex-col gap-3.5 max-w-[560px] mt-2">
        <Field label="Webhook secret" hint={config.configured ? 'A secret is stored. Leave blank to keep it.' : 'Shared secret Darktrace signs webhook payloads with.'}>
          <PasswordInput value={config.webhook_secret} onChange={(e) => setConfig({ ...config, webhook_secret: e.target.value })} />
        </Field>
        <Field label="Darktrace Console URL" hint="Used to build deep links back into the Darktrace console.">
          <TextInput value={config.url} placeholder="https://your-instance.darktrace.com" onChange={(e) => setConfig({ ...config, url: e.target.value })} />
        </Field>
        <Field label="Max body size (KB)" hint="Reject payloads larger than this.">
          <NumberInput value={config.max_body_kb} min={1} max={16384} onChange={(e) => setConfig({ ...config, max_body_kb: Math.max(1, Number(e.target.value) || 1024) })} />
        </Field>
      </div>
    </SettingsCard>
  )
}
