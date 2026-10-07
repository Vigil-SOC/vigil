import { useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { LevelBadge } from '../../shared/LevelBadge'
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
import { configApi, ingestionApi, type IngestionJob } from '../../services/api'
import {
  useDarktrace,
  useIngestionJob,
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

export function DemoDataClear({ notify }: SectionProps) {
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    configApi
      .getDemoMode()
      .then((res) => {
        if (live) setEnabled(Boolean(res.data?.enabled))
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  if (!enabled) return null

  const clear = async () => {
    setBusy(true)
    try {
      const res = await configApi.resetDemoData()
      const data = res.data as { findings_count?: number; cases_count?: number }
      const text = `Regenerated ${data.findings_count} findings and ${data.cases_count} cases.`
      setReport(text)
      notify('ok', text)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Could not clear demo data.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsCard
      title="Demo data"
      desc="Findings and cases on screen are generated sample data. Clearing them regenerates that set."
    >
      <button className="btn primary" onClick={clear} disabled={busy}>
        {busy ? 'Clearing…' : 'Clear demo data'}
      </button>
      {report && (
        <div className="settings-banner ok mt-3" role="status">
          <Icon name="check2" size={13} />
          <span className="text-xs">{report}</span>
        </div>
      )}
    </SettingsCard>
  )
}

const ACCEPTED_UPLOAD_TYPES = '.parquet,.csv,.json,.jsonl,.ndjson'

export function UploadCard({ notify }: SectionProps) {
  const { job, attaching, upload } = useIngestionJob()
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [dragging, setDragging] = useState(false)
  const running = job?.status === 'running'

  const doUpload = async () => {
    if (!file) return
    setSubmitting(true)
    try {
      await upload(file)
      setFile(null)
      notify('ok', `Ingesting ${file.name} in the background.`)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Upload failed.')
    } finally {
      setSubmitting(false)
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped && !running) setFile(dropped)
  }

  return (
    <SettingsCard title="Upload files" desc="Import findings or cases from a file. Duplicates are skipped.">
      <div
        className={`data-drop${dragging ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <Icon name="upload" size={20} />
        <span className="data-drop-title">Drop a file to import, or choose a file</span>
        <span className="data-drop-sub">
          {ACCEPTED_UPLOAD_TYPES.split(',').join(', ')} · large files keep importing in the background
        </span>
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPTED_UPLOAD_TYPES}
          className="hidden"
          data-testid="manual-upload-input"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          onClick={(e) => { (e.target as HTMLInputElement).value = '' }} // lets the same file be retried
        />
        <div className="flex items-center justify-center gap-2.5 flex-wrap">
          <button className={`btn ${file ? 'ghost' : 'primary'}`} onClick={() => fileRef.current?.click()} disabled={running}>
            <Icon name="paperclip" /> {file ? file.name : 'Choose File'}
          </button>
          {file && <span className="text-xs text-tx-3">{formatFileSize(file.size)}</span>}
          {file && (
            <button className="btn primary" disabled={submitting || running || attaching} onClick={doUpload}>
              <Icon name="upload" /> {submitting ? 'Uploading…' : 'Upload'}
            </button>
          )}
        </div>
      </div>

      {job && <IngestionJobStatus job={job} />}
    </SettingsCard>
  )
}

function IngestionJobStatus({ job }: { job: IngestionJob }) {
  const running = job.status === 'running'
  const ok = job.status === 'succeeded'
  const pct = job.determinate && job.total > 0
    ? Math.min(100, Math.round((job.processed / job.total) * 100))
    : null
  const message = running
    ? pct !== null ? `${job.processed} of ${job.total} rows (${pct}%)` : `${job.processed} rows so far`
    : job.message
  return (
    <div className="data-job" role="status" aria-live="polite">
      <span className="font-mono data-job-name" title={job.filename}>{job.filename}</span>
      <span className="data-job-msg" title={message}>{message}</span>
      {running ? (
        <span className="level-pill idle"><span className="spin" aria-hidden="true" /> Importing</span>
      ) : ok ? (
        <span className="level-pill good"><Icon name="check2" size={11} /> Done</span>
      ) : (
        <span className="level-pill poor"><Icon name="alert" size={11} /> Failed</span>
      )}
    </div>
  )
}

const AUTH_OPTIONS = [
  { value: 'credentials', label: 'Manual credentials' },
  { value: 'profile', label: 'AWS profile (SSO)' },
]

function S3Form({ notify, s3 }: SectionProps & { s3: ReturnType<typeof useS3> }) {
  const { config, setConfig, phase, error, reload, save } = s3
  const [saving, setSaving] = useState(false)
  const [confirmSave, setConfirmSave] = useState(false)

  const [prefix, setPrefix] = useState('')
  const [files, setFiles] = useState<{ key: string; size: number; last_modified: string }[]>([])
  const [browsing, setBrowsing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [ingesting, setIngesting] = useState(false)
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [results, setResults] = useState<{ key: string; success: boolean; message: string }[]>([])

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
  if (phase === 'error') {
    return (
      <div className="py-6 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load S3 config: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
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

  return (
    <>
      <p className="text-xs text-tx-3 mb-3">AWS S3 bucket for browsing and ingesting supported files.</p>
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

      <SaveRow saving={saving} onSave={onSave} />

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
    </>
  )
}

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

function KafkaForm({ notify, kafka }: SectionProps & { kafka: ReturnType<typeof useKafka> }) {
  const { config, setConfig, stats, daemonReachable, phase, save } = kafka
  const [saving, setSaving] = useState(false)
  const [topicInput, setTopicInput] = useState('')

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>

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
    <>
      <p className="text-xs text-tx-3 mb-3">Stream JSON-encoded finding objects from Kafka topics. SASL password and SSL CA path must be set via env vars (KAFKA_SASL_PASSWORD, KAFKA_SSL_CA_LOCATION).</p>
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
      <SaveRow saving={saving} onSave={handleSave} />
    </>
  )
}

function DarktraceForm({ notify, darktrace }: SectionProps & { darktrace: ReturnType<typeof useDarktrace> }) {
  const { config, setConfig, phase, save } = darktrace
  const [saving, setSaving] = useState(false)

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>

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
    <>
      <p className="text-xs text-tx-3 mb-3">Webhook receiver for Darktrace Model Breach, AI Analyst, and System Status alerts.</p>
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
      <SaveRow saving={saving} onSave={handleSave} />
    </>
  )
}

function SaveRow({ saving, onSave }: { saving: boolean; onSave: () => void }) {
  return (
    <div className="mt-4">
      <button className="btn primary" onClick={onSave} disabled={saving}>
        <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )
}

type StreamKey = 's3' | 'kafka' | 'darktrace'
type StreamStatus = { level: 'good' | 'fair' | 'poor' | null; text: string }

const OFF: StreamStatus = { level: null, text: 'Off' }

/** One row of the Streams and buckets table; `status.level` null renders as plain text. */
interface StreamRow {
  key: StreamKey
  name: string
  settings: string
  status: StreamStatus
  action: string
  form: ReactNode
}

function StatusCell({ status }: { status: StreamStatus }) {
  if (status.level === 'good') return <LevelBadge level="good" variant="pill" />
  if (!status.level) return <span className="muted">{status.text}</span>
  return <span className={`level-pill ${status.level}`}>{status.text}</span>
}

export function StreamsCard({ notify }: SectionProps) {
  const s3 = useS3()
  const kafka = useKafka()
  const darktrace = useDarktrace()
  const [open, setOpen] = useState<StreamKey | null>(null)

  const s3c = s3.config
  const kc = kafka.config
  const dc = darktrace.config
  const kafkaSetUp = kc.enabled || kc.topics.length > 0

  const s3Status: StreamStatus =
    s3.phase === 'loading' ? { level: null, text: 'Loading…' }
    : s3.phase === 'error' ? { level: 'poor', text: 'Unavailable' }
    : s3c.configured ? { level: 'good', text: 'Good' }
    : OFF
  const kafkaStatus: StreamStatus =
    kafka.phase === 'loading' ? { level: null, text: 'Loading…' }
    : kafka.stats?.connected ? { level: 'good', text: 'Good' }
    : kc.enabled ? { level: 'fair', text: 'Not connected' }
    : OFF
  const darktraceStatus: StreamStatus =
    darktrace.phase === 'loading' ? { level: null, text: 'Loading…' }
    : dc.enabled && dc.configured ? { level: 'good', text: 'Good' }
    : dc.enabled ? { level: 'fair', text: 'No secret' }
    : OFF

  const rows: StreamRow[] = [
    {
      key: 's3',
      name: 'Amazon S3',
      settings: s3c.configured
        ? [s3c.bucket_name, s3c.region, s3c.auth_method === 'profile' ? 'AWS profile (SSO)' : 'Access keys'].filter(Boolean).join(' · ')
        : 'Not set up',
      status: s3Status,
      action: s3c.configured ? 'Browse' : 'Set up',
      form: <S3Form notify={notify} s3={s3} />,
    },
    {
      key: 'kafka',
      name: 'Kafka',
      settings: kafkaSetUp
        ? [kc.bootstrap_servers, kc.topics.length ? `topics ${kc.topics.join(', ')}` : 'no topics', kc.security_protocol].join(' · ')
        : 'Not set up',
      status: kafkaStatus,
      action: kafkaSetUp ? 'Edit' : 'Set up',
      form: <KafkaForm notify={notify} kafka={kafka} />,
    },
    {
      key: 'darktrace',
      name: 'Darktrace webhook',
      settings: dc.configured ? dc.url || 'Webhook secret saved' : 'Not set up',
      status: darktraceStatus,
      action: dc.configured ? 'Edit' : 'Set up',
      form: <DarktraceForm notify={notify} darktrace={darktrace} />,
    },
  ]

  return (
    <SettingsCard title="Streams and buckets" desc="Read findings continuously from storage or a message bus.">
      <div className="table-wrap">
        <table className="tbl data-table">
          <thead>
            <tr><th>Source</th><th>Settings</th><th>Status</th><th aria-label="Action" /></tr>
          </thead>
          <tbody>
            {rows.flatMap((r) => [
              <tr key={r.key}>
                <td><b>{r.name}</b></td>
                <td className="data-clip" title={r.settings}>{r.settings}</td>
                <td><StatusCell status={r.status} /></td>
                <td className="data-act">
                  <button
                    className="data-link"
                    aria-expanded={open === r.key}
                    aria-label={`${open === r.key ? 'Close' : r.action} ${r.name}`}
                    onClick={() => setOpen(open === r.key ? null : r.key)}
                  >
                    {open === r.key ? 'Close' : r.action}
                  </button>
                </td>
              </tr>,
              // stays mounted while closed, so a browse or ingest in flight survives a collapse
              <tr key={`${r.key}-form`} className="data-expand" hidden={open !== r.key}>
                <td colSpan={4}>{r.form}</td>
              </tr>,
            ])}
          </tbody>
        </table>
      </div>
    </SettingsCard>
  )
}
