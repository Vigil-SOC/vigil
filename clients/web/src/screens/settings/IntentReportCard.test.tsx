import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import IntentReportCard from './IntentReportCard'
import { configApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {
    getIntent: vi.fn(),
  },
}))

const sameReport = {
  path: '/repo/INTENT.md',
  readable: true,
  rows: [
    { key: 'triage.auto_triage', declared: true, effective: true, source: 'default', label: 'same' },
    { key: 'investigate.enabled', declared: false, effective: false, source: 'default', label: 'same' },
  ],
}

const loosenReport = {
  path: '/repo/INTENT.md',
  readable: true,
  rows: [
    {
      key: 'investigate.enabled',
      declared: false,
      effective: true,
      source: 'db',
      label: 'loosen',
    },
  ],
}

describe('declared intent card', () => {
  beforeEach(() => {
    vi.mocked(configApi.getIntent).mockReset()
  })

  it('renders differing rows with declared, effective, source, and label', async () => {
    vi.mocked(configApi.getIntent).mockResolvedValue({ data: loosenReport } as never)
    render(<IntentReportCard />)

    expect(await screen.findByText('investigate.enabled')).toBeInTheDocument()
    expect(screen.getByText('false')).toBeInTheDocument()
    expect(screen.getByText('true')).toBeInTheDocument()
    expect(screen.getByText('db')).toBeInTheDocument()
    expect(screen.getByText('loosen')).toBeInTheDocument()
    expect(screen.getByText('/repo/INTENT.md is edited in git, not here.')).toBeInTheDocument()
  })

  it('renders every key as same when nothing differs', async () => {
    vi.mocked(configApi.getIntent).mockResolvedValue({ data: sameReport } as never)
    render(<IntentReportCard />)

    expect(await screen.findByText('triage.auto_triage')).toBeInTheDocument()
    expect(screen.getAllByText('same')).toHaveLength(sameReport.rows.length)
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('/repo/INTENT.md is edited in git, not here.')).toBeInTheDocument()
  })

  it('says the manifest was not read, and nothing else', async () => {
    vi.mocked(configApi.getIntent).mockResolvedValue({
      data: { path: '/nonexistent', readable: false, rows: [] },
    } as never)
    render(<IntentReportCard />)

    expect(await screen.findByText('Not read (/nonexistent).')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/edited in git/)).not.toBeInTheDocument()
  })

  it('refetches when the parent reports a save', async () => {
    vi.mocked(configApi.getIntent).mockResolvedValue({ data: sameReport } as never)
    const { rerender } = render(<IntentReportCard reloadKey={0} />)
    await screen.findByText('triage.auto_triage')

    vi.mocked(configApi.getIntent).mockResolvedValue({ data: loosenReport } as never)
    rerender(<IntentReportCard reloadKey={1} />)

    await waitFor(() => expect(configApi.getIntent).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('loosen')).toBeInTheDocument()
  })
})
