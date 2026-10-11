import { useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { extractApiError } from '../../shared/formKit'
import { SettingsCard } from '../../shared/ui'
import { ACCEPTED_UPLOAD_TYPES } from '../settings/DataIngestion'
import { useIngestionJob } from '../settings/useSettings'
import CheckTable, { type CheckRow } from './CheckTable'
import { DEMO_ENV_NOTICE, turnOnDemoMode } from './demoMode'

/** "Upload a file": a picker, and a row for the job this session started. */
export function UploadSource() {
  const { job, upload } = useIngestionJob()
  const input = useRef<HTMLInputElement>(null)
  // the hook attaches to the latest existing job on mount; only a job started here gets a row
  const [started, setStarted] = useState(false)
  const [uploading, setUploading] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  const pick = async (file: File | undefined) => {
    if (!file) return
    setFailure(null)
    setUploading(file.name)
    try {
      await upload(file)
      setStarted(true)
    } catch (e) {
      setFailure(extractApiError(e, 'Upload failed'))
    } finally {
      setUploading(null)
    }
  }

  const row = (r: Omit<CheckRow, 'id' | 'label'>): CheckRow => ({ id: 'upload', label: 'Upload', ...r })
  let rows: CheckRow[] = []
  if (uploading) rows = [row({ phase: 'checking', detail: `Uploading ${uploading}…` })]
  else if (failure) rows = [row({ phase: 'needs', detail: failure })]
  else if (started && job)
    rows = [
      job.status === 'running'
        ? row({
            phase: 'checking',
            detail: `Importing ${job.filename}…${job.determinate ? ` ${job.processed} of ${job.total}` : ''}`,
          })
        : job.status === 'succeeded'
          ? row({ phase: 'passed', detail: job.message || `Imported ${job.filename}` })
          : row({ phase: 'needs', detail: job.error || `Importing ${job.filename} failed` }),
    ]

  return (
    <SettingsCard title="Upload a file" desc="Import findings from an export. Duplicates are skipped.">
      <div className="flex flex-col gap-3.5">
        <div className="su-drop">
          <Icon name="upload" size={20} />
          <span>Parquet, CSV, JSON, JSONL or NDJSON</span>
          <input
            ref={input}
            type="file"
            hidden
            accept={ACCEPTED_UPLOAD_TYPES}
            aria-label="Choose a file to import"
            data-testid="setup-upload-input"
            onChange={(e) => {
              pick(e.target.files?.[0])
              e.target.value = ''
            }}
          />
          <button
            type="button"
            className="btn ghost"
            disabled={!!uploading}
            onClick={() => input.current?.click()}
          >
            Choose a file
          </button>
        </div>
        {rows.length > 0 && <CheckTable label="Upload status" rows={rows} />}
      </div>
    </SettingsCard>
  )
}

/** "Try demo data": turns demo mode on without leaving the wizard. */
export function DemoSource() {
  const [phase, setPhase] = useState<'idle' | 'busy' | 'on' | 'environment' | 'failed'>('idle')

  const turnOn = async () => {
    setPhase('busy')
    try {
      setPhase(await turnOnDemoMode())
    } catch {
      setPhase('failed')
    }
  }

  const rows: CheckRow[] =
    phase === 'on'
      ? [{ id: 'demo', label: 'Demo data', phase: 'passed', detail: 'Demo data on' }]
      : phase === 'environment'
        ? [{ id: 'demo', label: 'Demo data', phase: 'needs', detail: DEMO_ENV_NOTICE }]
        : phase === 'failed'
          ? [{ id: 'demo', label: 'Demo data', phase: 'needs', detail: 'Demo data could not be turned on' }]
          : []

  return (
    <SettingsCard title="Try demo data" desc="The fastest way to see how Vigil works.">
      <div className="flex flex-col gap-3.5">
        <div className="su-actions">
          <button type="button" className="btn ghost test" disabled={phase === 'busy'} onClick={turnOn}>
            <Icon name="sparkle" size={14} />
            {phase === 'busy' ? 'Turning on…' : 'Turn on demo data'}
          </button>
          <span className="su-note">Uses generated sample data instead of your database, so you can look around.</span>
        </div>
        {rows.length > 0 && <CheckTable label="Demo data status" rows={rows} />}
      </div>
    </SettingsCard>
  )
}
