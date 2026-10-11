import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DemoDataClear, StreamsCard, UploadCard } from './DataIngestion'
import { configApi, ingestionApi, kafkaApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {
    getS3: vi.fn(),
    setS3: vi.fn(),
    getDarktrace: vi.fn(() => Promise.resolve({ data: {} })),
    setDarktrace: vi.fn(),
    getDemoMode: vi.fn(() => Promise.resolve({ data: { enabled: false } })),
    resetDemoData: vi.fn(),
  },
  kafkaApi: {
    getConfig: vi.fn(() => Promise.resolve({ data: {} })),
    getStatus: vi.fn(() => Promise.resolve({ data: {} })),
    setConfig: vi.fn(),
  },
  ingestionApi: {
    listS3Files: vi.fn(),
    ingestS3File: vi.fn(),
    uploadFile: vi.fn(),
    listJobs: vi.fn(() => Promise.resolve({ data: [] })),
    getJob: vi.fn(),
  },
}))

const runningJob = {
  job_id: 'ing-abc123',
  filename: 'flows.parquet',
  format: 'parquet',
  data_type: 'finding',
  status: 'running' as const,
  determinate: true,
  processed: 40,
  total: 200,
  created_at: '2026-07-24T10:00:00Z',
  finished_at: null,
  message: '',
  error: null,
  stats: {},
}

function renderPanel() {
  const notify = vi.fn()
  return render(
    <>
      <UploadCard notify={notify} />
      <StreamsCard notify={notify} />
      <DemoDataClear notify={notify} />
    </>,
  )
}

function chooseFile(name = 'flows.parquet') {
  const input = screen.getByTestId('manual-upload-input') as HTMLInputElement
  fireEvent.change(input, {
    target: { files: [new File(['x'], name, { type: 'application/octet-stream' })] },
  })
}

describe('manual upload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getS3).mockResolvedValue({ data: { configured: false } } as never)
    vi.mocked(configApi.getDarktrace).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getDemoMode).mockResolvedValue({ data: { enabled: false } } as never)
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [] } as never)
  })

  // Regression: this section used to live inside the S3 card.
  it('stays available when the S3 config fails to load', async () => {
    vi.mocked(configApi.getS3).mockRejectedValue(new Error('backend unreachable'))
    renderPanel()

    expect(await screen.findByText('Upload files')).toBeInTheDocument()
    expect(await screen.findByText('Unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Choose File/ })).toBeEnabled()
  })

  it('hands the chosen file to the background ingest endpoint', async () => {
    vi.mocked(ingestionApi.uploadFile).mockResolvedValue({ data: runningJob } as never)
    renderPanel()
    await screen.findByText('Upload files')

    chooseFile()
    fireEvent.click(screen.getByRole('button', { name: 'Upload' }))

    await waitFor(() => expect(ingestionApi.uploadFile).toHaveBeenCalledOnce())
    expect(vi.mocked(ingestionApi.uploadFile).mock.calls[0][0].name).toBe('flows.parquet')
  })

  it('re-attaches to a job still running from a previous visit', async () => {
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [runningJob] } as never)
    renderPanel()

    expect(await screen.findByText(/40 of 200 rows \(20%\)/)).toBeInTheDocument()
    expect(screen.getByText('flows.parquet')).toBeInTheDocument()
  })

  it('blocks a second upload while one is still running', async () => {
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [runningJob] } as never)
    renderPanel()
    await screen.findByText(/40 of 200 rows/)

    expect(screen.getByRole('button', { name: /Choose File/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument()
  })

  it('takes a dropped file', async () => {
    renderPanel()
    const zone = (await screen.findByText(/Drop a file to import/)).closest('.data-drop') as HTMLElement
    fireEvent.drop(zone, { dataTransfer: { files: [new File(['x'], 'dropped.csv')] } })

    expect(screen.getByRole('button', { name: /dropped\.csv/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()
  })

  it('reports a row count without a percentage for row-counting formats', async () => {
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({
      data: [{ ...runningJob, filename: 'export.csv', format: 'csv', determinate: false, total: 0 }],
    } as never)
    renderPanel()

    expect(await screen.findByText(/40 rows so far/)).toBeInTheDocument()
  })

  it('surfaces the outcome of a finished job', async () => {
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({
      data: [{ ...runningJob, status: 'failed', message: 'Ingestion failed: bad parquet' }],
    } as never)
    renderPanel()

    expect(await screen.findByText(/Ingestion failed: bad parquet/)).toBeInTheDocument()
  })

  it('hides the demo clear control when demo mode is off', async () => {
    renderPanel()
    await screen.findByText('Upload files')
    await waitFor(() => expect(configApi.getDemoMode).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Clear demo data' })).not.toBeInTheDocument()
  })

  it('clears demo data and reports the regenerated counts', async () => {
    vi.mocked(configApi.getDemoMode).mockResolvedValue({ data: { enabled: true } } as never)
    vi.mocked(configApi.resetDemoData).mockResolvedValue({
      data: { success: true, findings_count: 25, cases_count: 5 },
    } as never)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Clear demo data' }))

    expect(await screen.findByText('Regenerated 25 findings and 5 cases.')).toBeInTheDocument()
    expect(configApi.resetDemoData).toHaveBeenCalledOnce()
  })

  it('clears the file input so the same file can be retried', async () => {
    renderPanel()
    await screen.findByText('Upload files')
    const input = screen.getByTestId('manual-upload-input') as HTMLInputElement

    chooseFile()
    fireEvent.click(input)

    expect(input.value).toBe('')
  })
})

describe('streams and buckets', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getS3).mockResolvedValue({ data: { configured: false } } as never)
    vi.mocked(configApi.getDarktrace).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getDemoMode).mockResolvedValue({ data: { enabled: false } } as never)
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [] } as never)
    vi.mocked(kafkaApi.getConfig).mockResolvedValue({ data: {} } as never)
    vi.mocked(kafkaApi.getStatus).mockResolvedValue({ data: {} } as never)
  })

  const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

  it('lists the three sources as not set up when nothing is configured', async () => {
    renderPanel()
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())

    for (const name of ['Amazon S3', 'Kafka', 'Darktrace webhook']) {
      expect(within(row(name)).getByText('Not set up')).toBeInTheDocument()
      expect(within(row(name)).getByText('Off')).toBeInTheDocument()
      expect(within(row(name)).getByRole('button', { name: `Set up ${name}` })).toBeInTheDocument()
    }
  })

  it('shows each source’s settings and status when populated', async () => {
    vi.mocked(configApi.getS3).mockResolvedValue({
      data: { configured: true, bucket_name: 'acme-sec', region: 'us-east-1', auth_method: 'profile' },
    } as never)
    vi.mocked(kafkaApi.getConfig).mockResolvedValue({
      data: { enabled: true, bootstrap_servers: 'kafka-1:9092', topics: ['sec.findings'], security_protocol: 'SASL_SSL' },
    } as never)
    vi.mocked(kafkaApi.getStatus).mockResolvedValue({
      data: { daemon_reachable: true, stats: { connected: true } },
    } as never)
    vi.mocked(configApi.getDarktrace).mockResolvedValue({
      data: { enabled: true, configured: true, url: 'https://dt.example' },
    } as never)
    renderPanel()

    await waitFor(() => expect(within(row('Amazon S3')).getByText('Good')).toBeInTheDocument())
    expect(within(row('Amazon S3')).getByText('acme-sec · us-east-1 · AWS profile (SSO)')).toBeInTheDocument()
    expect(within(row('Amazon S3')).getByRole('button', { name: 'Browse Amazon S3' })).toBeInTheDocument()
    expect(within(row('Kafka')).getByText('kafka-1:9092 · topics sec.findings · SASL_SSL')).toBeInTheDocument()
    expect(within(row('Kafka')).getByText('Good')).toBeInTheDocument()
    expect(within(row('Kafka')).getByRole('button', { name: 'Edit Kafka' })).toBeInTheDocument()
    expect(within(row('Darktrace webhook')).getByText('https://dt.example')).toBeInTheDocument()
  })

  it('marks Kafka as not connected when it is enabled but the consumer is down', async () => {
    vi.mocked(kafkaApi.getConfig).mockResolvedValue({ data: { enabled: true, topics: ['a'] } } as never)
    renderPanel()

    expect(await screen.findByText('Not connected')).toBeInTheDocument()
  })

  it('opens the S3 form in place, with its save confirm, and closes it again', async () => {
    vi.mocked(configApi.getS3).mockResolvedValue({
      data: { configured: true, bucket_name: 'acme-sec', region: 'us-east-1' },
    } as never)
    vi.mocked(configApi.setS3).mockResolvedValue({ data: {} } as never)
    renderPanel()
    expect(await screen.findByText('Browse & Ingest')).not.toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Amazon S3' }))

    expect(screen.getByText('Browse & Ingest')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))
    expect(await screen.findByText('Save S3 Configuration')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close Amazon S3' }))
    expect(screen.getByText('Browse & Ingest')).not.toBeVisible()
  })

  it('shows a load error with Retry, never Not set up, for every source whose read failed', async () => {
    vi.mocked(configApi.getS3).mockRejectedValue(new Error('backend unreachable'))
    vi.mocked(configApi.getDarktrace).mockRejectedValue(new Error('backend unreachable'))
    vi.mocked(kafkaApi.getConfig).mockRejectedValue(new Error('backend unreachable'))
    vi.mocked(kafkaApi.getStatus).mockRejectedValue(new Error('backend unreachable'))
    renderPanel()
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())

    expect(screen.queryByText('Not set up')).not.toBeInTheDocument()
    for (const name of ['Amazon S3', 'Kafka', 'Darktrace webhook']) {
      expect(within(row(name)).getByText('Couldn’t load')).toBeInTheDocument()
      expect(within(row(name)).getByText('Unavailable')).toBeInTheDocument()
      expect(within(row(name)).getByRole('button', { name: `Retry ${name}` })).toBeInTheDocument()
    }
  })

  it('Retry reloads that source and shows the real config once the API answers', async () => {
    vi.mocked(configApi.getDarktrace).mockRejectedValueOnce(new Error('backend unreachable'))
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Retry Darktrace webhook' }))

    expect(
      await within(row('Darktrace webhook')).findByRole('button', { name: 'Set up Darktrace webhook' }),
    ).toBeInTheDocument()
    expect(configApi.getDarktrace).toHaveBeenCalledTimes(2)
    expect(within(row('Darktrace webhook')).getByText('Not set up')).toBeInTheDocument()
  })
})

describe('streams row state', () => {
  it('keeps a browsed file list when the row is collapsed and reopened', async () => {
    vi.mocked(configApi.getS3).mockResolvedValue({ data: { configured: true, bucket_name: 'b' } } as never)
    vi.mocked(configApi.getDarktrace).mockResolvedValue({ data: {} } as never)
    vi.mocked(kafkaApi.getConfig).mockResolvedValue({ data: {} } as never)
    vi.mocked(kafkaApi.getStatus).mockResolvedValue({ data: {} } as never)
    vi.mocked(ingestionApi.listS3Files).mockResolvedValue({
      data: { files: [{ key: 'lake/a.parquet', size: 10, last_modified: '' }] },
    } as never)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Browse Amazon S3' }))
    fireEvent.click(await screen.findByRole('button', { name: /^Browse$/ }))
    expect(await screen.findByText('lake/a.parquet')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Close Amazon S3' }))
    fireEvent.click(screen.getByRole('button', { name: 'Browse Amazon S3' }))
    expect(screen.getByText('lake/a.parquet')).toBeVisible()
  })
})
