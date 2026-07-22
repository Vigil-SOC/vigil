import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import FindingPopup from './FindingPopup'
import { casesApi } from '../../../services/api'

const testState = vi.hoisted(() => ({ canWriteCases: true }))

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    hasPermission: (permission: string) => permission !== 'cases.write' || testState.canWriteCases,
  }),
}))

vi.mock('../../../services/api', () => ({
  findingsApi: {
    getById: vi.fn(() => Promise.resolve({ data: {
      finding_id: 'finding-42',
      severity: 'high',
      data_source: 'network',
      status: 'open',
      anomaly_score: 0.91,
      timestamp: '2026-07-21T12:00:00Z',
      mitre_predictions: { T1071: 0.82 },
      entity_context: { hostnames: ['host-42'] },
    } })),
    getEnrichment: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
  casesApi: { create: vi.fn() },
}))

beforeEach(() => {
  testState.canWriteCases = true
  vi.mocked(casesApi.create).mockReset()
})

describe('finding-linked case creation', () => {
  it('hides the action without cases.write permission', async () => {
    testState.canWriteCases = false
    render(<FindingPopup id="finding-42" onClose={vi.fn()} onCaseCreated={vi.fn()} />)

    expect(await screen.findAllByText('host-42')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Create case' })).not.toBeInTheDocument()
  })

  it('creates a linked case and returns its identifier', async () => {
    vi.mocked(casesApi.create).mockResolvedValueOnce({ data: { case_id: 'case-linked' } } as never)
    const onClose = vi.fn()
    const onCaseCreated = vi.fn()
    render(<FindingPopup id="finding-42" onClose={onClose} onCaseCreated={onCaseCreated} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Create case' }))
    expect(onClose).toHaveBeenCalledOnce()

    const dialog = screen.getByRole('dialog', { name: 'Create case from findings' })
    expect(within(dialog).getByRole('textbox', { name: 'Title' }))
      .toHaveValue('Investigation for finding-42')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create case' }))

    await waitFor(() => expect(casesApi.create).toHaveBeenCalledWith({
      title: 'Investigation for finding-42',
      description: undefined,
      finding_ids: ['finding-42'],
      priority: 'high',
      status: 'open',
    }))
    expect(onCaseCreated).toHaveBeenCalledWith('case-linked')
  })
})
