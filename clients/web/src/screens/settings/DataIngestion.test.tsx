import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import DataIngestionPanel from './DataIngestion'
import { configApi, ingestionApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {
    getS3: vi.fn(),
    setS3: vi.fn(),
    getDarktrace: vi.fn(() => Promise.resolve({ data: {} })),
    setDarktrace: vi.fn(),
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
  return render(<DataIngestionPanel notify={vi.fn()} />)
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
    vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [] } as never)
  })

  // Regression: this section used to live inside the S3 card.
  it('stays available when the S3 config fails to load', async () => {
    vi.mocked(configApi.getS3).mockRejectedValue(new Error('backend unreachable'))
    renderPanel()

    expect(await screen.findByText('Manual Upload')).toBeInTheDocument()
    expect(await screen.findByText(/Couldn’t load S3 config/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Choose File/ })).toBeEnabled()
  })

  it('hands the chosen file to the background ingest endpoint', async () => {
    vi.mocked(ingestionApi.uploadFile).mockResolvedValue({ data: runningJob } as never)
    renderPanel()
    await screen.findByText('Manual Upload')

    chooseFile()
    fireEvent.click(screen.getByRole('button', { name: /Upload/ }))

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
    expect(screen.getByRole('button', { name: /Upload/ })).toBeDisabled()
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

  it('clears the file input so the same file can be retried', async () => {
    renderPanel()
    await screen.findByText('Manual Upload')
    const input = screen.getByTestId('manual-upload-input') as HTMLInputElement

    chooseFile()
    fireEvent.click(input)

    expect(input.value).toBe('')
  })
})
